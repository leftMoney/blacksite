"""OCR fallback: when qwen2.5vl:7b confidence is low, escalate to Claude vision.

Boss 5/7 directive: 「我有辦法偵測出來不行的圖用CC看?」

Detect 4 low-confidence signals from qwen output and re-OCR via claude.exe (OAuth,
no API billing). Cap daily fallback budget to stay within Pro plan rate limits.

Trigger signals (any one fires fallback):
  loop          — qwen output already truncated by _detect_and_truncate_loop
  big_notext    — file_size > 150KB AND ocr_text = '<NOTEXT>' (likely false negative)
  big_tiny      — file_size > 100KB AND 1 ≤ len(ocr_text) ≤ 9 AND not <NOTEXT>
                  (e.g. CHANEL selfie 100KB → qwen wrote 'D')
  hi_repeat     — single short word makes up >70% of output tokens (sub-loop guard)

Schema (added by migration in this module if missing):
  media.ocr_source           TEXT  — 'qwen' | 'claude_fallback' | 'gemini_fallback'
  media.ocr_confidence_signal TEXT — set if fallback fired (signal name)

Daily budget (default 50 fallbacks/day). Tracked by query of media table where
ocr_source = 'claude_fallback' AND processed_at LIKE today.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TZ = timezone(timedelta(hours=7))
DAILY_BUDGET = int(os.environ.get("OCR_FALLBACK_DAILY_BUDGET", "200"))
# 5/7 calibration: CHANEL selfie 100,824 B (98.5 KB) was just under the
# 100 KB threshold and missed; lowering to 50 KB covers all real photos
# while excluding thumbnails / stickers / TG UI sprites (typically <30 KB).
BIG_FILE_THRESHOLD = int(os.environ.get("OCR_FALLBACK_BIG_KB", "100")) * 1024
TINY_FILE_THRESHOLD = int(os.environ.get("OCR_FALLBACK_TINY_KB", "50")) * 1024
TINY_LEN_THRESHOLD = int(os.environ.get("OCR_FALLBACK_TINY_LEN", "9"))
REPEAT_RATIO = float(os.environ.get("OCR_FALLBACK_REPEAT_RATIO", "0.7"))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# ---------------------------------------------------------------
# Schema bootstrap (idempotent)
# ---------------------------------------------------------------

def ensure_schema(conn) -> None:
    """Add ocr_source / ocr_confidence_signal columns if missing."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(media)").fetchall()}
    if "ocr_source" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN ocr_source TEXT")
    if "ocr_confidence_signal" not in cols:
        conn.execute("ALTER TABLE media ADD COLUMN ocr_confidence_signal TEXT")
    conn.commit()


# ---------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------

def compute_signal(file_size: int | None, ocr_text: str | None) -> str | None:
    """Return signal name if low-confidence, else None.

    Order of checks matches priority (cheapest signal first).
    """
    if not ocr_text:
        return None  # NULL ocr_text = not yet processed; not our domain

    # 1. loop — already detected by _detect_and_truncate_loop in ocr_qwen_local
    if "[ocr_loop_truncated:" in ocr_text:
        return "loop"

    # File-size based — skip if we don't know size
    if file_size is None or file_size <= 0:
        return None

    text_stripped = ocr_text.strip()

    # 2. big_notext — substantial image but qwen says no text
    if file_size > BIG_FILE_THRESHOLD and text_stripped == "<NOTEXT>":
        return "big_notext"

    # 3. big_tiny — substantial image with very short non-empty output
    if (file_size > TINY_FILE_THRESHOLD
            and 1 <= len(text_stripped) <= TINY_LEN_THRESHOLD
            and text_stripped != "<NOTEXT>"):
        return "big_tiny"

    # 4. hi_repeat — sub-loop pattern below _detect_and_truncate_loop threshold
    tokens = text_stripped.split()
    if len(tokens) >= 30:
        from collections import Counter
        c = Counter(tokens)
        most, freq = c.most_common(1)[0]
        if len(most) <= 30 and freq / len(tokens) > REPEAT_RATIO:
            return "hi_repeat"

    return None


# ---------------------------------------------------------------
# Daily budget
# ---------------------------------------------------------------

