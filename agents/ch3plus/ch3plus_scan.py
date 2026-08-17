"""
Blacksite — CH3 Plus (Channel 3 / 3Plus) public feed scanner.

Per Q5 (ChatGPT 2026-04-30): Channel 3 digital app era; official BEC
platform. local drama / news / live TV / replays. Lottery/horoscope appears
through news + media (not app-native KOLs). Clean platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common.web_feed_scanner import PlatformConfig, run

CFG = PlatformConfig(
    name="ch3plus",
    policy_yaml_filename="ch3plus_targets.yaml",
    raw_subdir="ch3plus",
    seen_filename="ch3plus_seen_items.json",
    # v1.6 selector tuning (2026-05-02): ch3plus URL families:
    #   /news/<category>/<slug>/<numeric_id>  — actual articles (depth 4)
    #   /drama/<numeric_id>                   — drama detail
    #   /watch/<numeric_id>                   — episode watch page
    # Old regex captured the category as item_id (47 emits = 21 unique
    # categories with dupes, no real articles). New regex captures only
    # the trailing numeric ID — uniquely identifies each article.
    # CSS uses default broad anchor pattern (the first comma-combined
    # variant fails visibility check on hidden category links); the
    # regex does the real filtering.
    item_id_regex=r"/(?:news/[^/]+/[^/]+|drama|watch)/(\d{2,})(?:/?(?:\?|#|$))",
)

if __name__ == "__main__":
    run(CFG)
