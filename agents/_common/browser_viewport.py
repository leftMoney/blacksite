"""Shared browser dimensions for crawler and login automation."""

from __future__ import annotations

MOBILE_WIDTH = 430
MOBILE_HEIGHT = 932
MOBILE_WINDOW = (MOBILE_WIDTH, MOBILE_HEIGHT)
MOBILE_VIEWPORT = {"width": MOBILE_WIDTH, "height": MOBILE_HEIGHT}

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
    "Mobile/15E148 Safari/604.1"
)


def mobile_viewport() -> dict[str, int]:
    return dict(MOBILE_VIEWPORT)
