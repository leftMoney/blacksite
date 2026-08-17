"""Boss-facing audit page for Blacksite automation.

This script converts append-only machine logs into a human-checkable view:
recent automatic actions, config changes, strategy directive materialization,
Section Chief evaluations, open incidents, and pending investigation tasks.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db.connection import get_connection  # noqa: E402
from db.schema import init_db  # noqa: E402
from processors.history_log import query as history_query  # noqa: E402

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
KPI_DIR = RUNTIME / "agent_kpi"
INCIDENTS_DIR = RUNTIME / "agent_incidents"
TASK_DIR = RUNTIME / "lead_subagent_queue"
DIRECTIVE_DIR = RUNTIME / "strategy_directives"
STRATEGY_AUDIT = RUNTIME / "strategy_directive_audit.jsonl"
BRIEF_SENT_DIR = RUNTIME / "briefs" / "sent"
LOG_DIR = RUNTIME / "logs"


def now() -> datetime:
    return datetime.now(TZ)


def parse_window(value: str) -> timedelta:
    m = re.match(r"^(\d+)([hdw])$", value.strip().lower())
    if not m:
        raise SystemExit("window must look like 12h, 24h, 7d, or 2w")
    n, unit = int(m.group(1)), m.group(2)
    return {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def cutoff_iso(window: str) -> str:
    return (now() - parse_window(window)).isoformat(timespec="seconds")


def fmt_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except Exception:
        return {}


def load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def history_events(since: str, limit: int) -> list[dict]:
    rows = history_query(
        since=since,
        limit=limit,
        order="DESC",
    )
    keep = {
        "config_change", "directive", "milestone", "warning", "crash",
        "meeting", "directive_issued", "learning_added", "metric",
    }
    return [r for r in rows if r.get("kind") in keep]


def print_history_section(since: str, limit: int) -> None:
    rows = history_events(since, limit)
    print("▌1. AI 自動動作 / 系統事件")
    if not rows:
        print("  (沒有 system_history 事件)")
        return
    for r in rows[:limit]:
        scope = f"<{r['scope']}>" if r.get("scope") else ""
        print(f"  #{r['id']} {r['ts']} [{r['actor']}] {r['kind']} {scope}")
        print(f"      {r['title']}")
        if r.get("refs"):
            try:
                refs = json.loads(r["refs"])
                if isinstance(refs, list) and refs:
                    print(f"      refs: {', '.join(str(x) for x in refs[:3])}")
            except Exception:
                pass


def print_directive_section(cutoff: str) -> None:
    print("\n▌2. 策略長指令是否真的落地")
    if not STRATEGY_AUDIT.exists():
        print("  (沒有 strategy_directive_audit.jsonl)")
        return
    rows = []
    for line in STRATEGY_AUDIT.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if item.get("ts", "") >= cutoff:
            rows.append(item)
    if not rows:
        print("  (本窗口沒有已物化的 strategy directives)")
    for item in rows[-20:]:
        target = (
            item.get("agent_id") or item.get("chief_id") or item.get("target") or
            item.get("incident_id") or item.get("task") or ""
        )
        print(f"  - {item.get('ts')} {item.get('directive')} {target}")

    latest = sorted(DIRECTIVE_DIR.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True)
    if latest:
        print(f"  最新 directive file: {fmt_path(latest[0])}")


def kpi_rows(cutoff: str) -> list[dict]:
    out = []
    for path in sorted(KPI_DIR.glob("*.yaml")):
        data = load_yaml(path)
        if not data:
            continue
        out.append({
            "agent_id": data.get("agent_id") or path.stem,
            "status": data.get("status", "?"),
            "chief": data.get("last_evaluated_by") or data.get("managed_by") or "SECTION_CHIEF",
            "last_eval": data.get("last_evaluated_at") or "",
            "yield": (data.get("current_kpi") or {}).get("msg_yield_24h"),
            "directives": data.get("recent_directives") or [],
        })
    return out


def print_section_chief_section(cutoff: str) -> None:
    print("\n▌3. 小主管是否查核情報員")
    rows = kpi_rows(cutoff)
    recent = [r for r in rows if r["last_eval"] >= cutoff]
    if not recent:
        print("  ⚠ 本窗口沒有 agent KPI eval；查 `python scripts\\org.py meetings --since 24h`")
    counts: dict[str, int] = {}
    for r in recent:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if recent:
        print(
            f"  已查核 {len(recent)} agents: " +
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
    attention = [r for r in rows if r["status"] in ("yellow", "red")]
    for r in attention[:20]:
        stale = " stale_eval" if r["last_eval"] < cutoff else ""
        print(f"  - {r['status']:<6} {r['agent_id']:<24} yield={r['yield']} last_eval={r['last_eval']}{stale}")

    directed = [r for r in rows if r["directives"]]
    if directed:
        print("  有收到具體任務的情報員:")
        for r in directed[:12]:
            kinds = [d.get("kind", "?") for d in r["directives"] if isinstance(d, dict)]
            print(f"  - {r['agent_id']}: {len(r['directives'])} directive(s) {kinds}")


def incident_rows(state: str | None = None) -> list[dict]:
    out = []
    for path in sorted(INCIDENTS_DIR.glob("INC-*.md")):
        try:
            fm = parse_frontmatter(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not fm:
            continue
        if state and fm.get("state") != state:
            continue
        fm["_path"] = path
        out.append(fm)
    return sorted(out, key=lambda x: x.get("opened_at", ""), reverse=True)


def print_queue_section(cutoff: str) -> None:
    print("\n▌4. 待辦任務 / open incidents")
    open_inc = incident_rows("open")
    if open_inc:
        print(f"  Open incidents: {len(open_inc)}")
        for inc in open_inc[:10]:
            print(
                f"  - {inc.get('incident_id')} {inc.get('agent_id')} "
                f"{inc.get('violation_kind')} severity={inc.get('severity')} "
                f"file={fmt_path(inc['_path'])}"
            )
    else:
        print("  Open incidents: 0")

    tasks = []
    for path in sorted(TASK_DIR.glob("*.task"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {"target": path.stem}
        queued_at = data.get("queued_at", "")
        if queued_at and queued_at < cutoff:
            continue
        tasks.append((path, data))
    if tasks:
        print(f"  Pending investigation tasks in window: {len(tasks)}")
        for path, data in tasks[:10]:
            print(f"  - {path.name}: {data.get('target')} deadline={data.get('deadline')}")
    else:
        print("  Pending investigation tasks in window: 0")


def print_llm_section(cutoff: str) -> None:
    print("\n▌5. LLM / GPT 實際呼叫線索")
    patterns = ["codex ok", "codex fail", "tokens used", "stage2", "chief_strategist"]
    lines = []
    for path in sorted(LOG_DIR.glob("*2026-*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.stat().st_mtime < (now() - timedelta(days=3)).timestamp():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]:
                m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})", line)
                if not m or m.group(1) < cutoff:
                    continue
                if any(p in line.lower() for p in patterns):
                    lines.append((path, line))
        except Exception:
            continue
    if not lines:
        print("  (本窗口沒有明確 LLM log 命中)")
    for path, line in lines[-12:]:
        print(f"  - {fmt_path(path)} :: {line[:220]}")


def print_brief_section(cutoff: str) -> None:
    print("\n▌6. 你有沒有收到/可重看")
    sent = []
    for path in sorted(BRIEF_SENT_DIR.glob("sent_*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec="seconds")
        if mtime >= cutoff:
            sent.append((path, mtime))
    if not sent:
        print("  (本窗口沒有 sent brief)")
    for path, mtime in sent[:12]:
        print(f"  - {mtime} {fmt_path(path)}")


def print_drilldown() -> None:
    print("\n▌7. 人類查核命令")
    print("  python scripts\\boss_audit.py --since 24h")
    print("  python scripts\\history.py ls --since 24h --body 160")
    print("  python scripts\\org.py status")
    print("  python scripts\\org.py meetings --since 24h")
    print("  python scripts\\org.py directives --unprocessed")
    print("  python processors\\agent_incidents.py ls --state open")
    print("  Get-ChildItem instances\\_TEMPLATE\\runtime\\lead_subagent_queue")


def main() -> int:
    parser = argparse.ArgumentParser(description="Boss-facing automation audit page")
    parser.add_argument("--since", default="24h", help="window: 12h, 24h, 7d, 2w")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    init_db()
    cutoff = cutoff_iso(args.since)
    print(f"=== Blacksite 人類查核頁 @ {now().isoformat(timespec='seconds')} ===")
    print(f"instance={ACTIVE_INSTANCE}  window={args.since}  cutoff={cutoff}\n")
    print_history_section(args.since, args.limit)
    print_directive_section(cutoff)
    print_section_chief_section(cutoff)
    print_queue_section(cutoff)
    print_llm_section(cutoff)
    print_brief_section(cutoff)
    print_drilldown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
