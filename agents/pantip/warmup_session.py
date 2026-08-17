"""agents/pantip/warmup_session.py — P03 / P05 Pantip warmup session.

DOM: REGISTER_LESSONS.md §2.7. Pantip local forum.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents._common.warmup_session_base import parse_args_and_run

PLATFORM = "pantip"
HOME_URL = "https://pantip.com/"
LOGGED_IN_MARKERS = [
    'a[href*="/profile"]',
    '[class*="userZone" i]',
    'a[href*="/logout"]',  # logout link visible only when logged-in
    'a[href*="/notification"]',
    'a[href*="/message"]',
    'a[href*="/forum/new_topic"]',
    'a[href*="/home/feed"]',
    'a[href*="/settings/notifications"]',
    'img[class*="avatar" i]',
]

if __name__ == "__main__":
    raise SystemExit(parse_args_and_run(
        platform=PLATFORM, home_url=HOME_URL, logged_in_markers=LOGGED_IN_MARKERS))
