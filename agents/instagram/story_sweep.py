"""
Blacksite — Instagram Story sweep (≤24h capture window).

Walks each followed-account whose Story tray ring is unread, opens the Story,
captures the rendered image (screenshot or img.src), records caption text +
sticker overlay text. Stories disappear in 24h, so this MUST run more than
once per day; daemon cron schedules it 3-4× daily during persona online window.

Per fb_ig_strategy.md §5.1 + §5.4 (reactive trigger: Story shows promo
code/QR -> OCR + KB urgent ingest 24h decay).

Output:
  instances/<inst>/runtime/media/instagram/stories/<persona>/<acct>_<ts>.jpg
  Plus a manifest line per Story to:
  instances/<inst>/runtime/raw/instagram/<persona>/stories_<YYYY-MM-DD>.jsonl

Notes (selector-fragile — DOM rotates often):
  - Story tray sits at top of /<self_handle>/ feed page in the LEFT TWO ROWS
  - Each tray entry is a circular avatar with `aria-label` containing handle
  - "Unread" rings are a different color but DOM-marker is unstable
  - On click, story modal at /<acct>/stories/ shows; advance with right-arrow

Day 0-14 limited mode: this should run only after 7+ clean days
(meta_lifecycle.consecutive_clean_days). Safer to defer entirely until
calibration phase. Lifecycle gate enforced in `harvest()`.
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
MEDIA_DIR = RUNTIME / "media" / "instagram" / "stories"
LOG_DIR = RUNTIME / "logs"
for d in [RAW_DIR, MEDIA_DIR, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(persona_id: str, msg: str) -> None:
    line = f"[{now_iso()}] [ig_stories] [{persona_id}] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"meta_stories_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _manifest_path(persona_id: str) -> Path:
    d = RAW_DIR / persona_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"stories_{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"


def _media_dir(persona_id: str) -> Path:
    d = MEDIA_DIR / persona_id
    d.mkdir(parents=True, exist_ok=True)
    return d


HANDLE_RE = re.compile(r"^/([a-zA-Z0-9._]+)/stories/")


async def _harvest_one_story(page, persona_id: str, expected_handle: str | None,
                              manifest_fp) -> bool:
    """Capture one story frame: screenshot + caption + handle. Advance to next.

    Returns True if frame captured, False if exit (modal closed, error).
    """
    try:
        await page.wait_for_timeout(random.randint(1500, 3500))
        url = page.url
        m = HANDLE_RE.search(url) if "/stories/" in url else None
        handle = m.group(1) if m else expected_handle or "unknown"

        ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
        media_path = _media_dir(persona_id) / f"{handle}_{ts}.jpg"
        # Screenshot the visible viewport — avoids the Meta source-URL token rot
        await page.screenshot(path=str(media_path), type="jpeg", quality=72,
                              full_page=False)
        # Try to extract caption text overlays
        caption_text = ""
        try:
            caption_text = (await page.locator("body").inner_text(timeout=1500))[:1500]
        except Exception:
            pass

        manifest_fp.write(json.dumps({
            "handle": handle,
            "captured_at": now_iso(),
            "media_path": str(media_path.relative_to(ROOT)),
            "caption_text": caption_text,
            "url": url,
            "persona_id": persona_id,
            "source": "story",
        }, ensure_ascii=False) + "\n")
        manifest_fp.flush()

        # Mark stories_viewed in lifecycle (for budget accounting)
        try:
            meta_lifecycle.record("view_story", persona_id)
        except Exception:
            pass

        # Advance to next frame: ArrowRight or click right side of viewport
        try:
            await page.keyboard.press("ArrowRight")
        except Exception:
            pass
        return True
    except Exception as e:
        log(persona_id, f"story harvest frame error: {e}")
        return False


async def harvest(persona_id: str, max_accounts: int, max_frames_per: int) -> int:
    state = meta_lifecycle.load(persona_id)
    if state.get("current_stage") in ("register", "limited"):
        log(persona_id, f"stage={state.get('current_stage')} — skip story sweep (limited mode)")
        return 0

    log(persona_id, f"sweep start max_accounts={max_accounts} max_frames={max_frames_per}")
    captured = 0

    async with launch_persona(
        persona_id, "instagram", headless=True, use_storage_state=True,
    ) as (browser, context, page):

        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(random.randint(2500, 5000))

        cookies = await context.cookies()
        if not any(c["name"] == "sessionid" and "instagram" in c["domain"] for c in cookies):
            log(persona_id, "no IG sessionid — bail")
            return 0

        # Find Story tray entries — fragile selector; refine post-register
        try:
            tray_buttons = await page.locator(
                'button[aria-label*="Story"], canvas + button'
            ).all()
            tray_buttons = tray_buttons[:max_accounts]
        except Exception as e:
            log(persona_id, f"tray locator error: {e}")
            tray_buttons = []

        if not tray_buttons:
            log(persona_id, "no Story tray buttons found (DOM may have changed)")
            return 0

        with _manifest_path(persona_id).open("a", encoding="utf-8") as fp:
            for i, btn in enumerate(tray_buttons):
                try:
                    aria = (await btn.get_attribute("aria-label")) or ""
                except Exception:
                    aria = ""
                handle_guess = re.findall(r"@?([a-zA-Z0-9._]+)", aria)
                handle = handle_guess[0] if handle_guess else None

                try:
                    await btn.click(timeout=4000)
                except Exception as e:
                    log(persona_id, f"tray[{i}] click failed: {e}")
                    continue

                # Capture up to N frames per story
                frames_taken = 0
                while frames_taken < max_frames_per:
                    ok = await _harvest_one_story(page, persona_id, handle, fp)
                    if not ok:
                        break
                    frames_taken += 1
                    captured += 1
                # Close story modal back to feed
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(random.randint(800, 2200))
                except Exception:
                    pass

    log(persona_id, f"sweep done frames_captured={captured}")
    return captured


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True, choices=["P03", "P04", "P05"])
    parser.add_argument("--max-accounts", type=int, default=12)
    parser.add_argument("--max-frames-per", type=int, default=4)
    args = parser.parse_args()

    n = asyncio.run(harvest(args.persona, args.max_accounts, args.max_frames_per))
    print(f"[ig_stories] captured {n} frames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
