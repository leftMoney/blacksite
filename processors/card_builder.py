"""
Card synthesis layer (M4) — split design.

Two Python modes (this file) + ONE Claude scheduled task (separate, registered
via mcp__scheduled-tasks__create_scheduled_task) work together:

  * --prepare-queue   : Python; selects candidate entities, assembles 7-dim
                        evidence bundles, writes JSON to runtime/cards/queue/
                        Pure SQL work — deterministic, fast, no LLM call.

  * --apply-card      : Python; takes a card JSON (composed by the scheduled
                        Claude session) and UPSERTs into `cards` table.

  * --mark-processed  : Python; moves a queue file from queue/ → built/ once
                        all its cards have been applied.

The Claude scheduled task ("blacksite-cards-build") wakes every 4h (Bangkok
local), reads the latest pending queue file, composes a card for each bundle
per the 7-dim schema, writes them back via --apply-card. Runs inside the
user's Claude Code subscription — no per-token API charge.

Why split: Python is deterministic + cheap for evidence prep; Claude (Opus/
Sonnet via subscription) brings the reasoning + Traditional-Chinese prose
quality. Each side does what it's best at.

Cards carry the 7 dimensions from the 2026-04-28 design:
  D1 Entity        — name, kind, platform (subject of the card)
  D2 Relations     — top co-occurring entities (operator graph slice)
  D3 Temporal      — first/last seen, peak window, dormancy class
  D4 Semantic      — intent/topic/tone profile of msgs mentioning it
  D5 Amplification — avg / max amp on msgs containing it
  D6 Provenance    — raw_pointer_json + model_used columns
  D7 Decision      — decision_tags / actionability_score / risk_layer / time_decay_class
"""

from __future__ import annotations

# 2026-05-14: legacy Claude Code scheduled task `blacksite-cards-build` is
# disabled. Active M4 card synthesis is daemon -> scripts/compose_cards_loop.py
# -> this prepare/apply helper. Do not recreate the Claude scheduled task
# unless the boss explicitly moves M4 back to Claude Code subscription scheduling.

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
QUEUE_DIR = RUNTIME_DIR / "cards" / "queue"
BUILT_DIR = RUNTIME_DIR / "cards" / "built"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)
BUILT_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

CARD_FRESH_HOURS = int(os.environ.get("CARD_FRESH_HOURS", "4"))   # legacy fallback
CARD_MAX_PER_RUN = int(os.environ.get("CARD_MAX_PER_RUN", "20"))

# M5 — refresh frequency keyed by card.time_decay_class. Saves token spend
# on stable structural entities (KOLs, regulators) by refreshing them
# only every 24h, while perishable entities (one-shot promo bursts) get
# rebuilt every 4h. Falls back to legacy CARD_FRESH_HOURS when class unset.
CARD_REFRESH_BY_CLASS = {
    "perishable": int(os.environ.get("CARD_REFRESH_PERISHABLE_H", "4")),
    "seasonal":   int(os.environ.get("CARD_REFRESH_SEASONAL_H",   "12")),
    "structural": int(os.environ.get("CARD_REFRESH_STRUCTURAL_H", "24")),
}

DOMAIN_NOISE = {
    "t.co", "t.me", "bit.ly", "tinyurl.com", "goo.gl", "ow.ly",
    "youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com",
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "telegram.org", "telegram.me",
}


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_bkk().isoformat(timespec='seconds')}] [card] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"card_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------
# Candidate selection
# ----------------------------------------------------------------------

def select_candidates(conn, max_n: int = CARD_MAX_PER_RUN) -> list[dict]:
    # M5: skip dormant/superseded/noise/contradicted entities. Decay cron
    # transitions inactive entities out; manual `noise`/`contradicted`
    # remain excluded permanently.
    rows = conn.execute(
        """
        SELECT row_id, kind, name, platform, seen_count, role, tier,
               first_seen_ts, last_seen_ts
          FROM entities
         WHERE kind IN ('channel','user','domain','brand')
           AND seen_count >= 5
           AND state = 'active'
        """
    ).fetchall()

    out = []
    for r in rows:
        if r["kind"] == "domain" and r["name"] in DOMAIN_NOISE:
            continue
        out.append(dict(r))
    out.sort(key=lambda x: x["seen_count"], reverse=True)
    return out[:max_n]


