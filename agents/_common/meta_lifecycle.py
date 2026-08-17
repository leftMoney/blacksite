"""
Blacksite — Meta-family persona lifecycle state I/O and stage gating.

Persona lifecycle stages per fb_ig_strategy.md §3:
  register   ->  Day 0       ->  boss-in-loop register
  limited    ->  Day 0-14    ->  Meta-enforced limited mode, pure passive
  calibration->  Day 14-30   ->  passive + minimal reaction (per §4.2)
  ramp_up    ->  Day 30-60   ->  follow target Pages, save posts
  mission    ->  Day 60+     ->  full intel pipeline + sustained engagement

State file: personas/<persona_id>/state/meta_lifecycle.json
Schema (one source of truth — §6.3 of fb_ig_strategy):
  {
    "persona_id": "P03",
    "fb_register_at": ISO8601 +07:00,
    "ig_register_at": ISO8601 +07:00,
    "current_stage": "register" | "limited" | "calibration" | "ramp_up" | "mission",
    "stage_started_at": ISO8601,
    "limited_mode_lift_at": ISO8601,
    "consecutive_clean_days": 0,
    "burn_signals": [{"at": ISO8601, "kind": "...", "detail": "..."}, ...],
    "engagement_today": {"reactions": 0, "saves": 0, "stories_viewed": 0,
                         "reels_watched": 0, "minutes_on_platform": 0},
    "engagement_budget_today": {"max_reactions": N, "max_saves": N,
                                "max_session_min": N, ...},
    "_meta": {"schema_version": 1, "last_updated": ISO8601}
  }

Per CLAUDE.md §6.4 all timestamps are GMT+7-aware ISO 8601.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
PERSONAS_DIR = ROOT / "personas"

TZ = timezone(timedelta(hours=7))

STAGES = ("register", "limited", "calibration", "ramp_up", "mission")

# Per-tier daily engagement budget at MISSION phase (fb_ig_strategy.md §4.3)
# yolk = P03, white = P04, shell = P05
MISSION_BUDGET: dict[str, dict[str, int]] = {
    "yolk":  {"max_reactions": 15, "max_saves": 4,  "max_follows": 3,
              "max_session_min": 90, "max_reels_watch": 30, "max_stories_view": 20,
              "max_comments": 1},        # weekly avg ~1; budget allows 1/day cap
    "white": {"max_reactions": 12, "max_saves": 3,  "max_follows": 2,
              "max_session_min": 70, "max_reels_watch": 20, "max_stories_view": 12,
              "max_comments": 1},        # bi-weekly avg
    "shell": {"max_reactions": 2,  "max_saves": 1,  "max_follows": 1,
              "max_session_min": 30, "max_reels_watch": 12, "max_stories_view": 6,
              "max_comments": 0},        # never
}

# Limited-phase budget (Day 0-14): pure passive consume only
LIMITED_BUDGET: dict[str, int] = {
    "max_reactions": 0, "max_saves": 0,  "max_follows": 0,
    "max_session_min": 25, "max_reels_watch": 10, "max_stories_view": 5,
    "max_comments": 0,
}

# Calibration (Day 14-30): minimal reactions + saves, no follows of new accounts
def calibration_budget(tier: str) -> dict[str, int]:
    m = MISSION_BUDGET.get(tier, MISSION_BUDGET["shell"])
    return {
        "max_reactions": min(5, m["max_reactions"] // 3),
        "max_saves": min(2, m["max_saves"] // 2),
        "max_follows": 0,
        "max_session_min": min(40, m["max_session_min"] // 2),
        "max_reels_watch": min(10, m["max_reels_watch"] // 2),
        "max_stories_view": min(8, m["max_stories_view"] // 2),
        "max_comments": 0,
    }

# Ramp-up (Day 30-60): linear interpolation calibration -> mission
def ramp_up_budget(tier: str, days_into_ramp: int) -> dict[str, int]:
    cal = calibration_budget(tier)
    mis = MISSION_BUDGET.get(tier, MISSION_BUDGET["shell"])
    f = max(0.0, min(1.0, days_into_ramp / 30.0))
    out = {}
    for k in cal:
        out[k] = int(cal[k] * (1 - f) + mis[k] * f)
    return out


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _path(persona_id: str) -> Path:
    p = PERSONAS_DIR / persona_id / "state" / "meta_lifecycle.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load(persona_id: str) -> dict[str, Any]:
    """Load lifecycle JSON. If missing, return register-pending stub."""
    p = _path(persona_id)
    if not p.exists():
        return {
            "persona_id": persona_id,
            "fb_register_at": None,
            "ig_register_at": None,
            "current_stage": "register",
            "stage_started_at": None,
            "limited_mode_lift_at": None,
            "consecutive_clean_days": 0,
            "burn_signals": [],
            "engagement_today": _zero_engagement(),
            "engagement_budget_today": LIMITED_BUDGET.copy(),
            "_meta": {"schema_version": 1, "last_updated": now_iso()},
        }
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def save(persona_id: str, state: dict[str, Any]) -> None:
    state.setdefault("_meta", {})
    state["_meta"]["last_updated"] = now_iso()
    state["_meta"]["schema_version"] = 1
    p = _path(persona_id)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _zero_engagement() -> dict[str, int]:
    return {"reactions": 0, "saves": 0, "follows": 0, "stories_viewed": 0,
            "reels_watched": 0, "minutes_on_platform": 0, "comments": 0}


def reset_daily_engagement(persona_id: str, tier: str) -> dict[str, Any]:
    """Daily 23:50 BKK: zero today's counters and recompute budget for stage+tier."""
    s = load(persona_id)
    s["engagement_today"] = _zero_engagement()
    s["engagement_budget_today"] = budget_for(s["current_stage"], tier, s)
    save(persona_id, s)
    return s


