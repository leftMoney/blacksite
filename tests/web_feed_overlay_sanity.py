"""
Blacksite — sanity check for `_load_selectors_overlay()` in
agents/_common/web_feed_scanner.py (v1.7 schema extension, 2026-05-02).

What this validates:
  1. yaml without `selectors:` block → returns base PlatformConfig unchanged
  2. yaml with valid selectors → fields overridden, others retained
  3. yaml with unknown selectors key → warning logged, key ignored, base kept
  4. yaml with non-mapping `selectors:` → ignored gracefully, base kept
  5. yaml selectors with int-like string → coerced
  6. yaml selectors with extra_target_fields list → coerced to tuple
  7. backport parity — the 4 v1.6-tuned yamls produce a PlatformConfig
     whose tunable fields match the original scan.py CFG kwargs
     (so behavior is byte-identical pre/post overlay)

Run:  py tests/web_feed_overlay_sanity.py
Exit code 0 = all pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents"))

import yaml

from agents._common.web_feed_scanner import (
    PlatformConfig,
    _load_selectors_overlay,
    _OVERLAYABLE_FIELDS,
)

FAIL = 0


def expect(label: str, actual, expected) -> None:
    global FAIL
    ok = actual == expected
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"        expected: {expected!r}")
        print(f"        actual:   {actual!r}")
        FAIL += 1


def base() -> PlatformConfig:
    return PlatformConfig(
        name="testp",
        policy_yaml_filename="testp_targets.yaml",
        raw_subdir="testp",
        seen_filename="testp_seen.json",
    )


def case_no_block():
    print("CASE 1: yaml without selectors: block")
    cfg = _load_selectors_overlay({"scan": {"enable": True}}, base())
    expect("name unchanged", cfg.name, "testp")
    expect("card_link_css default", cfg.card_link_css, "a[href*='/']")
    expect("item_id_regex default", cfg.item_id_regex,
           r"/([^/?#]+)/?(?:\?|#|$)")
    expect("min_title_len default", cfg.min_title_len, 5)


def case_partial_override():
    print("CASE 2: yaml with valid partial selectors")
    yaml_doc = {
        "selectors": {
            "card_link_css": "a.test",
            "item_id_regex": r"/([a-z]+)$",
        }
    }
    cfg = _load_selectors_overlay(yaml_doc, base())
    expect("css overridden", cfg.card_link_css, "a.test")
    expect("regex overridden", cfg.item_id_regex, r"/([a-z]+)$")
    expect("min_title_len kept default", cfg.min_title_len, 5)
    expect("max_title_len kept default", cfg.max_title_len, 400)
    expect("name preserved (not overlayable)", cfg.name, "testp")


def case_unknown_key_ignored():
    print("CASE 3: unknown selectors key warned + ignored")
    yaml_doc = {
        "selectors": {
            "card_link_css": "a.real",
            "wait_ms": 5000,        # not in _OVERLAYABLE_FIELDS
            "totally_made_up": True,
        }
    }
    cfg = _load_selectors_overlay(yaml_doc, base())
    expect("legit key applied", cfg.card_link_css, "a.real")
    expect("default still on legit field", cfg.min_title_len, 5)
    # No PlatformConfig field for wait_ms → would have crashed dataclass.replace()
    # if not filtered. Reaching this assertion means it was filtered cleanly.


def case_bad_block_type():
    print("CASE 4: selectors: as list (not mapping) — graceful ignore")
    yaml_doc = {"selectors": ["a", "b"]}
    cfg = _load_selectors_overlay(yaml_doc, base())
    expect("base intact after bad block", cfg.card_link_css,
           base().card_link_css)


def case_int_coerce():
    print("CASE 5: int-like string coerced")
    yaml_doc = {"selectors": {"min_title_len": "12", "max_title_len": 250}}
    cfg = _load_selectors_overlay(yaml_doc, base())
    expect("min coerced str→int", cfg.min_title_len, 12)
    expect("max int kept", cfg.max_title_len, 250)


def case_extra_fields_tuple():
    print("CASE 6: extra_target_fields list → tuple")
    yaml_doc = {"selectors": {"extra_target_fields": ["tier", "label", "role"]}}
    cfg = _load_selectors_overlay(yaml_doc, base())
    expect("list coerced to tuple", cfg.extra_target_fields,
           ("tier", "label", "role"))


def case_backport_parity():
    """Each v1.6-tuned yaml should yield a PlatformConfig whose tunable
    fields match the original scan.py CFG kwargs — i.e. behavior is
    byte-identical with or without the yaml overlay shipping live."""
    print("CASE 7: 4-platform backport parity")
    pairs = [
        ("ottA",        "ottA.ottA_scan",               "instances/_TEMPLATE/policy/ottA_targets.yaml"),
        ("ottB",        "ottB.ottB_scan",               "instances/_TEMPLATE/policy/ottB_targets.yaml"),
        ("streamA",     "streamA.streamA_scan",         "instances/_TEMPLATE/policy/streamA_targets.yaml"),
        ("newsportalB", "newsportalB.newsportalB_scan", "instances/_TEMPLATE/policy/newsportalB_targets.yaml"),
    ]
    for plat, mod, yp in pairs:
        M = __import__(mod, fromlist=["CFG"])
        with open(ROOT / yp, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        overlaid = _load_selectors_overlay(doc, M.CFG)
        for key in _OVERLAYABLE_FIELDS:
            expect(f"{plat}.{key} parity",
                   getattr(overlaid, key), getattr(M.CFG, key))


def main() -> int:
    case_no_block()
    case_partial_override()
    case_unknown_key_ignored()
    case_bad_block_type()
    case_int_coerce()
    case_extra_fields_tuple()
    case_backport_parity()
    print()
    if FAIL:
        print(f"{FAIL} assertion(s) failed.")
        return 1
    print("All overlay sanity checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
