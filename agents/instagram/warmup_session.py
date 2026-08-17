"""agents/instagram/warmup_session.py — P03 / P04 Instagram warmup session."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents._common.warmup_session_base import parse_args_and_run

PLATFORM = "instagram"
HOME_URL = "https://www.instagram.com/"
LOGGED_IN_MARKERS = [
    'svg[aria-label="Home"]',  # nav home icon (logged-out has no nav)
    'a[href="/accounts/edit/"]',
    'a[href*="/direct/"]',  # DM nav
    '[role="link"][aria-label*="Profile" i]',
    'svg[aria-label="New post"]',
]

if __name__ == "__main__":
    raise SystemExit(parse_args_and_run(
        platform=PLATFORM, home_url=HOME_URL, logged_in_markers=LOGGED_IN_MARKERS))