def budget_for(stage: str, tier: str, state: dict[str, Any]) -> dict[str, int]:
    if stage == "register":
        return LIMITED_BUDGET.copy()
    if stage == "limited":
        return LIMITED_BUDGET.copy()
    if stage == "calibration":
        return calibration_budget(tier)
    if stage == "ramp_up":
        ss = state.get("stage_started_at")
        if not ss:
            return calibration_budget(tier)
        try:
            started = datetime.fromisoformat(ss)
        except ValueError:
            return calibration_budget(tier)
        days_in = (datetime.now(TZ) - started).days
        return ramp_up_budget(tier, days_in)
    if stage == "mission":
        return MISSION_BUDGET.get(tier, MISSION_BUDGET["shell"]).copy()
    raise ValueError(f"Unknown stage={stage}")


def can(action: str, state: dict[str, Any]) -> bool:
    """Check if a single action is permitted under today's budget.

    `action` ∈ {react, save, follow, comment, view_story, watch_reel}
    """
    budget = state["engagement_budget_today"]
    today = state["engagement_today"]
    cap_key = {
        "react":      ("max_reactions",      "reactions"),
        "save":       ("max_saves",          "saves"),
        "follow":     ("max_follows",        "follows"),
        "comment":    ("max_comments",       "comments"),
        "view_story": ("max_stories_view",   "stories_viewed"),
        "watch_reel": ("max_reels_watch",    "reels_watched"),
    }
    if action not in cap_key:
        return False
    cap, used = cap_key[action]
    return today.get(used, 0) < budget.get(cap, 0)


def record(action: str, persona_id: str) -> dict[str, Any]:
    """Increment today's counter for an action and persist."""
    s = load(persona_id)
    counter_key = {
        "react": "reactions", "save": "saves", "follow": "follows",
        "comment": "comments", "view_story": "stories_viewed",
        "watch_reel": "reels_watched",
    }.get(action)
    if counter_key:
        s["engagement_today"][counter_key] = s["engagement_today"].get(counter_key, 0) + 1
    save(persona_id, s)
    return s


def add_minutes(persona_id: str, minutes: int) -> None:
    s = load(persona_id)
    s["engagement_today"]["minutes_on_platform"] = (
        s["engagement_today"].get("minutes_on_platform", 0) + minutes
    )
    save(persona_id, s)


def add_burn_signal(persona_id: str, kind: str, detail: str) -> dict[str, Any]:
    s = load(persona_id)
    s.setdefault("burn_signals", []).append({
        "at": now_iso(), "kind": kind, "detail": detail,
    })
    s["consecutive_clean_days"] = 0
    save(persona_id, s)
    return s


def mark_clean_day(persona_id: str) -> dict[str, Any]:
    s = load(persona_id)
    s["consecutive_clean_days"] = s.get("consecutive_clean_days", 0) + 1
    save(persona_id, s)
    return s