def card_is_fresh(conn, entity_row_id: int, card_kind: str = "entity_brief") -> bool:
    """M5: freshness threshold differs by card.time_decay_class.
    perishable=4h, seasonal=12h, structural=24h. No card → not fresh."""
    r = conn.execute(
        """SELECT last_built_at, time_decay_class FROM cards
           WHERE entity_row_id = ? AND card_kind = ?""",
        (entity_row_id, card_kind),
    ).fetchone()
    if not r:
        return False
    try:
        last_built = datetime.fromisoformat(r["last_built_at"])
    except Exception:
        return False
    threshold_h = CARD_REFRESH_BY_CLASS.get(r["time_decay_class"]) or CARD_FRESH_HOURS
    return (now_bkk() - last_built) < timedelta(hours=threshold_h)


# ----------------------------------------------------------------------
# Evidence assembler
# ----------------------------------------------------------------------

def assemble_evidence(conn, entity: dict) -> dict:
    eid = entity["row_id"]

    msgs = conn.execute(
        """
        SELECT m.row_id, m.platform, m.ts, m.text, m.views, m.forwards,
               m.replies, m.reactions_total, m.amplification_count,
               m.intent, m.topic, m.tone, m.lang_detected, m.chat_username,
               m.sender_username
          FROM messages m
          JOIN messages_entities me ON me.message_row_id = m.row_id
         WHERE me.entity_row_id = ?
         ORDER BY COALESCE(m.amplification_count, 1) DESC, m.ts DESC
         LIMIT 10
        """,
        (eid,),
    ).fetchall()

    cooccurs = conn.execute(
        """
        SELECT e2.kind, e2.name, e2.platform, e2.seen_count,
               COUNT(*) co_msgs
          FROM messages_entities me1
          JOIN messages_entities me2 ON me2.message_row_id = me1.message_row_id
          JOIN entities e2 ON e2.row_id = me2.entity_row_id
         WHERE me1.entity_row_id = ?
           AND e2.row_id != ?
           AND e2.kind IN ('channel','user','domain','brand','phone','lineid','promo','wallet')
         GROUP BY e2.row_id
         ORDER BY co_msgs DESC
         LIMIT 12
        """,
        (eid, eid),
    ).fetchall()

    intents = conn.execute(
        """SELECT m.intent, COUNT(*) FROM messages m
           JOIN messages_entities me ON me.message_row_id = m.row_id
           WHERE me.entity_row_id = ? AND m.intent IS NOT NULL
           GROUP BY m.intent ORDER BY 2 DESC""",
        (eid,),
    ).fetchall()
    topics = conn.execute(
        """SELECT m.topic, COUNT(*) FROM messages m
           JOIN messages_entities me ON me.message_row_id = m.row_id
           WHERE me.entity_row_id = ? AND m.topic IS NOT NULL
           GROUP BY m.topic ORDER BY 2 DESC""",
        (eid,),
    ).fetchall()
    tones = conn.execute(
        """SELECT m.tone, COUNT(*) FROM messages m
           JOIN messages_entities me ON me.message_row_id = m.row_id
           WHERE me.entity_row_id = ? AND m.tone IS NOT NULL
           GROUP BY m.tone ORDER BY 2 DESC""",
        (eid,),
    ).fetchall()

    amp = conn.execute(
        """SELECT AVG(m.amplification_count), MAX(m.amplification_count),
                  COUNT(DISTINCT m.chat_external_id) AS distinct_chats,
                  COUNT(DISTINCT m.sender_external_id) AS distinct_senders
             FROM messages m
             JOIN messages_entities me ON me.message_row_id = m.row_id
            WHERE me.entity_row_id = ?
              AND m.amplification_count IS NOT NULL""",
        (eid,),
    ).fetchone()

    return {
        "entity_row_id": eid,
        "entity": {
            "kind": entity["kind"],
            "name": entity["name"],
            "platform": entity["platform"],
            "seen_count": entity["seen_count"],
            "role": entity["role"],
            "tier": entity["tier"],
            "first_seen_ts": entity["first_seen_ts"],
            "last_seen_ts": entity["last_seen_ts"],
        },
        "amplification": {
            "avg": round(amp[0], 1) if amp and amp[0] is not None else None,
            "max": amp[1] if amp else None,
            "distinct_chats": amp[2] if amp else None,
            "distinct_senders": amp[3] if amp else None,
        },
        "intent_distribution": {r[0]: r[1] for r in intents},
        "topic_distribution": {r[0]: r[1] for r in topics},
        "tone_distribution": {r[0]: r[1] for r in tones},
        "cooccurring_entities": [
            {"kind": r["kind"], "name": r["name"], "platform": r["platform"],
             "co_msgs": r["co_msgs"], "seen_count": r["seen_count"]}
            for r in cooccurs
        ],
        "sample_messages": [
            {
                "row_id": r["row_id"],
                "platform": r["platform"],
                "ts": r["ts"],
                "text": (r["text"] or "")[:280],
                "amp": r["amplification_count"],
                "views": r["views"], "forwards": r["forwards"], "replies": r["replies"],
                "intent": r["intent"], "topic": r["topic"], "tone": r["tone"],
                "chat": r["chat_username"],
            }
            for r in msgs
        ],
    }


