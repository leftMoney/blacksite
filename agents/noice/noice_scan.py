"""
Blacksite — NOICE the target country audio platform feed scanner.

Per Q5 (ChatGPT 2026-04-30): NOICE = local audio platform active early
2020s; podcasts, audio creators, comedy, talk. Low gambling adjacency
but possible horoscope/audio fortune content (hence shell-tier capture).
Lower priority than FB / Bigo / TrueID.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common.web_feed_scanner import PlatformConfig, run

CFG = PlatformConfig(
    name="noice",
    policy_yaml_filename="noice_targets.yaml",
    raw_subdir="noice",
    seen_filename="noice_seen_items.json",
    item_id_regex=r"/([a-zA-Z0-9\-_]{6,})/?(?:\?|#|$)",
)

if __name__ == "__main__":
    run(CFG)
