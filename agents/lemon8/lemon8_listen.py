"""
Blacksite — Lemon8 listener (skeleton).

Status: SKELETON. Activated when:
  - Lemon8 account registered (email + virtual target-country phone)
  - VPN to a target-country endpoint
  - Playwright-driven scraper (no public API)

Lemon8 is panel-undercounted but has meaningful web reach in some target markets.
Female / folk-belief / lifestyle skew — relevant for the client brand P0/P1 segments.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

print("[lemon8_listen] not yet activated — see module docstring", flush=True)
