"""
Blacksite — system_history append-only event log (M-history, schema v7).

Why this exists:
  CHECKPOINT.md is per CLAUDE.md §13.2 a "thin current state" file (full
  overwrite each write, no history retention). Reality: parallel sessions
  + need to mine "when/why did X change?" → CHECKPOINT was bloating
  unboundedly. system_history takes the narrative load off MD.

Write API:
    from processors.history_log import log_event
    log_event(actor='main', kind='decision', scope='gpu',
              title='5070 Ti switchover prep written',
              body='Wrote ocr_qwen_local.py + switchover_5070.py + ...',
              refs=['processors/ocr_qwen_local.py', 'docs/SWITCHOVER_5070.md'],
              parent_id=None)

Query API:
    from processors.history_log import query
    rows = query(since='24h', scope='bigo', kind='decision', limit=20)
    rows = query(grep='5070')
    rows = query(parent_id=42)  # children of an event

CLI: scripts/history.py wraps query() for boss.

Multi-writer safe: SQLite WAL mode + short transactions. daemon, main
session, commander_bridge, brief_send, cron jobs can all write concurrently.

Session attribution:
  session_id auto-derived from env CLAUDE_SESSION_ID if present, else
  '<actor>_<pid>_<startTs>' fallback. Caller may override.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from db.schema import init_db

TZ = timezone(timedelta(hours=7))

VALID_KINDS = {
    "decision",          # design / arch / scope choice
    "milestone",         # P03 Bigo LIVE / module shipped
    "config_change",     # .env / cron / policy edit
    "crash",             # daemon down / cron error / API 5xx burst
    "warning",           # OPSEC concern / risk noted
    "directive",         # boss instruction logged ("以後自動加群")
    "metric",            # daily counts / KPIs snapshot
    "trigger_fired",     # boss said "5070 上路" → engine ran switchover
    "checkpoint_update", # engine modified CHECKPOINT.md (with diff summary)
    # Phase B (5/5): organization activity audit trail. Surfaces in
    # daily brief 「🏛️ 組織狀態」 + scripts/org.py meetings.
    "meeting",           # section_chief_eval / chief_strategist run completed
    "directive_issued",  # CHIEF_STRATEGIST issued one directive (per item, not per file)
    "learning_added",    # agent_memory.append_learning() appended one entry
}

VALID_SCOPES = None  # open vocab; common: bigo p03 p04 p05 daemon gpu kb fb tg pantip


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# --------------------------------------------------------------------
# Session attribution
# --------------------------------------------------------------------

_session_id_cache: str | None = None


def _derive_session_id(actor: str) -> str:
    """Best-effort identifier for the current Claude Code / daemon process.
    Cached after first call."""
    global _session_id_cache
    if _session_id_cache is not None:
        return _session_id_cache
    explicit = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("BLACKSITE_SESSION_ID")
    if explicit:
        _session_id_cache = explicit
        return explicit
    pid = os.getpid()
    start = int(time.time())
    _session_id_cache = f"{actor}_{pid}_{start}"
    return _session_id_cache


# --------------------------------------------------------------------
# Writer
# --------------------------------------------------------------------

def log_event(
    actor: str,
    kind: str,
    title: str,
    *,
    scope: str | None = None,
    body: str | None = None,
    refs: list | dict | None = None,
    parent_id: int | None = None,
    session_id: str | None = None,
    ts: str | None = None,
) -> int:
    """Append one event to system_history. Returns inserted id.

    Raises ValueError on invalid kind. scope is open vocab (no validation).
    refs is JSON-serialized if dict/list. ts auto-set if None.

    Failure mode: if DB write fails (locked / disk full / etc.) — log to
    stderr but DO NOT raise. history logging must never bring down the
    caller (daemon / cron / bridge). Returns -1 on failure.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind {kind!r}; must be one of {sorted(VALID_KINDS)}")
    if not title:
        raise ValueError("title required")
    if len(title) > 120:
        title = title[:117] + "..."

    sid = session_id or _derive_session_id(actor)
    ts = ts or now_iso()
    refs_json = json.dumps(refs, ensure_ascii=False) if refs is not None else None

    try:
        init_db()  # idempotent; ensures v7 table exists on first call ever
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO system_history
                   (ts, session_id, actor, kind, scope, title, body, refs, parent_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, sid, actor, kind, scope, title, body, refs_json, parent_id),
            )
            new_id = cur.lastrowid
            conn.commit()
            return new_id
        finally:
            conn.close()
    except Exception as e:
        print(f"[history_log] WRITE FAIL ({type(e).__name__}: {str(e)[:120]}) "
              f"actor={actor} kind={kind} title={title[:60]!r}",
              file=sys.stderr, flush=True)
        return -1


