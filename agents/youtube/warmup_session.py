"""agents/youtube/warmup_session.py — P04 YouTube warmup session.

YouTube via Google login (PERSONA_P04_GMAIL). DOM: REGISTER_LESSONS §2.8.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents._common.warmup_session_base import parse_args_and_run

PLATFORM = "youtube"
HOME_URL = "https://www.youtube.com/"
LOGGED_IN_MARKERS = [
    'button#avatar-btn',
    '#avatar-btn img',
    'ytd-topbar-menu-button-renderer button',  # signed-in user button
    'ytd-button-renderer#button[aria-label*="Account" i]',
    'a[aria-label*="account"][role="button"]',
]

if __name__ == "__main__":
    raise SystemExit(parse_args_and_run(
        platform=PLATFORM, home_url=HOME_URL, logged_in_markers=LOGGED_IN_MARKERS))
