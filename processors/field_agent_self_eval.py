"""processors/field_agent_self_eval.py — Tier 1 daily self-eval.

Each Field Agent (persona-driven + anonymous_web) writes 1 line into its memory
`# 我的經驗` per day. Closes the bottom-up feedback gap boss raised 5/7:
no routine bottom-up signal → Section Chief eval is data-blind to what the
agent itself observed.

Cron: 18:00 daily GMT+7 via blacksite_daemon (before 19:00 Section Chief eval).

Template-driven (no LLM call per agent — 25 agents/day too costly).
Pulls past-24h KPI numbers + smoke status + open incidents per agent and
generates a structured 1-liner like:

    - 2026-05-07T18:00+07:00 | yield_24h=0/100 (verify_only); smoke=pass; no incident; mode=verify_only

If anomalies present (yield drop / smoke fail / new incident), the line tags
`[ANOMALY]` so Section Chief 19:00 eval can pick up the breadcrumb.

CLI:
  py processors/field_agent_self_eval.py
  py processors/field_agent_self_eval.py --dry-run
  py processors/field_agent_self_eval.py --agent P03_Pantip
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

KPI_DIR = RUNTIME_DIR / "agent_kpi"
INDEX_DB = RUNTIME_DIR / "index.db"


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def _log(msg: str) -> None:
    line = f"[{now_iso()}] [field_agent_self_eval] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"field_agent_self_eval_{now().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def list_agents() -> list[str]:
    if not KPI_DIR.exists():
        return []
    return sorted(p.stem for p in KPI_DIR.glob("*.yaml"))


def gather_per_agent(agent_id: str, conn: sqlite3.Connection) -> dict:
    kpi_path = KPI_DIR / f"{agent_id}.yaml"
    out: dict = {"agent_id": agent_id, "anomaly": False, "anomaly_reasons": []}

    if not kpi_path.exists():
        return out

    try:
        d = yaml.safe_load(kpi_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return out

    out["status"] = d.get("status", "unknown")
    out["sub_class"] = d.get("sub_class", "unknown")
    cur_kpi = (d.get("current_kpi") or {})
    tgt_kpi = (d.get("target_kpi") or {})
    out["yield_24h"] = cur_kpi.get("msg_yield_24h", 0)
    out["yield_target"] = tgt_kpi.get("msg_yield_baseline_24h", 0)
    out["is_verify_only"] = bool(tgt_kpi.get("is_verify_only", False))
    out["yellow_streak"] = d.get("yellow_streak", 0)
    out["smoke_status"] = (d.get("live_status") or {}).get("smoke_test_5_6", "n/a")

    cur = conn.cursor()
    start = (now() - timedelta(hours=24)).isoformat()
    end = now().isoformat()
    cur.execute(
        "SELECT id, kind, scope, title FROM system_history "
        "WHERE ts BETWEEN ? AND ? AND (title LIKE ? OR body LIKE ?) "
        "ORDER BY id DESC LIMIT 5",
        (start, end, f"%{agent_id}%", f"%{agent_id}%"),
    )
    out["history_24h"] = [
        {"id": r[0], "kind": r[1], "scope": r[2], "title": r[3]}
        for r in cur.fetchall()
    ]

    if not out["is_verify_only"]:
        if out["yield_target"] > 0 and out["yield_24h"] < 0.5 * out["yield_target"]:
            out["anomaly"] = True
            out["anomaly_reasons"].append(
                f"yield_24h={out['yield_24h']} < 50% baseline ({out['yield_target']})"
            )
    if out["status"] == "red":
        out["anomaly"] = True
        out["anomaly_reasons"].append(f"status=red")
    if out["status"] == "yellow" and out["yellow_streak"] >= 2:
        out["anomaly"] = True
        out["anomaly_reasons"].append(
            f"yellow_streak={out['yellow_streak']} (≥2 = 3 days from red)"
        )
    if "FAIL" in str(out["smoke_status"]):
        out["anomaly"] = True
        out["anomaly_reasons"].append(f"smoke_test={out['smoke_status']}")

    for h in out["history_24h"]:
        if h["kind"] == "warning":
            out["anomaly"] = True
            out["anomaly_reasons"].append(f"warning #{h['id']}")
            break

    return out


def render_self_eval_line(snap: dict) -> str:
    ts = now().isoformat(timespec="minutes")
    mode = "verify_only" if snap.get("is_verify_only") else "active"
    yield_str = f"yield_24h={snap.get('yield_24h', 0)}/{snap.get('yield_target', 0)}"
    if snap.get("is_verify_only"):
        yield_str += " (verify_only — yield not graded)"
    smoke = snap.get("smoke_status", "n/a")
    status = snap.get("status", "unknown")

    flag = "[ANOMALY]" if snap.get("anomaly") else "[OK]"
    reasons = ""
    if snap.get("anomaly_reasons"):
        reasons = " · " + "; ".join(snap["anomaly_reasons"])

    return f"- {ts} {flag} status={status} mode={mode} {yield_str} smoke={smoke}{reasons}"


def _append_learning(agent_id: str, line: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"[dry-run] {agent_id}: {line}")
        return True
    try:
        from agents._common.agent_memory import append_learning
    except ImportError:
        _log(f"agent_memory.append_learning unavailable; skip {agent_id}")
        return False
    try:
        append_learning(agent_id, line, category="self_eval")
        return True
    except Exception as e:
        _log(f"{agent_id}: append_learning failed: {e}")
        return False


def run_once(only_agent: str | None = None, dry_run: bool = False) -> dict:
    agents = list_agents()
    if only_agent:
        agents = [a for a in agents if a == only_agent]
        if not agents:
            _log(f"agent {only_agent!r} not found in {KPI_DIR}")
            return {"error": "agent_not_found"}

    _log(f"running self-eval for {len(agents)} agents (dry_run={dry_run})")

    conn = sqlite3.connect(str(INDEX_DB))
    summary = {"total": len(agents), "ok": 0, "anomaly": 0, "skipped": 0, "agents": []}

    for aid in agents:
        snap = gather_per_agent(aid, conn)
        if not snap.get("status"):
            summary["skipped"] += 1
            continue
        if snap.get("anomaly"):
            summary["anomaly"] += 1
        else:
            summary["ok"] += 1
        line = render_self_eval_line(snap)
        ok = _append_learning(aid, line, dry_run)
        summary["agents"].append({
            "agent_id": aid,
            "anomaly": snap.get("anomaly"),
            "wrote": ok,
            "line": line,
        })

    conn.close()
    _log(f"done: ok={summary['ok']} anomaly={summary['anomaly']} skipped={summary['skipped']}")

    if not dry_run:
        try:
            from processors.history_log import log_event
            log_event(
                actor="field_agent_self_eval", kind="metric", scope="fleet",
                title=f"Daily self-eval: {summary['ok']}OK / {summary['anomaly']}anomaly / "
                      f"{summary['skipped']}skip",
                body=json.dumps(summary, ensure_ascii=False),
            )
        except Exception as e:
            _log(f"log_event failed: {e}")
        try:
            from processors.org_task_audit_refresh import refresh_org_task_audit
            refresh_org_task_audit("field_agent_self_eval")
        except Exception:
            pass

    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--agent", default=None, help="single agent_id (default: all)")
    p.add_argument("--dry-run", action="store_true", help="don't write to memory")
    args = p.parse_args()
    summary = run_once(only_agent=args.agent, dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
