"""
Gemini Flash 2.0 batch OCR for media images (M2).

Reads `SELECT * FROM media WHERE media_kind='photo' AND ocr_text IS NULL
AND file_size > 30000`, sends each image to the Gemini API, writes back
ocr_text + processed_at on the media row. Sets media.processed_at_rules =
NULL so the rules-layer cron picks up the freshly OCR'd content (M2b).

Self-throttle to GEMINI_OCR_DAILY_CAP (default 1500) calls/day, sized to
match Gemini Flash 2.0's free-tier RPD even though our key sits on a paid
project — boss directive 2026-04-29: keep behavior free-tier-equivalent so
spend stays trivial regardless of paid tier.

Quota tracking: COUNT(*) of media WHERE ocr_text IS NOT NULL AND processed_at
LIKE 'YYYY-MM-DD%' (Bangkok local date). When today's count + this run's
count would exceed cap, exit clean — tomorrow's cron resumes.

Run sequentially. Latency 1-3s/image, full daily 1500 = 25-75 min wall clock.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
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

DAILY_CAP = int(os.environ.get("GEMINI_OCR_DAILY_CAP", "1000"))
# gemini-2.5-flash-lite: cheapest multimodal Gemini, free-tier 1000 RPD,
# strong text-in-image extraction in TH/VI/ID/MS/TL. Override via env if
# OCR quality on a particular language seems weak.
MODEL = os.environ.get("GEMINI_OCR_MODEL", "gemini-2.5-flash-lite")
MIN_FILE_SIZE = int(os.environ.get("GEMINI_OCR_MIN_BYTES", "30000"))

# OCR-only prompt — no analysis, preserves all script systems including local.
PROMPT = (
    "Extract ALL visible text from this image. Output ONLY the raw extracted "
    "text — no commentary, no formatting, no headers, no labels. Preserve "
    "original language(s), original line breaks, and original character order. "
    "If the image contains no readable text, output exactly: <NOTEXT>"
)


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_bkk().isoformat(timespec='seconds')}] [ocr] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"ocr_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def today_count(conn) -> int:
    today = now_bkk().strftime("%Y-%m-%d")
    r = conn.execute(
        "SELECT COUNT(*) FROM media "
        "WHERE ocr_text IS NOT NULL AND processed_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    return r[0] if r else 0


def fetch_batch(conn, limit: int) -> list:
    return conn.execute(
        """SELECT row_id, file_path, file_size, mime_type, message_row_id
             FROM media
            WHERE media_kind = 'photo'
              AND ocr_text IS NULL
              AND (file_size IS NULL OR file_size >= ?)
            ORDER BY captured_at ASC
            LIMIT ?""",
        (MIN_FILE_SIZE, limit),
    ).fetchall()


def guess_mime(file_path: str, fallback: str | None) -> str:
    if fallback:
        return fallback
    p = file_path.lower()
    if p.endswith(".jpg") or p.endswith(".jpeg"):
        return "image/jpeg"
    if p.endswith(".png"):
        return "image/png"
    if p.endswith(".webp"):
        return "image/webp"
    if p.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"


def write_result(conn, row_id: int, ocr_text: str | None) -> None:
    conn.execute(
        "UPDATE media SET ocr_text = ?, processed_at = ?, processed_at_rules = NULL "
        "WHERE row_id = ?",
        (ocr_text, now_bkk().isoformat(timespec="seconds"), row_id),
    )


def call_gemini(client, model: str, img_bytes: bytes, mime: str) -> str:
    """Single call. Returns OCR text (may be empty). Raises on API error."""
    from google.genai import types as gtypes
    resp = client.models.generate_content(
        model=model,
        contents=[
            gtypes.Part.from_bytes(data=img_bytes, mime_type=mime),
            PROMPT,
        ],
    )
    text = (resp.text or "").strip()
    if text == "<NOTEXT>":
        return ""
    return text


def process_one(client, conn, row, model: str) -> str:
    """Returns: 'ok' | 'empty' | 'missing' | 'error' | 'rate_limited'."""
    abs_path = ROOT / row["file_path"]
    if not abs_path.exists():
        write_result(conn, row["row_id"], "[file_missing]")
        return "missing"

    try:
        img_bytes = abs_path.read_bytes()
    except Exception as e:
        log(f"read fail row={row['row_id']} {type(e).__name__}: {e}")
        write_result(conn, row["row_id"], f"[read_error: {type(e).__name__}]")
        return "error"

    mime = guess_mime(row["file_path"], row["mime_type"])

    try:
        text = call_gemini(client, model, img_bytes, mime)
    except Exception as e:
        msg = str(e)
        is_rate = "RESOURCE_EXHAUSTED" in msg or "429" in msg or "rate limit" in msg.lower()
        if is_rate:
            log(f"rate-limited row={row['row_id']}; backing off 60s")
            time.sleep(60)
            try:
                text = call_gemini(client, model, img_bytes, mime)
            except Exception as e2:
                log(f"rate-limited again row={row['row_id']}: {type(e2).__name__}; aborting batch")
                return "rate_limited"
        else:
            time.sleep(2)
            try:
                text = call_gemini(client, model, img_bytes, mime)
            except Exception as e2:
                log(f"ocr error row={row['row_id']} {type(e2).__name__}: {str(e2)[:200]}")
                write_result(conn, row["row_id"], f"[ocr_error: {type(e2).__name__}]")
                return "error"

    write_result(conn, row["row_id"], text)
    return "ok" if text else "empty"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int,
                        help="cap rows processed this run (default: remaining daily quota)")
    parser.add_argument("--model", default=MODEL,
                        help=f"Gemini model (default: {MODEL})")
    parser.add_argument("--dry-run", action="store_true",
                        help="show counts without calling API")
    parser.add_argument("--no-cap", action="store_true",
                        help="disable daily-cap throttle (use carefully)")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log("GEMINI_API_KEY missing in env — abort")
        sys.exit(2)

    init_db()
    conn = get_connection()

    used_today = today_count(conn)
    remaining = max(0, DAILY_CAP - used_today) if not args.no_cap else 10**9
    if args.limit is not None:
        remaining = min(remaining, args.limit)

    pending = conn.execute(
        "SELECT COUNT(*) FROM media WHERE media_kind='photo' AND ocr_text IS NULL "
        "AND (file_size IS NULL OR file_size >= ?)",
        (MIN_FILE_SIZE,),
    ).fetchone()[0]

    log(f"start model={args.model} cap={DAILY_CAP} used_today={used_today} "
        f"remaining_quota={remaining} pending_photos={pending} dry_run={args.dry_run}")

    if args.dry_run:
        log("dry-run done")
        return
    if remaining <= 0:
        log("daily cap reached; exiting clean")
        return
    if pending == 0:
        log("no photos pending OCR; exiting")
        return

    from google import genai
    client = genai.Client(api_key=api_key)

    stats = {"ok": 0, "empty": 0, "missing": 0, "error": 0, "rate_limited": 0}
    batch = fetch_batch(conn, remaining)
    log(f"processing batch_size={len(batch)}")

    for row in batch:
        result = process_one(client, conn, row, args.model)
        stats[result] = stats.get(result, 0) + 1
        if result == "rate_limited":
            log("aborting batch on rate-limit")
            break
        # Light pacing — sequential is fine; small sleep is friendly.
        time.sleep(0.1)

    log(f"done {stats}")
    conn.close()


if __name__ == "__main__":
    main()
