"""agents/facebook/warmup_session.py — P03 / P04 Facebook warmup session.

DOM details: REGISTER_LESSONS.md §2.1 (mobile) / §2.2 (desktop).
Logged-in markers: profile/account links visible in top nav (logged-out has Login form).
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents._common.warmup_session_base import parse_args_and_run

PLATFORM = "facebook"
HOME_URL = "https://www.facebook.com/"
LOGGED_IN_MARKERS = [
    '[aria-label="Your profile"]',
    'a[href*="/me/"]',
    '[aria-label="Account"]',
    'div[role="navigation"] a[aria-label*="Home" i]',
    'a[aria-label="Marketplace"]',  # nav item only logged-in see
]

if __name__ == "__main__":
    raise SystemExit(parse_args_and_run(
        platform=PLATFORM, home_url=HOME_URL, logged_in_markers=LOGGED_IN_MARKERS))
