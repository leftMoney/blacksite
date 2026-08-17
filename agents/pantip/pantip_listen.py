"""
Blacksite — Pantip scanner (Playwright, anonymous read-only).

Pantip is a JS-rendered SPA + tag-based (no /forum/ slugs since redesign).
Some target-market-restricted tags return 404 from a non-local IP (e.g.
lottery / gambling tags); folk-belief-adjacent tags return 200 from any IP.
When a target-country SOCKS5 proxy becomes available
(env BLACKSITE_TH_PROXY=socks5://host:port), this listener picks it up
automatically and gains full coverage.

Despite the filename "_listen", this is a polling scanner: invoked by daemon
every 30 min (per policy/pantip_boards.yaml schedule_cron). Per run:
  - For each tag: launch headless context → goto /tag/<encoded> → scrape
    visible topic list (first ~30 topics) → write raw JSONL.
  - 404 tags log + skip (recorded as ip_blocked when proxy unset).

Output:
  instances/_TEMPLATE/runtime/raw/pantip/<YYYY-MM-DD>.jsonl

Usage:
  py agents/pantip/pantip_listen.py
  py agents/pantip/pantip_listen.py --tags example_keyword_1 example_keyword_2
  py agents/pantip/pantip_listen.py --dry-run
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
from urllib.parse import quote

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
POLICY_PATH = INSTANCE_DIR / "policy" / "pantip_boards.yaml"
RAW_DIR = INSTANCE_DIR / "runtime" / "raw" / "pantip"
LOG_DIR = INSTANCE_DIR / "runtime" / "logs"
SEEN_PATH = INSTANCE_DIR / "runtime" / "pantip_seen_topics.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

from agents._common.browser_viewport import mobile_viewport  # noqa: E402


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"pantip_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_policy() -> dict[str, Any]:
    with POLICY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen() -> set[int]:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set[int]) -> None:
    capped = sorted(seen)[-50000:]
    SEEN_PATH.write_text(json.dumps(capped), encoding="utf-8")


def write_jsonl(record: dict) -> None:
    today = now_bkk().strftime("%Y-%m-%d")
    out_path = RAW_DIR / f"{today}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def collect_tags(policy: dict) -> list[str]:
    """Pantip is now tag-based. Build tag list from filter_keywords categories.
    The legacy `rooms` list in policy is preserved for documentation but unused
    in v1 Playwright implementation.
    """
    fk = policy["filter_keywords"]
    tags: list[str] = []
    for cat in ("local_yolk", "local_white", "brand"):
        if cat in fk:
            tags.extend(fk[cat])
    # Dedupe preserve order
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


async def scan_tag(
    browser, tag: str, policy: dict, seen: set[int], dry_run: bool
) -> dict:
    """Open a context, navigate to /tag/<encoded>, scrape topic list."""
    sleep_rng = policy["scan"]["inter_request_sleep_s"]
    ua = random.choice(policy["scan"]["user_agent_pool"])
    url = f"https://pantip.com/tag/{quote(tag)}"

    stats = {"tag": tag, "status": "init", "topics_seen": 0, "topics_new": 0}
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
        if status == 404:
            log_line(f"[scan] {tag:<20} 404 (likely IP-blocked or tag-not-exist)")
            stats["status"] = "ip_blocked_or_missing"
            return stats
        if status != 200:
            log_line(f"[scan] {tag:<20} HTTP {status} — skip")
            return stats

        # Wait for topic list to render. Pantip uses /topic/<id> links inside <a>.
        try:
            await page.wait_for_selector("a[href^='/topic/']", timeout=8000)
        except Exception:
            log_line(f"[scan] {tag:<20} no topic links rendered after 8s")
            return stats

        # Extract topic_id + title pairs
        topics = await page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                document.querySelectorAll("a[href^='/topic/']").forEach(a => {
                    const m = a.getAttribute('href').match(/^\\/topic\\/(\\d+)/);
                    if (!m) return;
                    const tid = parseInt(m[1], 10);
                    if (seen.has(tid)) return;
                    seen.add(tid);
                    const title = (a.textContent || '').trim();
                    if (title.length < 3 || title.length > 400) return;
                    out.push({topic_id: tid, title: title});
                });
                return out;
            }"""
        )

        stats["topics_seen"] = len(topics)
        for t in topics:
            tid = int(t["topic_id"])
            if tid in seen:
                continue
            seen.add(tid)
            stats["topics_new"] += 1
            record = {
                "ts": now_bkk().isoformat(timespec="seconds"),
                "platform": "pantip",
                "tag": tag,
                "topic_id": tid,
                "title": t["title"],
                "url": f"https://pantip.com/topic/{tid}",
            }
            if not dry_run:
                write_jsonl(record)

    except Exception as e:
        log_line(f"[scan] {tag:<20} ERR {type(e).__name__}: {e}")
        stats["status"] = f"error:{type(e).__name__}"
    finally:
        await context.close()
        await asyncio.sleep(random.uniform(*sleep_rng))

    return stats


async def main_async(args) -> None:
    policy = load_policy()
    if not policy.get("scan", {}).get("enable", False):
        log_line("[pantip_listen] scan disabled in policy — exit")
        return

    if args.tags:
        tags = list(args.tags)
    else:
        tags = collect_tags(policy)

    seen = load_seen()
    proxy_url = os.environ.get("BLACKSITE_TH_PROXY")  # set when TH SOCKS5 online
    proxy_cfg = {"server": proxy_url} if proxy_url else None

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] pantip_listen start "
        f"tags={len(tags)} seen_cache={len(seen)} "
        f"proxy={'yes' if proxy_cfg else 'NO (TH-restricted tags will 404)'} "
        f"dry_run={args.dry_run}"
    )

    totals = {"topics_seen": 0, "topics_new": 0, "tags_ok": 0, "tags_blocked": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=proxy_cfg)
        for tag in tags:
            stats = await scan_tag(browser, tag, policy, seen, args.dry_run)
            log_line(
                f"[scan] {stats['tag']:<20} {stats['status']:<22} "
                f"seen={stats['topics_seen']:<3} new={stats['topics_new']}"
            )
            totals["topics_seen"] += stats["topics_seen"]
            totals["topics_new"] += stats["topics_new"]
            if stats["status"] == "http_200":
                totals["tags_ok"] += 1
            elif stats["status"] == "ip_blocked_or_missing":
                totals["tags_blocked"] += 1
        await browser.close()

    if not args.dry_run:
        save_seen(seen)

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] pantip_listen done "
        f"tags_ok={totals['tags_ok']} tags_blocked={totals['tags_blocked']} "
        f"seen={totals['topics_seen']} new={totals['topics_new']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tags", nargs="*", help="restrict to specific tag list")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
