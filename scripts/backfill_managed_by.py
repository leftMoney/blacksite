"""scripts/backfill_managed_by.py — one-shot: add `managed_by` field to all
agent_kpi/*.yaml files that don't have it. Default value: SECTION_CHIEF
(backward-compat: the singleton chief continues managing all 25 agents).

Boss directive 5/3 §15.Z: multi-chief scaling adds the framework, default
remains 1 chief.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
KPI_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "agent_kpi"
DEFAULT_CHIEF = "SECTION_CHIEF"


def main() -> int:
    if not KPI_DIR.exists():
        print(f"KPI dir missing: {KPI_DIR}")
        return 1
    n_updated = 0
    n_already = 0
    for p in sorted(KPI_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            print(f"SKIP {p.name}: {e}")
            continue
        if data.get("managed_by"):
            n_already += 1
            continue
        data["managed_by"] = DEFAULT_CHIEF
        p.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        n_updated += 1
        print(f"  + {p.name} → managed_by: {DEFAULT_CHIEF}")
    print(f"\n{n_updated} files updated · {n_already} already had managed_by field")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
