"""
Funnel-edge inferer (M4.5b).

Reads `messages × messages_entities × entities` joins where entity.kind ∈
{tg_invite, tg_channel_ref, tg_bot_deeplink} and synthesizes directed edges
`from_chat → to_target` into the `funnel_edges` table.

Aggregates per (from_chat_id, target_kind, target):
  - push_count       (number of msgs containing this push)
  - distinct_senders (how many separate senders pushed it — multi-sender
                      = paid campaign or coordinated team; single-sender =
                      possibly one bot)
  - avg_amplification
  - bait_intent      (mode of msg.intent — usually promo/recruit/bait if
                      this is a real funnel push)

Classifies edge_kind:
  - `funnel_push`     — bait_intent ∈ promo/recruit/bait AND avg_amp > 5
                        OR push_count >= 3 (multiple pushes = deliberate)
                        → these become M4.5c review candidates
  - `casual_mention`  — anything else (someone happened to drop a t.me URL)

UPSERT pattern preserves review_state and join_state across rebuilds —
we only refresh the observation columns. State-machine columns are touched
by M4.5c (review) and M4.5d (auto-join) downstream.
"""

from __future__ import annotations

import argparse
import os
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
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))

PUSH_INTENTS = {"promo", "recruit", "bait"}
PUSH_MIN_AMP = float(os.environ.get("FUNNEL_PUSH_MIN_AMP", "5"))
PUSH_MIN_COUNT = int(os.environ.get("FUNNEL_PUSH_MIN_COUNT", "3"))


def now_bkk():
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str):
    line = f"[{now_bkk()}] [funnel] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"funnel_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def classify_edge_kind(bait_intent: str | None, avg_amp: float | None,
                       push_count: int) -> str:
    if bait_intent in PUSH_INTENTS and (avg_amp or 0) >= PUSH_MIN_AMP:
        return "funnel_push"
    if push_count >= PUSH_MIN_COUNT:
        return "funnel_push"
    return "casual_mention"


def rebuild(conn) -> dict:
    """Recompute edges from scratch via single SQL aggregation, UPSERT into table."""
    rows = conn.execute(
        """
        WITH agg AS (
            SELECT
                m.chat_external_id      AS from_chat_id,
                MAX(m.chat_username)    AS from_chat_username,
                m.platform              AS from_platform,
                e.kind                  AS to_target_kind,
                e.name                  AS to_target,
                COUNT(*)                AS push_count,
                COUNT(DISTINCT m.sender_external_id) AS distinct_senders,
                AVG(m.amplification_count) AS avg_amplification,
                MIN(m.ts)               AS first_seen_ts,
                MAX(m.ts)               AS last_seen_ts,
                MIN(m.row_id)           AS sample_msg_row_id
            FROM messages m
            JOIN messages_entities me ON me.message_row_id = m.row_id
            JOIN entities e ON e.row_id = me.entity_row_id
            WHERE e.kind IN ('tg_invite','tg_channel_ref','tg_bot_deeplink')
              AND m.chat_external_id IS NOT NULL
            GROUP BY m.chat_external_id, m.platform, e.kind, e.name
        ),
        agg_intent AS (
            SELECT
                m.chat_external_id, e.kind, e.name,
                m.intent, COUNT(*) cnt
            FROM messages m
            JOIN messages_entities me ON me.message_row_id = m.row_id
            JOIN entities e ON e.row_id = me.entity_row_id
            WHERE e.kind IN ('tg_invite','tg_channel_ref','tg_bot_deeplink')
              AND m.chat_external_id IS NOT NULL
              AND m.intent IS NOT NULL
            GROUP BY m.chat_external_id, e.kind, e.name, m.intent
        ),
        top_intent AS (
            SELECT chat_external_id, kind, name, intent
            FROM agg_intent
            WHERE (chat_external_id, kind, name, cnt) IN (
                SELECT chat_external_id, kind, name, MAX(cnt)
                FROM agg_intent
                GROUP BY chat_external_id, kind, name
            )
        )
        SELECT a.*, t.intent AS bait_intent
        FROM agg a
        LEFT JOIN top_intent t
          ON t.chat_external_id = a.from_chat_id
         AND t.kind = a.to_target_kind
         AND t.name = a.to_target
        """
    ).fetchall()

    upserted = 0
    new_pending = 0
    for r in rows:
        edge_kind = classify_edge_kind(r["bait_intent"], r["avg_amplification"],
                                       r["push_count"])
        # UPSERT — preserve review_state/join_state by NOT writing them on conflict.
        cur = conn.execute(
            """
            INSERT INTO funnel_edges
              (from_chat_id, from_chat_username, from_platform,
               to_target_kind, to_target, edge_kind, bait_intent,
               push_count, distinct_senders, avg_amplification,
               sample_msg_row_id, first_seen_ts, last_seen_ts,
               review_state, join_state)
            VALUES (?,?,?, ?,?, ?,?, ?,?,?, ?,?,?, 'pending', 'not_attempted')
            ON CONFLICT(from_chat_id, to_target_kind, to_target) DO UPDATE SET
              from_chat_username = excluded.from_chat_username,
              from_platform      = excluded.from_platform,
              edge_kind          = excluded.edge_kind,
              bait_intent        = excluded.bait_intent,
              push_count         = excluded.push_count,
              distinct_senders   = excluded.distinct_senders,
              avg_amplification  = excluded.avg_amplification,
              sample_msg_row_id  = excluded.sample_msg_row_id,
              last_seen_ts       = excluded.last_seen_ts
            """,
            (r["from_chat_id"], r["from_chat_username"], r["from_platform"],
             r["to_target_kind"], r["to_target"], edge_kind, r["bait_intent"],
             r["push_count"], r["distinct_senders"], r["avg_amplification"],
             r["sample_msg_row_id"], r["first_seen_ts"], r["last_seen_ts"]),
        )
        upserted += 1

    # Count current state buckets for reporting
    by_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT edge_kind, COUNT(*) FROM funnel_edges GROUP BY edge_kind"
    ).fetchall()}
    by_review = {r[0]: r[1] for r in conn.execute(
        "SELECT review_state, COUNT(*) FROM funnel_edges GROUP BY review_state"
    ).fetchall()}
    by_target_kind = {r[0]: r[1] for r in conn.execute(
        "SELECT to_target_kind, COUNT(*) FROM funnel_edges GROUP BY to_target_kind"
    ).fetchall()}

    return {
        "upserted": upserted,
        "by_edge_kind": by_kind,
        "by_review_state": by_review,
        "by_target_kind": by_target_kind,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="show counts without writing")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    if args.dry_run:
        n = conn.execute(
            """SELECT COUNT(*) FROM (
                 SELECT m.chat_external_id, e.kind, e.name
                   FROM messages m
                   JOIN messages_entities me ON me.message_row_id = m.row_id
                   JOIN entities e ON e.row_id = me.entity_row_id
                  WHERE e.kind IN ('tg_invite','tg_channel_ref','tg_bot_deeplink')
                    AND m.chat_external_id IS NOT NULL
                  GROUP BY m.chat_external_id, e.kind, e.name)"""
        ).fetchone()[0]
        log(f"[dry] would upsert {n} edges")
        return

    log("rebuild start")
    stats = rebuild(conn)
    log(f"upserted={stats['upserted']} by_kind={stats['by_edge_kind']} "
        f"by_review={stats['by_review_state']} by_target_kind={stats['by_target_kind']}")
    conn.close()


if __name__ == "__main__":
    main()