# --------------------------------------------------------------------
# Query
# --------------------------------------------------------------------

_DUR_RE = re.compile(r"^(\d+)\s*([smhdw])$", re.I)


def _parse_since(s: str) -> str:
    """Convert '24h' / '7d' / '2w' / '30m' / ISO 8601 → ISO 8601 cutoff."""
    if not s:
        return ""
    m = _DUR_RE.match(s.strip())
    if not m:
        return s  # assume already ISO
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit] * n
    return (datetime.now(TZ) - timedelta(seconds=delta)).isoformat(timespec="seconds")


def query(
    since: str | None = None,
    until: str | None = None,
    scope: str | None = None,
    kind: str | None = None,
    actor: str | None = None,
    session_id: str | None = None,
    parent_id: int | None = None,
    grep: str | None = None,
    limit: int = 100,
    order: str = "DESC",
) -> list[dict]:
    """Query system_history. All filters AND-combined. grep matches title+body."""
    init_db()
    conn = get_connection()
    try:
        where = []
        params: list[Any] = []
        if since:
            where.append("ts >= ?")
            params.append(_parse_since(since))
        if until:
            where.append("ts <= ?")
            params.append(_parse_since(until))
        if scope:
            where.append("scope = ?")
            params.append(scope)
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if actor:
            where.append("actor = ?")
            params.append(actor)
        if session_id:
            where.append("session_id = ?")
            params.append(session_id)
        if parent_id is not None:
            where.append("parent_id = ?")
            params.append(parent_id)
        if grep:
            where.append("(title LIKE ? OR body LIKE ?)")
            params.append(f"%{grep}%")
            params.append(f"%{grep}%")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        order = "DESC" if order.upper() == "DESC" else "ASC"
        sql = f"""SELECT id, ts, session_id, actor, kind, scope, title, body, refs, parent_id
                    FROM system_history {clause}
                   ORDER BY ts {order}
                   LIMIT ?"""
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --------------------------------------------------------------------
# CLI (sub-commands)
# --------------------------------------------------------------------

def _fmt_row(r: dict, body_chars: int = 0) -> str:
    head = (
        f"#{r['id']:<5}  {r['ts']}  [{r['actor']:<14}] "
        f"{r['kind']:<18} {('<'+r['scope']+'>') if r['scope'] else '':<14}  "
        f"{r['title']}"
    )
    if body_chars and r.get("body"):
        body = r["body"].replace("\n", " ").strip()
        head += f"\n         {body[:body_chars]}{'...' if len(body)>body_chars else ''}"
    return head


