"""agents/_common/kpi_loader.py — read Field Agent KPI yaml at startup.

Per CLAUDE.md §15 + boss 5/2 Q3 lock: KPI changes propagate via yaml
file write; Field Agents read on next cron fire. This loader is the
canonical reader Field Agents call.

Returns a dict; never raises on missing file (falls back to baseline
defaults from instances/<active>/policy/agent_kpi_baseline.yaml). Field
Agents must NOT crash on missing KPI yaml — collection comes first.

Usage:
    from agents._common.kpi_loader import load_kpi
    kpi = load_kpi("P03_Bigo")
    target_yield = kpi["target_kpi"].get("msg_yield_baseline_24h", 200)
    directives = kpi.get("recent_directives", [])
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
KPI_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "agent_kpi"
BASELINE_PATH = ROOT / "instances" / ACTIVE_INSTANCE / "policy" / "agent_kpi_baseline.yaml"

TZ = timezone(timedelta(hours=7))


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


_baseline_cache: dict | None = None


def _load_baseline() -> dict:
    """Load policy/agent_kpi_baseline.yaml once per process."""
    global _baseline_cache
    if _baseline_cache is not None:
        return _baseline_cache
    if not BASELINE_PATH.exists():
        _baseline_cache = {"field_agent": {}, "defaults": {}}
        return _baseline_cache
    try:
        _baseline_cache = yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        _baseline_cache = {"field_agent": {}, "defaults": {}}
    return _baseline_cache


def _baseline_for(agent_id: str, sub_class_hint: str | None = None) -> dict:
    """Resolve baseline target_kpi for an agent. Order:
    1. exact match in baseline.field_agent[agent_id]
    2. baseline.defaults[<sub_class>] using sub_class_hint
    3. empty dict
    """
    base = _load_baseline()
    fa = (base.get("field_agent") or {}).get(agent_id)
    if fa:
        sub_class = fa.get("sub_class") or sub_class_hint or "persona_driven"
        target = {k: v for k, v in fa.items() if k not in ("sub_class", "notes")}
        return {"sub_class": sub_class, "target_kpi": target,
                "notes": fa.get("notes")}
    sub_class = sub_class_hint or "persona_driven"
    defaults = (base.get("defaults") or {}).get(sub_class) or {}
    return {"sub_class": sub_class, "target_kpi": dict(defaults), "notes": None}


def load_kpi(agent_id: str, sub_class_hint: str | None = None) -> dict:
    """Read runtime/agent_kpi/<agent_id>.yaml. Falls back to baseline
    defaults when the per-agent file doesn't exist yet.

    Returns dict with at minimum:
      agent_id (str)
      sub_class (str)
      current_kpi (dict — empty if no eval yet)
      target_kpi (dict)
      status (str — 'green' default)
      notes (str | None)
      recent_directives (list)

    Never raises. Field Agents must keep collecting even if KPI unavailable.
    """
    path = KPI_DIR / f"{agent_id}.yaml"
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            data.setdefault("agent_id", agent_id)
            data.setdefault("recent_directives", [])
            data.setdefault("current_kpi", {})
            data.setdefault("status", "green")
            # if target_kpi missing, fill from baseline
            if not data.get("target_kpi"):
                base = _baseline_for(agent_id, data.get("sub_class") or sub_class_hint)
                data["target_kpi"] = base["target_kpi"]
            return data
        except yaml.YAMLError:
            # fall through to baseline path
            pass

    # No file or parse failure → baseline-only stub
    base = _baseline_for(agent_id, sub_class_hint)
    return {
        "agent_id": agent_id,
        "sub_class": base["sub_class"],
        "last_evaluated_at": None,
        "last_evaluated_by": None,
        "current_kpi": {},
        "target_kpi": base["target_kpi"],
        "status": "green",
        "notes": base.get("notes") or "(loaded from baseline; no eval yet)",
        "recent_directives": [],
        "incident_history": [],
    }


def list_known_agents() -> list[str]:
    """Return all agent_ids known to baseline yaml. Used by section_chief_eval
    + scripts/agents.py to enumerate the fleet."""
    base = _load_baseline()
    return sorted((base.get("field_agent") or {}).keys())


def filter_active_directives(kpi: dict) -> list[dict]:
    """Filter recent_directives for non-expired entries. Field Agents apply
    only active directives."""
    now = _now_iso()
    out = []
    for d in kpi.get("recent_directives", []) or []:
        exp = d.get("expires_at")
        if exp and exp < now:
            continue
        out.append(d)
    return out


if __name__ == "__main__":
    # Smoke test: print KPI for a known agent + a missing one
    import json
    for aid in ("P01_TG", "oneD_anon", "ghost_agent_xyz"):
        k = load_kpi(aid)
        print(f"=== {aid} ===")
        print(json.dumps(k, ensure_ascii=False, indent=2, default=str))
        print()
    print(f"Known agents: {list_known_agents()}")
