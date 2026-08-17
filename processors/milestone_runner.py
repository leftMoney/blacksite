"""processors/milestone_runner.py — fire pre-scheduled milestone probes
and DM boss via commander (brief_send queue) on result.

Cron */30 min via daemon. Reads runtime/milestone_alerts.jsonl, finds any
milestone whose due_at <= now AND fired=False, runs the corresponding probe,
writes result to brief queue (brief_send picks up + DMs boss via P01), and
marks fired=True (idempotent).

Probe types:
- history_event_within: check if a system_history event matched in last N min
- brief_md_exists_and_llm: check if daily_brief produced LLM-composed md (size threshold)
- kb_leads_count_for: count kb_leads emitted on a specific date
- kb_leads_state_count: count kb_leads in specified states within last N hours
- brief_contains_sections: check brief md contains required section headers

Result format DM'd to boss (concise, ASCII + 中文):
   ✅ M-<id> <title> — <brief evidence>
or
   ❌ M-<id> <title> — <fail_msg> (<diagnostic>)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

# Fix Windows daemon-subprocess stdout encoding — inherits cp950 (Traditional
# Chinese codepage) which can't encode emoji (✅ U+2705 / ❌ U+274C). Boss 5/3
# directive O-2026-05-03-012 / fix per CLAUDE.md §6.4. errors='replace' falls
# back to '?' on truly unsupported chars instead of crashing.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from db.connection import get_connection  # noqa: E402

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
ALERTS_PATH = RUNTIME / "milestone_alerts.jsonl"
BRIEF_QUEUE = RUNTIME / "briefs" / "queue"
BRIEF_QUEUE.mkdir(parents=True, exist_ok=True)
LOG_DIR = RUNTIME / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [milestone] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"milestone_runner_{now().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def hist(kind: str, title: str, body: str = "", scope: str = "milestone",
         refs: list | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(actor="milestone_runner", kind=kind, scope=scope,
                         title=title, body=body, refs=refs)
    except Exception as e:
        log(f"hist write fail: {e}")
        return -1


# ============================================================================
# PROBES
# ============================================================================

def probe_history_event_within(args: dict) -> tuple[bool, str]:
    """Check if a system_history event matched the title pattern within the
    last N minutes for the given scope."""
    import re
    scope = args.get("scope")
    pattern = args.get("title_pattern", "")
    window_min = int(args.get("window_min", 30))
    cutoff = (now() - timedelta(minutes=window_min)).isoformat(timespec="seconds")
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, title, ts FROM system_history "
            "WHERE scope=? AND ts >= ? "
            "ORDER BY ts DESC",
            (scope, cutoff),
        ).fetchall()
    finally:
        conn.close()
    matched = []
    for r in rows:
        if re.search(pattern, r["title"] or "", re.IGNORECASE):
            matched.append(f"#{r['id']} {r['title'][:60]} @ {r['ts']}")
    if matched:
        return True, f"matched {len(matched)} events; sample: {matched[0]}"
    return False, f"0 events matching '{pattern}' in scope='{scope}' last {window_min} min"


def probe_brief_md_exists_and_llm(args: dict) -> tuple[bool, str]:
    """Check daily_brief for given date is composed via LLM (size > threshold)."""
    date_str = args["date"]
    min_size = int(args.get("min_size_for_llm", 5000))
    queue_md = BRIEF_QUEUE / f"pending_{date_str}.md"
    sent_md = RUNTIME / "briefs" / "sent" / f"sent_{date_str}.md"
    md = queue_md if queue_md.exists() else (sent_md if sent_md.exists() else None)
    if md is None:
        return False, f"neither queue nor sent {date_str}.md exists"
    size = md.stat().st_size
    if size >= min_size:
        return True, f"{md.name} {size}B (LLM-composed)"
    return False, f"{md.name} only {size}B (likely template fallback, threshold {min_size})"


def probe_kb_leads_count_for(args: dict) -> tuple[bool, str]:
    """Count kb_leads emitted on a specific date."""
    date_str = args["date"]
    min_count = int(args.get("min_count", 1))
    conn = get_connection()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM kb_leads WHERE date(emitted_at) = ?",
            (date_str,),
        ).fetchone()[0]
    finally:
        conn.close()
    if n >= min_count:
        return True, f"{n} kb_leads emitted {date_str}"
    return False, f"only {n} kb_leads emitted {date_str} (threshold {min_count})"


def probe_kb_leads_state_count(args: dict) -> tuple[bool, str]:
    """Count kb_leads in specified states changed within last N hours."""
    states = args["states"]
    since_hours = float(args.get("since_hours", 1))
    min_count = int(args.get("min_count", 1))
    cutoff = (now() - timedelta(hours=since_hours)).isoformat(timespec="seconds")
    placeholders = ",".join(["?"] * len(states))
    conn = get_connection()
    try:
        n = conn.execute(
            f"SELECT COUNT(*) FROM kb_leads "
            f"WHERE state IN ({placeholders}) "
            f"AND COALESCE(triaged_at, emitted_at) >= ?",
            (*states, cutoff),
        ).fetchone()[0]
    finally:
        conn.close()
    if n >= min_count:
        return True, f"{n} kb_leads in states {states} within {since_hours}h"
    return False, f"only {n} (threshold {min_count}) kb_leads transitioned to {states}"


def probe_brief_contains_sections(args: dict) -> tuple[bool, str]:
    """Check brief md contains required section headers."""
    date_str = args["date"]
    required = args.get("required_sections", [])
    queue_md = BRIEF_QUEUE / f"pending_{date_str}.md"
    sent_md = RUNTIME / "briefs" / "sent" / f"sent_{date_str}.md"
    md = queue_md if queue_md.exists() else (sent_md if sent_md.exists() else None)
    if md is None:
        return False, f"brief {date_str}.md not found"
    text = md.read_text(encoding="utf-8", errors="replace")
    missing = [s for s in required if s not in text]
    if missing:
        return False, f"missing sections: {missing}"
    return True, f"all {len(required)} sections present in {md.name}"


PROBES = {
    "history_event_within": probe_history_event_within,
    "brief_md_exists_and_llm": probe_brief_md_exists_and_llm,
    "kb_leads_count_for": probe_kb_leads_count_for,
    "kb_leads_state_count": probe_kb_leads_state_count,
    "brief_contains_sections": probe_brief_contains_sections,
}


# ============================================================================
# DM via brief_send queue
# ============================================================================

def emit_to_boss(milestone_id: str, ok: bool, title: str, evidence: str) -> Path:
    """Write a milestone alert markdown to brief queue. brief_send_loop polls
    queue/pending_*.md every 5 min and DMs boss via P01."""
    icon = "✅" if ok else "❌"
    ts = now().strftime("%Y-%m-%dT%H-%M")
    fname = f"pending_{ts}_milestone_{milestone_id}.md"
    md = BRIEF_QUEUE / fname
    result_zh = "✅ 達成" if ok else "❌ 未達成"
    body = (
        f"[里程碑] {title}\n\n"
        f"• {result_zh} → {evidence}\n\n"
        f"自動驗收 @ {now_iso()}（{milestone_id}）"
    )
    md.write_text(body, encoding="utf-8")
    return md


# ============================================================================
# Main loop
# ============================================================================

def load_alerts() -> list[dict]:
    if not ALERTS_PATH.exists():
        return []
    out = []
    with ALERTS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def save_alerts(alerts: list[dict]) -> None:
    with ALERTS_PATH.open("w", encoding="utf-8") as f:
        for a in alerts:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")


def main() -> int:
    alerts = load_alerts()
    if not alerts:
        log("no milestones loaded")
        return 0
    log(f"loaded {len(alerts)} milestones")
    fired_now = 0
    n = now()
    for a in alerts:
        if a.get("fired"):
            continue
        try:
            due = datetime.fromisoformat(a["due_at"])
        except Exception:
            log(f"bad due_at on {a.get('id')}")
            continue
        if due > n:
            continue
        # Due — run probe
        probe_type = a.get("probe_type")
        probe_fn = PROBES.get(probe_type)
        if probe_fn is None:
            log(f"unknown probe_type {probe_type} on {a['id']}")
            a["fired"] = True
            a["result"] = {"ok": False, "evidence": f"unknown probe_type {probe_type}"}
            a["fired_at"] = now_iso()
            continue
        try:
            ok, evidence = probe_fn(a.get("probe_args", {}))
        except Exception as e:
            ok, evidence = False, f"probe error: {type(e).__name__}: {e}"
        log(f"{'✅' if ok else '❌'} {a['id']} — {evidence}")
        a["fired"] = True
        a["fired_at"] = now_iso()
        a["result"] = {"ok": ok, "evidence": evidence}
        title = a.get("title", a["id"])
        emit_to_boss(a["id"], ok, title, evidence)
        hist(
            kind="trigger_fired" if ok else "warning",
            title=f"milestone {a['id']}: {'PASS' if ok else 'FAIL'} — {title}",
            body=f"due_at={a['due_at']}\nevidence={evidence}\nfail_msg={a.get('fail_msg', '')}",
        )
        fired_now += 1
    save_alerts(alerts)
    log(f"fired {fired_now} milestones this pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
