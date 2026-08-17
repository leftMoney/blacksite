"""promote_to_kb — admit row → KB card (cards table) writer.

Boss directive 2026-05-08: 「整理入庫 = admit row → KB 卡片」

Each kb_admit=1 row in media_kb_decision becomes one row in `cards`:
  - card_kind          = 'media_admit'
  - title              = first 80 chars of Haiku rationale (or first OCR line)
  - body_md            = markdown bundle of (decision_tags, ocr_text snippet,
                         Haiku rationale, Stage 3 commercial_action+pattern if any)
  - decision_tags      = from media_kb_decision.decision_tags
  - actionability_score = kb_value_score / 100.0
  - risk_layer         = derived from decision_tags
  - time_decay_class   = 'perishable' default; 'structural' if regulatory/competitor tag
  - state              = 'active'
  - raw_pointer_json   = {"media_row_id": X, "platform": "...", "file_path": "..."}
  - model_used         = 'promote_to_kb_v1' (so cards from this path identifiable)
  - entity_row_id      = NULL initially (entity-resolution v2 task)

Idempotent: media_kb_decision.promoted_at column tracks which rows already
promoted; re-run skips them.

NOT using LLM compose (token economy — boss 5/8 observation). Cards built
directly from existing structured fields. Future iteration can layer
LLM-rewriting on top via compose_cards_loop pattern.

Usage:
    py -m processors.pipeline.promote_to_kb               # dry-run, prints plan
    py -m processors.pipeline.promote_to_kb --commit
    py -m processors.pipeline.promote_to_kb --commit --limit 100
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from db.connection import get_connection
from db.schema import init_db

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [promote_to_kb] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"promote_to_kb_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------
# Heuristics: tag → risk_layer / time_decay_class
# ----------------------------------------------------------------------

def derive_risk_layer(decision_tags: str | None) -> str:
    if not decision_tags:
        return "none"
    tags = {t.strip().lower() for t in decision_tags.split(",")}
    if {"regulatory", "regulatory_news"} & tags:
        return "regulatory"
    if {"scam_template", "kol_persona", "kol"} & tags:
        return "brand_safety"
    if {"funnel_invite", "competitor", "operator_graph"} & tags:
        return "regulatory"  # competitor funnel = regulator-watched
    return "none"


def derive_time_decay_class(decision_tags: str | None) -> str:
    if not decision_tags:
        return "perishable"
    tags = {t.strip().lower() for t in decision_tags.split(",")}
    if {"regulatory", "regulatory_news", "competitor"} & tags:
        return "structural"
    if {"sports", "kol", "folk-belief"} & tags:
        return "seasonal"
    return "perishable"


def make_title(rationale: str | None, ocr_text: str | None) -> str:
    src = (rationale or "").strip()
    if not src:
        # fallback to first non-empty OCR line
        for line in (ocr_text or "").splitlines():
            line = line.strip()
            if line:
                src = line
                break
    src = src.replace("\n", " ").strip()
    if not src:
        src = "(empty rationale + empty OCR)"
    return src[:80]


def make_body_md(row, stage3) -> str:
    parts = ["## Stage 2 verdict (Haiku)",
             f"- value_class: **{row['kb_value_class']}**",
             f"- value_score: **{row['kb_value_score']}/100**",
             f"- decision_tags: `{row['decision_tags'] or '(none)'}`",
             f"- model: `{row['model_used']}`",
             "",
             "### Rationale",
             (row["rationale"] or "*(empty)*").strip()]

    ocr = (row["ocr_text"] or "").strip()
    if ocr:
        parts += ["", "## OCR text (extracted by upstream Qwen)",
                  "```", ocr[:1500] + ("\n... [truncated]" if len(ocr) > 1500 else ""),
                  "```"]

    if stage3:
        parts += ["", "## Stage 3 strategic interpretation (Sonnet)"]
        if stage3.get("commercial_action"):
            parts += ["", "### Commercial action",
                      stage3["commercial_action"].strip()]
        if stage3.get("cross_case_pattern"):
            parts += ["", "### Cross-case pattern",
                      stage3["cross_case_pattern"].strip()]
        if stage3.get("confidence") is not None:
            parts += ["", f"_Confidence: {stage3['confidence']}_"]

    parts += ["", "---",
              f"*Promoted to KB by promote_to_kb v1 at {now_iso()}; "
              f"source = media_row_id `{row['media_row_id']}`.*"]
    return "\n".join(parts)


# ----------------------------------------------------------------------
# Fetch + insert
# ----------------------------------------------------------------------

def fetch_pending(conn, limit: int) -> list:
    return conn.execute(
        """SELECT d.media_row_id, d.kb_admit, d.kb_value_class, d.kb_value_score,
                  d.decision_tags, d.rationale, d.model_used,
                  m.platform, m.file_path, m.ocr_text
             FROM media_kb_decision d
             JOIN media m ON m.row_id = d.media_row_id
            WHERE d.kb_admit = 1
              AND d.promoted_at IS NULL
         ORDER BY d.kb_value_score DESC, d.media_row_id ASC
            LIMIT ?""",
        (limit,),
    ).fetchall()


def total_pending(conn) -> int:
    r = conn.execute(
        """SELECT COUNT(*) FROM media_kb_decision
            WHERE kb_admit=1 AND promoted_at IS NULL"""
    ).fetchone()
    return r[0] if r else 0


def latest_stage3(conn, media_row_id: int) -> dict | None:
    r = conn.execute(
        """SELECT commercial_action, cross_case_pattern, confidence,
                  related_media_ids, model_used, processed_at
             FROM media_strategic_brief
            WHERE media_row_id=?
         ORDER BY row_id DESC LIMIT 1""",
        (media_row_id,),
    ).fetchone()
    return dict(r) if r else None


def insert_card(conn, row, stage3) -> int:
    """Returns inserted card row_id."""
    title = make_title(row["rationale"], row["ocr_text"])
    body = make_body_md(row, stage3)
    score = row["kb_value_score"] or 0
    actionability = round(score / 100.0, 2)
    risk = derive_risk_layer(row["decision_tags"])
    decay = derive_time_decay_class(row["decision_tags"])
    raw_ptr = {
        "media_row_id": row["media_row_id"],
        "platform": row["platform"],
        "file_path": row["file_path"],
        "stage2_model": row["model_used"],
    }
    if stage3:
        raw_ptr["stage3_model"] = stage3.get("model_used")
        raw_ptr["stage3_at"] = stage3.get("processed_at")

    cur = conn.execute(
        """INSERT INTO cards
           (entity_row_id, card_kind, title, body_md, decision_tags,
            actionability_score, risk_layer, time_decay_class, state,
            evidence_count, first_built_at, last_built_at, last_seen_at,
            raw_pointer_json, model_used)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            None,  # entity_row_id — v2 task to wire
            "media_admit",
            title,
            body,
            row["decision_tags"],
            actionability,
            risk,
            decay,
            "active",
            1,  # one media row = 1 evidence
            now_iso(),
            now_iso(),
            now_iso(),
            json.dumps(raw_ptr, ensure_ascii=False),
            "promote_to_kb_v1",
        ),
    )
    return cur.lastrowid


