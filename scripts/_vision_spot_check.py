"""Spot-check any (persona, platform) ground-truth state via vision.

Usage:
  py scripts/_vision_spot_check.py --persona P03 --platform facebook
  py scripts/_vision_spot_check.py --persona P03 --platform instagram --url https://www.instagram.com/
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents._common.camoufox_session import launch_persona
from agents._common.vision_verify import verify_state

TZ = timezone(timedelta(hours=7))
SCREENSHOT_DIR = ROOT / "instances" / "_TEMPLATE" / "runtime" / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

PLATFORM_HOME = {
    "facebook":  "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
    "tiktok":    "https://www.tiktok.com/foryou",
    "twitter_x": "https://x.com/home",
    "pantip":    "https://pantip.com/",
    "reddit":    "https://www.reddit.com/",
    "discord":   "https://discord.com/channels/@me",
    "youtube":   "https://www.youtube.com/",
}


async def grab_screenshot(persona: str, platform: str, url: str | None) -> Path:
    home = url or PLATFORM_HOME[platform]
    ts = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
    out = SCREENSHOT_DIR / f"spotcheck_{persona}_{platform}_{ts}.png"
    async with launch_persona(persona, platform, headless=True,
                              use_storage_state=True) as (br, ctx, page):
        await page.goto(home, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(6_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        await page.wait_for_timeout(2_500)
        await page.screenshot(path=str(out), full_page=False)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--persona", required=True)
    p.add_argument("--platform", required=True, choices=list(PLATFORM_HOME))
    p.add_argument("--url", default=None)
    args = p.parse_args()

    shot = asyncio.run(grab_screenshot(args.persona, args.platform, args.url))
    print(f"[probe] screenshot: {shot}")
    v = verify_state(shot, args.platform, "logged_in_and_modals",
                     persona=args.persona)
    print(json.dumps({
        "persona": args.persona, "platform": args.platform,
        "screenshot": str(shot.relative_to(ROOT)).replace("\\", "/"),
        "ok": v.ok, "logged_in": v.logged_in,
        "modal_present": v.modal_present, "modal_kind": v.modal_kind,
        "geo_hint": v.geo_hint, "human_gate": v.human_gate,
        "error": v.error,
    }, ensure_ascii=False, indent=2))
    print()
    print("--- vision notes ---")
    print(v.notes)


if __name__ == "__main__":
    main()