def maybe_advance_stage(persona_id: str) -> tuple[bool, str]:
    """Auto-advance stage when gate criteria met. Returns (advanced, new_stage).

    Gates per fb_ig_strategy.md §3:
      register  -> limited      : after fb_register_at + ig_register_at both set
      limited   -> calibration  : 14 calendar days + 0 burn_signals + ≥3 sessions/day evidence
                                   (sessions/day evidence = consecutive_clean_days >= 14)
      calibrat. -> ramp_up      : no Meta "limited"/"review" notice + algo-feed has vertical
                                   content (consecutive_clean_days >= 14 in calibration)
      ramp_up   -> mission      : following 30+ Pages + 7 days no friction
                                   (we measure 7 consecutive_clean_days in ramp_up)
    """
    s = load(persona_id)
    stage = s["current_stage"]

    if stage == "register":
        if s.get("fb_register_at") and s.get("ig_register_at"):
            return _advance(s, persona_id, "limited")

    elif stage == "limited":
        lift = s.get("limited_mode_lift_at")
        if lift:
            try:
                if datetime.now(TZ) >= datetime.fromisoformat(lift):
                    if s.get("consecutive_clean_days", 0) >= 14 and not s.get("burn_signals"):
                        return _advance(s, persona_id, "calibration")
            except ValueError:
                pass

    elif stage == "calibration":
        ss = s.get("stage_started_at")
        if ss:
            try:
                started = datetime.fromisoformat(ss)
                if (datetime.now(TZ) - started).days >= 14:
                    if s.get("consecutive_clean_days", 0) >= 14:
                        return _advance(s, persona_id, "ramp_up")
            except ValueError:
                pass

    elif stage == "ramp_up":
        ss = s.get("stage_started_at")
        if ss:
            try:
                started = datetime.fromisoformat(ss)
                if (datetime.now(TZ) - started).days >= 30:
                    if s.get("consecutive_clean_days", 0) >= 7:
                        return _advance(s, persona_id, "mission")
            except ValueError:
                pass

    return (False, stage)


def _advance(s: dict[str, Any], persona_id: str, to_stage: str) -> tuple[bool, str]:
    if to_stage not in STAGES:
        raise ValueError(f"Unknown stage={to_stage}")
    s["current_stage"] = to_stage
    s["stage_started_at"] = now_iso()
    s["consecutive_clean_days"] = 0
    save(persona_id, s)
    return (True, to_stage)


def mark_register_event(persona_id: str, platform: str) -> dict[str, Any]:
    """Called by register.py on FB or IG register success."""
    s = load(persona_id)
    key = {"facebook": "fb_register_at", "instagram": "ig_register_at"}.get(platform)
    if not key:
        raise ValueError(f"Unknown platform={platform}")
    s[key] = now_iso()
    if platform == "facebook":
        # Meta-enforced 14-day limited mode for new FB accounts
        s["limited_mode_lift_at"] = (
            datetime.now(TZ) + timedelta(days=14)
        ).isoformat(timespec="seconds")
    save(persona_id, s)
    return s


def get_tier(persona_id: str) -> str:
    """Read tier from persona profile.yaml."""
    import yaml
    profile = PERSONAS_DIR / persona_id / "profile.yaml"
    if not profile.exists():
        return "shell"
    with profile.open("r", encoding="utf-8") as f:
        d = yaml.safe_load(f)
    return d.get("tier", "shell")


# CLI for boss inspection
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Inspect / advance Meta lifecycle state.")
    p.add_argument("persona_id", help="P03 / P04 / P05")
    p.add_argument("--reset-today", action="store_true", help="Reset today's engagement counters")
    p.add_argument("--advance", action="store_true", help="Try to auto-advance stage if gate passes")
    args = p.parse_args()

    if args.reset_today:
        tier = get_tier(args.persona_id)
        s = reset_daily_engagement(args.persona_id, tier)
        print(json.dumps(s["engagement_budget_today"], indent=2))
    elif args.advance:
        advanced, stage = maybe_advance_stage(args.persona_id)
        print(f"advanced={advanced} stage={stage}")
    else:
        print(json.dumps(load(args.persona_id), indent=2, ensure_ascii=False))
