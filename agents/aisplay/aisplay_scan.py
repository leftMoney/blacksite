"""
Blacksite — AIS Play public feed scanner.

Per Q5 (ChatGPT 2026-04-30): AIS Play sports push intensified 2024-2025;
local League distribution. Bundled OTT for AIS subscribers (large mobile
subs context). Public sports feed should be visible without login.
Full account = AIS SIM (target-country residential), deferred to v2.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common.web_feed_scanner import PlatformConfig, run

CFG = PlatformConfig(
    name="aisplay",
    policy_yaml_filename="aisplay_targets.yaml",
    raw_subdir="aisplay",
    seen_filename="aisplay_seen_items.json",
    # v1.6 selector tuning (2026-05-02): aisplay structure:
    #   /portal/get_item/<24-hex-id>/    — content items (videos)
    #   /portal/get_section/<24-hex-id>/ — section pages
    #   /portal/live/?vid=<24-hex-id>    — live channels
    # Live URLs use ?vid= query param (no path slug); regex matches both
    # path-segment IDs and ?vid=<id> query-string IDs. Content needs ~2s
    # to JS-render — wait_for_selector against the specific card pattern
    # blocks until items appear (default `a[href*='/']` matches nav too
    # early and the subsequent evaluate() finds 0 content cards).
    card_link_css="a[href*='/portal/get_item/'], a[href*='/portal/live/']",
    item_id_regex=r"(?:/portal/(?:get_item|get_section)/|[?&]vid=)([a-f0-9]{24})",
)

if __name__ == "__main__":
    run(CFG)
