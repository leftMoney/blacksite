"""
Blacksite — Facebook organic-engagement session orchestrator (per persona).

Fired by daemon at persona's online window (P03 morning/afternoon/evening,
P04 lunch/evening, P05 lunch/evening). Reads lifecycle state, picks a
budget-respecting action plan, executes it. Concurrent harvesting happens
inline (feed scroll capture).

Per fb_ig_strategy.md §4.3 cadence + §6.2 cron schedule.

Stage gate:
  register / limited  -> only feed_harvest passive scroll, no engagement
  calibration         -> small reaction + save budget; no follows yet
  ramp_up             -> add follow target Pages (mass-market first)
  mission             -> full mission; KOL Page targeting; cross-persona

Usage:
  py agents/facebook/warmup_loop.py --persona P03 --session morning
  py agents/facebook/warmup_loop.py --persona P03 --duration-min 25
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
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
from agents._common import meta_engagement

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

SESSION_DEFAULTS = {
    # (duration_min, max_scrolls, max_reactions_attempt, max_saves_attempt)
    "morning":   (15, 12, 3, 1),
    "afternoon": (12, 10, 2, 1),
    "evening":   (20, 18, 4, 1),
    "lunch":     (15, 12, 3, 1),
    "default":   (15, 12, 3, 1),
}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(persona_id: str, msg: str) -> None:
    line = f"[{now_iso()}] [fb_warmup] [{persona_id}] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"meta_warmup_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def session(persona_id: str, kind: str, duration_min: int | None) -> dict:
    state = meta_lifecycle.load(persona_id)
    stage = state["current_stage"]
    log(persona_id, f"session start kind={kind} stage={stage}")

    dur, scrolls, react_target, save_target = SESSION_DEFAULTS.get(
        kind, SESSION_DEFAULTS["default"]
    )
    if duration_min:
        dur = duration_min
    deadline = datetime.now(TZ) + timedelta(minutes=dur)

    metrics = {"scrolls": 0, "reactions_made": 0, "saves_made": 0,
               "reels_watched": 0, "stage": stage, "kind": kind}

    # Limited / register stages: pure passive scroll; defer to feed_harvest.
    if stage in ("register", "limited"):
        log(persona_id, "passive-only stage; delegating to feed_harvest")
        from agents.facebook.feed_harvest import harvest
        new_n = await harvest(persona_id, max_scrolls=scrolls, duration_min=dur)
        metrics["new_posts_harvested"] = new_n
        meta_lifecycle.add_minutes(persona_id, dur)
        return metrics

    async with launch_persona(
        persona_id, "facebook", headless=True, use_storage_state=True,
    ) as (browser, context, page):

        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
        await page.wait_for_timeout(random.randint(2500, 5000))

        cookies = await context.cookies()
        if not any(c["name"] == "c_user" for c in cookies):
            log(persona_id, "no c_user cookie — bail")
            return metrics

        for scroll_n in range(scrolls):
            if datetime.now(TZ) >= deadline:
                break

            # Pick a random article to (maybe) react / save on
            try:
                articles = await page.locator('[role="article"]').all()
            except Exception:
                articles = []

            if articles:
                target_idx = random.randint(0, max(0, len(articles) - 1))
                target = page.locator('[role="article"]').nth(target_idx)

                # Reaction probability driven by remaining budget
                if metrics["reactions_made"] < react_target and random.random() < 0.45:
                    if await meta_engagement.react(page, persona_id,
                                                   platform="facebook",
                                                   article_locator=target):
                        metrics["reactions_made"] += 1

                # Save probability — rarer than react
                if metrics["saves_made"] < save_target and random.random() < 0.10:
                    if await meta_engagement.save(page, persona_id,
                                                  platform="facebook",
                                                  article_locator=target):
                        metrics["saves_made"] += 1

                # Reel watch — applies if article has video element
                try:
                    has_vid = await target.locator("video").count() > 0
                except Exception:
                    has_vid = False
                if has_vid and random.random() < 0.55:
                    if await meta_engagement.watch_reel(page, persona_id):
                        metrics["reels_watched"] += 1

            # Scroll with jitter
            await page.evaluate(
                "window.scrollBy(0, window.innerHeight * (0.7 + Math.random() * 0.5));"
            )
            await page.wait_for_timeout(random.randint(2200, 4800))
            metrics["scrolls"] += 1

    meta_lifecycle.add_minutes(persona_id, dur)
    log(persona_id, f"session done {metrics}")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True, choices=["P03", "P04", "P05"])
    parser.add_argument("--session", default="default",
                        choices=["morning", "afternoon", "evening", "lunch", "default"])
    parser.add_argument("--duration-min", type=int, default=None)
    args = parser.parse_args()

    metrics = asyncio.run(session(args.persona, args.session, args.duration_min))
    print(f"[fb_warmup] {metrics}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
