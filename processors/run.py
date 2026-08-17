"""
Rules-layer processor — CLI entrypoint.

Reads unprocessed messages from SQLite, runs language detect + intent/topic/
tone classification + identifier extraction + content_hash, writes back.
Recomputes amplification window after batch.

Usage:
  py -m processors.run                    # process all unprocessed (default)
  py -m processors.run --backfill         # alias of default
  py -m processors.run --since 6h         # only msgs ts >= now-6h that are unprocessed
  py -m processors.run --reprocess        # blow away processed_at_rules and redo all
  py -m processors.run --limit 500        # cap batch size
  py -m processors.run --dry-run          # classify but don't write

Designed to run as a 30-min daemon cron OR ad-hoc backfill. Idempotent —
running twice on the same row is a no-op (processed_at_rules is set after
write so the second run skips).
"""

from __future__ import annotations

import argparse
import os
import re
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
from processors.dedupe import content_hash, recompute_amplification
from processors.lang_detect import detect as detect_lang
from processors.rules_layer import classify

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))


def now_bkk() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_bkk()}] [rules] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"rules_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------
# Identifier upsert (entities + messages_entities)
# ----------------------------------------------------------------------

def upsert_identifier_entity(conn, kind: str, name: str, ts: str) -> int:
    """Identifier entities are platform-agnostic (kind+name unique)."""
    cur = conn.execute(
        "SELECT row_id, seen_count FROM entities WHERE kind=? AND platform IS NULL AND name=?",
        (kind, name),
    )
    r = cur.fetchone()
    if r:
        conn.execute(
            "UPDATE entities SET last_seen_ts=?, seen_count=? WHERE row_id=?",
            (ts, r["seen_count"] + 1, r["row_id"]),
        )
        return r["row_id"]
    cur = conn.execute(
        "INSERT INTO entities (kind, platform, name, first_seen_ts, last_seen_ts, seen_count) "
        "VALUES (?,NULL,?,?,?,1)",
        (kind, name, ts, ts),
    )
    return cur.lastrowid


# ----------------------------------------------------------------------
# Time parsing
# ----------------------------------------------------------------------

def parse_since(s: str) -> str:
    """Accepts '6h', '24h', '3d', '2w', '14d' → ISO ts cutoff in Bangkok TZ."""
    m = re.fullmatch(r"(\d+)([hdwm])", s.strip().lower())
    if not m:
        raise ValueError(f"bad --since {s!r}; want e.g. 6h, 24h, 3d")
    n = int(m.group(1))
    unit = m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n),
             "w": timedelta(weeks=n), "m": timedelta(days=30 * n)}[unit]
    return (datetime.now(TZ) - delta).isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# Main batch processor
# ----------------------------------------------------------------------

def process_batch(conn, since_ts: str | None, limit: int | None,
                  reprocess: bool, dry_run: bool) -> dict:
    where = []
    params: list = []
    if not reprocess:
        where.append("processed_at_rules IS NULL")
    if since_ts:
        where.append("ts >= ?")
        params.append(since_ts)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit_sql = f"LIMIT {int(limit)}" if limit else ""

    rows = conn.execute(
        f"""SELECT row_id, platform, persona, ts, text
              FROM messages
              {where_sql}
              ORDER BY ts ASC
              {limit_sql}""",
        params,
    ).fetchall()

    stats = {
        "scanned": len(rows),
        "with_text": 0,
        "intent_set": 0,
        "topic_set": 0,
        "tone_set": 0,
        "hashed": 0,
        "identifiers_extracted": 0,
        "languages": {},
    }

    if not rows:
        return stats

    now = now_bkk()
    for r in rows:
        text = r["text"] or ""
        if not text.strip():
            if not dry_run:
                conn.execute(
                    "UPDATE messages SET processed_at_rules=? WHERE row_id=?",
                    (now, r["row_id"]),
                )
            continue
        stats["with_text"] += 1

        lang = detect_lang(text)
        stats["languages"][lang] = stats["languages"].get(lang, 0) + 1

        cls = classify(text)
        h = content_hash(text)

        if cls["intent"]:
            stats["intent_set"] += 1
        if cls["topic"]:
            stats["topic_set"] += 1
        if cls["tone"]:
            stats["tone_set"] += 1
        if h:
            stats["hashed"] += 1

        if dry_run:
            continue

        conn.execute(
            """UPDATE messages
                  SET intent=?, topic=?, tone=?, lang_detected=?,
                      content_hash=?, processed_at_rules=?
                WHERE row_id=?""",
            (cls["intent"], cls["topic"], cls["tone"], lang, h, now, r["row_id"]),
        )

        # Identifier entities + link rows
        for ident in cls["identifiers"]:
            try:
                eid = upsert_identifier_entity(conn, ident["kind"], ident["name"], r["ts"])
                conn.execute(
                    "INSERT OR IGNORE INTO messages_entities VALUES (?,?,?)",
                    (r["row_id"], eid, "identifier_extracted"),
                )
                stats["identifiers_extracted"] += 1
            except Exception as e:
                log(f"identifier upsert err on row {r['row_id']}: {type(e).__name__}: {e}")

    return stats


