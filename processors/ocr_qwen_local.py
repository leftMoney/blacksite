"""
Local Qwen2.5-VL OCR for media images (M2-local, replaces ocr_gemini.py
when boss says trigger phrase "5070 上路" — see docs/SWITCHOVER_5070.md).

Same I/O contract as ocr_gemini.py:
  - read media WHERE media_kind='photo' AND ocr_text IS NULL AND file_size >= MIN
  - per row: load image bytes → VLM inference → write back ocr_text +
    processed_at + processed_at_rules=NULL
  - CLI: --limit / --model / --dry-run / --backend
  - result codes: ok / empty / missing / error / oom

Backend: Ollama HTTP API (default — easiest Windows install, GGUF auto-mgmt).
Fallback: vLLM HTTP / direct transformers — switch via OCR_LOCAL_BACKEND env.

Speed (5070 Ti 16GB Qwen2.5-VL 7B INT8 via Ollama):
  - 2-5 sec/image typical
  - 800-1500 images/h sustained throughput
  - 2050-photo backfill: ~2-3 hr wallclock (vs Gemini 30+ hr)

VRAM:
  - 7B INT8: ~9 GB → leaves 7 GB for KV + concurrent Whisper large-v3 INT8 (3.5 GB)
  - 7B INT4: ~5 GB → leaves 11 GB (room for 32B INT4 OCR via env override on big jobs)

Switch back to Gemini cloud anytime: OCR_BACKEND=gemini in .env, restart daemon.
"""

from __future__ import annotations

import argparse
import base64
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

# Backend selection — keep parity with ocr_gemini.py env naming where possible
BACKEND = os.environ.get("OCR_LOCAL_BACKEND", "ollama").lower()
MODEL = os.environ.get("OCR_LOCAL_MODEL", "qwen2.5vl:7b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8000")
MIN_FILE_SIZE = int(os.environ.get("GEMINI_OCR_MIN_BYTES", "30000"))  # reuse same threshold
DAILY_CAP = int(os.environ.get("OCR_LOCAL_DAILY_CAP", "100000"))  # huge default — local has no real cap
BATCH_TIMEOUT_S = int(os.environ.get("OCR_LOCAL_PER_IMG_TIMEOUT_S", "120"))
# keep_alive: how long Ollama holds model in VRAM after last request.
# "30s" = within-batch requests stay loaded (~7s gaps), but model unloads
# 30s after batch ends → frees ~14.8 GB for ASR/concurrent jobs.
# Set "5m" for chained light batches; "0" to unload immediately (slow).
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30s")

PROMPT = (
    # 5/6 bench finding: default prompt let qwen2.5vl:7b escape with <NOTEXT>
    # on logo-on-clothing images (CHANEL chest logo, LV monogram shirt) and
    # under-extract on local promo codes. Aggressive prompt forces enumeration
    # of brand logos, monograms, and tiny text. Verified 11/11 vs 7/11 default.
    "Extract ALL visible text from this image — including brand names, "
    "logos (e.g. CHANEL, LOUIS VUITTON, NIKE, GUCCI), monograms, "
    "watermarks, tiny background text, and any letters embroidered or "
    "printed on clothing or accessories. List the text you see verbatim, "
    "preserving original language(s), line breaks, and character order. "
    "If a brand monogram/logo repeats many times (e.g. monogram pattern "
    "on fabric), output the brand name ONCE, not repeatedly. "
    "Never reply <NOTEXT> unless the image truly contains no text or "
    "logo at all (e.g. pure landscape photo, blank scene)."
)


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_bkk().isoformat(timespec='seconds')}] [ocr_local] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"ocr_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def today_count(conn) -> int:
    today = now_bkk().strftime("%Y-%m-%d")
    r = conn.execute(
        "SELECT COUNT(*) FROM media WHERE ocr_text IS NOT NULL AND processed_at LIKE ?",
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


def write_result(conn, row_id: int, ocr_text: str | None) -> None:
    conn.execute(
        "UPDATE media SET ocr_text = ?, processed_at = ?, processed_at_rules = NULL "
        "WHERE row_id = ?",
        (ocr_text, now_bkk().isoformat(timespec="seconds"), row_id),
    )


# ----------------------------------------------------------------------
# Backend: Ollama HTTP API (default)
# ----------------------------------------------------------------------

def call_ollama(img_bytes: bytes, model: str) -> str:
    """POST /api/generate with images=[base64]. Returns OCR text or raises."""
    import requests
    b64 = base64.b64encode(img_bytes).decode("ascii")
    resp = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": model,
            "prompt": PROMPT,
            "images": [b64],
            "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": {"temperature": 0.0, "num_predict": 2048},
        },
        timeout=BATCH_TIMEOUT_S,
    )
    resp.raise_for_status()
    text = (resp.json().get("response") or "").strip()
    return "" if text == "<NOTEXT>" else text


