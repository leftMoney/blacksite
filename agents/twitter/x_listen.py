"""
Blacksite — X / Twitter scanner (Playwright, anonymous, metadata-only v1).

Reality check (probed 2026-04-27):
  - x.com/<handle> profile pages return 200 anonymously but tweet timeline is
    hidden (article/tweet selectors return 0 elements).
  - syndication.twitter.com endpoints return only HTML wrapper, no tweet payload.
  - nitter.net / xcancel.com mirrors degraded (empty / 503 cloudflare).
  - Hashtag + search pages redirect to login wall.

v1 scope: metadata-only — per handle, capture og:title/description/image,
follower/following counts from DOM, verified flag, joined date, website URL.
This is enough to verify brand presence on X and rough scale (followers).

v2 scope (after V3 X account registration): logged-in scrape of full timeline,
search, hashtag landing pages, replies depth.

Output:
  instances/_TEMPLATE/runtime/raw/x/<YYYY-MM-DD>.jsonl

Usage:
  py agents/twitter/x_listen.py
  py agents/twitter/x_listen.py --handles example_handle ExampleAthlete
  py agents/twitter/x_listen.py --dry-run
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
POLICY_PATH = INSTANCE_DIR / "policy" / "x_targets.yaml"
RAW_DIR = INSTANCE_DIR / "runtime" / "raw" / "x"
LOG_DIR = INSTANCE_DIR / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

from agents._common.browser_viewport import MOBILE_USER_AGENT, mobile_viewport  # noqa: E402


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"x_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_policy() -> dict[str, Any]:
    with POLICY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def write_jsonl(record: dict, *, agent_id: str | None = None) -> None:
    today = now_bkk().strftime("%Y-%m-%d")
    out_path = RAW_DIR / f"{today}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    if agent_id:
        agent_raw = INSTANCE_DIR / "runtime" / "raw" / agent_id
        agent_raw.mkdir(parents=True, exist_ok=True)
        agent_path = agent_raw / f"{today}.jsonl"
        agent_record = dict(record)
        agent_record["agent_id"] = agent_id
        with agent_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(agent_record, ensure_ascii=False) + "\n")


def collect_handles(
    policy: dict,
    restrict: list[str] | None,
    categories: list[str] | None = None,
) -> list[tuple[str, str, str]]:
    """Return list of (handle, category, tier)."""
    out = []
    for cat_name, cat in policy["handles"].items():
        if categories and cat_name not in categories:
            continue
        tier = cat["tier"]
        for h in cat["accounts"]:
            if restrict and h not in restrict:
                continue
            out.append((h, cat_name, tier))
    return out


# Follower/following counts in X profile header are in <a> with href containing
# /verified_followers or /following. The number is in span aria-label.
EXTRACT_JS = """() => {
    const og = (prop) => {
        const el = document.querySelector(`meta[property="${prop}"]`)
            || document.querySelector(`meta[name="${prop}"]`);
        return el ? el.getAttribute('content') : null;
    };
    const result = {
        og_title: og('og:title'),
        og_description: og('og:description'),
        og_image: og('og:image'),
        og_url: og('og:url'),
        twitter_title: og('twitter:title'),
        twitter_description: og('twitter:description'),
        title: document.title,
    };
    // Counts: try aria-label of follower/following links
    const links = document.querySelectorAll('a[href*="/followers"], a[href*="/verified_followers"], a[href*="/following"]');
    const counts = {};
    links.forEach(a => {
        const href = a.getAttribute('href') || '';
        const label = a.getAttribute('aria-label') || a.textContent || '';
        if (href.includes('/following') && !counts.following) counts.following = label.trim();
        if ((href.includes('/followers') || href.includes('/verified_followers')) && !counts.followers) counts.followers = label.trim();
    });
    result.counts_raw = counts;
    // Joined date is in <span> with text "Joined"
    const spans = document.querySelectorAll('span');
    for (const s of spans) {
        const t = s.textContent || '';
        if (t.startsWith('Joined ')) { result.joined = t; break; }
    }
    // Verified badge
    result.verified = !!document.querySelector('[data-testid="icon-verified"]');
    // Bio
    const bio = document.querySelector('[data-testid="UserDescription"]');
    if (bio) result.bio = (bio.textContent || '').trim();
    // Website URL on profile
    const urlEl = document.querySelector('[data-testid="UserUrl"]');
    if (urlEl) result.website = urlEl.getAttribute('href');
    // Location
    const locEl = document.querySelector('[data-testid="UserLocation"]');
    if (locEl) result.location = (locEl.textContent || '').trim();
    return result;
}"""


def parse_count(raw: str | None) -> int | None:
    """X uses '12.3K Followers' / '5.6M' style. Parse to int."""
    if not raw:
        return None
    m = re.search(r'([\d,.]+)\s*([KMB])?', raw)
    if not m:
        return None
    n = float(m.group(1).replace(',', ''))
    suf = m.group(2)
    if suf == 'K':
        n *= 1_000
    elif suf == 'M':
        n *= 1_000_000
    elif suf == 'B':
        n *= 1_000_000_000
    return int(n)


async def scan_handle(
    browser,
    handle: str,
    category: str,
    tier: str,
    dry_run: bool,
    *,
    agent_id: str | None = None,
    work_order_id: str | None = None,
    task_focus: str | None = None,
) -> dict:
    url = f"https://x.com/{handle}"
    stats = {"handle": handle, "status": "init"}
    context = await browser.new_context(
        user_agent=MOBILE_USER_AGENT,
        locale="en-US",
        viewport=mobile_viewport(),
        is_mobile=True,
        has_touch=True,
    )
    page = await context.new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        status = resp.status if resp else 0
        stats["status"] = f"http_{status}"
        if status != 200:
            return stats
        # Wait briefly for profile header to render
        try:
            await page.wait_for_selector('meta[property="og:title"]', timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(2)

        # Detect login wall (X redirects /handle to /i/flow/login when handle missing)
        final_url = page.url
        if "/i/flow/login" in final_url:
            stats["status"] = "login_wall_or_missing"
            return stats

        meta = await page.evaluate(EXTRACT_JS)
        # Account suspended detection
        title = meta.get("title") or ""
        if "Account suspended" in title or "Page not found" in title:
            stats["status"] = "suspended_or_not_found"
            return stats

        followers_n = parse_count((meta.get("counts_raw") or {}).get("followers"))
        following_n = parse_count((meta.get("counts_raw") or {}).get("following"))

        record = {
            "ts": now_bkk().isoformat(timespec="seconds"),
            "platform": "x",
            "kind": "profile_metadata",
            "event": "profile_metadata",
            "handle": handle,
            "category": category,
            "tier": tier,
            "url": url,
            "og_title": meta.get("og_title"),
            "og_description": meta.get("og_description"),
            "og_image": meta.get("og_image"),
            "bio": meta.get("bio"),
            "website": meta.get("website"),
            "location": meta.get("location"),
            "verified": meta.get("verified", False),
            "joined": meta.get("joined"),
            "followers_count": followers_n,
            "following_count": following_n,
            "raw_counts": meta.get("counts_raw"),
        }
        if work_order_id:
            record["work_order_id"] = work_order_id
        if task_focus:
            record["task_focus"] = task_focus
        if not dry_run:
            write_jsonl(record, agent_id=agent_id)
        stats["status"] = f"ok (followers={followers_n})"
    except Exception as e:
        stats["status"] = f"err:{type(e).__name__}"
        log_line(f"[scan] @{handle:<20} ERR: {type(e).__name__}: {str(e)[:120]}")
    finally:
        await context.close()
        await asyncio.sleep(random.uniform(4, 9))
    return stats


async def main_async(args) -> None:
    policy = load_policy()
    if not policy.get("scan", {}).get("enable", False):
        log_line("[x_listen] scan disabled in policy — exit")
        return

    handles = collect_handles(policy, args.handles, args.categories)
    if not handles:
        log_line("[x_listen] no handles after filter — exit")
        return

    proxy_url = os.environ.get("BLACKSITE_TH_PROXY")
    proxy_cfg = {"server": proxy_url} if proxy_url else None

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] x_listen start "
        f"handles={len(handles)} proxy={'yes' if proxy_cfg else 'no'} "
        f"dry_run={args.dry_run} (v1 metadata-only — search/timeline await V3 acct)"
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=proxy_cfg)
        for handle, cat, tier in handles:
            stats = await scan_handle(
                browser,
                handle,
                cat,
                tier,
                args.dry_run,
                agent_id=args.agent_id,
                work_order_id=args.work_order_id,
                task_focus=args.task_focus,
            )
            log_line(f"[scan] @{stats['handle']:<22} {tier:<5} {cat:<22} {stats['status']}")
        await browser.close()

    log_line(f"[{now_bkk().isoformat(timespec='seconds')}] x_listen done")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--handles", nargs="*")
    parser.add_argument("--categories", nargs="*")
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--work-order-id", default=None)
    parser.add_argument("--task-focus", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
