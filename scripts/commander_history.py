"""scripts/commander_history.py — query boss_opinions extracted from commander chat.

Subcommands:
  ls [--since 24h] [--topic X] [--kind Y] [-n N] [--asc]
                            list opinions, default last 30 (newest first)
  grep <text>               full-text search content
  show <opinion_id>         full body of one opinion
  topics                    list distinct topics with counts
  kinds                     list distinct kinds with counts

Boss invocation pattern from main session:
  py scripts/commander_history.py grep "kb design"
  py scripts/commander_history.py ls --topic persona_opsec --since 7d
  py scripts/commander_history.py show O-2026-05-02-005

Output: tab-separated for grep-friendliness.
"""

from __future__ import annotations

import argparse
import json
import os
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
    """Parse '24h' / '7d' / '2w' or ISO ts; return ISO cutoff string."""
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
    return s  # assume already ISO


def fmt_row(row: dict, body_limit: int = 80) -> str:
    body = (row["content"] or "")[:body_limit]
    if len(row["content"] or "") > body_limit:
        body += "…"
    return (
        f"{row['opinion_id']:<22}  {row['source_ts'][:19]}  "
        f"[{row['kind']:<10}]  {row['topic']:<22}  {body}"
    )


def cmd_ls(args) -> int:
    conn = get_connection()
    try:
        sql = "SELECT * FROM boss_opinions WHERE status='active'"
        params: list = []
        if args.since:
            sql += " AND source_ts >= ?"
            params.append(parse_since(args.since))
        if args.topic:
            sql += " AND topic LIKE ?"
            params.append(f"%{args.topic}%")
        if args.kind:
            sql += " AND kind = ?"
            params.append(args.kind)
        order = "ASC" if args.asc else "DESC"
        sql += f" ORDER BY source_ts {order} LIMIT ?"
        params.append(args.n or 30)
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            print("(no matching opinions)")
            return 0
        for r in rows:
            print(fmt_row(dict(r)))
        print(f"\n  ({len(rows)} rows)")
        return 0
    finally:
        conn.close()


def cmd_grep(args) -> int:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM boss_opinions "
            "WHERE status='active' AND (content LIKE ? OR context_summary LIKE ? OR topic LIKE ?) "
            "ORDER BY source_ts DESC LIMIT ?",
            (f"%{args.text}%", f"%{args.text}%", f"%{args.text}%", args.n or 30),
        ).fetchall()
        if not rows:
            print(f"(no opinions matching {args.text!r})")
            return 0
        for r in rows:
            print(fmt_row(dict(r), body_limit=120))
        print(f"\n  ({len(rows)} rows)")
        return 0
    finally:
        conn.close()


def cmd_show(args) -> int:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM boss_opinions WHERE opinion_id=?",
            (args.opinion_id,),
        ).fetchone()
        if not row:
            print(f"opinion_id {args.opinion_id!r} not found")
            return 1
        d = dict(row)
        print(f"opinion_id     : {d['opinion_id']}")
        print(f"source_ts      : {d['source_ts']}")
        print(f"source_offset  : {d['source_offset']}")
        print(f"extracted_at   : {d['extracted_at']}")
        print(f"topic          : {d['topic']}")
        print(f"kind           : {d['kind']}")
        print(f"status         : {d['status']}")
        if d.get("superseded_by"):
            print(f"superseded_by  : {d['superseded_by']}")
        print(f"context_summary: {d['context_summary']}")
        if d.get("refs"):
            try:
                refs = json.loads(d["refs"])
                if refs:
                    print(f"refs           : {refs}")
            except json.JSONDecodeError:
                pass
        print()
        print("--- content ---")
        print(d["content"])
        return 0
    finally:
        conn.close()


def cmd_topics(args) -> int:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT topic, COUNT(*) as n FROM boss_opinions WHERE status='active' "
            "GROUP BY topic ORDER BY n DESC, topic"
        ).fetchall()
        if not rows:
            print("(no opinions)")
            return 0
        for r in rows:
            print(f"  {r['n']:>4}  {r['topic']}")
        return 0
    finally:
        conn.close()


def cmd_kinds(args) -> int:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT kind, COUNT(*) as n FROM boss_opinions WHERE status='active' "
            "GROUP BY kind ORDER BY n DESC, kind"
        ).fetchall()
        for r in rows:
            print(f"  {r['n']:>4}  {r['kind']}")
        return 0
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Query boss_opinions (extracted from commander chat)")
    sub = p.add_subparsers(dest="cmd")

    p_ls = sub.add_parser("ls", help="list opinions")
    p_ls.add_argument("--since", default=None)
    p_ls.add_argument("--topic", default=None)
    p_ls.add_argument("--kind", default=None)
    p_ls.add_argument("-n", type=int, default=30)
    p_ls.add_argument("--asc", action="store_true")
    p_ls.set_defaults(func=cmd_ls)

    p_grep = sub.add_parser("grep", help="full-text search content")
    p_grep.add_argument("text")
    p_grep.add_argument("-n", type=int, default=30)
    p_grep.set_defaults(func=cmd_grep)

    p_show = sub.add_parser("show", help="show full opinion body")
    p_show.add_argument("opinion_id")
    p_show.set_defaults(func=cmd_show)

    sub.add_parser("topics", help="list distinct topics with counts").set_defaults(func=cmd_topics)
    sub.add_parser("kinds", help="list distinct kinds with counts").set_defaults(func=cmd_kinds)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