def today_fallback_count(conn) -> int:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    r = conn.execute(
        "SELECT COUNT(*) FROM media WHERE ocr_source = 'claude_fallback' "
        "AND processed_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    return r[0] if r else 0


# ---------------------------------------------------------------
# Claude vision fallback
# ---------------------------------------------------------------

CLAUDE_PROMPT = (
    # Production fallback prompt — different goal from benchmark prompt:
    # we WANT comprehensive extraction including brand logos because qwen
    # already failed and we're using Claude as the safety net.
    # NOT used for any benchmark — only when qwen output is suspect.
    "Read the image at this absolute path: {image_path}\n\n"
    "List every piece of visible text in the image. Include: "
    "regular text on signs / screens / posters / packaging; "
    "brand names and logos (printed or embroidered, e.g. on clothing, "
    "bags, watches); monograms and watermarks; tiny background text; "
    "captions and labels. Capture any language — the target country's local "
    "script, English, Chinese, regional scripts, digits.\n\n"
    "Output the raw extracted text verbatim, one item per line, "
    "preserving original characters. No commentary, no explanation, "
    "no markdown, no headers.\n\n"
    "If a brand monogram pattern repeats many times across the image "
    "(e.g. logo print on fabric), output the brand name ONCE, not "
    "repeatedly.\n\n"
    "If the image truly has no text or brand at all (e.g. a pure "
    "landscape, an abstract pattern), output exactly: <NOTEXT>"
)


def claude_fallback_ocr(image_abs_path: str) -> str | None:
    """Spawn claude.exe via _llm_synth.claude_run with image read access.

    Returns extracted text, or None on error. claude.exe uses OAuth (Pro plan,
    no API billing per CLAUDE.md §6 + reference_setup_token_oauth_lifecycle).
    """
    try:
        from processors._llm_synth import claude_run
        ok, raw = claude_run(
            task=CLAUDE_PROMPT.format(image_path=image_abs_path),
            skill_prefix=False,
            allowed_tools="Read",
            permission_mode="default",
            timeout_s=120.0,
            max_retries=2,
        )
        if not ok or not raw:
            return None
        # claude.exe sometimes wraps in extra prose; strip leading/trailing chatter.
        text = raw.strip()
        # If output starts with a sentence about the image, take the last block
        # (heuristic — Claude usually outputs raw text per our prompt; but if
        # it adds boilerplate, our heuristic below still extracts).
        return text
    except Exception:
        return None


# ---------------------------------------------------------------
# Public entry point — called from ocr_qwen_local.process_one
# ---------------------------------------------------------------

def maybe_fallback(conn, row_id: int, file_path: str, file_size: int | None,
                   ocr_text: str | None) -> tuple[str | None, str | None]:
    """If qwen output is suspect AND budget remains, re-OCR via Claude.

    Returns (new_ocr_text or None, signal or None). Caller should:
      - if new_ocr_text returned: write it back + set ocr_source='claude_fallback'
      - else: leave qwen output, optionally tag confidence_signal for visibility

    Caller is responsible for ensure_schema(conn) on its first run.
    """
    signal = compute_signal(file_size, ocr_text)
    if not signal:
        return None, None

    used = today_fallback_count(conn)
    if used >= DAILY_BUDGET:
        # Budget exhausted — record signal but no fallback
        conn.execute(
            "UPDATE media SET ocr_confidence_signal = ? WHERE row_id = ?",
            (f"{signal}_budget_exhausted", row_id),
        )
        conn.commit()
        return None, signal

    abs_path = ROOT / file_path
    if not abs_path.exists():
        return None, signal

    new_text = claude_fallback_ocr(str(abs_path).replace("\\", "/"))
    if new_text is None:
        # Claude failed — keep qwen output, mark signal
        conn.execute(
            "UPDATE media SET ocr_confidence_signal = ? WHERE row_id = ?",
            (f"{signal}_claude_failed", row_id),
        )
        conn.commit()
        return None, signal

    return new_text, signal


if __name__ == "__main__":
    # Manual probe: show today's fallback usage + signal stats
    from db.connection import get_connection
    conn = get_connection()
    ensure_schema(conn)
    used = today_fallback_count(conn)
    print(f"Today's fallback usage: {used}/{DAILY_BUDGET}")
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT ocr_confidence_signal, COUNT(*) FROM media "
        "WHERE processed_at LIKE ? AND ocr_confidence_signal IS NOT NULL "
        "GROUP BY ocr_confidence_signal",
        (f"{today}%",),
    ).fetchall()
    for sig, n in rows:
        print(f"  {sig}: {n}")
