"""
Blacksite — oneD (Channel One local) public feed scanner.

Per Q5 (ChatGPT 2026-04-30): oneD has 15M Android downloads, ~220K
downloads in last 30d (AppBrain Apr 2026). local drama / sitcom / variety /
concerts / one31 / GMM25 live / vertical shorts. Mostly clean platform
but horoscope/variety can amplify folk-belief content.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common.web_feed_scanner import PlatformConfig, run

CFG = PlatformConfig(
    name="oned",
    policy_yaml_filename="oned_targets.yaml",
    raw_subdir="oned",
    seen_filename="oned_seen_items.json",
    # v1.6 selector tuning (2026-05-02): one31 uses two URL families:
    #   /shows/(detail|video)/<numeric_id>  — show / video pages
    #   /news/detail/<numeric_id>           — news article pages
    # The /news template emits backslash hrefs (\news/detail/<id>) so the
    # CSS selector matches both variants and the regex normalizes both.
    card_link_css="a[href*='shows/detail/'], a[href*='shows/video/'], a[href*='news/detail/']",
    item_id_regex=r"[\\/](?:shows[\\/](?:detail|video)|news[\\/]detail)[\\/](\d{2,})",
)

if __name__ == "__main__":
    run(CFG)