def mark_promoted(conn, media_row_id: int, card_row_id: int) -> None:
    conn.execute(
        "UPDATE media_kb_decision SET promoted_at=?, promoted_card_row_id=? "
        "WHERE media_row_id=?",
        (now_iso(), card_row_id, media_row_id),
    )


def fetch_refresh_pending(conn, limit: int) -> list:
    """Cards whose raw_pointer_json.media_row_id has a media_strategic_brief
    NEWER than the card's last_built_at — i.e. Stage 3 added a verdict after
    initial card creation, body_md needs refresh."""
    return conn.execute(
        """SELECT c.row_id as card_row_id, c.last_built_at, c.raw_pointer_json,
                  d.media_row_id, d.kb_admit, d.kb_value_class, d.kb_value_score,
                  d.decision_tags, d.rationale, d.model_used,
                  m.platform, m.file_path, m.ocr_text,
                  b.processed_at as stage3_at
             FROM cards c
             JOIN media_strategic_brief b
               ON json_extract(c.raw_pointer_json, '$.media_row_id') = b.media_row_id
             JOIN media_kb_decision d
               ON d.media_row_id = b.media_row_id
             JOIN media m ON m.row_id = d.media_row_id
            WHERE c.card_kind = 'media_admit'
              AND c.model_used = 'promote_to_kb_v1'
              AND b.processed_at > c.last_built_at
         ORDER BY b.processed_at DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()


def total_refresh_pending(conn) -> int:
    r = conn.execute(
        """SELECT COUNT(*)
             FROM cards c
             JOIN media_strategic_brief b
               ON json_extract(c.raw_pointer_json, '$.media_row_id') = b.media_row_id
            WHERE c.card_kind = 'media_admit'
              AND c.model_used = 'promote_to_kb_v1'
              AND b.processed_at > c.last_built_at"""
    ).fetchone()
    return r[0] if r else 0


def refresh_card(conn, row) -> None:
    """Rewrite cards.body_md to include latest Stage 3 commercial_action."""
    stage3 = latest_stage3(conn, row["media_row_id"])
    body = make_body_md(row, stage3)
    conn.execute(
        """UPDATE cards SET body_md=?, last_built_at=?, last_seen_at=?
           WHERE row_id=?""",
        (body, now_iso(), now_iso(), row["card_row_id"]),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=2500)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--refresh-stage3", action="store_true",
                        help="Refresh existing cards whose Stage 3 brief was "
                             "added AFTER initial promotion (body_md update only).")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    if args.refresh_stage3:
        pending = total_refresh_pending(conn)
        log(f"REFRESH-STAGE3 mode: pending={pending} limit={args.limit} commit={args.commit}")
        if pending == 0:
            log("no Stage 3 refreshes pending — done")
            return
        rows = fetch_refresh_pending(conn, args.limit)
        if not args.commit:
            log("DRY-RUN — first 3 cards to refresh:")
            for row in rows[:3]:
                log(f"  card={row['card_row_id']} media={row['media_row_id']} "
                    f"stage3_at={row['stage3_at']} card_built_at={row['last_built_at']}")
            log("DRY-RUN complete — pass --commit to execute")
            conn.close()
            return
        stats = {"refreshed": 0, "error": 0}
        import time
        t0 = time.time()
        for i, row in enumerate(rows, 1):
            try:
                refresh_card(conn, row)
                conn.commit()
                stats["refreshed"] += 1
            except Exception as e:
                stats["error"] += 1
                log(f"  ERR card={row['card_row_id']} media={row['media_row_id']}: "
                    f"{type(e).__name__}: {e}")
            if i % 100 == 0:
                elapsed = time.time() - t0
                log(f"  progress {i}/{len(rows)} {stats} rate={i/max(elapsed,0.001):.1f}/s")
        log(f"DONE {stats} elapsed={time.time()-t0:.1f}s")
        conn.close()
        return

    pending = total_pending(conn)
    log(f"start pending={pending} limit={args.limit} commit={args.commit}")
    if pending == 0:
        log("no pending — done")
        return

    rows = fetch_pending(conn, args.limit)
    log(f"will promote {len(rows)} rows")

    if not args.commit:
        log("DRY-RUN — first 3 rows preview:")
        for row in rows[:3]:
            stage3 = latest_stage3(conn, row["media_row_id"])
            title = make_title(row["rationale"], row["ocr_text"])
            log(f"  media={row['media_row_id']} score={row['kb_value_score']} "
                f"tags={row['decision_tags']} stage3={'yes' if stage3 else 'no'} "
                f"title={title!r}")
        log("DRY-RUN complete — pass --commit to execute")
        conn.close()
        return

    stats = {"inserted": 0, "skipped": 0, "error": 0}
    import time
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        try:
            stage3 = latest_stage3(conn, row["media_row_id"])
            card_id = insert_card(conn, row, stage3)
            mark_promoted(conn, row["media_row_id"], card_id)
            conn.commit()
            stats["inserted"] += 1
        except Exception as e:
            stats["error"] += 1
            log(f"  ERR media={row['media_row_id']}: {type(e).__name__}: {e}")
        if i % 100 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            log(f"  progress {i}/{len(rows)} {stats} rate={rate:.1f} rows/s")

    elapsed = time.time() - t0
    log(f"DONE {stats} elapsed={elapsed:.1f}s")
    conn.close()


if __name__ == "__main__":
    main()
