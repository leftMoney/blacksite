"""agents/discord/warmup_session.py — P05 Discord warmup session.

DOM: REGISTER_LESSONS.md §2.5. Discord webapp; community-graph.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents._common.warmup_session_base import parse_args_and_run

PLATFORM = "discord"
HOME_URL = "https://discord.com/channels/@me"
LOGGED_IN_MARKERS = [
    'div[class*="container"][class*="avatar"]',
    'nav[aria-label*="Server" i]',
    'div[class*="nameTag"]',  # username + discriminator badge
    'div[class*="panels"][class*="user-area"]',
    'a[href="/channels/@me"]',  # only nav-rendered when logged-in
]

if __name__ == "__main__":
    raise SystemExit(parse_args_and_run(
        platform=PLATFORM, home_url=HOME_URL, logged_in_markers=LOGGED_IN_MARKERS))
