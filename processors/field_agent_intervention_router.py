"""Route Field Agent repair tasks into deterministic / vision / LLM lanes.

The router does not spend LLM tokens by default. It turns Section Chief repair
tasks into explicit intervention plans so cron or Commander can execute the next
step without guessing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from processors.section_chief_work_audit import raw_dir_candidates  # noqa: E402

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
REPAIR_TASKS_JSON = RUNTIME / "field_agent_repair_tasks" / "current.json"
OUT_DIR = RUNTIME / "field_agent_interventions"
OUT_JSON = OUT_DIR / "current.json"
OUT_MD = OUT_DIR / "current.md"
SCREENSHOT_DIR = RUNTIME / "screenshots"


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def recent_page_states(agent_id: str, *, limit: int = 3) -> list[dict]:
    states: list[dict] = []
    cutoff_mtime = (now() - timedelta(hours=72)).timestamp()
    for directory in raw_dir_candidates(agent_id):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            if path.stat().st_mtime < cutoff_mtime:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for line in lines:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if item.get("event") != "page_state_check":
                    continue
                shot = item.get("screenshot")
                if shot and not Path(str(shot)).is_absolute():
                    item["screenshot_path"] = str((SCREENSHOT_DIR / str(shot)).resolve())
                item["_raw_path"] = str(path.relative_to(ROOT))
                states.append(item)
    states.sort(key=lambda x: str(x.get("checked_at") or x.get("ts") or ""), reverse=True)
    return states[:limit]


def route_task(task: dict) -> dict:
    task_type = task.get("task_type")
    agent_id = str(task.get("agent_id") or "")
    page_states = recent_page_states(agent_id)
    latest = page_states[0] if page_states else None
    verdict = str((latest or {}).get("verdict") or "")

    if task_type == "account_recovery":
        lane = "deterministic_account_recovery"
        ai_tier = None
        call_ai = verdict in {"captcha", "human_action_required", "unknown"}
        trigger = "AI vision only when screenshot evidence is ambiguous or human-gate-like."
    elif task_type == "diagnose_zero_output":
        lane = "mobile_smoke_then_selector_repair"
        ai_tier = "fast_vision"
        call_ai = verdict in {"unknown", "empty_feed", "wrong_page_or_logged_out"} or latest is None
        trigger = "Use AI vision after deterministic mobile smoke when page_state cannot explain zero output."
    elif task_type == "quality_sample":
        lane = "llm_quality_sample"
        ai_tier = "fast_text"
        call_ai = True
        trigger = "Use low-tier GPT/Codex to judge sample relevance after raw output exists."
    elif task_type in {"build_or_assign_collector", "activate_feed_harvest"}:
        lane = "engineering_collector_repair"
        ai_tier = "fast_text"
        call_ai = False
        trigger = "Engineering repair first; LLM optional only if selector logic is unclear after smoke evidence."
    elif task_type == "assign_mission_or_mark_reserve":
        lane = "section_chief_assignment"
        ai_tier = None
        call_ai = False
        trigger = "No LLM needed; this is a management assignment."
    else:
        lane = "manual_triage"
        ai_tier = None
        call_ai = False
        trigger = "Unknown task type; keep deterministic."

    return {
        "task_id": task.get("task_id"),
        "agent_id": agent_id,
        "priority": task.get("priority"),
        "owner": task.get("owner"),
        "task_type": task_type,
        "lane": lane,
        "ai_tier": ai_tier,
        "ai_call_recommended": call_ai,
        "trigger_reason": trigger,
        "latest_page_state": latest,
        "page_state_count": len(page_states),
        "status": "queued",
        "created_at": now_iso(),
    }


def write_outputs(routes: list[dict], *, execute_ai: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now_iso(),
        "instance": ACTIVE_INSTANCE,
        "execute_ai": execute_ai,
        "note": "execute_ai=false means no subscription/API tokens were spent.",
        "routes": routes,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# Field Agent Intervention Routes - {now_iso()}",
        "",
        f"execute_ai: {str(execute_ai).lower()}",
        "",
    ]
    if not routes:
        lines.append("- No intervention route.")
    for route in routes:
        latest = route.get("latest_page_state") or {}
        lines.append(
            f"- [{route['priority']}] {route['agent_id']} -> {route['lane']} "
            f"ai={route['ai_call_recommended']} verdict={latest.get('verdict', '-')}"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh(*, execute_ai: bool = False) -> dict:
    data = load_json(REPAIR_TASKS_JSON, {"tasks": []})
    tasks = data.get("tasks", []) if isinstance(data, dict) else []
    routes = [route_task(task) for task in tasks]
    write_outputs(routes, execute_ai=execute_ai)
    try:
        from processors.history_log import log_event

        log_event(
            actor="SECTION_CHIEF",
            kind="metric",
            scope="field_agent_intervention",
            title="field agent intervention routes refreshed",
            body=json.dumps(
                {
                    "routes": len(routes),
                    "ai_recommended": sum(1 for r in routes if r["ai_call_recommended"]),
                    "execute_ai": execute_ai,
                },
                ensure_ascii=False,
            ),
            refs=[OUT_JSON.relative_to(ROOT).as_posix(), OUT_MD.relative_to(ROOT).as_posix()],
        )
    except Exception:
        pass
    return {"routes": routes}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-ai", action="store_true", help="reserved; default spends no LLM tokens")
    args = parser.parse_args()
    result = refresh(execute_ai=args.execute_ai)
    print(json.dumps({"routes": len(result["routes"])}, ensure_ascii=False, sort_keys=True))
    print(str(OUT_JSON))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