def main() -> None:
    p = argparse.ArgumentParser(description="system_history reader/writer")
    sub = p.add_subparsers(dest="cmd", required=True)

    pq = sub.add_parser("ls", help="list events (default)")
    pq.add_argument("--since", default="24h", help="duration like '24h' '7d' '2w' or ISO")
    pq.add_argument("--until")
    pq.add_argument("--scope")
    pq.add_argument("--kind", choices=sorted(VALID_KINDS))
    pq.add_argument("--actor")
    pq.add_argument("--session-id")
    pq.add_argument("--parent-id", type=int)
    pq.add_argument("--grep")
    pq.add_argument("--limit", type=int, default=50)
    pq.add_argument("--body", type=int, default=0, help="show first N chars of body")
    pq.add_argument("--asc", action="store_true")

    pa = sub.add_parser("add", help="manually log a new event")
    pa.add_argument("--actor", required=True)
    pa.add_argument("--kind", required=True, choices=sorted(VALID_KINDS))
    pa.add_argument("--scope")
    pa.add_argument("--title", required=True)
    pa.add_argument("--body")
    pa.add_argument("--refs", help="comma-separated paths (stored as JSON list)")
    pa.add_argument("--parent-id", type=int)

    ps = sub.add_parser("show", help="show one event with full body + children")
    ps.add_argument("id", type=int)

    pst = sub.add_parser("stats", help="counts by scope/kind/actor over a window")
    pst.add_argument("--since", default="7d")

    args = p.parse_args()

    if args.cmd == "ls":
        rows = query(
            since=args.since, until=args.until, scope=args.scope, kind=args.kind,
            actor=args.actor, session_id=args.session_id,
            parent_id=args.parent_id, grep=args.grep, limit=args.limit,
            order="ASC" if args.asc else "DESC",
        )
        for r in rows:
            print(_fmt_row(r, body_chars=args.body))
        print(f"\n  ({len(rows)} rows)")

    elif args.cmd == "add":
        refs = [s.strip() for s in args.refs.split(",")] if args.refs else None
        new_id = log_event(
            actor=args.actor, kind=args.kind, title=args.title,
            scope=args.scope, body=args.body, refs=refs, parent_id=args.parent_id,
        )
        print(f"logged id={new_id}")

    elif args.cmd == "show":
        rows = query(limit=1)  # placeholder; we need single by id
        init_db()
        conn = get_connection()
        try:
            row = conn.execute(
                """SELECT id,ts,session_id,actor,kind,scope,title,body,refs,parent_id
                     FROM system_history WHERE id = ?""", (args.id,)
            ).fetchone()
            if not row:
                print(f"id {args.id} not found"); sys.exit(1)
            r = dict(row)
            print(_fmt_row(r))
            if r["body"]:
                print(f"\nBODY:\n{r['body']}")
            if r["refs"]:
                print(f"\nREFS: {r['refs']}")
            children = conn.execute(
                """SELECT id,ts,actor,kind,scope,title FROM system_history
                    WHERE parent_id = ? ORDER BY ts""", (args.id,)
            ).fetchall()
            if children:
                print(f"\nCHILDREN ({len(children)}):")
                for c in children:
                    cd = dict(c)
                    print(f"  #{cd['id']} {cd['ts']} [{cd['actor']}] {cd['kind']:<14} {cd['title']}")
        finally:
            conn.close()

    elif args.cmd == "stats":
        init_db()
        conn = get_connection()
        try:
            cutoff = _parse_since(args.since)
            print(f"=== system_history stats since {cutoff} ===\n")
            for col in ("scope", "kind", "actor"):
                rows = conn.execute(
                    f"""SELECT COALESCE({col},'(none)') AS k, COUNT(*) AS n
                          FROM system_history WHERE ts >= ?
                         GROUP BY {col} ORDER BY n DESC""",
                    (cutoff,),
                ).fetchall()
                print(f"by {col}:")
                for r in rows:
                    print(f"  {r['k']:<22} {r['n']}")
                print()
            total = conn.execute(
                "SELECT COUNT(*) FROM system_history WHERE ts >= ?", (cutoff,)
            ).fetchone()[0]
            grand = conn.execute("SELECT COUNT(*) FROM system_history").fetchone()[0]
            print(f"window total: {total}    all-time total: {grand}")
        finally:
            conn.close()


if __name__ == "__main__":
    main()
