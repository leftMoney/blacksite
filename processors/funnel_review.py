"""
Funnel-edge AI-review pipeline (M4.5c).

Two Python modes (this file) + ONE Claude scheduled task (separate, registered
via mcp__scheduled-tasks) work together — same split design as M4 cards:

  * `prepare`        : Python; selects funnel_edges with review_state IN
                       (pending, uncertain) AND edge_kind='funnel_push',
                       assembles evidence bundles (parent msg text, sender
                       pattern, amplification stats, target name), writes
                       JSON to runtime/funnel/review/pending_<ts>.json.

  * `apply-verdict`  : Python; takes a verdict JSON (composed by scheduled
                       Claude session) and UPDATEs review_state +
                       review_verdict + review_reason on the edge.

  * `mark-processed` : moves queue file from review/ → reviewed/.

Edge gets one of three review states from AI:
  - `approved`   : real funnel target worth M4.5d auto-join
  - `rejected`   : noise / news quote / journalism mention / not joinable
  - `uncertain`  : insufficient signal; recheck on next prepare with more data

Verdict NEVER auto-joins; M4.5d is a separate gated step. M4.5c only labels.
"""

from __future__ import annotations

# 2026-05-14: legacy Claude Code scheduled task `blacksite-funnel-review` is
# disabled. Routine review now runs through processors/funnel_auto_review.py
# under Blacksite daemon/Codex provider; this helper remains for manual/audit
# queue workflows.

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from db.schema import init_db

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_DIR = RUNTIME_DIR / "funnel" / "review"
DONE_DIR = RUNTIME_DIR / "funnel" / "reviewed"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)
DONE_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

VALID_STATES = {"approved", "rejected", "uncertain"}


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_bkk().isoformat(timespec='seconds')}] [funnel-rev] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"funnel_review_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------
# Mode A — prepare review queue
# ----------------------------------------------------------------------

def assemble_edge_bundle(conn, edge) -> dict:
    """Pull supporting evidence for one edge: sample msg, all msgs,
    target's other appearances, sender info."""
    sample_msg = None
    if edge["sample_msg_row_id"]:
        m = conn.execute(
            """SELECT row_id, ts, text, sender_username, sender_external_id,
                      views, forwards, replies, intent, topic, tone
                 FROM messages WHERE row_id = ?""",
            (edge["sample_msg_row_id"],),
        ).fetchone()
        if m:
            sample_msg = {
                "row_id": m["row_id"], "ts": m["ts"],
                "text": (m["text"] or "")[:500],
                "sender_username": m["sender_username"],
                "sender_external_id": m["sender_external_id"],
                "views": m["views"], "forwards": m["forwards"], "replies": m["replies"],
                "intent": m["intent"], "topic": m["topic"], "tone": m["tone"],
            }

    # All push messages for this edge (up to 6)
    push_msgs = []
    rows = conn.execute(
        """SELECT m.row_id, m.ts, substr(m.text, 1, 240) text,
                  m.sender_external_id, m.amplification_count, m.intent
             FROM messages m
             JOIN messages_entities me ON me.message_row_id = m.row_id
             JOIN entities e ON e.row_id = me.entity_row_id
            WHERE m.chat_external_id = ?
              AND e.kind = ? AND e.name = ?
            ORDER BY m.ts ASC LIMIT 6""",
        (edge["from_chat_id"], edge["to_target_kind"], edge["to_target"]),
    ).fetchall()
    for r in rows:
        push_msgs.append({
            "row_id": r["row_id"], "ts": r["ts"], "text": r["text"],
            "sender": r["sender_external_id"], "amp": r["amplification_count"],
            "intent": r["intent"],
        })

    # Has the target appeared in OTHER chats too? (Cross-chat = stronger signal)
    cross_chats = conn.execute(
        """SELECT COUNT(DISTINCT m.chat_external_id)
             FROM messages m
             JOIN messages_entities me ON me.message_row_id = m.row_id
             JOIN entities e ON e.row_id = me.entity_row_id
            WHERE e.kind = ? AND e.name = ?""",
        (edge["to_target_kind"], edge["to_target"]),
    ).fetchone()[0]

    return {
        "edge_row_id": edge["row_id"],
        "from_chat_id": edge["from_chat_id"],
        "from_chat_username": edge["from_chat_username"],
        "to_target_kind": edge["to_target_kind"],
        "to_target": edge["to_target"],
        "edge_kind": edge["edge_kind"],
        "bait_intent": edge["bait_intent"],
        "push_count": edge["push_count"],
        "distinct_senders": edge["distinct_senders"],
        "avg_amplification": edge["avg_amplification"],
        "first_seen_ts": edge["first_seen_ts"],
        "last_seen_ts": edge["last_seen_ts"],
        "current_review_state": edge["review_state"],
        "cross_chat_appearances": cross_chats,
        "sample_msg": sample_msg,
        "push_msgs": push_msgs,
    }


