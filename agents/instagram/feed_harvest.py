"""
Blacksite — Instagram feed harvester (logged-in, Day 0+).

Mirror of agents/facebook/feed_harvest.py for IG feed (home / Following).
Per fb_ig_strategy.md §5.1.

Output: instances/<inst>/runtime/raw/instagram/<persona>/<YYYY-MM-DD>.jsonl

Usage:
  py agents/instagram/feed_harvest.py --persona P03 --max-scrolls 10
  py agents/instagram/feed_harvest.py --persona P03 --duration-min 6
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

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agents._common.camoufox_session import launch_persona
from agents._common import meta_lifecycle

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
RAW_DIR = RUNTIME / "raw" / "instagram"
LOG_DIR = RUNTIME / "logs"
RAW_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))
DEFAULT_MAX_SCROLLS = 12


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(persona_id: str, msg: str) -> None:
    line = f"[{now_iso()}] [ig_feed] [{persona_id}] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"meta_feed_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _seen_path(persona_id: str) -> Path:
    return RUNTIME / f"instagram_feed_seen_{persona_id}.json"


def _load_seen(persona_id: str) -> set[str]:
    p = _seen_path(persona_id)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(persona_id: str, seen: set[str]) -> None:
    keep = list(seen)[-5000:]
    _seen_path(persona_id).write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")


def _raw_path(persona_id: str) -> Path:
    d = RAW_DIR / persona_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"


# IG post permalink: /p/<shortcode>/ or /reel/<shortcode>/
IG_POST_RE = re.compile(r"/(p|reel)/([A-Za-z0-9_-]+)/")


async def _extract_article(article) -> dict | None:
    try:
        text = await article.inner_text(timeout=2000)
    except Exception:
        text = ""
    text = text.strip()[:5000]

    shortcode = None
    media_type = "post"
    handle = None
    try:
        anchors = await article.locator("a").all()
        for a in anchors[:30]:
            href = (await a.get_attribute("href")) or ""
            m = IG_POST_RE.search(href)
            if m:
                media_type = "reel" if m.group(1) == "reel" else "post"
                shortcode = m.group(2)
                break
        # Handle = first /username/ anchor
        for a in anchors[:30]:
            href = (await a.get_attribute("href")) or ""
            mm = re.match(r"^/([a-zA-Z0-9._]+)/?$", href)
            if mm and mm.group(1) not in ("explore", "reels", "p", "stories", "accounts"):
                handle = mm.group(1)
                break
    except Exception:
        pass

    if not shortcode:
        return None

    media_urls: list[str] = []
    try:
        for img in (await article.locator("img").all())[:6]:
            src = await img.get_attribute("src")
            if src and src.startswith("http") and src not in media_urls:
                media_urls.append(src)
        for vid in (await article.locator("video").all())[:3]:
            src = await vid.get_attribute("src")
            if src and src not in media_urls:
                media_urls.append(src)
    except Exception:
        pass

    return {
        "shortcode": shortcode,
        "media_type": media_type,
        "handle": handle,
        "text": text,
        "media_urls": media_urls,
        "scraped_at": now_iso(),
        "source": "feed",
    }


async def harvest(persona_id: str, max_scrolls: int, duration_min: int | None) -> int:
    log(persona_id, f"harvest start max_scrolls={max_scrolls} duration_min={duration_min}")
    deadline = None
    if duration_min:
        deadline = datetime.now(TZ) + timedelta(minutes=duration_min)

    seen = _load_seen(persona_id)
    raw = _raw_path(persona_id)
    new_count = 0
    dup = 0

    async with launch_persona(
        persona_id, "instagram", headless=True, use_storage_state=True,
    ) as (browser, context, page):

        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(random.randint(2500, 5000))

        cookies = await context.cookies()
        if not any(c["name"] == "sessionid" and "instagram" in c["domain"] for c in cookies):
            log(persona_id, "no IG sessionid — session expired or not registered yet")
            return 0

        with raw.open("a", encoding="utf-8") as out:
            for scroll_n in range(max_scrolls):
                if deadline and datetime.now(TZ) >= deadline:
                    log(persona_id, f"duration ceiling hit at scroll #{scroll_n}")
                    break
                try:
                    articles = await page.locator("article").all()
                except Exception as e:
                    log(persona_id, f"article locator error: {e}")
                    articles = []

                for art in articles:
                    rec = await _extract_article(art)
                    if not rec:
                        continue
                    if rec["shortcode"] in seen:
                        dup += 1
                        continue
                    seen.add(rec["shortcode"])
                    rec["persona_id"] = persona_id
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    new_count += 1

                await page.evaluate(
                    "window.scrollBy(0, window.innerHeight * (0.6 + Math.random() * 0.6));"
                )
                await page.wait_for_timeout(random.randint(2000, 4500))

    _save_seen(persona_id, seen)
    log(persona_id, f"harvest done new={new_count} dupes={dup} -> {raw.name}")
    if duration_min:
        meta_lifecycle.add_minutes(persona_id, duration_min)
    return new_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True, choices=["P03", "P04", "P05"])
    parser.add_argument("--max-scrolls", type=int, default=DEFAULT_MAX_SCROLLS)
    parser.add_argument("--duration-min", type=int, default=None)
    args = parser.parse_args()

    n = asyncio.run(harvest(args.persona, args.max_scrolls, args.duration_min))
    print(f"[ig_feed] harvested {n} new posts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
