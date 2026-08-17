"""Best-effort refresh helper for the boss task-intelligence audit HTML."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def refresh_org_task_audit(reason: str, since: str = "7d") -> None:
    """Refresh the mobile audit page after task-layer review/report events.

    Best-effort by design: audit HTML must never break the producing job.
    """
    try:
        from processors.section_chief_work_audit import refresh_work_audit

        refresh_work_audit(reason)
    except Exception:
        pass
    try:
        from processors import field_agent_intervention_router

        field_agent_intervention_router.refresh(execute_ai=False)
    except Exception:
        pass
    try:
        from processors import field_agent_factory

        field_agent_factory.refresh(allow_dispatch=False, force_checkin=False)
    except Exception:
        pass
    try:
        import render_boss_audit_html

        render_boss_audit_html.refresh(reason=reason, since=since)
    except Exception:
        pass
