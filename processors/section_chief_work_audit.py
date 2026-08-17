"""Section Chief work-state audit.

This is the guardrail the KPI evaluator was missing: login health is not field
work. The audit classifies every Field Agent into mission states and writes a
boss-readable work-order report without calling an LLM.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
RUNTIME = INSTANCE_DIR / "runtime"
POLICY = INSTANCE_DIR / "policy"
KPI_DIR = RUNTIME / "agent_kpi"
RAW_DIR = RUNTIME / "raw"
REPORT_DIR = RUNTIME / "reports"
BASELINE_PATH = POLICY / "agent_kpi_baseline.yaml"
SCHEDULE_PATH = POLICY / "persona_warmup_schedule.yaml"

REPORT_JSON = REPORT_DIR / "section_chief_work_audit.json"
REPORT_MD = REPORT_DIR / "section_chief_work_orders.md"
REPAIR_DIR = RUNTIME / "field_agent_repair_tasks"
REPAIR_JSON = REPAIR_DIR / "current.json"
REPAIR_MD = REPAIR_DIR / "current.md"

SUPPORT_EVENTS = {
    "verify_session",
    "session_recovery",
    "session_recovery_visual_check",
    "manual_relogin_handoff",
    "manual_relogin_completed",
    "alias_login",
    "register",
    "active_mode_scaffold_only",
    "page_state_check",
    "feed_harvest_summary",
    "collector_status",
}

RESOLVED_REASONS = {"", "resolved", "ok", "pass", "none", "false"}

RAW_DIR_OVERRIDES = {
    "P01_TG": ["P01"],
    "P02_TG": ["P02"],
    "P03_FB": ["P03_FB", "facebook/P03"],
    "P03_IG": ["P03_IG", "instagram/P03"],
    "P04_IG": ["P04_IG", "instagram/P04"],
    "trueid_anon": ["trueid"],
    "sanook_anon": ["sanook"],
    "lottery_eco_anon": ["lottery_eco"],
    "payment_intel_anon": ["payment_intel"],
    "regulator_pulse_anon": ["regulator_pulse"],
    "bigo_lobby_anon": ["bigo"],
    "nimo_lobby_anon": ["nimo"],
    "fb_page_anon": ["facebook"],
    "fb_og_meta_anon": ["facebook_og_meta"],
    # 5/30 sports pivot — new monitoring tracks (collect via existing scripts)
    "tl1_fan_groups_anon": ["facebook_og_meta", "youtube"],
    "example_fanclub_anon": ["facebook_og_meta", "youtube"],
    "esports_fans_anon": ["youtube"],
    "sports_kol_anon": ["youtube", "tiktok"],
}

COLLECTOR_SCRIPT_OVERRIDES = {
    "trueid_anon": "agents/trueid/trueid_listen.py",
    "sanook_anon": "agents/_common/policy_target_scan.py",
    "lottery_eco_anon": "agents/_common/policy_target_scan.py",
    "payment_intel_anon": "agents/_common/policy_target_scan.py",
    "regulator_pulse_anon": "agents/_common/policy_target_scan.py",
    "bigo_lobby_anon": "agents/bigo/bigo_lobby_scan.py",
    "nimo_lobby_anon": "agents/nimo/nimo_lobby_scan.py",
    "fb_page_anon": "agents/facebook/fb_page_scan.py",
    "fb_og_meta_anon": "agents/facebook/fb_og_meta_scan.py",
    # 5/30 sports pivot — new monitoring tracks share existing collectors
    "tl1_fan_groups_anon": "agents/facebook/fb_og_meta_scan.py",
    "example_fanclub_anon": "agents/facebook/fb_og_meta_scan.py",
    "esports_fans_anon": "agents/youtube/yt_channel_monitor.py",
    "sports_kol_anon": "agents/youtube/yt_channel_monitor.py",
}

MISSION_LABELS = {
    "collecting": "有任務產出",
    "login_only": "只有登入維持",
    "scanner_missing": "缺少採集器",
    "scaffold_only": "只有 active 骨架",
    "blocked": "登入/平台卡關",
    "no_output": "採集器無產出",
    "dormant": "已停用",
}

MISSION_ACTIONS = {
    "collecting": "持續採集；下一步是抽樣檢查品質，而不是只看產量。",
    "login_only": "帳號健康檢查不能算任務完成；需要派實際採集任務或明確標成備援帳號。",
    "scanner_missing": "需要補採集器或把任務改派給已有採集能力的情報員。",
    "scaffold_only": "active 模式尚未實作；需要補 feed_harvest 或具體平台採集流程。",
    "blocked": "先處理登入、checkpoint、captcha 或平台冷卻；不要把卡關帳號列為正常任務完成。",
    "no_output": "已有採集器但 24h 無產出；需要檢查 selector、IP、登入狀態或任務目標。",
    "dormant": "已停用或 dead；排除在任務完成率之外，除非重新啟用。",
}


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def cutoff_iso(hours: int = 24) -> str:
    return (now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def write_yaml(path: Path, data: dict) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def load_baseline() -> dict[str, dict]:
    data = load_yaml(BASELINE_PATH)
    return data.get("field_agent", {}) if isinstance(data, dict) else {}


def load_schedule_kinds(baseline: dict[str, dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    data = load_yaml(SCHEDULE_PATH)
    for item in data.get("daily_windows", []) if isinstance(data, dict) else []:
        agent_id = item.get("agent_id")
        platform = str(item.get("platform") or "").lower()
        if not agent_id:
            continue
        is_verify_only = bool((baseline.get(agent_id) or {}).get("is_verify_only", True))
        if is_verify_only:
            out[agent_id] = "login_health_only"
            continue
        plat_dir = "twitter" if platform == "twitter_x" else platform
        harvest_script = ROOT / "agents" / plat_dir / "feed_harvest.py"
        out[agent_id] = "active_feed_harvest" if harvest_script.exists() else "active_scaffold_only"
    return out


def raw_dir_candidates(agent_id: str) -> list[Path]:
    names = RAW_DIR_OVERRIDES.get(agent_id, [agent_id])
    if re.match(r"^P\d{2}_TG$", agent_id):
        names.append(agent_id.split("_", 1)[0])
    if agent_id.endswith("_anon"):
        names.append(agent_id.replace("_anon", ""))
    out = []
    for name in dict.fromkeys(names):
        p = Path(name)
        out.append(p if p.is_absolute() else RAW_DIR / p)
    return out


def collector_script_state(agent_id: str) -> str:
    rel = COLLECTOR_SCRIPT_OVERRIDES.get(agent_id)
    if not rel:
        return "none"
    return "exists" if (ROOT / rel).exists() else "missing"


def read_recent_raw(agent_id: str, cutoff: str) -> tuple[int, int, Counter]:
    total = 0
    mission = 0
    events: Counter = Counter()
    for directory in raw_dir_candidates(agent_id):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            if path.stat().st_mtime < (now() - timedelta(hours=30)).timestamp():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                ts = str(item.get("ts") or "")
                if ts and ts < cutoff:
                    continue
                total += 1
                event = str(item.get("event") or item.get("kind") or "message")
                events[event] += 1
                if event not in SUPPORT_EVENTS:
                    mission += 1
    return total, mission, events


def live_health_issue(kpi: dict) -> str | None:
    live_status = kpi.get("live_status") or {}
    if live_status.get("human_action_required"):
        return str(live_status.get("human_action_required"))
    reason = str(live_status.get("fail_reason") or "").strip()
    if reason.lower() not in RESOLVED_REASONS:
        return reason
    return None


def _health(level: str, reason: str, *, evidence: dict | None = None) -> dict:
    return {
        "level": level,
        "reason": reason,
        "checked_at": now_iso(),
        "evidence": evidence or {},
    }


def make_health_tracks(
    *,
    mission_state: str,
    health_issue: str | None,
    yld: int,
    raw_mission: int,
    raw_total: int,
    current: dict,
    target: dict,
    is_verify_only: bool,
    scan_pending: bool,
    collector_state: str,
    events: Counter,
) -> dict:
    """Phase-1 deterministic health model.

    Later phases add vision and LLM QA. This phase makes login health unable to
    mask mission failure.
    """
    if health_issue:
        account = _health("red", f"platform/account blocker: {health_issue}")
    elif events and raw_mission == 0 and raw_total > 0:
        account = _health("green", "session produced support events only")
    else:
        account = _health("green", "no account blocker recorded")

    mission_level = {
        "collecting": "green",
        "login_only": "yellow",
        "dormant": "gray",
    }.get(mission_state, "red")
    mission = _health(
        mission_level,
        MISSION_ACTIONS.get(mission_state, "Section Chief must inspect mission state"),
        evidence={
            "mission_state": mission_state,
            "yield_24h": yld,
            "raw_mission_24h": raw_mission,
            "raw_total_24h": raw_total,
            "is_verify_only": is_verify_only,
            "scan_pending": scan_pending,
            "collector_script_state": collector_state,
        },
    )

    sn = current.get("signal_noise")
    sn_min = target.get("signal_noise_min")
    if mission_state != "collecting":
        quality = _health("unknown", "no mission output to sample yet")
    elif sn is None or sn_min is None:
        quality = _health("pending", "mission output exists; relevance/quality sample not yet judged")
    elif float(sn) >= float(sn_min):
        quality = _health("green", f"signal_noise {sn} >= target {sn_min}")
    else:
        quality = _health("yellow", f"signal_noise {sn} below target {sn_min}")

    tos = int(current.get("tos_violations") or 0)
    tos_max = int(target.get("tos_violation_max") or 0)
    if health_issue:
        risk = _health("red", f"risk blocker: {health_issue}")
    elif tos > tos_max:
        risk = _health("red", f"ToS warnings {tos} exceed max {tos_max}")
    elif tos > 0:
        risk = _health("yellow", f"ToS warnings present: {tos}")
    else:
        risk = _health("green", "no risk warning recorded")

    return {
        "account_health": account,
        "mission_health": mission,
        "quality_health": quality,
        "risk_health": risk,
    }


def classify_agent(agent_id: str, kpi: dict, baseline: dict, launch_kinds: dict, cutoff: str) -> dict:
    current = kpi.get("current_kpi") or {}
    target = kpi.get("target_kpi") or {}
    live_status = kpi.get("live_status") or {}
    yld = int(current.get("msg_yield_24h") or 0)
    baseline_entry = baseline.get(agent_id) or {}
    note_blob = " ".join(
        str(x or "")
        for x in [
            baseline_entry.get("notes"),
            kpi.get("notes"),
            live_status.get("primary_focus"),
        ]
    ).lower()
    is_verify_only = bool(target.get("is_verify_only", baseline_entry.get("is_verify_only", False)))
    scan_pending = bool(live_status.get("scan_pending"))
    health_issue = live_health_issue(kpi)
    raw_total, raw_mission, events = read_recent_raw(agent_id, cutoff)
    launch_kind = launch_kinds.get(agent_id, "daemon_or_unscheduled")
    collector_state = collector_script_state(agent_id)

    if "dead" in note_blob or kpi.get("status") == "dormant":
        mission_state = "dormant"
        action = "已標記 dormant/dead；小主管應下架或改由替代情報面承接"
    elif health_issue:
        mission_state = "blocked"
        action = "解除登入/平台阻塞後再派工"
    elif yld > 0 or raw_mission > 0:
        mission_state = "collecting"
        action = "維持採集；下一步看品質與洞察"
    elif scan_pending:
        mission_state = "scanner_missing"
        action = "補 scanner 或明確下架此情報面"
    elif collector_state == "missing":
        mission_state = "scanner_missing"
        action = "政策保留此情報面，但採集器不存在；補 scanner 或下架"
    elif collector_state == "exists":
        mission_state = "no_output"
        action = "採集器存在但 24h 無任務產出；檢查排程、selector、IP 或目標源"
    elif is_verify_only or launch_kind == "login_health_only":
        mission_state = "login_only"
        action = "小主管需派 active 工作；不能把登入成功算完成"
    elif launch_kind == "active_scaffold_only" or events.get("active_mode_scaffold_only"):
        mission_state = "scaffold_only"
        action = "補 active Phase A/B 或 feed_harvest"
    else:
        mission_state = "no_output"
        action = "確認排程/採集器/目標源是否真的有跑"

    action = MISSION_ACTIONS.get(mission_state, action)

    severity_order = {
        "blocked": 0,
        "scanner_missing": 1,
        "scaffold_only": 2,
        "login_only": 3,
        "no_output": 4,
        "dormant": 8,
        "collecting": 9,
    }
    health_status = make_health_tracks(
        mission_state=mission_state,
        health_issue=health_issue,
        yld=yld,
        raw_mission=raw_mission,
        raw_total=raw_total,
        current=current,
        target=target,
        is_verify_only=is_verify_only,
        scan_pending=scan_pending,
        collector_state=collector_state,
        events=events,
    )
    return {
        "agent_id": agent_id,
        "status": kpi.get("status") or "unknown",
        "mission_state": mission_state,
        "mission_label": MISSION_LABELS.get(mission_state, mission_state),
        "severity_rank": severity_order.get(mission_state, 8),
        "action": action,
        "health_status": health_status,
        "launch_kind": launch_kind,
        "msg_yield_24h": yld,
        "raw_total_24h": raw_total,
        "raw_mission_24h": raw_mission,
        "support_events_24h": dict(events),
        "is_verify_only": is_verify_only,
        "scan_pending": scan_pending,
        "health_issue": health_issue,
        "collector_script_state": collector_state,
        "last_evaluated_at": kpi.get("last_evaluated_at"),
        "managed_by": kpi.get("managed_by") or kpi.get("last_evaluated_by") or "SECTION_CHIEF",
    }


def update_kpi_mission_fields(rows: list[dict]) -> None:
    by_id = {row["agent_id"]: row for row in rows}
    for path in KPI_DIR.glob("*.yaml"):
        data = load_yaml(path)
        agent_id = data.get("agent_id") or path.stem
        row = by_id.get(agent_id)
        if not row:
            continue
        data["mission_status"] = {
            "state": row["mission_state"],
            "label": row.get("mission_label"),
            "action": row["action"],
            "launch_kind": row["launch_kind"],
            "raw_mission_24h": row["raw_mission_24h"],
            "raw_total_24h": row["raw_total_24h"],
            "last_audited_at": now_iso(),
        }
        data["health_status"] = row.get("health_status") or {}
        write_yaml(path, data)


def _due_at(priority: str) -> str:
    hours = {"P0": 4, "P1": 24, "P2": 48, "P3": 120}.get(priority, 48)
    return (now() + timedelta(hours=hours)).isoformat(timespec="seconds")


def repair_task_for_row(row: dict) -> dict | None:
    state = row.get("mission_state")
    health = row.get("health_status") or {}
    quality_level = (health.get("quality_health") or {}).get("level")
    if state == "collecting":
        if quality_level in {"pending", "yellow"}:
            task_type = "quality_sample"
            owner = "SECTION_CHIEF.qa"
            priority = "P2"
            title = "Sample mission output relevance and signal/noise quality"
            next_action = "Draw recent raw samples, judge whether the output changes the client brand decisions, then update signal_noise and repair prompt/selectors if weak."
        else:
            return None
    elif state == "blocked":
        task_type = "account_recovery"
        owner = "SECTION_CHIEF.account_ops"
        priority = "P0"
        title = "Recover blocked platform/account state"
        next_action = "Use latest page_state_check screenshot/log, attempt credential recovery once, then escalate human gate only if screenshot confirms it."
    elif state == "scanner_missing":
        task_type = "build_or_assign_collector"
        owner = "SECTION_CHIEF.collector_repair"
        priority = "P0"
        title = "Build collector or reassign mission to an agent with collector coverage"
        next_action = "Create or connect a real collector for this surface; login verification alone must not count as work."
    elif state == "no_output":
        task_type = "diagnose_zero_output"
        owner = "SECTION_CHIEF.collector_repair"
        priority = "P0"
        title = "Diagnose collector with zero mission output"
        next_action = "Run a low-impact mobile viewport smoke, inspect page_state_check evidence, then repair selector/IP/login/target scope."
    elif state == "scaffold_only":
        task_type = "activate_feed_harvest"
        owner = "SECTION_CHIEF.collector_repair"
        priority = "P1"
        title = "Replace active scaffold with mission-producing harvest"
        next_action = "Attach feed_harvest or a platform-specific collector before marking the agent active."
    elif state == "login_only":
        task_type = "assign_mission_or_mark_reserve"
        owner = "SECTION_CHIEF.ops"
        priority = "P1"
        title = "Assign mission work or mark the account as reserve"
        next_action = "Either dispatch a concrete collection job or mark this agent as reserve so it is not reported as completed work."
    else:
        return None

    return {
        "task_id": f"FAR-{now().strftime('%Y%m%d')}-{row.get('agent_id')}",
        "created_at": now_iso(),
        "due_at": _due_at(priority),
        "priority": priority,
        "owner": owner,
        "agent_id": row.get("agent_id"),
        "task_type": task_type,
        "mission_state": state,
        "title": title,
        "next_action": next_action,
        "evidence": {
            "health_issue": row.get("health_issue"),
            "collector_script_state": row.get("collector_script_state"),
            "raw_mission_24h": row.get("raw_mission_24h"),
            "raw_total_24h": row.get("raw_total_24h"),
            "support_events_24h": row.get("support_events_24h"),
            "quality_health": health.get("quality_health"),
            "risk_health": health.get("risk_health"),
        },
    }


def build_repair_tasks(rows: list[dict]) -> list[dict]:
    tasks = [task for row in rows if (task := repair_task_for_row(row))]
    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    tasks.sort(key=lambda x: (priority_order.get(x.get("priority"), 9), str(x.get("agent_id"))))
    return tasks


def write_repair_tasks(tasks: list[dict], reason: str, cutoff: str) -> None:
    REPAIR_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": now_iso(),
        "instance": ACTIVE_INSTANCE,
        "reason": reason,
        "cutoff": cutoff,
        "tasks": tasks,
    }
    REPAIR_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# Field Agent Repair Tasks - {now_iso()}",
        "",
        f"reason: {reason}",
        f"cutoff: {cutoff}",
        "",
    ]
    if not tasks:
        lines.append("- No repair task.")
    for task in tasks:
        lines.append(
            f"- [{task['priority']}] {task['agent_id']} {task['task_type']} -> {task['owner']} "
            f"(due={task['due_at']}): {task['next_action']}"
        )
    REPAIR_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_reports(rows: list[dict], reason: str, cutoff: str) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    counts = Counter(row["mission_state"] for row in rows)
    repair_tasks = build_repair_tasks(rows)
    write_repair_tasks(repair_tasks, reason, cutoff)
    health_counts = {
        track: dict(Counter((row.get("health_status") or {}).get(track, {}).get("level", "unknown") for row in rows))
        for track in ("account_health", "mission_health", "quality_health", "risk_health")
    }
    payload = {
        "generated_at": now_iso(),
        "instance": ACTIVE_INSTANCE,
        "reason": reason,
        "cutoff": cutoff,
        "counts": dict(counts),
        "health_counts": health_counts,
        "repair_task_count": len(repair_tasks),
        "repair_tasks_ref": REPAIR_JSON.relative_to(ROOT).as_posix(),
        "rows": rows,
    }
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    problem_rows = [r for r in rows if r["mission_state"] != "collecting"]
    problem_rows.sort(key=lambda r: (r["severity_rank"], r["agent_id"]))
    lines = [
        f"# Section Chief Work Orders - {now_iso()}",
        "",
        f"reason: {reason}",
        f"cutoff: {cutoff}",
        "",
        "## Summary",
        "",
    ]
    for state, count in sorted(counts.items()):
        lines.append(f"- {MISSION_LABELS.get(state, state)} ({state}): {count}")
    lines.extend(["", "## Health Tracks", ""])
    for track, track_counts in health_counts.items():
        summary = ", ".join(f"{k}={v}" for k, v in sorted(track_counts.items()))
        lines.append(f"- {track}: {summary}")
    lines.extend(["", "## Repair Task Pool", ""])
    lines.append(f"- current_tasks: {len(repair_tasks)}")
    lines.append(f"- source: {REPAIR_JSON.relative_to(ROOT).as_posix()}")
    lines.extend(["", "## Work Orders", ""])
    if not problem_rows:
        lines.append("- All agents have mission output in the last 24h.")
    for row in problem_rows:
        lines.append(
            f"- [{MISSION_LABELS.get(row['mission_state'], row['mission_state'])}] {row['agent_id']}: {row['action']} "
            f"(yield={row['msg_yield_24h']}, raw_mission={row['raw_mission_24h']}, "
            f"launch={row['launch_kind']}, health={row['health_issue'] or '-'})"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_work_audit(reason: str = "manual", *, update_kpis: bool = True) -> dict:
    baseline = load_baseline()
    launch_kinds = load_schedule_kinds(baseline)
    cutoff = cutoff_iso(24)
    rows = []
    for path in sorted(KPI_DIR.glob("*.yaml")):
        if path.parent.name == "_retired":
            continue
        data = load_yaml(path)
        if not data:
            continue
        agent_id = data.get("agent_id") or path.stem
        rows.append(classify_agent(agent_id, data, baseline, launch_kinds, cutoff))
    rows.sort(key=lambda r: (r["severity_rank"], r["agent_id"]))
    if update_kpis:
        update_kpi_mission_fields(rows)
    write_reports(rows, reason, cutoff)
    try:
        from processors.history_log import log_event

        counts = Counter(row["mission_state"] for row in rows)
        log_event(
            actor="SECTION_CHIEF",
            kind="metric",
            scope="section_chief_work",
            title="field agent mission-state audit",
            body=json.dumps(dict(counts), ensure_ascii=False),
            refs=[REPORT_JSON.relative_to(ROOT).as_posix(), REPORT_MD.relative_to(ROOT).as_posix()],
        )
    except Exception:
        pass
    return {"rows": rows, "counts": dict(Counter(row["mission_state"] for row in rows))}


def main() -> int:
    result = refresh_work_audit("manual_cli", update_kpis=True)
    print(json.dumps(result["counts"], ensure_ascii=False, sort_keys=True))
    print(str(REPORT_MD))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
