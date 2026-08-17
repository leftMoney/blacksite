"""
Blacksite — TrueID public feed scanner (anonymous, Playwright).

TrueID news.trueid.net + sport.trueid.net expose category feed pages
(lottery / horoscope / sports / football / muaylocal / news) without login.
Each feed page lists article cards with title, URL, thumbnail, and byline.

This v1 captures the article-list snapshot per feed. Article body fetch is
a v1.5 enhancement when boss confirms intel quality is high enough to
justify the bandwidth.

Output:
  instances/_TEMPLATE/runtime/raw/trueid/<YYYY-MM-DD>.jsonl

Usage:
  py agents/trueid/trueid_listen.py
  py agents/trueid/trueid_listen.py --target lottery horoscope
  py agents/trueid/trueid_listen.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

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
POLICY_PATH = INSTANCE_DIR / "policy" / "trueid_targets.yaml"
RAW_DIR = INSTANCE_DIR / "runtime" / "raw" / "trueid"
LOG_DIR = INSTANCE_DIR / "runtime" / "logs"
SEEN_PATH = INSTANCE_DIR / "runtime" / "trueid_seen_articles.json"
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
    log_path = LOG_DIR / f"trueid_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_policy() -> dict[str, Any]:
    with POLICY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set[str]) -> None:
    capped = sorted(seen)[-50000:]
    SEEN_PATH.write_text(json.dumps(capped), encoding="utf-8")


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
            f"trueid_{label}_{stage}",
        )
    record = await capture_page_state(
        page=page,
        aid=f"trueid_anon:{label}",
        persona="anonymous",
        platform="trueid",
        stage=stage,
        logged_in=None,
        matched_marker=None,
        screenshot_path=screenshot,
    )
    if not dry_run:
        write_page_state_jsonl(RAW_DIR, record)
    return record


# TrueID content URL patterns — Phase C verified 2026-04-30:
#   /watch/{vertical}/<id1>/<id2>            — series/movie/documentary
#   /watch/shortseries/<id1>/<id2>           — short series
#   /read/<lang>/<slug>                       — articles
# We derive a stable item_id from the LAST 1-2 path segments joined.
ITEM_ID_RE = re.compile(r"/(?:watch|read)/[^?#]+")


def derive_article_id(url: str) -> str | None:
    """Capture the path under /watch/ or /read/ as the canonical id."""
    m = ITEM_ID_RE.search(url)
    if m:
        # Strip trailing slash + query/hash; use the path as a stable key
        path = m.group(0).split("?")[0].split("#")[0].rstrip("/")
        return path
    parts = [p for p in url.split("/") if p]
    return parts[-1] if parts else None


async def scan_target(
    browser, target: dict, policy: dict, seen: set[str], dry_run: bool
) -> dict:
    sleep_rng = policy["scan"]["inter_request_sleep_s"]
    ua = random.choice(policy["scan"]["user_agent_pool"])
    url = target["url"]
    label = target["label"]
    max_arts = int(policy.get("per_target_max_articles", 30))

    stats = {"target": label, "tier": target.get("tier"), "status": "init",
             "articles_seen": 0, "articles_new": 0}
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
            log_line(f"[scan] {label:<12} HTTP {status} — skip")
            stats["page_state"] = await emit_page_state(page, label, "http_error", dry_run)
            return stats

        # Wait for content cards. Phase C 2026-04-30 inspection:
        # trueid.net's primary content link pattern is /watch/* (movies, series,
        # short series, documentary etc.) — 49 anchors on homepage. /article/
        # was a wrong guess. Sport content lives on sport.trueid.net subdomain.
        try:
            await page.wait_for_selector("a[href*='/watch/'], a[href*='/read/']", timeout=8000)
            await page.wait_for_timeout(3000)
        except Exception:
            log_line(f"[scan] {label:<12} no content links rendered")
            stats["page_state"] = await emit_page_state(page, label, "selector_missing", dry_run)
            return stats

        # Extract /watch/ and /read/ links
        articles = await page.evaluate(
            """(maxArts) => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll("a[href*='/watch/'], a[href*='/read/']").forEach(a => {
                    if (out.length >= maxArts) return;
                    const href = a.getAttribute('href') || '';
                    if (seen.has(href)) return;
                    seen.add(href);
                    const title = (a.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (title.length < 5 || title.length > 400) return;
                    out.push({href: href, title: title});
                });
                return out;
            }""",
            max_arts,
        )

        stats["articles_seen"] = len(articles)
        if not articles:
            stats["page_state"] = await emit_page_state(page, label, "zero_items", dry_run)
        for a in articles:
            full_url = urljoin(url, a["href"])
            art_id = derive_article_id(full_url)
            if not art_id:
                continue
            key = f"{label}:{art_id}"
            if key in seen:
                continue
            seen.add(key)
            stats["articles_new"] += 1
            record = {
                "ts": now_bkk().isoformat(timespec="seconds"),
                "platform": "trueid",
                "feed": label,
                "tier": target.get("tier"),
                "article_id": art_id,
                "title": a["title"],
                "url": full_url,
            }
            if not dry_run:
                write_jsonl(record)

    except Exception as e:
        log_line(f"[scan] {label:<12} ERR {type(e).__name__}: {e}")
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
        log_line("[trueid_listen] scan disabled in policy — exit")
        return

    targets = policy.get("targets") or []
    if args.target:
        wanted = set(args.target)
        targets = [t for t in targets if t["label"] in wanted]

    seen = load_seen()
    proxy_url = os.environ.get("BLACKSITE_TH_PROXY")
    proxy_cfg = {"server": proxy_url} if proxy_url else None

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] trueid_listen start "
        f"targets={len(targets)} seen_cache={len(seen)} "
        f"proxy={'yes' if proxy_cfg else 'no (datacenter IP)'} "
        f"dry_run={args.dry_run}"
    )

    totals = {"articles_seen": 0, "articles_new": 0, "ok": 0, "err": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=proxy_cfg)
        for target in targets:
            stats = await scan_target(browser, target, policy, seen, args.dry_run)
            log_line(
                f"[scan] {stats['target']:<12} {stats['status']:<14} "
                f"seen={stats['articles_seen']:<3} new={stats['articles_new']}"
            )
            totals["articles_seen"] += stats["articles_seen"]
            totals["articles_new"] += stats["articles_new"]
            if stats["status"].startswith("http_2"):
                totals["ok"] += 1
            elif stats["status"].startswith("error"):
                totals["err"] += 1
        await browser.close()

    if not args.dry_run:
        save_seen(seen)

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] trueid_listen done "
        f"ok={totals['ok']} err={totals['err']} "
        f"seen={totals['articles_seen']} new={totals['articles_new']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", nargs="*", help="restrict to specific target labels")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
