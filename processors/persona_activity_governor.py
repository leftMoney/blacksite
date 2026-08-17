"""Persona Activity Governor.

Guards high-risk/social Field Agent dispatches so this 24h workstation behaves
like a set of plausible the target country-based users instead of a machine firing
browser sessions whenever cron wakes up.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
RUNTIME = INSTANCE_DIR / "runtime"
POLICY = INSTANCE_DIR / "policy"
SCHEDULE_PATH = POLICY / "persona_warmup_schedule.yaml"
WORK_ORDERS_JSON = RUNTIME / "field_agent_work_orders" / "current.json"
FACTORY_EVENTS_JSONL = RUNTIME / "field_agent_factory" / "events.jsonl"
OUT_DIR = RUNTIME / "activity_governor"
OUT_JSON = OUT_DIR / "current.json"
OUT_MD = OUT_DIR / "current.md"

for directory in (OUT_DIR,):
    directory.mkdir(parents=True, exist_ok=True)

HIGH_RISK_PLATFORMS = {"facebook", "instagram", "tiktok", "twitter_x", "discord"}
MEDIUM_RISK_PLATFORMS = {"youtube", "reddit", "pantip", "bigo", "nimo", "lemon8"}

# Example (persona × platform) → activity profile. TODO: replace with YOUR instance's
# agents. Verticals map to your INSTANCE.md domain rings (yolk/white/shell).
ALGORITHM_AGENTS = {
    "P01_FB": {"vertical": "vertical_a", "risk": "high", "operation": "logged_feed"},
    "P01_IG": {"vertical": "vertical_a", "risk": "high", "operation": "logged_feed"},
    "P02_IG": {"vertical": "vertical_b", "risk": "high", "operation": "logged_feed"},
    "P01_TikTok": {"vertical": "vertical_a", "risk": "high", "operation": "algorithm_feed"},
    "P02_TikTok": {"vertical": "vertical_b", "risk": "high", "operation": "algorithm_feed"},
    "P02_X": {"vertical": "vertical_b", "risk": "high", "operation": "algorithm_feed"},
    "P02_YouTube": {"vertical": "vertical_b", "risk": "medium", "operation": "watch_time"},
    "P01_Bigo": {"vertical": "vertical_a", "risk": "medium", "operation": "live_recommendation"},
    "P03_Reddit": {"vertical": "vertical_c", "risk": "medium", "operation": "ranking_feed"},
    "P03_Discord": {"vertical": "vertical_c", "risk": "medium", "operation": "ranking_feed"},
}

# Human-use windows in the instance's locked offset. Broad enough for real work, narrow
# enough to stop cron from waking high-risk accounts all day. TODO: set to your market.
HUMAN_WINDOWS = {
    "high": [("06:30", "12:30"), ("12:00", "13:40"), ("18:00", "23:30")],
    "medium": [("06:30", "23:30")],
    "low": [("00:00", "23:59")],
}

COOLDOWN_MINUTES = {"high": 150, "medium": 90, "low": 30}
MAX_DISPATCHES_PER_DAY = {"high": 3, "medium": 6, "low": 24}
SCHEDULE_TOLERANCE_MINUTES = 25


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)
    except Exception:
        return None


def minutes_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


def parse_hhmm(value: str) -> int:
    hh, mm = value.split(":", 1)
    return int(hh) * 60 + int(mm)


def at_time(day: datetime, minute: int) -> datetime:
    return day.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)


def next_window_start(windows: list[tuple[str, str]], at: datetime) -> str:
    minute_now = minutes_of_day(at)
    starts = []
    for start, _end in windows:
        m = parse_hhmm(start)
        candidate = at_time(at, m)
        if m <= minute_now:
            candidate += timedelta(days=1)
        starts.append(candidate)
    return min(starts).isoformat(timespec="seconds")


def in_windows(windows: list[tuple[str, str]], at: datetime) -> bool:
    minute_now = minutes_of_day(at)
    for start, end in windows:
        s = parse_hhmm(start)
        e = parse_hhmm(end)
        if s <= minute_now <= e:
            return True
    return False


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_schedule() -> dict[str, dict]:
    try:
        data = yaml.safe_load(SCHEDULE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return {row.get("agent_id"): row for row in data.get("daily_windows", []) if row.get("agent_id")}


def load_orders() -> list[dict]:
    data = load_json(WORK_ORDERS_JSON, {"orders": []})
    return data.get("orders", []) if isinstance(data, dict) else []


def command_class(order: dict, command: dict | None) -> str:
    risk = str((command or {}).get("risk") or "").lower()
    script = str((command or {}).get("script") or "")
    agent_id = str(order.get("agent_id") or "")
    if risk.startswith("low_public") or "policy_target_scan.py" in script:
        return "public_scan"
    if risk in {"no_selfbot_status_only", "public_collector_not_ready_status_only"}:
        return "status_only"
    if "feed_harvest.py" in script or agent_id in {"P03_FB", "P03_IG", "P04_IG"}:
        return "logged_feed"
    if agent_id in ALGORITHM_AGENTS:
        return ALGORITHM_AGENTS[agent_id]["operation"]
    return "public_scan"


def platform_risk(order: dict, command: dict | None) -> str:
    cls = command_class(order, command)
    if cls in {"public_scan", "status_only"}:
        return "low"
    agent_id = str(order.get("agent_id") or "")
    if agent_id in ALGORITHM_AGENTS:
        return ALGORITHM_AGENTS[agent_id]["risk"]
    platform = str(order.get("platform") or "").lower()
    if platform in HIGH_RISK_PLATFORMS:
        return "high"
    if platform in MEDIUM_RISK_PLATFORMS:
        return "medium"
    return "low"


def schedule_window(schedule_row: dict, at: datetime, tolerance_min: int) -> dict:
    hh = str(schedule_row.get("hh") or "")
    if "-" not in hh:
        return {"has_schedule": False, "inside": True, "next_allowed_at": None}
    start_s, end_s = [x.strip() for x in hh.split("-", 1)]
    start = parse_hhmm(start_s) - tolerance_min
    end = parse_hhmm(end_s) + tolerance_min
    minute_now = minutes_of_day(at)
    inside = start <= minute_now <= end
    next_start = at_time(at, max(0, start))
    if minute_now > end:
        next_start += timedelta(days=1)
    return {
        "has_schedule": True,
        "inside": inside,
        "window": hh,
        "next_allowed_at": next_start.isoformat(timespec="seconds"),
    }


def dispatch_counts_today(agent_id: str, at: datetime) -> int:
    day = at.strftime("%Y-%m-%d")
    count = 0
    if not FACTORY_EVENTS_JSONL.exists():
        return 0
    try:
        lines = FACTORY_EVENTS_JSONL.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return 0
    for line in lines[-3000:]:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if item.get("event") != "work_order_dispatched":
            continue
        if item.get("agent_id") != agent_id:
            continue
        if str(item.get("ts") or "").startswith(day):
            count += 1
    return count


def evaluate_order_gate(
    order: dict,
    command: dict | None = None,
    *,
    now_dt: datetime | None = None,
    schedule: dict[str, dict] | None = None,
) -> dict:
    at = now_dt or now()
    schedule = schedule if schedule is not None else load_schedule()
    agent_id = str(order.get("agent_id") or "")
    cls = command_class(order, command)
    risk = platform_risk(order, command)
    policy = ALGORITHM_AGENTS.get(agent_id, {})

    base = {
        "checked_at": at.isoformat(timespec="seconds"),
        "agent_id": agent_id,
        "platform": order.get("platform"),
        "activity_class": cls,
        "risk_tier": risk,
        "vertical": policy.get("vertical"),
        "operation": policy.get("operation") or cls,
        "allow": True,
        "decision": "allow",
        "reason": "public or low-risk dispatch",
        "next_allowed_at": None,
    }

    if risk == "low":
        return base

    human_windows = HUMAN_WINDOWS.get(risk, HUMAN_WINDOWS["medium"])
    if not in_windows(human_windows, at):
        return base | {
            "allow": False,
            "decision": "defer_outside_local_human_window",
            "reason": "High/medium-risk persona action must stay inside the target country human-use windows.",
            "next_allowed_at": next_window_start(human_windows, at),
        }

    sched_row = schedule.get(agent_id) or {}
    # Logged feeds and algorithm-feed shaping should run near that persona's
    # planned daily slot. Public metadata and low-risk portals do not need this.
    if cls in {"logged_feed", "algorithm_feed", "watch_time"} and sched_row:
        sw = schedule_window(sched_row, at, SCHEDULE_TOLERANCE_MINUTES)
        if sw["has_schedule"] and not sw["inside"]:
            return base | {
                "allow": False,
                "decision": "defer_outside_persona_window",
                "reason": f"Persona action outside scheduled window {sw.get('window')}.",
                "next_allowed_at": sw["next_allowed_at"],
                "schedule_window": sw.get("window"),
            }

    dispatch = order.get("dispatch") or {}
    last = parse_ts(dispatch.get("last_dispatched_at"))
    cooldown = timedelta(minutes=COOLDOWN_MINUTES.get(risk, 90))
    if last and at - last < cooldown:
        return base | {
            "allow": False,
            "decision": "defer_cooldown",
            "reason": f"{risk} risk cooldown not elapsed.",
            "next_allowed_at": (last + cooldown).isoformat(timespec="seconds"),
        }

    count = dispatch_counts_today(agent_id, at)
    max_day = MAX_DISPATCHES_PER_DAY.get(risk, 6)
    if count >= max_day:
        tomorrow = (at + timedelta(days=1)).replace(hour=6, minute=30, second=0, microsecond=0)
        return base | {
            "allow": False,
            "decision": "defer_daily_budget",
            "reason": f"Daily dispatch budget reached: {count}/{max_day}.",
            "next_allowed_at": tomorrow.isoformat(timespec="seconds"),
            "dispatches_today": count,
            "daily_budget": max_day,
        }

    return base | {
        "reason": "Within the target country human-use window, persona window, cooldown, and daily budget.",
        "dispatches_today": count,
        "daily_budget": max_day,
    }


def evaluate_orders() -> dict:
    schedule = load_schedule()
    orders = load_orders()
    gates = []
    counts: Counter = Counter()
    for order in orders:
        dispatch = order.get("dispatch") or {}
        command = dispatch.get("command") if isinstance(dispatch, dict) else None
        gate = evaluate_order_gate(order, command, schedule=schedule)
        gates.append(gate)
        counts[gate["decision"]] += 1
    payload = {
        "generated_at": now_iso(),
        "instance": ACTIVE_INSTANCE,
        "summary": dict(counts),
        "human_windows": HUMAN_WINDOWS,
        "cooldown_minutes": COOLDOWN_MINUTES,
        "max_dispatches_per_day": MAX_DISPATCHES_PER_DAY,
        "algorithm_agents": ALGORITHM_AGENTS,
        "gates": gates,
    }
    write_json(OUT_JSON, payload)
    write_report(payload)
    try:
        from processors.history_log import log_event

        log_event(
            actor="PERSONA_ACTIVITY_GOVERNOR",
            kind="metric",
            scope="activity_governor",
            title="persona activity gate refresh",
            body=json.dumps(dict(counts), ensure_ascii=False),
            refs=[OUT_JSON.relative_to(ROOT).as_posix(), OUT_MD.relative_to(ROOT).as_posix()],
        )
    except Exception:
        pass
    return payload


def write_report(payload: dict) -> None:
    lines = [
        f"# Persona Activity Governor - {payload['generated_at']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in sorted((payload.get("summary") or {}).items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Deferred Gates", ""])
    deferred = [g for g in payload.get("gates", []) if not g.get("allow")]
    if not deferred:
        lines.append("- none")
    for gate in deferred[:60]:
        lines.append(
            f"- {gate.get('agent_id')} {gate.get('decision')} "
            f"next={gate.get('next_allowed_at')} reason={gate.get('reason')}"
        )
    lines.extend(["", "## Algorithm Agents", ""])
    for agent_id, policy in sorted((payload.get("algorithm_agents") or {}).items()):
        lines.append(
            f"- {agent_id}: vertical={policy.get('vertical')} "
            f"risk={policy.get('risk')} operation={policy.get('operation')}"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    payload = evaluate_orders()
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps({"summary": payload["summary"], "path": str(OUT_JSON)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
