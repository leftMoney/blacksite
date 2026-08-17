"""
KB Phase 0 — sample chunk loader (READ-ONLY).

Reads rows from existing v7 `messages` table and emits Document/Chunk JSONL
records per kb/DESIGN.md §3 four-layer abstraction. Output goes to
`instances/<active>/runtime/kb/sample_chunks_v0.jsonl` — never written back
to index.db.

Phase-0 simplifications (deliberate):
  • 1 message → 1 document → 1 chunk (no sub-message chunking yet)
  • signal_score components left NULL — value_gate.py is Phase 1
  • valid_from = event_at; valid_to = NULL (decay rotation is Phase 2)
  • entity list comes from existing `messages_entities` join (no fresh NER)

Per CLAUDE.md §6.4: every datetime carries +07:00 offset.
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

# --- §6.4 canonical datetime pattern -----------------------------------------
TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# Validates ISO 8601 with explicit +HH:MM / -HH:MM suffix (per §6.4)
_OFFSET_RE = re.compile(r"[+-]\d{2}:\d{2}$")


def assert_offset(ts: str | None, label: str) -> None:
    """Self-audit: refuse to emit any timestamp without an explicit offset."""
    if ts is None:
        return
    if not _OFFSET_RE.search(ts):
        raise ValueError(
            f"§6.4 violation: {label}={ts!r} has no +HH:MM offset suffix"
        )


# --- defaults ----------------------------------------------------------------
ACTIVE_INSTANCE = "_TEMPLATE"
OUT_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "kb"
OUT_PATH = OUT_DIR / "sample_chunks_v0.jsonl"


def parse_since(spec: str) -> str:
    """Convert '24h' / '7d' / '30d' / ISO string → ISO 8601 +07:00 cutoff."""
    if not spec:
        return (datetime.now(TZ) - timedelta(days=7)).isoformat(timespec="seconds")
    spec = spec.strip()
    m = re.fullmatch(r"(\d+)([hdwm])", spec)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
            "m": timedelta(days=30 * n),
        }[unit]
        return (datetime.now(TZ) - delta).isoformat(timespec="seconds")
    # Assume already-ISO; sanity-check the offset
    assert_offset(spec, "since")
    return spec


def fetch_messages(conn, since_iso: str, limit: int):
    """Read v7 messages with their joined entities. Read-only SELECT."""
    sql = """
        SELECT m.row_id, m.platform, m.external_id, m.persona, m.ts,
               m.chat_external_id, m.chat_username, m.chat_title,
               m.sender_external_id, m.sender_username, m.sender_name,
               m.text, m.url, m.views, m.reactions_total, m.forwards,
               m.replies, m.score, m.content_hash, m.intent, m.topic,
               m.tone, m.lang_detected, m.amplification_count,
               m.indexed_at, m.raw_path, m.raw_offset
        FROM messages m
        WHERE m.text IS NOT NULL
          AND m.text != ''
          AND m.ts >= ?
        ORDER BY m.ts DESC
        LIMIT ?
    """
    return conn.execute(sql, (since_iso, limit)).fetchall()


def fetch_entities_for_messages(conn, message_row_ids: list[int]) -> dict:
    """Map message_row_id -> [entity records]. Read-only."""
    if not message_row_ids:
        return {}
    placeholders = ",".join("?" * len(message_row_ids))
    sql = f"""
        SELECT me.message_row_id, me.mention_kind,
               e.row_id, e.kind, e.platform, e.name,
               e.tier, e.role
        FROM messages_entities me
        JOIN entities e ON e.row_id = me.entity_row_id
        WHERE me.message_row_id IN ({placeholders})
    """
    out: dict[int, list[dict]] = {}
    for r in conn.execute(sql, message_row_ids).fetchall():
        out.setdefault(r["message_row_id"], []).append(
            {
                "entity_row_id": r["row_id"],
                "kind": r["kind"],
                "platform": r["platform"],
                "name": r["name"],
                "tier": r["tier"],
                "role": r["role"],
                "mention_kind": r["mention_kind"],
            }
        )
    return out


def message_to_chunk(m, entities: list[dict], built_at: str) -> dict:
    """Build a single chunk record per DESIGN §3 / §6.4. Phase 0 = 1 msg → 1 chunk."""
    doc_id = f"msg:{m['row_id']}"
    chunk_id = f"{doc_id}#0"

    # observed_at = when engine ingested (indexed_at preferred, fallback ts).
    # event_at    = when the message was published (m.ts).
    # valid_from  = event_at (Phase-0: facts valid from publish time).
    # valid_to    = NULL (Phase-0: no decay-driven rotation; Phase 2 fills).
    observed_at = m["indexed_at"] or m["ts"]
    event_at = m["ts"]
    valid_from = event_at
    valid_to = None

    for label, val in (
        ("observed_at", observed_at),
        ("event_at", event_at),
        ("valid_from", valid_from),
    ):
        assert_offset(val, label)

    score_signals = {
        # Phase 0: leave components NULL — value_gate.py (Phase 1) populates.
        "entity_density": None,
        "amplification": None,
        "novelty": None,
        "corroboration": None,
        "intent_polarity": None,
        "source_trust": None,
        # Phase-0 raw-amplification proxy (no normalization yet)
        "raw_amplification_count": m["amplification_count"],
    }

    return {
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "chunk_index": 0,
        "text": m["text"],
        "text_len": len(m["text"]),
        # §6.4 KB schema contract — three offset-aware timestamps
        "observed_at": observed_at,
        "event_at": event_at,
        "valid_from": valid_from,
        "valid_to": valid_to,
        # platform context
        "platform": m["platform"],
        "persona": m["persona"],
        "chat": {
            "external_id": m["chat_external_id"],
            "username": m["chat_username"],
            "title": m["chat_title"],
        },
        "sender": {
            "external_id": m["sender_external_id"],
            "username": m["sender_username"],
            "name": m["sender_name"],
        },
        # entities (joined from v7 messages_entities)
        "entities": entities,
        # rules-layer hints (already on v7 messages)
        "rules_hints": {
            "intent": m["intent"],
            "topic": m["topic"],
            "tone": m["tone"],
            "lang_detected": m["lang_detected"],
            "amplification_count": m["amplification_count"],
        },
        "score_signals": score_signals,
        "signal_score": None,           # Phase 1 fills
        "decay_class": "14d",           # default per §9.3 default
        # provenance (DESIGN §4.2 / §10.5)
        "provenance": {
            "source_kind": "message",
            "source_row_id": m["row_id"],
            "external_id": m["external_id"],
            "url": m["url"],
            "content_hash": m["content_hash"],
            "raw_path": m["raw_path"],
            "raw_offset": m["raw_offset"],
        },
        "schema_version": 8,            # matches v8 migration draft
        "loader_built_at": built_at,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="KB Phase 0 sample chunk loader (read-only)."
    )
    p.add_argument("--limit", type=int, default=1000,
                   help="Max chunks to emit (default 1000).")
    p.add_argument("--since", type=str, default="24h",
                   help="Time window: '24h' | '7d' | '30d' | ISO 8601 (default 24h).")
    p.add_argument("--out", type=str, default=str(OUT_PATH),
                   help=f"Output JSONL path (default {OUT_PATH}).")
    args = p.parse_args()

    since_iso = parse_since(args.since)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    built_at = now_iso()
    assert_offset(built_at, "loader_built_at")

    conn = get_connection()
    try:
        rows = fetch_messages(conn, since_iso, args.limit)
        ent_map = fetch_entities_for_messages(conn, [r["row_id"] for r in rows])
    finally:
        conn.close()

    n = 0
    with out_path.open("w", encoding="utf-8") as fp:
        for r in rows:
            chunk = message_to_chunk(r, ent_map.get(r["row_id"], []), built_at)
            fp.write(json.dumps(chunk, ensure_ascii=False))
            fp.write("\n")
            n += 1

    size_bytes = out_path.stat().st_size
    print(json.dumps({
        "built_at": built_at,
        "since": since_iso,
        "limit": args.limit,
        "out_path": str(out_path),
        "chunks_emitted": n,
        "bytes": size_bytes,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
