"""agents/tiktok/warmup_session.py — P03 / P04 TikTok warmup session.

DOM: REGISTER_LESSONS.md §2.4. TikTok virtualized scroll listbox.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents._common.warmup_session_base import parse_args_and_run

PLATFORM = "tiktok"
HOME_URL = "https://www.tiktok.com/foryou"
LOGGED_IN_MARKERS = [
    # Verified working 2026-05-06 with P03 + P04 storage_state:
    '[data-e2e*="nav-profile" i]',     # nav profile area
    '[data-e2e*="profile" i]',          # any data-e2e profile attr
    'button[aria-label*="profile" i]',  # profile button
    'img[class*="avatar" i]',           # avatar img
    # Legacy fallbacks:
    '[data-e2e="profile-icon"]',
    'a[href*="/@"]',
    'div[data-e2e="upload-icon"]',
]

if __name__ == "__main__":
    raise SystemExit(parse_args_and_run(
        platform=PLATFORM, home_url=HOME_URL, logged_in_markers=LOGGED_IN_MARKERS))