# ----------------------------------------------------------------------
# Backend: vLLM HTTP (OpenAI-compat /v1/chat/completions)
# ----------------------------------------------------------------------

def call_vllm(img_bytes: bytes, model: str) -> str:
    import requests
    b64 = base64.b64encode(img_bytes).decode("ascii")
    resp = requests.post(
        f"{VLLM_URL}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
            "temperature": 0.0,
            "max_tokens": 2048,
        },
        timeout=BATCH_TIMEOUT_S,
    )
    resp.raise_for_status()
    text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    return "" if text == "<NOTEXT>" else text


# ----------------------------------------------------------------------
# Backend: direct transformers (no server, slowest startup)
# ----------------------------------------------------------------------

_hf_pipe = None

def call_hf_direct(img_bytes: bytes, model: str) -> str:
    global _hf_pipe
    if _hf_pipe is None:
        log(f"loading HF Qwen2.5-VL {model} (one-time, ~30-90s)")
        from transformers import pipeline
        _hf_pipe = pipeline("image-text-to-text", model=model, device_map="cuda")
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(img_bytes))
    out = _hf_pipe(img, text=PROMPT, max_new_tokens=2048)
    text = (out[0]["generated_text"][-1]["content"] if isinstance(out, list) else str(out)).strip()
    return "" if text == "<NOTEXT>" else text


def dispatch_call(img_bytes: bytes, model: str) -> str:
    if BACKEND == "ollama":
        return call_ollama(img_bytes, model)
    if BACKEND == "vllm":
        return call_vllm(img_bytes, model)
    if BACKEND in ("hf", "transformers", "direct"):
        return call_hf_direct(img_bytes, model)
    raise RuntimeError(f"unknown OCR_LOCAL_BACKEND={BACKEND!r} (expected: ollama / vllm / hf)")