# ----------------------------------------------------------------------
# Media OCR rules pass (M2b)
# ----------------------------------------------------------------------

def process_media_batch(conn, limit: int | None, dry_run: bool) -> dict:
    """
    Classify identifiers in media.ocr_text (filled by processors.ocr_gemini).

    Identifier-extraction only — intent/topic/tone classification is skipped
    for OCR'd images at v1; visual-content sentiment is fuzzy and the parent
    message's text already carries that signal. Extracted identifier entities
    link to the parent message via messages_entities (mention_kind='ocr_extracted')
    so the operator graph collapses cleanly across text-msg + image-derived
    evidence.
    """
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    rows = conn.execute(
        f"""SELECT row_id, message_row_id, ocr_text, captured_at
              FROM media
             WHERE ocr_text IS NOT NULL
               AND ocr_text != ''
               AND ocr_text NOT LIKE '[ocr_error:%'
               AND ocr_text NOT LIKE '[file_missing]'
               AND ocr_text NOT LIKE '[read_error:%'
               AND processed_at_rules IS NULL
             ORDER BY captured_at ASC
             {limit_sql}""",
    ).fetchall()

    stats = {"media_scanned": len(rows), "media_identifiers_extracted": 0,
             "media_orphan_skipped": 0}
    if not rows:
        return stats

    from processors.rules_layer import extract_identifiers

    now = now_bkk()
    for r in rows:
        text = r["ocr_text"] or ""
        ids = extract_identifiers(text)

        if dry_run:
            continue

        for ident in ids:
            try:
                eid = upsert_identifier_entity(conn, ident["kind"], ident["name"], r["captured_at"])
                if r["message_row_id"] is None:
                    stats["media_orphan_skipped"] += 1
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO messages_entities VALUES (?,?,?)",
                    (r["message_row_id"], eid, "ocr_extracted"),
                )
                stats["media_identifiers_extracted"] += 1
            except Exception as e:
                log(f"ocr-id err media row {r['row_id']}: {type(e).__name__}: {e}")

        conn.execute(
            "UPDATE media SET processed_at_rules=? WHERE row_id=?",
            (now, r["row_id"]),
        )

    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", help="window e.g. 6h, 24h, 3d (default: all unprocessed)")
    parser.add_argument("--backfill", action="store_true",
                        help="alias of default (process all unprocessed); kept for clarity")
    parser.add_argument("--reprocess", action="store_true",
                        help="redo every row in window even if already classified")
    parser.add_argument("--limit", type=int, help="cap rows processed this run")
    parser.add_argument("--dry-run", action="store_true",
                        help="classify and report but do not write")
    parser.add_argument("--no-amplification", action="store_true",
                        help="skip the amplification recompute step (faster batches)")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    since_ts = parse_since(args.since) if args.since else None
    log(f"start since={since_ts or 'all'} reprocess={args.reprocess} "
        f"limit={args.limit or '∞'} dry_run={args.dry_run}")

    stats = process_batch(conn, since_ts, args.limit, args.reprocess, args.dry_run)
    log(f"classified scanned={stats['scanned']} with_text={stats['with_text']} "
        f"intent={stats['intent_set']} topic={stats['topic_set']} tone={stats['tone_set']} "
        f"hashed={stats['hashed']} ids={stats['identifiers_extracted']}")
    log(f"languages: {stats['languages']}")

    media_stats = process_media_batch(conn, args.limit, args.dry_run)
    log(f"ocr-rules media_scanned={media_stats['media_scanned']} "
        f"ids_linked={media_stats['media_identifiers_extracted']} "
        f"orphans_skipped={media_stats['media_orphan_skipped']}")

    if not args.dry_run and not args.no_amplification and stats["with_text"] > 0:
        amp_rows = recompute_amplification(conn, window_days=7)
        log(f"amplification recompute updated_rows={amp_rows}")

    conn.close()
    log("done")


if __name__ == "__main__":
    main()
