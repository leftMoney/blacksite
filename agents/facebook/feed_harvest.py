"""
Blacksite — Facebook feed harvester (logged-in, Day 0+).

Runs during persona's organic browsing session: scrolls FB feed, extracts
visible post DOM, dedupes by post_id, writes raw JSONL. Read-only —
no engagement actions here (those live in agents/_common/meta_engagement.py).

Per fb_ig_strategy.md §5.1 + §6.1. During Day 0-14 limited mode this is the
ONLY active FB job for personas; engagement is gated by lifecycle stage.

Output:
  instances/<inst>/runtime/raw/facebook/<persona>/<YYYY-MM-DD>.jsonl
  Each line: {post_id, page_id, page_name, text, urls, media_urls,
              reactions_count, comments_count, shares_count, scraped_at,
              persona_id, source: "feed"}

Usage:
  py agents/facebook/feed_harvest.py --persona P03 --max-scrolls 12
  py agents/facebook/feed_harvest.py --persona P03 --duration-min 8
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
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
from agents._common.page_state_check import (
    capture_page_state,
    save_page_state_screenshot,
    write_page_state_jsonl,
)

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
RAW_DIR = RUNTIME / "raw" / "facebook"
LOG_DIR = RUNTIME / "logs"
SCREENSHOT_DIR = RUNTIME / "screenshots"
RAW_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

# Persona scroll budget per session — gentle, mimics human.
DEFAULT_MAX_SCROLLS = 15


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(persona_id: str, msg: str) -> None:
    line = f"[{now_iso()}] [fb_feed] [{persona_id}] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"meta_feed_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _seen_path(persona_id: str) -> Path:
    return RUNTIME / f"facebook_feed_seen_{persona_id}.json"


def _load_seen(persona_id: str) -> set[str]:
    p = _seen_path(persona_id)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(persona_id: str, seen: set[str]) -> None:
    p = _seen_path(persona_id)
    # Keep only the most recent 5000 to bound file size.
    keep = list(seen)[-5000:]
    p.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")


def _raw_path(persona_id: str) -> Path:
    d = RAW_DIR / persona_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"


def _agent_id(persona_id: str) -> str:
    return f"{persona_id}_FB"


def _agent_raw_path(persona_id: str) -> Path:
    d = RUNTIME / "raw" / _agent_id(persona_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"


def _append_agent_raw(persona_id: str, record: dict) -> None:
    with _agent_raw_path(persona_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# Selectors are best-effort — Meta DOM rotates. If selectors fail, the
# function logs and skips; harvesting degrades gracefully rather than
# crashing the daemon job.
async def emit_page_state(
    page,
    persona_id: str,
    stage: str,
    raw_dir: Path,
    *,
    logged_in: bool | None = None,
    matched_marker: str | None = None,
) -> dict:
    screenshot = await save_page_state_screenshot(
        page,
        SCREENSHOT_DIR,
        f"facebook_{persona_id}_{stage}",
    )
    record = await capture_page_state(
        page=page,
        aid=_agent_id(persona_id),
        persona=persona_id,
        platform="facebook",
        stage=stage,
        logged_in=logged_in,
        matched_marker=matched_marker,
        screenshot_path=screenshot,
    )
    write_page_state_jsonl(raw_dir, record)
    _append_agent_raw(persona_id, {**record, "ts": record.get("checked_at") or now_iso()})
    return record


ARTICLE_SEL = '[role="article"]'

POST_ID_RE = re.compile(r'/(\d{15,18})(?:/|$|\?)')


async def _extract_post(article) -> dict | None:
    """Pull a post payload out of one feed [role=article]."""
    try:
        text = await article.evaluate(
            "(el) => el.innerText || el.textContent || ''",
            timeout=2500,
        )
    except Exception:
        try:
            text = await article.text_content(timeout=2500)
        except Exception:
            text = ""
    text = text.strip()[:8000]

    # Try to find the canonical post permalink for post_id
    post_id = None
    page_id = None
    page_name = None
    try:
        anchors = await article.locator("a").all()
        for a in anchors[:30]:
            href = (await a.get_attribute("href")) or ""
            if "/posts/" in href or "/permalink/" in href or "/videos/" in href:
                m = POST_ID_RE.search(href)
                if m:
                    post_id = m.group(1)
                    break
        # First anchor with text often = page name; non-perfect
        if anchors:
            first_text = (await anchors[0].inner_text()).strip()
            if first_text and len(first_text) < 80:
                page_name = first_text
            href0 = await anchors[0].get_attribute("href")
            if href0:
                m = POST_ID_RE.search(href0)
                if m:
                    page_id = m.group(1)
    except Exception:
        pass

    if not post_id:
        compact = " ".join(text.split())
        if len(compact) < 35:
            return None
        post_id = "snap_" + hashlib.sha1(compact[:1500].encode("utf-8")).hexdigest()[:18]
        extraction_quality = "snapshot_no_permalink"
    else:
        extraction_quality = "canonical_permalink"

    # External URLs in post body
    urls: list[str] = []
    try:
        anchors = await article.locator('a[href^="http"]').all()
        for a in anchors[:20]:
            href = await a.get_attribute("href")
            if href and "facebook.com" not in href and href not in urls:
                urls.append(href)
    except Exception:
        pass

    # Media (images / videos) — collect src attrs
    media_urls: list[str] = []
    try:
        for img in (await article.locator("img").all())[:8]:
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
        "ts": now_iso(),
        "event": "facebook_feed_post",
        "kind": "facebook_feed_post",
        "post_id": post_id,
        "page_id": page_id,
        "page_name": page_name,
        "text": text,
        "urls": urls,
        "media_urls": media_urls,
        "scraped_at": now_iso(),
        "source": "feed",
        "extraction_quality": extraction_quality,
    }


async def _visible_text_snapshots(page, seen: set[str], limit: int = 10) -> list[dict]:
    try:
        blocks = await page.evaluate(
            """(limit) => {
                const out = [];
                const accepted = [];
                const nodes = Array.from(document.querySelectorAll('article, section, div'));
                for (const el of nodes) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 120 || rect.height < 45) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    let text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (text.length < 60 || text.length > 1600) continue;
                    if (text.includes('HasteSupportData') || text.includes('qplTimingsServerJS')) continue;
                    if (text.includes('Facebook ©') || text.includes('Log in') || text.includes('Create new account')) continue;
                    let nested = false;
                    for (const existing of accepted) {
                        if (existing.includes(text) || text.includes(existing)) {
                            nested = true;
                            break;
                        }
                    }
                    if (nested) continue;
                    accepted.push(text);
                    out.push({text, url: window.location.href});
                    if (out.length >= limit) break;
                }
                return out;
            }""",
            limit,
        )
    except Exception:
        blocks = []

    records: list[dict] = []
    for block in blocks:
        text = str((block or {}).get("text") or "").strip()
        if not text:
            continue
        post_id = "snap_" + hashlib.sha1(text[:1500].encode("utf-8")).hexdigest()[:18]
        if post_id in seen:
            continue
        seen.add(post_id)
        records.append(
            {
                "ts": now_iso(),
                "event": "facebook_feed_post",
                "kind": "facebook_feed_post",
                "post_id": post_id,
                "page_id": None,
                "page_name": None,
                "text": text[:8000],
                "urls": [],
                "media_urls": [],
                "scraped_at": now_iso(),
                "source": "feed_visible_snapshot",
                "extraction_quality": "visible_text_fallback",
                "url": (block or {}).get("url"),
            }
        )
    return records


async def harvest(persona_id: str, max_scrolls: int, duration_min: int | None) -> int:
    log(persona_id, f"harvest start max_scrolls={max_scrolls} duration_min={duration_min}")
    deadline = None
    if duration_min:
        deadline = datetime.now(TZ) + timedelta(minutes=duration_min)

    seen = _load_seen(persona_id)
    raw = _raw_path(persona_id)
    new_count = 0
    duplicate_count = 0
    article_seen_count = 0

    async with launch_persona(
        persona_id, "facebook", headless=True, use_storage_state=True,
    ) as (browser, context, page):

        await page.goto("https://m.facebook.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(random.randint(2500, 5000))

        # Quick session sanity — bail early if not logged in
        cookies = await context.cookies()
        if not any(c["name"] == "c_user" for c in cookies):
            log(persona_id, "no c_user cookie — session expired or not registered yet")
            state = await emit_page_state(
                page,
                persona_id,
                "session_cookie_missing",
                raw.parent,
                logged_in=False,
            )
            log(persona_id, f"page_state={state['verdict']} stage=session_cookie_missing")
            return 0

        with raw.open("a", encoding="utf-8") as out:
            for scroll_n in range(max_scrolls):
                if deadline and datetime.now(TZ) >= deadline:
                    log(persona_id, f"duration ceiling hit at scroll #{scroll_n}")
                    break
                try:
                    articles = await page.locator(ARTICLE_SEL).all()
                except Exception as e:
                    log(persona_id, f"articles locator error: {e}")
                    state = await emit_page_state(
                        page,
                        persona_id,
                        "selector_exception",
                        raw.parent,
                        logged_in=True,
                        matched_marker="c_user_cookie",
                    )
                    log(persona_id, f"page_state={state['verdict']} stage=selector_exception")
                    articles = []
                article_seen_count += len(articles)

                for art in articles:
                    rec = await _extract_post(art)
                    if not rec:
                        continue
                    if rec["post_id"] in seen:
                        duplicate_count += 1
                        continue
                    seen.add(rec["post_id"])
                    rec["persona_id"] = persona_id
                    rec["agent_id"] = _agent_id(persona_id)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    _append_agent_raw(persona_id, rec)
                    new_count += 1

                # Scroll with human-paced jitter
                await page.evaluate(
                    "window.scrollBy(0, window.innerHeight * (0.7 + Math.random() * 0.5));"
                )
                await page.wait_for_timeout(random.randint(1800, 4200))

            if new_count == 0:
                for rec in await _visible_text_snapshots(page, seen, limit=10):
                    rec["persona_id"] = persona_id
                    rec["agent_id"] = _agent_id(persona_id)
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    _append_agent_raw(persona_id, rec)
                    new_count += 1

            summary = {
                "ts": now_iso(),
                "event": "feed_harvest_summary",
                "kind": "feed_harvest_summary",
                "agent_id": _agent_id(persona_id),
                "persona_id": persona_id,
                "platform": "facebook",
                "new_count": new_count,
                "duplicate_count": duplicate_count,
                "article_seen_count": article_seen_count,
                "max_scrolls": max_scrolls,
                "duration_min": duration_min,
            }
            out.write(json.dumps(summary, ensure_ascii=False) + "\n")
            _append_agent_raw(persona_id, summary)

            if new_count == 0:
                stage = "zero_articles" if article_seen_count == 0 else "zero_new_posts"
                state = await emit_page_state(
                    page,
                    persona_id,
                    stage,
                    raw.parent,
                    logged_in=True,
                    matched_marker="c_user_cookie",
                )
                log(
                    persona_id,
                    f"page_state={state['verdict']} stage={stage} articles={article_seen_count} dupes={duplicate_count}",
                )

    _save_seen(persona_id, seen)
    log(persona_id, f"harvest done new={new_count} dupes={duplicate_count} -> {raw.name}")

    # Track minutes-on-platform in lifecycle
    if duration_min:
        meta_lifecycle.add_minutes(persona_id, duration_min)
    return new_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True, choices=["P03", "P04", "P05"])
    parser.add_argument("--max-scrolls", type=int, default=DEFAULT_MAX_SCROLLS)
    parser.add_argument("--duration-min", type=int, default=None,
                        help="Hard time ceiling; overrides max-scrolls")
    args = parser.parse_args()

    n = asyncio.run(harvest(args.persona, args.max_scrolls, args.duration_min))
    print(f"[fb_feed] harvested {n} new posts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
