"""
Shared anonymous-web-feed scanner used by oneD / CH3 Plus / AIS Play / NOICE
(the target-country local OTT-broadcaster cohort). All four platforms expose the same
shape of public web pages: a feed URL renders article/video cards, each
card is an <a href> that follows a platform-specific URL pattern, and the
visible link text is the title.

Each platform agent file is ~40 lines and just supplies a `PlatformConfig`
to `run()`. Higher-fidelity platforms (FB / Bigo / TrueID) have their own
agent files with custom DOM extraction logic.

Selectors overlay (v1.7 schema extension, 2026-05-02):
  Platform yaml may carry an optional top-level `selectors:` block whose
  keys override matching `PlatformConfig` fields at policy-load time.
  Lookup precedence: yaml `selectors:` > scan.py `PlatformConfig` defaults.
  Yamls without the block keep prior behavior (full backward compat).
  Example:
      selectors:
        card_link_css: "a[href*='/shows/detail/']"
        item_id_regex: "/shows/detail/(\\\\d+)"
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import yaml
from dotenv import load_dotenv
from playwright.async_api import Browser, async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
LOG_DIR = INSTANCE_DIR / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

from agents._common.browser_viewport import mobile_viewport  # noqa: E402
from agents._common.page_state_check import (  # noqa: E402
    capture_page_state,
    save_page_state_screenshot,
    write_page_state_jsonl,
)

SCREENSHOT_DIR = INSTANCE_DIR / "runtime" / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def now_bkk() -> datetime:
    return datetime.now(TZ)


@dataclass
class PlatformConfig:
    """Per-platform configuration for the shared feed scanner."""
    name: str                                  # canonical platform name (used in JSONL + log file)
    policy_yaml_filename: str                  # under instances/<active>/policy/
    raw_subdir: str                            # under runtime/raw/
    seen_filename: str                         # under runtime/ — for dedup cache
    # Selector for clickable cards. CSS string passed to wait_for_selector + querySelectorAll.
    card_link_css: str = "a[href*='/']"
    # Regex applied to the card's href; group(1) becomes the canonical item_id.
    # If the regex doesn't match, the card is skipped.
    item_id_regex: str = r"/([^/?#]+)/?(?:\?|#|$)"
    # Min/max title length (filters out nav links, empty cards).
    min_title_len: int = 5
    max_title_len: int = 400
    # Extra fields to copy from each target into the output record.
    extra_target_fields: tuple[str, ...] = ("tier", "label")


def _log_line(platform: str, msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"{platform}_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


# Set of PlatformConfig field names overrideable from yaml `selectors:` block.
# Identity / path fields (`name`, `policy_yaml_filename`, `raw_subdir`,
# `seen_filename`) are intentionally excluded — those are platform identity,
# not tunables.
_OVERLAYABLE_FIELDS: tuple[str, ...] = (
    "card_link_css",
    "item_id_regex",
    "min_title_len",
    "max_title_len",
    "extra_target_fields",
)


def _coerce(field_name: str, raw: Any) -> Any:
    """Coerce a yaml-loaded value to the type expected by PlatformConfig.

    yaml.safe_load already produces native int / bool / list / str, so for
    most cases this is a no-op. Helpers here handle the edge cases:
      - tuples: yaml lists need tuple wrap (extra_target_fields is a tuple)
      - ints: tolerate "30" string form
    """
    if field_name in ("min_title_len", "max_title_len"):
        return int(raw)
    if field_name == "extra_target_fields":
        if isinstance(raw, (list, tuple)):
            return tuple(str(x) for x in raw)
        # single string → 1-tuple
        return (str(raw),)
    # card_link_css / item_id_regex: keep as-is (string)
    return raw


def _load_selectors_overlay(yaml_data: dict, base_config: PlatformConfig) -> PlatformConfig:
    """Return a PlatformConfig with yaml `selectors:` keys overlaid on `base_config`.

    Lookup precedence:
      1. yaml `selectors:` block (if present and key recognized)
      2. scan.py-supplied `PlatformConfig` field default

    Yamls without the block return `base_config` unchanged — pure backward
    compat.

    Unknown keys in `selectors:` are warned (one log line per key) and
    ignored; they never crash the scanner. Type coercion attempts to be
    forgiving (string "30" → 30); on coercion failure the override is
    dropped with a warning and base value retained.

    Identity/path fields (name / policy_yaml_filename / raw_subdir /
    seen_filename) are NOT overlayable — those are platform identity.
    """
    selectors = (yaml_data or {}).get("selectors")
    if not selectors:
        return base_config
    if not isinstance(selectors, dict):
        _log_line(base_config.name,
            f"[overlay] `selectors:` must be a mapping, got "
            f"{type(selectors).__name__} — ignored")
        return base_config

    overrides: dict[str, Any] = {}
    for key, raw in selectors.items():
        if key not in _OVERLAYABLE_FIELDS:
            _log_line(base_config.name,
                f"[overlay] unknown selectors key {key!r} — ignored "
                f"(valid: {', '.join(_OVERLAYABLE_FIELDS)})")
            continue
        try:
            overrides[key] = _coerce(key, raw)
        except (TypeError, ValueError) as e:
            _log_line(base_config.name,
                f"[overlay] coerce fail for {key}={raw!r} ({e}) — "
                f"keeping base default")

    if not overrides:
        return base_config

    # Build new PlatformConfig from base + overrides. dataclass `replace()`
    # cleanly handles partial updates without us listing every field.
    from dataclasses import replace as _replace
    overlaid = _replace(base_config, **overrides)
    _log_line(base_config.name,
        f"[overlay] applied {len(overrides)} yaml override(s): "
        f"{sorted(overrides.keys())}")
    return overlaid


def _load_policy(filename: str) -> dict[str, Any]:
    path = INSTANCE_DIR / "policy" / filename
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_seen(seen_path: Path) -> set[str]:
    if seen_path.exists():
        try:
            return set(json.loads(seen_path.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_seen(seen_path: Path, seen: set[str]) -> None:
    capped = sorted(seen)[-50000:]
    seen_path.write_text(json.dumps(capped), encoding="utf-8")


def _write_jsonl(raw_dir: Path, record: dict) -> None:
    today = now_bkk().strftime("%Y-%m-%d")
    out_path = raw_dir / f"{today}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def _emit_page_state(
    *,
    page,
    cfg: PlatformConfig,
    raw_dir: Path,
    label: str,
    stage: str,
    dry_run: bool,
) -> dict:
    screenshot = None
    if not dry_run:
        screenshot = await save_page_state_screenshot(
            page,
            SCREENSHOT_DIR,
            f"{cfg.name}_{label}_{stage}",
        )
    record = await capture_page_state(
        page=page,
        aid=f"{cfg.name}:{label}",
        persona="anonymous",
        platform=cfg.name,
        stage=stage,
        logged_in=None,
        matched_marker=None,
        screenshot_path=screenshot,
    )
    if not dry_run:
        write_page_state_jsonl(raw_dir, record)
    return record


async def _scan_target(
    browser: Browser,
    target: dict,
    policy: dict,
    cfg: PlatformConfig,
    raw_dir: Path,
    seen: set[str],
    dry_run: bool,
) -> dict:
    sleep_rng = policy["scan"]["inter_request_sleep_s"]
    ua = random.choice(policy["scan"]["user_agent_pool"])
    url = target["url"]
    label = target.get("label") or url

    stats = {"target": label, "tier": target.get("tier"), "status": "init",
             "items_seen": 0, "items_new": 0}
    context = await browser.new_context(
        user_agent=ua,
        locale="th-TH",
        viewport=mobile_viewport(),
        is_mobile=True,
        has_touch=True,
    )
    page = await context.new_page()

    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        status = resp.status if resp else 0
        stats["status"] = f"http_{status}"
        if status not in (200, 302):
            _log_line(cfg.name, f"[scan] {label:<14} HTTP {status} — skip")
            stats["page_state"] = await _emit_page_state(
                page=page,
                cfg=cfg,
                raw_dir=raw_dir,
                label=label,
                stage="http_error",
                dry_run=dry_run,
            )
            return stats

        try:
            await page.wait_for_selector(cfg.card_link_css, timeout=8000)
        except Exception:
            _log_line(cfg.name, f"[scan] {label:<14} no card links rendered")
            stats["page_state"] = await _emit_page_state(
                page=page,
                cfg=cfg,
                raw_dir=raw_dir,
                label=label,
                stage="selector_missing",
                dry_run=dry_run,
            )
            return stats

        max_items = int(policy.get("per_target_max_items", 30))
        items = await page.evaluate(
            """([css, idRe, minLen, maxLen, maxItems]) => {
                const out = [];
                const seen = new Set();
                const re = new RegExp(idRe);
                document.querySelectorAll(css).forEach(a => {
                    if (out.length >= maxItems) return;
                    const href = a.getAttribute('href') || '';
                    const m = href.match(re);
                    if (!m) return;
                    const id = m[1];
                    if (!id || seen.has(id)) return;
                    seen.add(id);
                    const title = (a.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (title.length < minLen || title.length > maxLen) return;
                    out.push({id: id, href: href, title: title});
                });
                return out;
            }""",
            [cfg.card_link_css, cfg.item_id_regex, cfg.min_title_len, cfg.max_title_len, max_items],
        )

        stats["items_seen"] = len(items)
        if not items:
            stats["page_state"] = await _emit_page_state(
                page=page,
                cfg=cfg,
                raw_dir=raw_dir,
                label=label,
                stage="zero_items",
                dry_run=dry_run,
            )
        for item in items:
            full_url = urljoin(url, item["href"])
            key = f"{label}:{item['id']}"
            if key in seen:
                continue
            seen.add(key)
            stats["items_new"] += 1
            record = {
                "ts": now_bkk().isoformat(timespec="seconds"),
                "platform": cfg.name,
                "feed": label,
                "item_id": item["id"],
                "title": item["title"],
                "url": full_url,
            }
            for f in cfg.extra_target_fields:
                if f in target and f != "label":  # label already serves as 'feed'
                    record[f] = target[f]
            if not dry_run:
                _write_jsonl(raw_dir, record)

    except Exception as e:
        _log_line(cfg.name, f"[scan] {label:<14} ERR {type(e).__name__}: {e}")
        stats["status"] = f"error:{type(e).__name__}"
        try:
            stats["page_state"] = await _emit_page_state(
                page=page,
                cfg=cfg,
                raw_dir=raw_dir,
                label=label,
                stage="exception",
                dry_run=dry_run,
            )
        except Exception:
            pass
    finally:
        await context.close()
        await asyncio.sleep(random.uniform(*sleep_rng))

    return stats


async def _main_async(cfg: PlatformConfig, args) -> None:
    policy = _load_policy(cfg.policy_yaml_filename)
    if not policy.get("scan", {}).get("enable", False):
        _log_line(cfg.name, f"[{cfg.name}] scan disabled in policy — exit")
        return

    # v1.7 (2026-05-02): apply yaml `selectors:` overlay on the scan.py CFG
    # default. Yamls without the block leave cfg untouched (backward compat).
    cfg = _load_selectors_overlay(policy, cfg)

    targets = policy.get("targets") or []
    if args.target:
        wanted = set(args.target)
        targets = [t for t in targets if t.get("label") in wanted]

    raw_dir = INSTANCE_DIR / "runtime" / "raw" / cfg.raw_subdir
    raw_dir.mkdir(parents=True, exist_ok=True)
    seen_path = INSTANCE_DIR / "runtime" / cfg.seen_filename
    seen = _load_seen(seen_path)

    proxy_url = os.environ.get("BLACKSITE_TH_PROXY")
    proxy_cfg = {"server": proxy_url} if proxy_url else None

    _log_line(cfg.name,
        f"[{now_bkk().isoformat(timespec='seconds')}] {cfg.name} start "
        f"targets={len(targets)} seen_cache={len(seen)} "
        f"proxy={'yes' if proxy_cfg else 'no'} dry_run={args.dry_run}"
    )

    totals = {"items_seen": 0, "items_new": 0, "ok": 0, "err": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=proxy_cfg)
        for target in targets:
            stats = await _scan_target(browser, target, policy, cfg, raw_dir, seen, args.dry_run)
            _log_line(cfg.name,
                f"[scan] {stats['target']:<14} {stats['status']:<14} "
                f"seen={stats['items_seen']:<3} new={stats['items_new']}"
            )
            totals["items_seen"] += stats["items_seen"]
            totals["items_new"] += stats["items_new"]
            if stats["status"].startswith("http_2"):
                totals["ok"] += 1
            elif stats["status"].startswith("error"):
                totals["err"] += 1
        await browser.close()

    if not args.dry_run:
        _save_seen(seen_path, seen)

    _log_line(cfg.name,
        f"[{now_bkk().isoformat(timespec='seconds')}] {cfg.name} done "
        f"ok={totals['ok']} err={totals['err']} "
        f"seen={totals['items_seen']} new={totals['items_new']}"
    )


def run(cfg: PlatformConfig) -> None:
    """Entry point — each platform's wrapper script calls this with its config."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", nargs="*", help="restrict to specific target labels")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(_main_async(cfg, args))