# ----------------------------------------------------------------------
# Mode A — prepare queue (Python only)
# ----------------------------------------------------------------------

def prepare_queue(limit: int, force: bool) -> Path:
    init_db()
    conn = get_connection()
    candidates = select_candidates(conn, max_n=limit)
    bundles = []
    for ent in candidates:
        if not force and card_is_fresh(conn, ent["row_id"]):
            continue
        bundles.append(assemble_evidence(conn, ent))
    conn.close()

    queue_id = now_bkk().strftime("%Y-%m-%dT%H-%M-%S")
    out_path = QUEUE_DIR / f"pending_{queue_id}.json"
    out_path.write_text(
        json.dumps({"queue_id": queue_id, "candidates": bundles},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"queue prepared: {len(bundles)} bundles → {out_path.name}")
    print(str(out_path), flush=True)
    return out_path


# ----------------------------------------------------------------------
# Mode B — apply a single composed card (Python writes to SQLite)
# ----------------------------------------------------------------------

REQUIRED_CARD_FIELDS = ("entity_row_id", "title", "body_md")
DECISION_TAG_VALUES = {
    "TA_acquisition", "funnel_competitor_intel", "regulatory_weather",
    "KOL_safety_audit", "payment_behavior", "folk-belief_x_lottery_overlap",
    "brand_seed_pulse", "operator_graph", "bot_pump_noise_filter",
}
RISK_LAYER_VALUES = {"regulatory", "brand_safety", "persona_burn", "none"}
DECAY_CLASS_VALUES = {"perishable", "seasonal", "structural"}


def validate_card(card: dict) -> None:
    for f in REQUIRED_CARD_FIELDS:
        if f not in card or card[f] in (None, ""):
            raise ValueError(f"missing/empty required field: {f}")
    if "decision_tags" in card and card["decision_tags"]:
        for tag in [t.strip() for t in card["decision_tags"].split(",")]:
            if tag and tag not in DECISION_TAG_VALUES:
                raise ValueError(f"invalid decision_tag: {tag!r}")
    if "risk_layer" in card and card["risk_layer"] not in RISK_LAYER_VALUES:
        raise ValueError(f"invalid risk_layer: {card['risk_layer']!r}")
    if "time_decay_class" in card and card["time_decay_class"] not in DECAY_CLASS_VALUES:
        raise ValueError(f"invalid time_decay_class: {card['time_decay_class']!r}")
    if "actionability_score" in card and card["actionability_score"] is not None:
        s = float(card["actionability_score"])
        if not 0.0 <= s <= 1.0:
            raise ValueError(f"actionability_score out of range: {s}")


def apply_card(card: dict, model_used: str = "claude-opus-via-subscription",
               raw_pointer: dict | None = None) -> int:
    validate_card(card)
    init_db()
    conn = get_connection()
    now = now_bkk().isoformat(timespec="seconds")
    eid = int(card["entity_row_id"])
    pointer = json.dumps(raw_pointer or {}, ensure_ascii=False)
    evidence_count = card.get("evidence_count", 0)

    existing = conn.execute(
        "SELECT row_id, first_built_at FROM cards WHERE entity_row_id=? AND card_kind=?",
        (eid, card.get("card_kind", "entity_brief")),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE cards SET title=?, body_md=?, decision_tags=?,
                                actionability_score=?, risk_layer=?, time_decay_class=?,
                                state='active', evidence_count=?,
                                last_built_at=?, last_seen_at=?, raw_pointer_json=?,
                                model_used=?
                 WHERE row_id=?""",
            (card["title"], card["body_md"], card.get("decision_tags"),
             card.get("actionability_score"), card.get("risk_layer"),
             card.get("time_decay_class"), evidence_count,
             now, now, pointer, model_used, existing["row_id"]),
        )
        rid = existing["row_id"]
    else:
        cur = conn.execute(
            """INSERT INTO cards (entity_row_id, card_kind, title, body_md,
                                  decision_tags, actionability_score, risk_layer,
                                  time_decay_class, state, evidence_count,
                                  first_built_at, last_built_at, last_seen_at,
                                  raw_pointer_json, model_used)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, card.get("card_kind", "entity_brief"), card["title"], card["body_md"],
             card.get("decision_tags"), card.get("actionability_score"),
             card.get("risk_layer"), card.get("time_decay_class"),
             "active", evidence_count, now, now, now, pointer, model_used),
        )
        rid = cur.lastrowid
    conn.close()
    return rid


