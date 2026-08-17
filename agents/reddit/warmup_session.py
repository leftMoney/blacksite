"""agents/reddit/warmup_session.py — P05 Reddit warmup session entry.

Thin wrapper around agents._common.warmup_session_base.

Reddit logged-in markers (REGISTER_LESSONS.md §2.6 + 2026-05-06 verify):
- `[aria-label="Open user menu"]` — top-right user menu button (logged-in only)
- `a[href*="/user/"]` — user profile link in nav
- `[data-testid="user-menu"]` — alt selector

Mode default = verify_only (smoke). active = TODO v1.2.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents._common.warmup_session_base import parse_args_and_run

PLATFORM = "reddit"
HOME_URL = "https://www.reddit.com/"
LOGGED_IN_MARKERS = [
    # Verified working 2026-05-06 with P05 storage_state:
    'button#expand-user-drawer-button',  # current Reddit user menu button
    'a[href^="/user/"]',                  # any user profile link in nav (logged-in only)
    # Legacy fallbacks (in case Reddit reverts):
    '[aria-label="Open user menu"]',
    'button[id^="USER_DROPDOWN"]',
    '[data-testid="user-menu"]',
]


if __name__ == "__main__":
    raise SystemExit(parse_args_and_run(
        platform=PLATFORM, home_url=HOME_URL,
        logged_in_markers=LOGGED_IN_MARKERS))
