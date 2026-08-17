"""
Blacksite — Nimo TV anonymous lobby scanner.

Per Q6 Gemini DR (2026-04-30): Nimo TV = Tencent Games-partnered livestream
platform; "Bullet Screen" comment system enables high-velocity tip/odds
sharing before moderation reacts. Low-latency design attractive to
high-stakes sports-betting streams. Post-de-platforming refuge for
mid-tier KOLs.

This v1 captures lobby snapshots (target-country lang feed + RoV / PUBG /
Free Fire gaming categories). Per-room comment-stream capture is a v1.5
enhancement (matches Bigo Phase 1.5 register pattern).

Output:
  instances/_TEMPLATE/runtime/raw/nimo/<YYYY-MM-DD>.jsonl

Usage:
  py agents/nimo/nimo_lobby_scan.py
  py agents/nimo/nimo_lobby_scan.py --target local-feed
  py agents/nimo/nimo_lobby_scan.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
POLICY_PATH = INSTANCE_DIR / "policy" / "nimo_lobby.yaml"
RAW_DIR = INSTANCE_DIR / "runtime" / "raw" / "nimo"
LOG_DIR = INSTANCE_DIR / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

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


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"nimo_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_policy() -> dict[str, Any]:
    with POLICY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def write_jsonl(record: dict) -> None:
    today = now_bkk().strftime("%Y-%m-%d")
    out_path = RAW_DIR / f"{today}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def emit_page_state(page, label: str, stage: str, dry_run: bool) -> dict:
    screenshot = None
    if not dry_run:
        screenshot = await save_page_state_screenshot(
            page,
            SCREENSHOT_DIR,
            f"nimo_{label}_{stage}",
        )
    record = await capture_page_state(
        page=page,
        aid=f"nimo_lobby_anon:{label}",
        persona="anonymous",
        platform="nimo",
        stage=stage,
        logged_in=None,
        matched_marker=None,
        screenshot_path=screenshot,
    )
    if not dry_run:
        write_page_state_jsonl(RAW_DIR, record)
    return record


def target_url(target: dict) -> str:
    kind = target["kind"]
    if kind == "lang_feed":
        return f"https://www.nimo.tv/{target['code']}"
    if kind == "category":
        return f"https://www.nimo.tv/category/{target['name']}"
    raise ValueError(f"unknown target kind {kind}")


def target_label(target: dict) -> str:
    return target.get("label") or target.get("name") or target.get("code") or "unknown"


async def scan_target(
    browser, target: dict, policy: dict, dry_run: bool
) -> dict:
    sleep_rng = policy["scan"]["inter_request_sleep_s"]
    ua = random.choice(policy["scan"]["user_agent_pool"])
    url = target_url(target)
    label = target_label(target)
    max_rooms = int(policy.get("per_target_max_rooms", 50))

    stats = {"target": label, "tier": target.get("tier"), "status": "init",
             "rooms_seen": 0, "rooms_new": 0}
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
            log_line(f"[scan] {label:<14} HTTP {status} — skip")
            stats["page_state"] = await emit_page_state(page, label, "http_error", dry_run)
            return stats

        try:
            await page.wait_for_selector("a[href]", timeout=8000)
            await page.wait_for_timeout(2000)
        except Exception:
            log_line(f"[scan] {label:<14} no anchors rendered")
            stats["page_state"] = await emit_page_state(page, label, "selector_missing", dry_run)
            return stats

        # Nimo TV room links are typically /<streamer_slug> or /room/<id>.
        # We capture distinct numeric/slug-pattern paths that aren't generic
        # navigation links (category, login, etc.). Conservative regex: any
        # path with 4+ alphanumeric chars that's not a known nav segment.
        rooms = await page.evaluate(
            """(maxRooms) => {
                const navBlacklist = new Set([
                    'login', 'signup', 'category', 'channel', 'live',
                    'about', 'help', 'terms', 'privacy', 'contact',
                    'th', 'en', 'id', 'vi', 'pt', 'ru', 'es',
                    'home', 'browse', 'search', 'profile', 'settings',
                ]);
                const out = [];
                const seen = new Set();
                document.querySelectorAll('a[href]').forEach(a => {
                    if (out.length >= maxRooms) return;
                    const href = a.getAttribute('href') || '';
                    // Match /<id> or /room/<id> patterns
                    let m = href.match(/^\\/(?:room\\/)?([a-zA-Z0-9_-]{4,})(?:\\/|\\?|#|$)/);
                    if (!m) return;
                    const id = m[1];
                    if (navBlacklist.has(id.toLowerCase())) return;
                    if (seen.has(id)) return;
                    seen.add(id);
                    let card = a;
                    for (let i = 0; i < 4 && card.parentElement; i++) {
                        if (card.parentElement.querySelectorAll('a').length > 1) break;
                        card = card.parentElement;
                    }
                    const text = (card.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 600);
                    // TODO: set UI markers for your instance's language — add the
                    // target language's "viewers" word to this alternation.
                    const viewerMatch = text.match(/(\\d+(?:\\.\\d+)?[KMB]?)\\s*(?:viewers?|觀眾)?/i);
                    out.push({
                        room_id: id,
                        href: href,
                        text: text,
                        viewer_str: viewerMatch ? viewerMatch[1] : null,
                    });
                });
                return out;
            }""",
            max_rooms,
        )

        stats["rooms_seen"] = len(rooms)
        if not rooms:
            stats["page_state"] = await emit_page_state(page, label, "zero_items", dry_run)
        ts = now_bkk().isoformat(timespec="seconds")
        for r in rooms:
            stats["rooms_new"] += 1
            record = {
                "ts": ts,
                "platform": "nimo",
                "kind": "lobby_snapshot",
                "target": label,
                "tier": target.get("tier"),
                "room_id": r["room_id"],
                "url": f"https://www.nimo.tv{r['href']}" if r["href"].startswith("/") else r["href"],
                "card_text": r.get("text") or "",
                "viewer_str": r.get("viewer_str"),
            }
            if not dry_run:
                write_jsonl(record)

    except Exception as e:
        log_line(f"[scan] {label:<14} ERR {type(e).__name__}: {e}")
        stats["status"] = f"error:{type(e).__name__}"
        try:
            stats["page_state"] = await emit_page_state(page, label, "exception", dry_run)
        except Exception:
            pass
    finally:
        await context.close()
        await asyncio.sleep(random.uniform(*sleep_rng))

    return stats


async def main_async(args) -> None:
    policy = load_policy()
    if not policy.get("scan", {}).get("enable", False):
        log_line("[nimo_lobby_scan] scan disabled in policy — exit")
        return

    targets = policy.get("targets") or []
    if args.target:
        wanted = set(args.target)
        targets = [t for t in targets if target_label(t) in wanted]
        if not targets:
            log_line(f"[nimo_lobby_scan] no targets match {args.target}")
            return

    proxy_url = os.environ.get("BLACKSITE_TH_PROXY")
    proxy_cfg = {"server": proxy_url} if proxy_url else None

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] nimo_lobby_scan start "
        f"targets={len(targets)} proxy={'yes' if proxy_cfg else 'no'} "
        f"dry_run={args.dry_run}"
    )

    totals = {"rooms_seen": 0, "ok": 0, "err": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=proxy_cfg)
        for target in targets:
            stats = await scan_target(browser, target, policy, args.dry_run)
            log_line(
                f"[scan] {stats['target']:<14} {stats['status']:<14} rooms={stats['rooms_seen']}"
            )
            totals["rooms_seen"] += stats["rooms_seen"]
            if stats["status"].startswith("http_2"):
                totals["ok"] += 1
            elif stats["status"].startswith("error"):
                totals["err"] += 1
        await browser.close()

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] nimo_lobby_scan done "
        f"ok={totals['ok']} err={totals['err']} rooms={totals['rooms_seen']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", nargs="*", help="restrict to specific target labels")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