def mark_processed(queue_path: Path) -> Path:
    if not queue_path.exists():
        log(f"queue file missing: {queue_path}")
        return queue_path
    target = BUILT_DIR / queue_path.name.replace("pending_", "built_")
    shutil.move(str(queue_path), str(target))
    log(f"moved {queue_path.name} → built/")
    return target


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)

    p_prep = sub.add_parser("prepare-queue", help="select candidates + write evidence JSON")
    p_prep.add_argument("--limit", type=int, default=CARD_MAX_PER_RUN)
    p_prep.add_argument("--force", action="store_true",
                        help="ignore freshness check; emit all candidates")

    p_apply = sub.add_parser("apply-card", help="UPSERT a composed card from JSON file")
    p_apply.add_argument("card_path", help="path to JSON file with card content")
    p_apply.add_argument("--model-used", default="claude-opus-via-subscription")

    p_mark = sub.add_parser("mark-processed", help="move a queue file to built/")
    p_mark.add_argument("queue_path", help="path to queue/pending_*.json")

    p_list = sub.add_parser("list-pending", help="show queue/*.json files awaiting compose")

    args = parser.parse_args()

    if args.mode == "prepare-queue":
        prepare_queue(args.limit, args.force)
    elif args.mode == "apply-card":
        with open(args.card_path, "r", encoding="utf-8") as f:
            card = json.load(f)
        rid = apply_card(card, model_used=args.model_used,
                         raw_pointer=card.get("raw_pointer"))
        log(f"applied card row_id={rid} entity_row_id={card['entity_row_id']}")
    elif args.mode == "mark-processed":
        mark_processed(Path(args.queue_path))
    elif args.mode == "list-pending":
        for p in sorted(QUEUE_DIR.glob("pending_*.json")):
            print(p)


if __name__ == "__main__":
    main()
