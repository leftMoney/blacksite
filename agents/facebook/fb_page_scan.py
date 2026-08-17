"""
Blacksite — Facebook KOL Page scanner (mbasic, anonymous read-only).

Scans curated public KOL Pages via mbasic.facebook.com (server-rendered HTML
that survives anonymous datacenter-IP reads). Per §9 Meta read-only rule:
no login, no like, no comment, no friend, no DM, no group join. Public Page
wall posts + visible comments only.

Despite filename "_scan", it's a polling agent invoked by daemon every 1h.
Per run:
  - For each Page in policy/facebook_pages.yaml: launch headless context →
    GET mbasic.facebook.com/<page_id> → parse latest N posts → dedupe by
    post_id → write raw JSONL.
  - 404 / login-redirect logged + skipped.

Output:
  instances/_TEMPLATE/runtime/raw/facebook/<YYYY-MM-DD>.jsonl

Usage:
  py agents/facebook/fb_page_scan.py
  py agents/facebook/fb_page_scan.py --pages https://www.facebook.com/foo
  py agents/facebook/fb_page_scan.py --dry-run
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
from urllib.parse import urlparse

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
POLICY_PATH = INSTANCE_DIR / "policy" / "facebook_pages.yaml"
RAW_DIR = INSTANCE_DIR / "runtime" / "raw" / "facebook"
LOG_DIR = INSTANCE_DIR / "runtime" / "logs"
SEEN_PATH = INSTANCE_DIR / "runtime" / "facebook_seen_posts.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

from agents._common.browser_viewport import mobile_viewport  # noqa: E402


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"facebook_{now_bkk().strftime('%Y-%m-%d')}.log"
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


def www_to_mbasic(url: str) -> str:
    """Rewrite www.facebook.com / m.facebook.com → mbasic.facebook.com."""
    p = urlparse(url)
    netloc = "mbasic.facebook.com"
    return f"https://{netloc}{p.path}"


def page_slug_from_url(url: str) -> str:
    """Best-effort Page slug for logging / chat_username field."""
    p = urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    return parts[0] if parts else url


# mbasic post anchor pattern: /story.php?story_fbid=<id>&id=<page_id> or
# /<slug>/posts/<post_id>. Also seeing /permalink.php?story_fbid=...
POST_HREF_RE = re.compile(
    r"/(?:story\.php|permalink\.php)\?(?:story_fbid|fbid)=(\d+)"
    r"|/[^/]+/posts/([A-Za-z0-9]+)"
)


async def scan_page(
    browser, page_cfg: dict, policy: dict, seen: set[str], dry_run: bool
) -> dict:
    sleep_rng = policy["scan"]["inter_request_sleep_s"]
    ua = random.choice(policy["scan"]["user_agent_pool"])
    raw_url = page_cfg["url"]
    mbasic_url = www_to_mbasic(raw_url)
    slug = page_slug_from_url(raw_url)

    stats = {
        "slug": slug,
        "tier": page_cfg.get("tier"),
        "status": "init",
        "posts_seen": 0,
        "posts_new": 0,
    }
    context = await browser.new_context(
        user_agent=ua,
        locale="th-TH",
        viewport=mobile_viewport(),
        is_mobile=True,
        has_touch=True,
    )
    page = await context.new_page()

    try:
        resp = await page.goto(mbasic_url, wait_until="domcontentloaded", timeout=20000)
        status = resp.status if resp else 0
        stats["status"] = f"http_{status}"

        # Detect login redirect: mbasic returns 200 but path becomes /login/ or /checkpoint/
        cur_url = page.url
        if "/login" in cur_url or "/checkpoint" in cur_url:
            log_line(f"[scan] {slug:<32} login_redirect ({cur_url})")
            stats["status"] = "login_redirect"
            return stats
        if status not in (200, 302):
            log_line(f"[scan] {slug:<32} HTTP {status} — skip")
            return stats

        # Extract post anchors + nearby text. mbasic structure:
        #   <div role="article"> ... <a href="/story.php?...">text</a> ... </div>
        # We scrape latest articles directly via JS in page context.
        max_posts = int(policy["scan"]["per_page_max_posts"])
        posts = await page.evaluate(
            """(maxPosts) => {
                const out = [];
                const seen = new Set();
                // mbasic articles live in id^="u_" or [role="article"]; try both.
                const articles = document.querySelectorAll(
                    "div[role='article'], div[data-ft], div[id^='u_']"
                );
                for (const art of articles) {
                    if (out.length >= maxPosts) break;
                    // Find post permalink
                    let postId = null;
                    let permalink = null;
                    const anchors = art.querySelectorAll("a[href*='story_fbid'], a[href*='/posts/'], a[href*='permalink.php']");
                    for (const a of anchors) {
                        const href = a.getAttribute('href') || '';
                        let m = href.match(/story_fbid=(\\d+)/);
                        if (m) { postId = m[1]; permalink = href; break; }
                        m = href.match(/\\/posts\\/([A-Za-z0-9]+)/);
                        if (m) { postId = m[1]; permalink = href; break; }
                    }
                    if (!postId || seen.has(postId)) continue;
                    seen.add(postId);
                    // Capture visible text content (truncate to bound)
                    let text = (art.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (text.length > 4000) text = text.slice(0, 4000);
                    out.push({
                        post_id: postId,
                        permalink: permalink,
                        text: text,
                    });
                }
                return out;
            }""",
            max_posts,
        )

        stats["posts_seen"] = len(posts)
        for p in posts:
            pid = f"{slug}:{p['post_id']}"
            if pid in seen:
                continue
            seen.add(pid)
            stats["posts_new"] += 1
            record = {
                "ts": now_bkk().isoformat(timespec="seconds"),
                "platform": "facebook",
                "page_slug": slug,
                "page_url": raw_url,
                "tier": page_cfg.get("tier"),
                "role": page_cfg.get("role"),
                "post_id": p["post_id"],
                "permalink": "https://mbasic.facebook.com" + p["permalink"]
                    if p.get("permalink", "").startswith("/") else p.get("permalink"),
                "text": p.get("text") or "",
            }
            if not dry_run:
                write_jsonl(record)

    except Exception as e:
        log_line(f"[scan] {slug:<32} ERR {type(e).__name__}: {e}")
        stats["status"] = f"error:{type(e).__name__}"
    finally:
        await context.close()
        await asyncio.sleep(random.uniform(*sleep_rng))

    return stats


async def main_async(args) -> None:
    policy = load_policy()
    if not policy.get("scan", {}).get("enable", False):
        log_line("[fb_page_scan] scan disabled in policy — exit")
        return

    if args.pages:
        page_cfgs = [{"url": u, "tier": "manual", "role": None, "notes": "ad-hoc"} for u in args.pages]
    else:
        page_cfgs = policy.get("kol_pages") or []

    # Skip placeholder + sentinel URLs: TBD_*, REMOVED_*, login_walled_*, etc.
    # Only navigate to real https:// URLs.
    real = [c for c in page_cfgs if str(c.get("url", "")).startswith("https://")]
    skipped = len(page_cfgs) - len(real)
    if skipped:
        log_line(f"[fb_page_scan] skipped {skipped} non-https placeholder/sentinel entries")
    page_cfgs = real

    if not page_cfgs:
        log_line("[fb_page_scan] no verified kol_pages configured — exit (waiting on URL verification)")
        return

    seen = load_seen()
    proxy_url = os.environ.get("BLACKSITE_TH_PROXY")
    proxy_cfg = {"server": proxy_url} if proxy_url else None

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] fb_page_scan start "
        f"pages={len(page_cfgs)} seen_cache={len(seen)} "
        f"proxy={'yes' if proxy_cfg else 'no (datacenter IP)'} "
        f"dry_run={args.dry_run}"
    )

    totals = {"posts_seen": 0, "posts_new": 0, "ok": 0, "login_redirect": 0, "err": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=proxy_cfg)
        for cfg in page_cfgs:
            stats = await scan_page(browser, cfg, policy, seen, args.dry_run)
            log_line(
                f"[scan] {stats['slug']:<32} {stats['status']:<22} "
                f"seen={stats['posts_seen']:<3} new={stats['posts_new']}"
            )
            totals["posts_seen"] += stats["posts_seen"]
            totals["posts_new"] += stats["posts_new"]
            if stats["status"].startswith("http_2"):
                totals["ok"] += 1
            elif stats["status"] == "login_redirect":
                totals["login_redirect"] += 1
            elif stats["status"].startswith("error"):
                totals["err"] += 1
        await browser.close()

    if not args.dry_run:
        save_seen(seen)

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] fb_page_scan done "
        f"ok={totals['ok']} login={totals['login_redirect']} err={totals['err']} "
        f"seen={totals['posts_seen']} new={totals['posts_new']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", nargs="*", help="restrict to specific Page URLs (ad-hoc)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