def process_one(conn, row, model: str) -> str:
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
    try:
        text = dispatch_call(img_bytes, model)
    except Exception as e:
        msg = str(e).lower()
        is_oom = "out of memory" in msg or "cuda oom" in msg or "oom" in msg
        if is_oom:
            log(f"VRAM OOM row={row['row_id']} — abort batch (boss check VRAM headroom)")
            return "oom"
        log(f"ocr error row={row['row_id']} {type(e).__name__}: {str(e)[:200]}")
        write_result(conn, row["row_id"], f"[ocr_error: {type(e).__name__}]")
        return "error"
    # Defense against hallucination loop (5/6 bench saw 305KB / 183KB outputs
    # of "Louis Vuitton" / "LOUIS VUITTON" repeated 12,800× / 7,700× during
    # earlier batch run). Symptom: same 1-3-token phrase >50× and >50% of
    # output tokens. Action: truncate to first occurrence + tag for retry.
    text = _detect_and_truncate_loop(text, row["row_id"])
    write_result(conn, row["row_id"], text)

    # Claude vision fallback for low-confidence qwen outputs (boss 5/7
    # directive: 「我有辦法偵測出來不行的圖用CC看?」). Triggers on:
    # loop / big_notext / big_tiny / hi_repeat. Uses claude.exe OAuth
    # (Pro plan, no API billing). Daily budget cap (default 50/day) via
    # OCR_FALLBACK_DAILY_BUDGET env. See processors/ocr_fallback.py.
    try:
        from processors.ocr_fallback import maybe_fallback
        # sqlite3.Row uses bracket access (no .get()); file_size key always
        # selected by fetch_batch SELECT.
        new_text, signal = maybe_fallback(
            conn, row["row_id"], row["file_path"], row["file_size"], text,
        )
        if new_text is not None:
            log(f"claude_fallback row={row['row_id']} signal={signal} "
                f"qwen_len={len(text)} claude_len={len(new_text)}")
            conn.execute(
                "UPDATE media SET ocr_text = ?, ocr_source = 'claude_fallback', "
                "ocr_confidence_signal = ?, processed_at = ? WHERE row_id = ?",
                (new_text, signal, now_bkk().isoformat(timespec="seconds"),
                 row["row_id"]),
            )
            conn.commit()
            text = new_text
        elif signal:
            log(f"signal_no_fallback row={row['row_id']} signal={signal} "
                f"(budget exhausted or claude failed — see ocr_confidence_signal)")
    except Exception as e:
        log(f"fallback err row={row['row_id']} {type(e).__name__}: {e}")

    return "ok" if text else "empty"


def _detect_and_truncate_loop(text: str, row_id: int) -> str:
    if not text or len(text) < 200:
        return text
    tokens = text.split()
    if len(tokens) < 30:
        return text
    from collections import Counter
    for window in (1, 2, 3):
        if len(tokens) < window * 30:
            continue
        ngrams = [" ".join(tokens[i:i + window]) for i in range(len(tokens) - window + 1)]
        c = Counter(ngrams)
        most_common, freq = c.most_common(1)[0]
        if freq > 50 and freq / len(ngrams) > 0.5:
            log(f"hallucination loop row={row_id} phrase={most_common!r} ×{freq} "
                f"orig_len={len(text)} → truncated")
            return f"{most_common}\n[ocr_loop_truncated: phrase repeated {freq} times]"
    return text


def main() -> None:
    global BACKEND
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="cap rows processed this run")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--backend", default=BACKEND, choices=["ollama", "vllm", "hf"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    BACKEND = args.backend

    init_db()
    conn = get_connection()

    # Ensure ocr_source / ocr_confidence_signal columns (claude fallback path)
    from processors.ocr_fallback import ensure_schema as _ensure_fallback_schema
    _ensure_fallback_schema(conn)

    used_today = today_count(conn)
    remaining = max(0, DAILY_CAP - used_today)
    if args.limit is not None:
        remaining = min(remaining, args.limit)

    pending = conn.execute(
        "SELECT COUNT(*) FROM media WHERE media_kind='photo' AND ocr_text IS NULL "
        "AND (file_size IS NULL OR file_size >= ?)",
        (MIN_FILE_SIZE,),
    ).fetchone()[0]

    log(f"start backend={BACKEND} model={args.model} pending={pending} "
        f"used_today={used_today} remaining={remaining} dry_run={args.dry_run}")

    if args.dry_run or remaining <= 0 or pending == 0:
        return

    stats = {"ok": 0, "empty": 0, "missing": 0, "error": 0, "oom": 0}
    batch = fetch_batch(conn, remaining)
    log(f"processing batch_size={len(batch)}")

    t0 = time.time()
    for i, row in enumerate(batch, 1):
        result = process_one(conn, row, args.model)
        stats[result] = stats.get(result, 0) + 1
        if result == "oom":
            log("aborting batch on OOM")
            break
        if i % 20 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            log(f"  progress {i}/{len(batch)} {stats} rate={rate:.1f} img/s")

    elapsed = time.time() - t0
    log(f"done {stats} elapsed={elapsed:.1f}s avg={elapsed/max(1,sum(stats.values())):.2f}s/img")
    conn.close()


if __name__ == "__main__":
    main()
