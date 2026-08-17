"""
Blacksite — Meta-family engagement actions (FB+IG shared).

Tier-budgeted micro-actions per fb_ig_strategy.md §4.2 matrix:
  react / save / follow / comment / view_story / watch_reel

Each function:
  1. Checks meta_lifecycle.can(action) — gate by stage + tier + today's budget
  2. Performs the DOM action with human-paced jitter
  3. Records to lifecycle on success

DOM selectors are best-effort and labelled TODO — to be refined by direct
observation in Camoufox after first persona register. The function bodies
fail gracefully (log + return False) when selectors miss; the caller
warmup_loop handles partial-success sessions normally.

⚠️ All exposed functions return bool: True = action completed, False = skipped
(either budget-blocked or selector-fail). Caller never raises.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agents._common import meta_lifecycle

TZ = timezone(timedelta(hours=7))


def _log(persona_id: str, msg: str) -> None:
    line = f"[{datetime.now(TZ).isoformat(timespec='seconds')}] [eng] [{persona_id}] {msg}"
    print(line, flush=True)


async def _human_pause(min_s: float = 1.5, max_s: float = 4.5) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


# -------------------------------------------------------------------------
# REACT (like / heart) — works for both FB and IG with platform-specific selectors
# -------------------------------------------------------------------------

async def react(page, persona_id: str, *, platform: str, article_locator) -> bool:
    """React (Like on FB, Heart on IG) on a single feed article.

    `article_locator` is the Playwright Locator pointing at the article
    container. Caller pre-selects which article to react on.
    """
    state = meta_lifecycle.load(persona_id)
    if not meta_lifecycle.can("react", state):
        _log(persona_id, f"budget gate: react skipped (today/cap = "
                        f"{state['engagement_today'].get('reactions',0)}/"
                        f"{state['engagement_budget_today'].get('max_reactions',0)})")
        return False

    sel_fb = ['div[aria-label="Like"]', 'span[aria-label="Like"]',
              '[data-pagelet*="FeedUnit"] [aria-label*="Like"]']
    sel_ig = ['svg[aria-label="Like"]', 'button:has(svg[aria-label="Like"])']
    selectors = sel_fb if platform == "facebook" else sel_ig

    for sel in selectors:
        try:
            el = article_locator.locator(sel).first
            if await el.count() > 0:
                await _human_pause(0.8, 2.0)
                await el.click(timeout=3000)
                await _human_pause(1.2, 3.0)
                meta_lifecycle.record("react", persona_id)
                _log(persona_id, f"reacted ({platform}) via {sel}")
                return True
        except Exception:
            continue
    _log(persona_id, f"react failed — no working selector hit")
    return False


# -------------------------------------------------------------------------
# SAVE (bookmark)
# -------------------------------------------------------------------------

async def save(page, persona_id: str, *, platform: str, article_locator) -> bool:
    state = meta_lifecycle.load(persona_id)
    if not meta_lifecycle.can("save", state):
        _log(persona_id, "budget gate: save skipped")
        return False

    sel_fb = ['[aria-label="Save"]', 'div[aria-label*="Save"]']
    sel_ig = ['svg[aria-label="Save"]', 'button:has(svg[aria-label="Save"])']
    selectors = sel_fb if platform == "facebook" else sel_ig

    for sel in selectors:
        try:
            el = article_locator.locator(sel).first
            if await el.count() > 0:
                await _human_pause()
                await el.click(timeout=3000)
                await _human_pause()
                meta_lifecycle.record("save", persona_id)
                _log(persona_id, f"saved ({platform}) via {sel}")
                return True
        except Exception:
            continue
    _log(persona_id, "save failed — no working selector")
    return False


# -------------------------------------------------------------------------
# FOLLOW (Page on FB, account on IG)
# -------------------------------------------------------------------------

async def follow(page, persona_id: str, *, platform: str,
                 target_url: str | None = None,
                 article_locator=None) -> bool:
    state = meta_lifecycle.load(persona_id)
    if not meta_lifecycle.can("follow", state):
        _log(persona_id, "budget gate: follow skipped")
        return False

    if target_url:
        try:
            await page.goto(target_url, wait_until="domcontentloaded")
            await _human_pause(2.0, 4.5)
        except Exception as e:
            _log(persona_id, f"follow target_url nav failed: {e}")
            return False

    sel_fb = ['div[aria-label="Follow"]', 'div[aria-label="Like"][role="button"]']
    sel_ig = ['button:has-text("Follow")']
    selectors = sel_fb if platform == "facebook" else sel_ig

    target = page if not article_locator else article_locator
    for sel in selectors:
        try:
            el = target.locator(sel).first
            if await el.count() > 0:
                await el.click(timeout=3500)
                await _human_pause()
                meta_lifecycle.record("follow", persona_id)
                _log(persona_id, f"followed ({platform}) via {sel}")
                return True
        except Exception:
            continue
    _log(persona_id, "follow failed — no working selector")
    return False


# -------------------------------------------------------------------------
# WATCH REEL — pause on a Reel for 80-100% of duration
# -------------------------------------------------------------------------

async def watch_reel(page, persona_id: str, *, min_sec: int = 8, max_sec: int = 22) -> bool:
    """Treat the currently-visible Reel as 'watched' by hovering for N seconds.

    Best invoked after scrolling to a Reel container; we just spend time on
    page (algo signal). 80-100% completion is preferred but exact percent
    requires reading <video>.duration which is fragile.
    """
    state = meta_lifecycle.load(persona_id)
    if not meta_lifecycle.can("watch_reel", state):
        _log(persona_id, "budget gate: watch_reel skipped")
        return False

    dwell = random.uniform(min_sec, max_sec)
    await asyncio.sleep(dwell)
    meta_lifecycle.record("watch_reel", persona_id)
    _log(persona_id, f"watched reel ~{dwell:.1f}s")
    return True


# -------------------------------------------------------------------------
# VIEW STORY — recorded by story_sweep.py directly; helper here for completeness
# -------------------------------------------------------------------------

async def view_story_recorded(persona_id: str) -> bool:
    state = meta_lifecycle.load(persona_id)
    if not meta_lifecycle.can("view_story", state):
        return False
    meta_lifecycle.record("view_story", persona_id)
    return True


# -------------------------------------------------------------------------
# POST OWN STORY (24h ephemeral) — boss-supplies image; engine uploads.
# Cadence per fb_ig_strategy.md §4.3.
# -------------------------------------------------------------------------

async def post_story(page, persona_id: str, *, platform: str,
                     image_path: str) -> bool:
    """Upload image as Story. Skipped if path missing.

    NOTE: selectors for Story upload UI rotate frequently; recommended to
    refine after first persona register. For v1: log-and-skip if uncertain.
    """
    if not Path(image_path).exists():
        _log(persona_id, f"post_story skip — image not found: {image_path}")
        return False

    # TODO(post-register): observe Camoufox FB / IG real DOM for Story upload.
    # For now: log intent + return False (boss can do this manually for first
    # weeks; engine assumes responsibility once selectors are pinned).
    _log(persona_id, f"post_story TODO ({platform}) — boss does manually until "
                    f"selectors pinned. image={image_path}")
    return False


# -------------------------------------------------------------------------
# POST OWN GRID (FB feed post / IG square post) — boss-supplies image+caption
# -------------------------------------------------------------------------

async def post_grid(page, persona_id: str, *, platform: str,
                    image_path: str, caption: str) -> bool:
    if not Path(image_path).exists():
        _log(persona_id, f"post_grid skip — image not found: {image_path}")
        return False
    # TODO(post-register): same as post_story — pin selectors after first observation
    _log(persona_id, f"post_grid TODO ({platform}) — caption={caption[:50]!r}")
    return False