def prepare(only_pushes: bool, limit: int) -> Path | None:
    init_db()
    conn = get_connection()
    where = ["review_state IN ('pending','uncertain')"]
    if only_pushes:
        where.append("edge_kind = 'funnel_push'")
    sql = (f"SELECT * FROM funnel_edges WHERE {' AND '.join(where)} "
           f"ORDER BY push_count DESC, avg_amplification DESC LIMIT ?")
    edges = conn.execute(sql, (limit,)).fetchall()

    if not edges:
        log("queue empty — no pending funnel-pushes to review")
        return None

    bundles = [assemble_edge_bundle(conn, e) for e in edges]
    queue_id = now_bkk().strftime("%Y-%m-%dT%H-%M-%S")
    out_path = QUEUE_DIR / f"pending_{queue_id}.json"
    out_path.write_text(
        json.dumps({"queue_id": queue_id, "edges": bundles},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"queue prepared: {len(bundles)} edges → {out_path.name}")
    print(str(out_path), flush=True)
    conn.close()
    return out_path


# ----------------------------------------------------------------------
# Mode B — apply verdict
# ----------------------------------------------------------------------

def apply_verdict(verdict_path: Path) -> None:
    with open(verdict_path, "r", encoding="utf-8") as f:
        v = json.load(f)

    eid = int(v["edge_row_id"])
    state = v.get("review_state")
    if state not in VALID_STATES:
        raise ValueError(f"invalid review_state: {state!r} (must be one of {VALID_STATES})")

    init_db()
    conn = get_connection()
    cur = conn.execute(
        """UPDATE funnel_edges
              SET review_state  = ?,
                  review_verdict = ?,
                  review_reason  = ?,
                  review_at      = ?,
                  review_model   = ?
            WHERE row_id = ?""",
        (state, v.get("review_verdict"), v.get("review_reason"),
         now_bkk().isoformat(timespec="seconds"),
         v.get("review_model", "claude-via-subscription"), eid),
    )
    conn.close()
    if cur.rowcount == 0:
        log(f"WARN: edge_row_id={eid} not found")
    else:
        log(f"applied verdict eid={eid} state={state} verdict={v.get('review_verdict','')[:40]}")


def mark_processed(queue_path: Path) -> Path:
    if not queue_path.exists():
        log(f"queue file missing: {queue_path}")
        return queue_path
    target = DONE_DIR / queue_path.name.replace("pending_", "reviewed_")
    shutil.move(str(queue_path), str(target))
    log(f"moved {queue_path.name} → reviewed/")
    return target


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)

    p_prep = sub.add_parser("prepare", help="emit pending-funnel-push review queue JSON")
    p_prep.add_argument("--include-casual", action="store_true",
                        help="also include casual_mention edges (default: only funnel_push)")
    p_prep.add_argument("--limit", type=int, default=20)

    p_apply = sub.add_parser("apply-verdict", help="UPDATE review state from verdict JSON file")
    p_apply.add_argument("verdict_path")

    p_mark = sub.add_parser("mark-processed", help="move queue file to reviewed/")
    p_mark.add_argument("queue_path")

    p_list = sub.add_parser("list-pending", help="list queue files awaiting verdict")

    args = parser.parse_args()

    if args.mode == "prepare":
        prepare(only_pushes=not args.include_casual, limit=args.limit)
    elif args.mode == "apply-verdict":
        apply_verdict(Path(args.verdict_path))
    elif args.mode == "mark-processed":
        mark_processed(Path(args.queue_path))
    elif args.mode == "list-pending":
        for p in sorted(QUEUE_DIR.glob("pending_*.json")):
            print(p)


if __name__ == "__main__":
    main()
