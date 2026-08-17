"""agents/twitter/warmup_session.py — P04 X (Twitter) warmup session.

X DOM 5/5 hotel CC manual register; modern selectors data-testid based.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agents._common.warmup_session_base import parse_args_and_run

PLATFORM = "twitter_x"
# X redirects /home → / for logged-out users. For logged-in users, going to / often shows
# the home timeline directly. Use root URL + check primary column or compose button.
HOME_URL = "https://x.com/"
LOGGED_IN_MARKERS = [
    'a[data-testid="AppTabBar_Profile_Link"]',
    'a[data-testid="SideNav_AccountSwitcher_Button"]',
    'a[data-testid="SideNav_NewTweet_Button"]',
    'a[aria-label="Profile"]',
    'div[data-testid="primaryColumn"]',
    'div[data-testid="sidebarColumn"]',
    'a[href="/compose/post"]',
    'a[href="/notifications"]',
    'a[href="/messages"]',
]

if __name__ == "__main__":
    raise SystemExit(parse_args_and_run(
        platform=PLATFORM, home_url=HOME_URL, logged_in_markers=LOGGED_IN_MARKERS))
