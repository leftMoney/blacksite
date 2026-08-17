"""Field Agent factory workflow.

Turns Section Chief repair tasks into work orders, tracks 4h check-ins, and
writes reusable lessons. Default tick is token-free. Platform work dispatch is
limited to explicit, low-risk commands listed in `mission_command_for_order`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from processors.section_chief_work_audit import raw_dir_candidates  # noqa: E402
from processors.persona_activity_governor import evaluate_order_gate  # noqa: E402

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
RUNTIME = INSTANCE_DIR / "runtime"
POLICY = INSTANCE_DIR / "policy"
LOG_DIR = RUNTIME / "logs"
FACTORY_DIR = RUNTIME / "field_agent_factory"
WORK_ORDER_DIR = RUNTIME / "field_agent_work_orders"
CHECKIN_DIR = RUNTIME / "field_agent_checkins"
REVIEW_DIR = RUNTIME / "field_agent_reviews"
LESSON_DIR = RUNTIME / "field_agent_lessons"
REPAIR_TASKS_JSON = RUNTIME / "field_agent_repair_tasks" / "current.json"
SCHEDULE_PATH = POLICY / "persona_warmup_schedule.yaml"
WORK_ORDERS_JSON = WORK_ORDER_DIR / "current.json"
WORK_ORDERS_MD = WORK_ORDER_DIR / "current.md"
CHECKINS_JSON = CHECKIN_DIR / "current.json"
REVIEWS_JSON = REVIEW_DIR / "current.json"
LESSONS_JSONL = LESSON_DIR / "lessons.jsonl"
EVENTS_JSONL = FACTORY_DIR / "events.jsonl"

for directory in (LOG_DIR, FACTORY_DIR, WORK_ORDER_DIR, CHECKIN_DIR, REVIEW_DIR, LESSON_DIR):
    directory.mkdir(parents=True, exist_ok=True)

CHECKIN_CADENCE_HOURS = int(os.environ.get("FIELD_AGENT_CHECKIN_HOURS", "4"))
MAX_DISPATCH_PER_TICK = int(os.environ.get("FIELD_AGENT_MAX_DISPATCH_PER_TICK", "6"))

ACTIVE_STATES = {
    "assigned",
    "accepted",
    "dispatching",
    "collecting",
    "report_due",
    "needs_repair",
    "strategist_review",
}


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=TZ)
        return dt
    except Exception:
        return None


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [field_agent_factory] {msg}"
    print(line, flush=True)
    with (LOG_DIR / f"field_agent_factory_{now().strftime('%Y-%m-%d')}.log").open(
        "a", encoding="utf-8"
    ) as f:
        f.write(line + "\n")


def hist(kind: str, title: str, body: str = "", refs: list[str] | None = None) -> None:
    try:
        from processors.history_log import log_event

        log_event(
            actor="field_agent_factory",
            kind=kind,
            scope="field_agent_factory",
            title=title,
            body=body,
            refs=refs,
        )
    except Exception:
        pass


def load_schedule_map() -> dict[str, dict]:
    try:
        data = yaml.safe_load(SCHEDULE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {item.get("agent_id"): item for item in data.get("daily_windows", []) if item.get("agent_id")}


def load_repair_tasks() -> list[dict]:
    data = load_json(REPAIR_TASKS_JSON, {"tasks": []})
    return data.get("tasks", []) if isinstance(data, dict) else []


def load_work_orders() -> list[dict]:
    data = load_json(WORK_ORDERS_JSON, {"orders": []})
    return data.get("orders", []) if isinstance(data, dict) else []


def stable_order_id(agent_id: str, order_kind: str, task_type: str) -> str:
    return f"WO-{now().strftime('%Y%m%d')}-{agent_id}-{order_kind}-{task_type}".replace(" ", "_")


def order_key(order: dict) -> tuple[str, str, str]:
    return (
        str(order.get("agent_id") or ""),
        str(order.get("order_kind") or ""),
        str(order.get("task_type") or ""),
    )


def is_active(order: dict) -> bool:
    return str(order.get("state") or "") in ACTIVE_STATES


def adapter_command(
    *,
    order: dict,
    script: str,
    raw_sources: list[str],
    args: list[str] | None = None,
    timeout_s: int = 900,
    risk: str = "low_read_only",
) -> dict:
    adapter_args = [
        "--agent-id",
        str(order["agent_id"]),
        "--work-order-id",
        str(order["order_id"]),
        "--task-focus",
        str(order.get("primary_focus") or ""),
        "--script",
        script,
    ]
    for raw_source in raw_sources:
        adapter_args.extend(["--raw-source", raw_source])
    adapter_args.append("--")
    adapter_args.extend(args or [])
    return {
        "script": "agents/_common/factory_dispatch_adapter.py",
        "args": adapter_args,
        "timeout_s": timeout_s,
        "risk": risk,
    }


def policy_scan_command(
    *,
    order: dict,
    policy: str,
    platform: str,
    raw_subdir: str,
    limit_per_target: int = 12,
    ignore_disabled: bool = False,
) -> dict:
    args = [
        "--policy",
        policy,
        "--agent-id",
        str(order["agent_id"]),
        "--platform",
        platform,
        "--raw-subdir",
        raw_subdir,
        "--work-order-id",
        str(order["order_id"]),
        "--task-focus",
        str(order.get("primary_focus") or ""),
        "--limit-per-target",
        str(limit_per_target),
    ]
    if ignore_disabled:
        args.append("--ignore-disabled")
    return {
        "script": "agents/_common/policy_target_scan.py",
        "args": args,
        "timeout_s": 600,
        "risk": "low_public_html_metadata",
    }


def persona_from_agent(agent_id: str) -> str:
    return agent_id.split("_", 1)[0]


def mission_command_for_order(order: dict) -> dict | None:
    """Return a safe dispatch command for orders that can run automatically."""
    agent_id = str(order.get("agent_id") or "")
    platform = str(order.get("platform") or "").lower()
    if order.get("task_type") == "quality_sample":
        return None
    if agent_id == "P03_FB" and platform == "facebook":
        return {
            "script": "agents/facebook/feed_harvest.py",
            "args": [
                "--persona",
                "P03",
                "--duration-min",
                "4",
                "--max-scrolls",
                "10",
            ],
            "timeout_s": 600,
            "risk": "low_read_only_feed_snapshot",
        }
    if agent_id in {"P03_IG", "P04_IG"}:
        persona = persona_from_agent(agent_id)
        return adapter_command(
            order=order,
            script="agents/instagram/feed_harvest.py",
            raw_sources=[f"instagram/{persona}"],
            args=["--persona", persona, "--duration-min", "4", "--max-scrolls", "8"],
            timeout_s=700,
            risk="low_logged_in_read_only_feed_snapshot",
        )
    if agent_id in {"P03_TikTok", "P04_TikTok_sports"}:
        # === INSTANCE SEARCH QUERIES (customize per instance — replace with the
        # target country's native-language search terms) ===
        if agent_id == "P03_TikTok":
            queries = [
                "national lottery hot numbers",
                "folk-belief lucky numbers",
                "lucky-number seller",
                "famous temple lucky numbers",
                "government lottery",
            ]
        else:
            queries = [
                "muay highlights",
                "local football highlights",
                "national league",
                "local football analysis",
                "ONE Championship",
            ]
        return adapter_command(
            order=order,
            script="agents/tiktok/tiktok_listen.py",
            raw_sources=["tiktok"],
            args=["--search", *queries],
            timeout_s=1200,
            risk="low_public_tiktok_search_metadata",
        )
    if agent_id in {"P03_Pantip", "P05_Pantip"}:
        # === INSTANCE TAG QUERIES (customize per instance — replace with the
        # target country's native-language forum tags) ===
        tags = (
            ["lottery", "hot numbers", "fortune telling", "folk-belief", "amulet"]
            if agent_id == "P03_Pantip"
            else ["AI", "ChatGPT", "AI tools", "productivity", "technology"]
        )
        return adapter_command(
            order=order,
            script="agents/pantip/pantip_listen.py",
            raw_sources=["pantip"],
            args=["--tags", *tags],
            timeout_s=900,
            risk="low_public_pantip_tag_scan",
        )
    if agent_id in {"P03_Bigo", "P04_Bigo", "bigo_lobby_anon"}:
        return adapter_command(
            order=order,
            script="agents/bigo/bigo_lobby_scan.py",
            raw_sources=["bigo"],
            args=[],
            timeout_s=900,
            risk="low_public_bigo_lobby_snapshot",
        )
    if agent_id in {"P04_Nimo", "nimo_lobby_anon"}:
        return adapter_command(
            order=order,
            script="agents/nimo/nimo_lobby_scan.py",
            raw_sources=["nimo"],
            args=[],
            timeout_s=900,
            risk="low_public_nimo_lobby_snapshot",
        )
    if agent_id == "P04_YouTube_sports" and platform == "youtube":
        return {
            "script": "agents/youtube/yt_search.py",
            "args": [
                "--limit",
                "8",
                "--agent-id",
                "P04_YouTube_sports",
                "--work-order-id",
                str(order["order_id"]),
                "--task-focus",
                str(order.get("primary_focus") or "local football highlights + muay_local watch"),
                # === INSTANCE SEARCH QUERIES (customize per instance — native-language terms) ===
                "local football highlights",
                "local football",
                "muay",
                "Example FC United",
                "national football team",
            ],
            "timeout_s": 900,
            "risk": "low_public_youtube_metadata",
        }
    if agent_id == "P04_X" and platform == "twitter_x":
        return {
            "script": "agents/twitter/x_listen.py",
            "args": [
                "--categories",
                "sports_kol",
                "brand_grey",
                "--agent-id",
                "P04_X",
                "--work-order-id",
                str(order["order_id"]),
                "--task-focus",
                str(order.get("primary_focus") or "Sports betting meta + match analysis tweets"),
            ],
            "timeout_s": 900,
            "risk": "low_read_only_metadata",
        }
    if agent_id == "P05_Reddit":
        return adapter_command(
            order=order,
            script="agents/reddit/reddit_listen.py",
            raw_sources=["reddit"],
            # === INSTANCE SUBREDDITS (customize per instance — target country + capital subs) ===
            args=["--subs", "TargetCountry", "CapitalCity", "MachineLearning", "artificial", "ChatGPT"],
            timeout_s=900,
            risk="low_public_reddit_read_only",
        )
    if agent_id == "P05_Discord":
        return adapter_command(
            order=order,
            script="agents/discord/discord_listen.py",
            raw_sources=["discord"],
            args=[],
            timeout_s=120,
            risk="no_selfbot_status_only",
        )
    if agent_id == "P05_Lemon8":
        return adapter_command(
            order=order,
            script="agents/lemon8/lemon8_listen.py",
            raw_sources=["lemon8"],
            args=[],
            timeout_s=120,
            risk="public_collector_not_ready_status_only",
        )
    if agent_id == "trueid_anon":
        return adapter_command(
            order=order,
            script="agents/trueid/trueid_listen.py",
            raw_sources=["trueid"],
            args=[],
            timeout_s=900,
            risk="low_public_trueid_feed",
        )
    if agent_id == "sanook_anon":
        return policy_scan_command(
            order=order,
            policy="sanook_targets.yaml",
            platform="sanook",
            raw_subdir="sanook",
            limit_per_target=12,
            ignore_disabled=True,
        )
    if agent_id == "lottery_eco_anon":
        return policy_scan_command(
            order=order,
            policy="lottery_eco_targets.yaml",
            platform="lottery_eco",
            raw_subdir="lottery_eco",
            limit_per_target=12,
            ignore_disabled=True,
        )
    if agent_id == "payment_intel_anon":
        return policy_scan_command(
            order=order,
            policy="payment_intel_targets.yaml",
            platform="payment_intel",
            raw_subdir="payment_intel",
            limit_per_target=12,
            ignore_disabled=True,
        )
    if agent_id == "regulator_pulse_anon":
        return policy_scan_command(
            order=order,
            policy="regulator_pulse_targets.yaml",
            platform="regulator_pulse",
            raw_subdir="regulator_pulse",
            limit_per_target=12,
            ignore_disabled=True,
        )
    if agent_id == "fb_page_anon":
        return adapter_command(
            order=order,
            script="agents/facebook/fb_page_scan.py",
            raw_sources=["facebook"],
            args=[],
            timeout_s=900,
            risk="low_public_facebook_page_scan",
        )
    if agent_id == "fb_og_meta_anon":
        return adapter_command(
            order=order,
            script="agents/facebook/fb_og_meta_scan.py",
            raw_sources=["facebook_og_meta"],
            args=[],
            timeout_s=900,
            risk="low_public_facebook_og_metadata",
        )
    return None


def build_order_from_task(task: dict, schedule: dict[str, dict]) -> dict | None:
    task_type = task.get("task_type")
    agent_id = str(task.get("agent_id") or "")
    sched = schedule.get(agent_id, {})
    platform = sched.get("platform") or "unknown"
    persona = sched.get("persona") or (agent_id.split("_", 1)[0] if "_" in agent_id else None)
    focus = sched.get("primary_focus") or task.get("title") or ""

    if task_type == "assign_mission_or_mark_reserve":
        order_kind = "collection_mission"
        success = {
            "first_4h": "at least 1 mission record OR page_state evidence explaining why none",
            "second_4h": "mission records must exist, otherwise Section Chief repair required",
            "quality": "sample relevance must be judged by Section Chief once raw exists",
        }
        state = "accepted"
    elif task_type in {"diagnose_zero_output", "build_or_assign_collector", "activate_feed_harvest"}:
        order_kind = "repair_mission"
        success = {
            "repair": "latest page_state_check or smoke result explains the failure",
            "validation": "next run emits mission output or an explicit blocker",
        }
        state = "accepted"
    elif task_type == "quality_sample":
        order_kind = "quality_review"
        success = {
            "quality": "sampled raw output receives relevance judgment and a reusable lesson",
        }
        state = "accepted"
    elif task_type == "account_recovery":
        order_kind = "account_recovery"
        success = {
            "account": "logged_in page_state_check and no unresolved human gate",
        }
        state = "accepted"
    else:
        return None

    order = {
        "order_id": stable_order_id(agent_id, order_kind, str(task_type)),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "agent_id": agent_id,
        "persona": persona,
        "platform": platform,
        "order_kind": order_kind,
        "task_type": task_type,
        "source_task_id": task.get("task_id"),
        "priority": task.get("priority"),
        "state": state,
        "primary_focus": focus,
        "assigned_by": "SECTION_CHIEF",
        "accepted_at": now_iso(),
        "checkin_cadence_hours": CHECKIN_CADENCE_HOURS,
        "next_checkin_due_at": (now() + timedelta(hours=CHECKIN_CADENCE_HOURS)).isoformat(timespec="seconds"),
        "failure_count": 0,
        "success_criteria": success,
        "evidence": task.get("evidence") or {},
        "last_checkin_at": None,
        "last_review": None,
        "dispatch": None,
    }
    command = mission_command_for_order(order)
    if command:
        order["dispatch"] = {
            "auto_dispatch": True,
            "last_dispatched_at": None,
            "command": command,
        }
    return order


def upsert_orders(tasks: list[dict], existing: list[dict], schedule: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    active_by_key = {order_key(o): o for o in existing if is_active(o)}
    created: list[dict] = []
    for task in tasks:
        order = build_order_from_task(task, schedule)
        if not order:
            continue
        key = order_key(order)
        if key in active_by_key:
            current = active_by_key[key]
            current["updated_at"] = now_iso()
            current["source_task_id"] = task.get("task_id")
            current["priority"] = task.get("priority")
            current["evidence"] = task.get("evidence") or {}
            if order.get("dispatch"):
                if not current.get("dispatch"):
                    current["dispatch"] = order.get("dispatch")
            elif current.get("task_type") == "quality_sample":
                current["dispatch"] = None
            accepted = parse_ts(current.get("accepted_at") or current.get("created_at"))
            last_review = current.get("last_review") or {}
            if (
                current.get("state") == "needs_repair"
                and int(current.get("failure_count") or 0) <= 1
                and last_review.get("decision") == "repair_or_dispatch"
                and accepted
                and now() < accepted + timedelta(hours=CHECKIN_CADENCE_HOURS)
            ):
                current["state"] = "accepted"
                current["failure_count"] = 0
                current["last_review"] = {
                    "reviewed_at": now_iso(),
                    "reviewed_by": "SECTION_CHIEF",
                    "decision": "await_first_cadence",
                    "reason": "Premature forced check-in reset; first real 4h window is not due yet.",
                    "failure_count": 0,
                }
            continue
        existing.append(order)
        active_by_key[key] = order
        created.append(order)
        append_jsonl(EVENTS_JSONL, {"ts": now_iso(), "event": "work_order_created", "order": order})
        remember_lesson(
            agent_id=order["agent_id"],
            category="work_order",
            lesson={
                "trigger": f"{order['agent_id']} entered {task.get('mission_state')} after repair-task refresh",
                "diagnosis": "Agent is online/account-healthy enough to receive factory work, but mission success still requires raw output.",
                "fix": f"Created {order['order_kind']} work order {order['order_id']}.",
                "validation": f"Require 4h check-in cadence; success criteria: {order['success_criteria']}",
                "reusable_rule": "Online or recovered accounts must become work orders, not completed work.",
            },
        )
    return existing, created


def attach_missing_dispatch(orders: list[dict]) -> int:
    attached = 0
    for order in orders:
        if not is_active(order) or order.get("dispatch"):
            continue
        command = mission_command_for_order(order)
        if not command:
            continue
        order["dispatch"] = {
            "auto_dispatch": True,
            "last_dispatched_at": None,
            "command": command,
        }
        order["updated_at"] = now_iso()
        attached += 1
    return attached


def raw_counts_since(agent_id: str, since_iso: str | None) -> tuple[int, int, Counter, list[dict]]:
    total = 0
    mission = 0
    events: Counter = Counter()
    page_states: list[dict] = []
    since = since_iso or (now() - timedelta(hours=CHECKIN_CADENCE_HOURS)).isoformat(timespec="seconds")
    for directory in raw_dir_candidates(agent_id):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl")):
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
                ts = str(item.get("ts") or item.get("checked_at") or "")
                if ts and ts < since:
                    continue
                event = str(item.get("event") or item.get("kind") or "message")
                events[event] += 1
                total += 1
                if event == "page_state_check":
                    page_states.append(item)
                    continue
                if event not in {
                    "verify_session",
                    "session_recovery",
                    "session_recovery_visual_check",
                    "manual_relogin_handoff",
                    "manual_relogin_completed",
                    "alias_login",
                    "register",
                    "active_mode_scaffold_only",
                    "feed_harvest_summary",
                    "collector_status",
                }:
                    mission += 1
    page_states.sort(key=lambda x: str(x.get("checked_at") or x.get("ts") or ""), reverse=True)
    return total, mission, events, page_states[:3]


def dispatch_due_orders(orders: list[dict], *, allow_dispatch: bool) -> list[dict]:
    dispatched: list[dict] = []
    if not allow_dispatch:
        return dispatched
    for order in orders:
        if len(dispatched) >= MAX_DISPATCH_PER_TICK:
            break
        if not is_active(order):
            continue
        dispatch = order.get("dispatch") or {}
        if not dispatch.get("auto_dispatch"):
            continue
        last = parse_ts(dispatch.get("last_dispatched_at"))
        if last and (now() - last) < timedelta(hours=CHECKIN_CADENCE_HOURS):
            continue
        command = dispatch.get("command") or {}
        script = command.get("script")
        if not script:
            continue
        next_allowed = parse_ts(dispatch.get("next_allowed_at"))
        if next_allowed and now() < next_allowed:
            continue
        gate = evaluate_order_gate(order, command, now_dt=now())
        dispatch["last_activity_gate"] = gate
        if not gate.get("allow", True):
            dispatch["next_allowed_at"] = gate.get("next_allowed_at")
            order["last_review"] = {
                "decision": "activity_governor_defer",
                "reason": gate.get("reason"),
                "next_allowed_at": gate.get("next_allowed_at"),
                "activity_gate": gate.get("decision"),
            }
            order["updated_at"] = now_iso()
            append_jsonl(EVENTS_JSONL, {
                "ts": now_iso(),
                "event": "work_order_activity_deferred",
                "order_id": order["order_id"],
                "agent_id": order["agent_id"],
                "gate": gate,
            })
            continue
        dispatch.pop("next_allowed_at", None)
        script_path = ROOT / script
        if not script_path.exists():
            order["last_review"] = {"decision": "dispatch_failed", "reason": f"missing script {script}"}
            continue
        cmd = [sys.executable, str(script_path), *[str(a) for a in command.get("args", [])]]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log_path = LOG_DIR / f"field_agent_order_{order['order_id']}_{now().strftime('%Y%m%dT%H%M%S')}.log"
        with log_path.open("a", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=logf,
                creationflags=flags,
            )
        dispatch["last_dispatched_at"] = now_iso()
        dispatch["last_pid"] = proc.pid
        dispatch["last_log"] = rel(log_path)
        order["state"] = "dispatching"
        order["updated_at"] = now_iso()
        dispatched.append(order)
        append_jsonl(EVENTS_JSONL, {
            "ts": now_iso(),
            "event": "work_order_dispatched",
            "order_id": order["order_id"],
            "agent_id": order["agent_id"],
            "pid": proc.pid,
            "log": rel(log_path),
        })
    return dispatched


def make_checkins(orders: list[dict], *, force: bool = False) -> list[dict]:
    checkins: list[dict] = []
    for order in orders:
        if not is_active(order):
            continue
        due = parse_ts(order.get("next_checkin_due_at"))
        due_now = not due or now() >= due
        if not force and not due_now:
            continue
        since = order.get("last_checkin_at") or order.get("accepted_at") or order.get("created_at")
        raw_total, raw_mission, events, page_states = raw_counts_since(order["agent_id"], since)
        report_status = "mission_output" if raw_mission > 0 else "no_mission_output"
        checkin = {
            "checkin_id": f"CI-{now().strftime('%Y%m%dT%H%M%S')}-{order['agent_id']}",
            "ts": now_iso(),
            "order_id": order["order_id"],
            "agent_id": order["agent_id"],
            "platform": order.get("platform"),
            "since": since,
            "raw_total": raw_total,
            "raw_mission": raw_mission,
            "events": dict(events),
            "latest_page_states": page_states,
            "report_status": report_status,
            "pre_due": not due_now,
        }
        review = review_checkin(order, checkin)
        checkin["section_chief_review"] = review
        if not (checkin.get("pre_due") and checkin["raw_mission"] == 0):
            order["last_checkin_at"] = checkin["ts"]
            order["next_checkin_due_at"] = (now() + timedelta(hours=CHECKIN_CADENCE_HOURS)).isoformat(timespec="seconds")
        order["last_review"] = review
        order["updated_at"] = now_iso()
        checkins.append(checkin)
        append_jsonl(CHECKIN_DIR / f"{now().strftime('%Y-%m-%d')}.jsonl", checkin)
        append_jsonl(EVENTS_JSONL, {"ts": now_iso(), "event": "agent_checkin", "checkin": checkin})
    return checkins


def review_checkin(order: dict, checkin: dict) -> dict:
    if checkin["raw_mission"] > 0:
        order["state"] = "collecting"
        order["failure_count"] = 0
        decision = "continue_collecting"
        reason = "Mission output exists; next step is quality sampling."
    elif checkin.get("pre_due"):
        order["state"] = "accepted"
        decision = "await_first_cadence"
        reason = "First 4h check-in is not due yet; status recorded without counting failure."
    else:
        order["failure_count"] = int(order.get("failure_count") or 0) + 1
        if order["failure_count"] >= 2:
            order["state"] = "strategist_review"
            decision = "escalate_strategist"
            reason = "Two consecutive check-ins without mission output; task direction or platform viability may need strategy adjustment."
        else:
            order["state"] = "needs_repair"
            decision = "repair_or_dispatch"
            reason = "No mission output yet; inspect page_state evidence and repair/dispatch before next check-in."
    review = {
        "reviewed_at": now_iso(),
        "reviewed_by": "SECTION_CHIEF",
        "decision": decision,
        "reason": reason,
        "failure_count": order.get("failure_count", 0),
    }
    if decision in {"repair_or_dispatch", "escalate_strategist"}:
        remember_lesson(
            agent_id=order["agent_id"],
            category="factory_review",
            lesson={
                "trigger": f"{order['agent_id']} check-in returned {checkin['raw_mission']} mission records.",
                "diagnosis": reason,
                "fix": "Keep evidence attached to work order; do not mark login-only work as completed.",
                "validation": "Next 4h check-in must show mission output or explicit blocker.",
                "reusable_rule": "Factory check-ins grade mission output separately from account health.",
            },
        )
    return review


def remember_lesson(agent_id: str, category: str, lesson: dict) -> None:
    record = {"ts": now_iso(), "agent_id": agent_id, "category": category, **lesson}
    append_jsonl(LESSONS_JSONL, record)
    line = (
        f"trigger={lesson.get('trigger')} | diagnosis={lesson.get('diagnosis')} | "
        f"fix={lesson.get('fix')} | validation={lesson.get('validation')} | "
        f"reusable_rule={lesson.get('reusable_rule')}"
    )
    try:
        from agents._common.agent_memory import append_learning

        append_learning(agent_id, line, category=category)
        append_learning("SECTION_CHIEF", line, category=category)
    except Exception:
        pass


def write_work_order_outputs(orders: list[dict]) -> None:
    active = [o for o in orders if is_active(o)]
    state_counts = dict(Counter(o.get("state") for o in active))
    payload = {
        "generated_at": now_iso(),
        "instance": ACTIVE_INSTANCE,
        "checkin_cadence_hours": CHECKIN_CADENCE_HOURS,
        "state_counts": state_counts,
        "orders": active,
    }
    write_json(WORK_ORDERS_JSON, payload)
    lines = [
        f"# Field Agent Work Orders - {now_iso()}",
        "",
        f"checkin_cadence_hours: {CHECKIN_CADENCE_HOURS}",
        "",
    ]
    if not active:
        lines.append("- No active work order.")
    for order in active:
        lines.append(
            f"- [{order.get('state')}] {order.get('agent_id')} {order.get('order_kind')} "
            f"{order.get('task_type')} due={order.get('next_checkin_due_at')} "
            f"focus={order.get('primary_focus')}"
        )
    WORK_ORDERS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def sanitize_checkins(orders: list[dict], checkins: list[dict]) -> list[dict]:
    """Preserve audit history while neutralizing premature forced check-in noise."""
    by_id = {str(o.get("order_id")): o for o in orders if o.get("order_id")}
    cleaned: list[dict] = []
    for checkin in checkins:
        order = by_id.get(str(checkin.get("order_id")))
        review = checkin.get("section_chief_review") or {}
        if (
            order
            and review.get("decision") == "repair_or_dispatch"
            and order.get("state") == "accepted"
            and int(order.get("failure_count") or 0) == 0
            and order.get("last_review", {}).get("decision") == "await_first_cadence"
        ):
            checkin = dict(checkin)
            review = dict(review)
            review.update(
                {
                    "decision": "await_first_cadence",
                    "reason": "Premature forced check-in was reset; first real 4h window is not due yet.",
                    "failure_count": 0,
                }
            )
            checkin["section_chief_review"] = review
            checkin["pre_due"] = True
            checkin["superseded_by"] = "premature_force_checkin_reset"
        cleaned.append(checkin)
    return cleaned


def write_checkin_outputs(checkins: list[dict], orders: list[dict]) -> None:
    previous = load_json(CHECKINS_JSON, {"checkins": []})
    old = previous.get("checkins", []) if isinstance(previous, dict) else []
    merged = sanitize_checkins(orders, (checkins + old)[:200])
    write_json(CHECKINS_JSON, {"generated_at": now_iso(), "instance": ACTIVE_INSTANCE, "checkins": merged})
    reviews = [c.get("section_chief_review") | {"agent_id": c.get("agent_id"), "order_id": c.get("order_id")} for c in merged if c.get("section_chief_review")]
    write_json(REVIEWS_JSON, {"generated_at": now_iso(), "instance": ACTIVE_INSTANCE, "reviews": reviews[:200]})


def refresh(*, allow_dispatch: bool = False, force_checkin: bool = False) -> dict:
    tasks = load_repair_tasks()
    schedule = load_schedule_map()
    orders = load_work_orders()
    orders, created = upsert_orders(tasks, orders, schedule)
    attached = attach_missing_dispatch(orders)
    dispatched = dispatch_due_orders(orders, allow_dispatch=allow_dispatch)
    checkins = make_checkins(orders, force=force_checkin)
    write_work_order_outputs(orders)
    write_checkin_outputs(checkins, orders)
    hist(
        "metric",
        "field agent factory tick",
        body=json.dumps(
            {
                "orders": len([o for o in orders if is_active(o)]),
                "created": len(created),
                "dispatch_attached": attached,
                "dispatched": len(dispatched),
                "checkins": len(checkins),
                "allow_dispatch": allow_dispatch,
            },
            ensure_ascii=False,
        ),
        refs=[rel(WORK_ORDERS_JSON), rel(CHECKINS_JSON), rel(LESSONS_JSONL)],
    )
    log(f"tick orders={len([o for o in orders if is_active(o)])} created={len(created)} dispatch_attached={attached} dispatched={len(dispatched)} checkins={len(checkins)}")
    return {
        "orders": orders,
        "created": created,
        "dispatch_attached": attached,
        "dispatched": dispatched,
        "checkins": checkins,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dispatch", action="store_true", help="allow explicit safe mission commands to run")
    parser.add_argument("--no-dispatch", action="store_true", help="force no platform dispatch")
    parser.add_argument("--force-checkin", action="store_true", help="emit check-ins even before next due time")
    args = parser.parse_args()
    allow_dispatch = args.dispatch and not args.no_dispatch
    result = refresh(allow_dispatch=allow_dispatch, force_checkin=args.force_checkin)
    print(json.dumps(
        {
            "active_orders": len([o for o in result["orders"] if is_active(o)]),
            "created": len(result["created"]),
            "dispatch_attached": result["dispatch_attached"],
            "dispatched": len(result["dispatched"]),
            "checkins": len(result["checkins"]),
        },
        ensure_ascii=False,
        sort_keys=True,
    ))
    print(str(WORK_ORDERS_JSON))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
