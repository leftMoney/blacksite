"""scripts/leads.py — query kb_leads (P1-P4 lead-to-lifecycle pipeline).

Subcommands:
  ls [--state X] [--lane Y] [--type Z] [--since Nh] [-n N]
                           list leads (newest first)
  show <lead_id>           full body of one lead (incl. evidence + resolution)
  stats                    counts by state / lane / type
  escalated                alias for `ls --state escalated --state conflict_flag`
  pending                  alias for `ls --state pending`
  executed                 alias for `ls --state executed`

Output: tab/space-separated for grep-friendly piping.

Boss invocation patterns:
  py scripts/leads.py ls --since 24h
  py scripts/leads.py escalated
  py scripts/leads.py show L-2026-05-02-007
  py scripts/leads.py stats
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))


def parse_since(s: str | None) -> str | None:
    if not s:
        return None
    m = re.match(r"^(\d+)([hdwm])$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
            "m": timedelta(days=n * 30),
        }[unit]
        return (datetime.now(TZ) - delta).isoformat(timespec="seconds")
    return s


def fmt_row(row: dict) -> str:
    target = (row.get("target") or "")[:36]
    state = row.get("state") or ""
    lane = (row.get("triage_lane") or "")[:18]
    res = (row.get("resolution") or "")[:60]
    return (
        f"{row['lead_id']:<22}  {row['emitted_at'][:19]}  "
        f"[{row['type']:<22}]  state={state:<18}  lane={lane:<18}  "
        f"target={target:<36}  {res}"
    )


def cmd_ls(args) -> int:
    conn = get_connection()
    try:
        sql = "SELECT * FROM kb_leads WHERE 1=1"
        params: list = []
        if args.state:
            placeholders = ",".join(["?"] * len(args.state))
            sql += f" AND state IN ({placeholders})"
            params.extend(args.state)
        if args.lane:
            sql += " AND triage_lane = ?"
            params.append(args.lane)
        if args.type:
            sql += " AND type = ?"
            params.append(args.type)
        if args.since:
            sql += " AND emitted_at >= ?"
            params.append(parse_since(args.since))
        order = "ASC" if args.asc else "DESC"
        sql += f" ORDER BY emitted_at {order} LIMIT ?"
        params.append(args.n or 30)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            print("(no matching leads)")
            return 0
        for r in rows:
            print(fmt_row(dict(r)))
        print(f"\n  ({len(rows)} rows)")
        return 0
    finally:
        conn.close()


def cmd_show(args) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM kb_leads WHERE lead_id=?",
            (args.lead_id,),
        ).fetchone()
        if not row:
            print(f"lead_id {args.lead_id!r} not found")
            return 1
        d = dict(row)
        print(f"lead_id          : {d['lead_id']}")
        print(f"origin           : {d['origin']}")
        print(f"origin_ref       : {d['origin_ref']}")
        print(f"emitted_at       : {d['emitted_at']}")
        print(f"type             : {d['type']}")
        print(f"target           : {d['target']}")
        print(f"suggested_action : {d['suggested_action']}")
        print(f"confidence       : {d['confidence']}")
        print(f"actionability    : {d['actionability']}")
        print(f"reversibility    : {d['reversibility']}")
        print(f"auto_safe        : {d['auto_safe']}")
        print(f"triage_lane      : {d['triage_lane']}")
        print(f"triaged_at       : {d['triaged_at']}")
        print(f"state            : {d['state']}")
        print(f"resolution       : {d['resolution']}")
        print(f"resolution_at    : {d['resolution_at']}")
        print(f"re_queued_until  : {d['re_queued_until']}")
        print(f"parent_lead_id   : {d['parent_lead_id']}")
        if d.get("refs"):
            try:
                refs = json.loads(d["refs"])
                if refs:
                    print(f"refs             : {refs}")
            except json.JSONDecodeError:
                print(f"refs (raw)       : {d['refs']}")
        if d.get("evidence"):
            print()
            print("--- evidence ---")
            try:
                ev = json.loads(d["evidence"])
                print(json.dumps(ev, ensure_ascii=False, indent=2))
            except json.JSONDecodeError:
                print(d["evidence"])
        return 0
    finally:
        conn.close()


def cmd_stats(args) -> int:
    conn = get_connection()
    try:
        for col, label in (("state", "by state"),
                            ("triage_lane", "by lane"),
                            ("type", "by type")):
            print(f"{label}:")
            rows = conn.execute(
                f"""SELECT COALESCE({col},'(none)') AS k, COUNT(*) AS n
                      FROM kb_leads
                     GROUP BY {col}
                     ORDER BY n DESC"""
            ).fetchall()
            for r in rows:
                print(f"  {r['n']:>5}  {r['k']}")
            print()
        total = conn.execute("SELECT COUNT(*) FROM kb_leads").fetchone()[0]
        print(f"total: {total}")
        return 0
    finally:
        conn.close()


def cmd_escalated(args) -> int:
    args.state = ["escalated", "conflict_flag"]
    args.lane = None
    args.type = None
    args.since = args.since if hasattr(args, "since") else None
    args.n = args.n if hasattr(args, "n") else 30
    args.asc = False
    return cmd_ls(args)


def cmd_pending(args) -> int:
    args.state = ["pending"]
    args.lane = None
    args.type = None
    args.since = None
    args.n = 30
    args.asc = False
    return cmd_ls(args)


def cmd_executed(args) -> int:
    args.state = ["executed"]
    args.lane = None
    args.type = None
    args.since = None
    args.n = 30
    args.asc = False
    return cmd_ls(args)


def main() -> int:
    p = argparse.ArgumentParser(description="Query kb_leads (lead-to-lifecycle pipeline)")
    sub = p.add_subparsers(dest="cmd")

    p_ls = sub.add_parser("ls", help="list leads")
    p_ls.add_argument("--state", action="append", default=None,
                      help="filter by state (repeatable for IN clause)")
    p_ls.add_argument("--lane", default=None)
    p_ls.add_argument("--type", default=None)
    p_ls.add_argument("--since", default=None, help="duration like '24h' '7d' or ISO")
    p_ls.add_argument("-n", type=int, default=30)
    p_ls.add_argument("--asc", action="store_true")
    p_ls.set_defaults(func=cmd_ls)

    p_show = sub.add_parser("show", help="show full lead body")
    p_show.add_argument("lead_id")
    p_show.set_defaults(func=cmd_show)

    sub.add_parser("stats", help="counts by state/lane/type").set_defaults(func=cmd_stats)

    p_esc = sub.add_parser("escalated",
                           help="alias for ls --state escalated --state conflict_flag")
    p_esc.add_argument("-n", type=int, default=30)
    p_esc.add_argument("--since", default=None)
    p_esc.set_defaults(func=cmd_escalated)

    p_pen = sub.add_parser("pending", help="alias for ls --state pending")
    p_pen.add_argument("-n", type=int, default=30)
    p_pen.set_defaults(func=cmd_pending)

    p_exe = sub.add_parser("executed", help="alias for ls --state executed")
    p_exe.add_argument("-n", type=int, default=30)
    p_exe.set_defaults(func=cmd_executed)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
