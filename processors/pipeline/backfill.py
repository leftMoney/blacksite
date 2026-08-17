"""P9 — Backfill photos with no pipeline verdict yet (i.e. weren't part of
the 5/7 Opus re-audit).

Per CLAUDE.md §2.1: only photos that flow through Stage 1 first then Stage 2
get pipeline verdicts. Pre-5/8 photos that were already re-audited by 5/7
are NOT re-processed (their legacy verdict at media_kb_decision.model_used =
opus_default_via_claude_exe_2026_05_07 stays as-is).

Loop:
  1. SELECT next chunk of photos WHERE media_signal_filter IS NULL AND
     media_kb_decision IS NULL.
  2. Call Stage 1 (Qwen local) on chunk.
  3. Call Stage 2 (Haiku OAuth) on the new signals up to daily budget.
  4. Sleep briefly between chunks; loop until no more pending OR --max-rows hit.

Resume-safe — restartable any time. Each row commits independently.

Usage:
    py -m processors.pipeline.backfill --max-rows 3000 --chunk 100
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from db.connection import get_connection
from db.schema import init_db
from processors.pipeline import stage1_qwen_filter as s1
from processors.pipeline import stage2_haiku_precision as s2

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [backfill] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"backfill_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch_backfill_chunk(conn, limit: int, min_size: int) -> list:
    """Photos not yet in media_signal_filter AND not yet in media_kb_decision."""
    return conn.execute(
        """SELECT m.row_id, m.file_path, m.file_size, m.ocr_text
             FROM media m
        LEFT JOIN media_signal_filter sf ON sf.media_row_id = m.row_id
        LEFT JOIN media_kb_decision  kd ON kd.media_row_id = m.row_id
            WHERE m.media_kind = 'photo'
              AND sf.media_row_id IS NULL
              AND kd.media_row_id IS NULL
              AND (m.file_size IS NULL OR m.file_size >= ?)
         ORDER BY m.row_id ASC
            LIMIT ?""",
        (min_size, limit),
    ).fetchall()


def total_backfill_pending(conn, min_size: int) -> int:
    return conn.execute(
        """SELECT COUNT(*)
             FROM media m
        LEFT JOIN media_signal_filter sf ON sf.media_row_id = m.row_id
        LEFT JOIN media_kb_decision  kd ON kd.media_row_id = m.row_id
            WHERE m.media_kind = 'photo'
              AND sf.media_row_id IS NULL
              AND kd.media_row_id IS NULL
              AND (m.file_size IS NULL OR m.file_size >= ?)""",
        (min_size,),
    ).fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-rows", type=int, default=3000,
                        help="cap total rows processed this run")
    parser.add_argument("--chunk", type=int, default=100,
                        help="rows per Stage 1 sub-batch")
    parser.add_argument("--stop-at", type=str,
                        help="ISO timestamp to stop at (e.g. 2026-05-08T22:00)")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    token = os.environ.get("ANTHROPIC_OAUTH_TOKEN", "")
    if not token:
        log("ABORT: ANTHROPIC_OAUTH_TOKEN missing")
        sys.exit(1)

    pending_at_start = total_backfill_pending(conn, s1.MIN_FILE_SIZE)
    log(f"start max_rows={args.max_rows} chunk={args.chunk} "
        f"pending={pending_at_start}")

    stop_at = None
    if args.stop_at:
        try:
            stop_at = datetime.fromisoformat(args.stop_at)
            if stop_at.tzinfo is None:
                stop_at = stop_at.replace(tzinfo=TZ)
            log(f"will stop by {stop_at.isoformat()}")
        except Exception as e:
            log(f"bad --stop-at: {e}")

    overall_stats = {"s1_signal": 0, "s1_noise": 0, "s1_error": 0,
                     "s2_admit": 0, "s2_reject": 0, "s2_error": 0,
                     "rows_processed": 0}
    t0 = time.time()
    while overall_stats["rows_processed"] < args.max_rows:
        if stop_at and datetime.now(TZ) >= stop_at:
            log(f"stop_at reached, breaking")
            break

        remaining = args.max_rows - overall_stats["rows_processed"]
        chunk_size = min(args.chunk, remaining)
        rows = fetch_backfill_chunk(conn, chunk_size, s1.MIN_FILE_SIZE)
        if not rows:
            log("no more pending — done")
            break

        # Stage 1 on chunk
        s1_results = []
        for row in rows:
            res = s1.process_one(conn, row, s1.MODEL)
            s1_results.append((row["row_id"], res))
            if res == "signal":
                overall_stats["s1_signal"] += 1
            elif res == "noise":
                overall_stats["s1_noise"] += 1
            else:
                overall_stats["s1_error"] += 1
            overall_stats["rows_processed"] += 1

        # Stage 2 on signals from this chunk only
        # (re-fetch with the relation join so we have full context)
        signal_ids = [rid for rid, res in s1_results if res == "signal"]
        if signal_ids:
            placeholders = ",".join("?" * len(signal_ids))
            s2_rows = conn.execute(
                f"""SELECT s.media_row_id, s.verdict, s.confidence,
                          m.file_path, m.file_size, m.ocr_text
                     FROM media_signal_filter s
                     JOIN media m ON m.row_id = s.media_row_id
                LEFT JOIN media_kb_decision d ON d.media_row_id = s.media_row_id
                    WHERE s.media_row_id IN ({placeholders})
                      AND s.verdict = 'signal'
                      AND d.media_row_id IS NULL""",
                signal_ids,
            ).fetchall()
            for row in s2_rows:
                res = s2.process_one(conn, row, token)
                if res == "admit":
                    overall_stats["s2_admit"] += 1
                elif res == "reject":
                    overall_stats["s2_reject"] += 1
                else:
                    overall_stats["s2_error"] += 1

        elapsed = time.time() - t0
        rate = overall_stats["rows_processed"] / elapsed if elapsed else 0
        pending = total_backfill_pending(conn, s1.MIN_FILE_SIZE)
        log(f"chunk done. processed={overall_stats['rows_processed']}/{args.max_rows} "
            f"pending={pending} rate={rate:.2f} img/s "
            f"stats={overall_stats}")

    elapsed = time.time() - t0
    log(f"DONE elapsed={elapsed:.1f}s "
        f"({elapsed/3600:.2f}h) final_stats={overall_stats}")
    conn.close()

    # Boss directive 2026-05-08: long-running GPU runs MUST explicit-unload
    # the Qwen model from VRAM at end. Ollama keep_alive=30s would do it
    # passively but is not trustworthy.
    try:
        from processors.pipeline._qwen_unload import unload_qwen
        unload_qwen(s1.MODEL, log_fn=log)
    except Exception as e:
        log(f"unload_qwen failed (non-fatal): {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
