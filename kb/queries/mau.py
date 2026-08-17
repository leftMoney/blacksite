"""
KB Phase 0 — MAU (Monthly Active Users) query (READ-ONLY).

Computes 30-day MAU for a given entity = count of distinct senders that
mentioned this entity within a rolling time window. Reads existing v7
`messages` × `messages_entities` tables; never writes.

Used by Manager Pack §1 Header (DESIGN §21.3 / §21.4) — boss-facing KPI
signal showing how many distinct accounts are talking about a target
operator/KOL/brand in the last N days.

Per CLAUDE.md §6.4: every datetime carries +07:00 offset.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402

# §6.4 canonical pattern
TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def resolve_entity(conn, entity_id: str):
    """Accept either int row_id or string entity name. Read-only SELECT."""
    if entity_id.isdigit():
        row = conn.execute(
            "SELECT row_id, kind, name, tier, role, platform "
            "FROM entities WHERE row_id = ?",
            (int(entity_id),),
        ).fetchone()
        if row:
            return row
    # Fallback: name-based lookup, prefer brand-kind > others
    row = conn.execute(
        "SELECT row_id, kind, name, tier, role, platform FROM entities "
        "WHERE name = ? "
        "ORDER BY CASE kind WHEN 'brand' THEN 1 WHEN 'channel' THEN 2 "
        "                  WHEN 'domain' THEN 3 ELSE 4 END "
        "LIMIT 1",
        (entity_id,),
    ).fetchone()
    return row


def compute_mau(conn, entity_row_id: int, days: int, as_of: datetime):
    """COUNT(DISTINCT sender_external_id) over last `days`. Read-only."""
    cutoff = (as_of - timedelta(days=days)).isoformat(timespec="seconds")
    sql = """
        SELECT COUNT(DISTINCT m.sender_external_id) AS mau,
               COUNT(*) AS mention_count,
               MIN(m.ts) AS earliest_ts,
               MAX(m.ts) AS latest_ts
        FROM messages_entities me
        JOIN messages m ON m.row_id = me.message_row_id
        WHERE me.entity_row_id = ?
          AND m.ts >= ?
          AND m.sender_external_id IS NOT NULL
    """
    return conn.execute(sql, (entity_row_id, cutoff)).fetchone()


def main() -> int:
    p = argparse.ArgumentParser(
        description="KB Phase 0 MAU query — distinct senders mentioning an entity."
    )
    p.add_argument("entity", type=str,
                   help="Entity row_id (int) or canonical name (e.g. 'examplebet').")
    p.add_argument("--days", type=int, default=30,
                   help="Rolling window in days (default 30).")
    p.add_argument("--fallback-days", type=int, default=7,
                   help="Fallback window if MAU=0 in primary window (default 7).")
    args = p.parse_args()

    t0 = time.time()
    conn = get_connection()
    try:
        ent = resolve_entity(conn, args.entity)
        if not ent:
            print(json.dumps({"error": f"entity not found: {args.entity}"}),
                  file=sys.stderr)
            return 2

        as_of = datetime.now(TZ)
        primary = compute_mau(conn, ent["row_id"], args.days, as_of)
        used_window = args.days
        result = primary

        # Phase-0 fallback per task spec: if 30d returns zero in 5-day-old DB,
        # fall back to 7d and document the fact.
        fallback_used = False
        if primary["mau"] == 0 and args.fallback_days < args.days:
            fb = compute_mau(conn, ent["row_id"], args.fallback_days, as_of)
            if fb["mau"] > 0:
                result = fb
                used_window = args.fallback_days
                fallback_used = True

        runtime_ms = int((time.time() - t0) * 1000)

        out = {
            "entity_id": ent["row_id"],
            "entity_name": ent["name"],
            "entity_kind": ent["kind"],
            "entity_tier": ent["tier"],
            "entity_role": ent["role"],
            "window_days": used_window,
            "mau": result["mau"],
            "mention_count": result["mention_count"],
            "earliest_mention_ts": result["earliest_ts"],
            "latest_mention_ts": result["latest_ts"],
            "as_of": as_of.isoformat(timespec="seconds"),
            "fallback_window_used": fallback_used,
            "runtime_ms": runtime_ms,
        }
        print(json.dumps(out, ensure_ascii=False))
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
