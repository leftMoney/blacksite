"""
Local Whisper ASR for media voice/video files (M3).

Reads media WHERE media_kind IN ('voice','video','audio') AND transcript IS
NULL, runs faster-whisper on each file, writes transcript + transcript_lang
+ processed_at back to media. Sets processed_at_rules = NULL so the rules-
layer cron picks up the freshly transcribed text and extracts identifier
entities.

Hardware target: GTX 1050 Ti (4GB Pascal, no Tensor Cores). Default
device=cpu / compute=int8 — Pascal CUDA setup on Windows is finicky and
ASR volume is tiny (< 30 voices/day expected). Switch to GPU via
WHISPER_DEVICE=cuda env once boss installs newer GPU; no other changes.

Model: medium (~1.5GB int8). Sweet spot for SEA-5 accuracy (TH/ID/MS/VI/TL).
Roughly 0.5-1× realtime on i5-11400 CPU int8 mode.

ffmpeg: bundled via `imageio-ffmpeg` package; we copy/symlink its binary as
`ffmpeg.exe` in its own dir at import time so faster-whisper's subprocess
call to `ffmpeg` resolves cleanly without a system-wide install.
"""

from __future__ import annotations

import argparse
import os
import shutil
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

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_CPU_FALLBACK = os.environ.get("WHISPER_CPU_FALLBACK", "0").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
WHISPER_CPU_FALLBACK_MODEL = os.environ.get("WHISPER_CPU_FALLBACK_MODEL", "medium")
WHISPER_BEAM = int(os.environ.get("WHISPER_BEAM_SIZE", "5"))


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_bkk().isoformat(timespec='seconds')}] [asr] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"asr_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------
# ffmpeg setup (idempotent, runs at import)
# ----------------------------------------------------------------------

def _ensure_ffmpeg_on_path() -> None:
    """faster-whisper subprocesses 'ffmpeg' by name. Resolve via imageio-ffmpeg
    bundle: copy its binary to 'ffmpeg.exe' in its own dir, prepend dir to PATH.
    Idempotent."""
    try:
        import imageio_ffmpeg
    except ImportError:
        log("imageio-ffmpeg not installed; system ffmpeg required on PATH")
        return
    src = Path(imageio_ffmpeg.get_ffmpeg_exe())
    target_dir = src.parent
    target = target_dir / "ffmpeg.exe"
    if not target.exists() and src.exists() and src.suffix == ".exe":
        try:
            shutil.copy2(src, target)
            log(f"ffmpeg shim created at {target}")
        except Exception as e:
            log(f"ffmpeg shim copy failed: {type(e).__name__}: {e}")
    if str(target_dir) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(target_dir) + os.pathsep + os.environ.get("PATH", "")


_ensure_ffmpeg_on_path()


# ----------------------------------------------------------------------
# Model singleton (avoid 30-90s reload between batch items)
# ----------------------------------------------------------------------

_model = None
_model_name = None
_model_device = None
_model_compute = None


def get_model(
    device: str | None = None,
    compute_type: str | None = None,
    model_name: str | None = None,
):
    global _model, _model_name, _model_device, _model_compute
    target_model = model_name or WHISPER_MODEL
    target_device = device or WHISPER_DEVICE
    target_compute = compute_type or WHISPER_COMPUTE
    if (
        _model is None
        or _model_name != target_model
        or _model_device != target_device
        or _model_compute != target_compute
    ):
        log(f"loading whisper model={target_model} device={target_device} "
            f"compute={target_compute} (first run downloads ~1.5GB if not cached)")
        from faster_whisper import WhisperModel
        t0 = time.time()
        _model = WhisperModel(target_model, device=target_device, compute_type=target_compute)
        _model_name = target_model
        _model_device = target_device
        _model_compute = target_compute
        log(f"  model ready in {time.time()-t0:.1f}s")
    return _model


# ----------------------------------------------------------------------
# Per-file transcription
# ----------------------------------------------------------------------

def has_audio_stream(abs_path: Path) -> bool:
    """Return True iff the file has a decodable audio stream. faster-whisper's
    decode_audio() raises IndexError on video-only mp4s (silent funnel-ad
    slideshows are common in TG bot pumps), so we pre-check via pyav.
    Failures here treated as 'no audio' so we mark + skip cleanly."""
    try:
        import av  # bundled by faster-whisper
        with av.open(str(abs_path)) as container:
            return len(container.streams.audio) > 0
    except Exception:
        return False


def transcribe_file(abs_path: Path) -> tuple[str, str, float]:
    """Returns (full_text, detected_lang, lang_prob).
    Raises ValueError('no_audio') if file has no audio stream — caller
    should treat as 'empty' / no-transcript outcome, not an error."""
    if not has_audio_stream(abs_path):
        raise ValueError("no_audio")
    def _run(model):
        return model.transcribe(
            str(abs_path),
            language=None,           # auto-detect; SEA-5 all well-supported by medium
            beam_size=WHISPER_BEAM,
            vad_filter=True,         # silence-trim front/back so noise doesn't
                                     # produce hallucinated text on dead air
        )

    model = get_model()
    try:
        segments, info = _run(model)
    except RuntimeError as e:
        msg = str(e)
        if WHISPER_DEVICE.lower() != "cpu" and (
            "CUBLAS_STATUS_NOT_SUPPORTED" in msg or "cublas" in msg.lower()
        ):
            if not WHISPER_CPU_FALLBACK:
                log("cuda/asr runtime unsupported; CPU fallback disabled")
                raise
            log(
                "cuda/asr runtime unsupported; "
                f"falling back to cpu/int8 model={WHISPER_CPU_FALLBACK_MODEL}"
            )
            model = get_model("cpu", "int8", WHISPER_CPU_FALLBACK_MODEL)
            segments, info = _run(model)
        else:
            raise
    parts = []
    for s in segments:
        t = (s.text or "").strip()
        if t:
            parts.append(t)
    return " ".join(parts), info.language, float(info.language_probability or 0.0)


# ----------------------------------------------------------------------
# Batch processor
# ----------------------------------------------------------------------

def fetch_batch(conn, limit: int) -> list:
    return conn.execute(
        """SELECT row_id, file_path, file_size, duration_s, media_kind, message_row_id
             FROM media
            WHERE media_kind IN ('voice','video','audio')
              AND (transcript IS NULL OR transcript = '')
            ORDER BY captured_at ASC
            LIMIT ?""",
        (limit,),
    ).fetchall()


def write_result(conn, row_id: int, transcript: str, lang: str | None, prob: float | None) -> None:
    if transcript in ("<NOAUDIO>", "<NOTEXT>") or transcript.startswith("[asr_error:"):
        quality = "exclude"
        note = transcript
    elif prob is not None and prob < 0.65:
        quality = "low_confidence"
        note = f"decoder_lang_prob={prob:.3f}"
    else:
        quality = "pending_audit"
        note = f"decoder_lang_prob={prob:.3f}" if prob is not None else "decoder_lang_prob=null"
    conn.execute(
        """UPDATE media
              SET transcript = ?, transcript_lang = ?, transcript_lang_prob = ?,
                  transcript_quality = ?, transcript_quality_at = ?,
                  transcript_quality_note = ?, processed_at = ?,
                  processed_at_rules = NULL
            WHERE row_id = ?""",
        (
            transcript,
            lang,
            prob,
            quality,
            now_bkk().isoformat(timespec="seconds"),
            note,
            now_bkk().isoformat(timespec="seconds"),
            row_id,
        ),
    )


def process_one(conn, row) -> tuple[str, float]:
    """Returns (status, elapsed_s). status ∈ {ok, empty, missing, error}."""
    abs_path = ROOT / row["file_path"]
    if not abs_path.exists():
        write_result(conn, row["row_id"], "[file_missing]", None, None)
        return "missing", 0.0

    t0 = time.time()
    try:
        text, lang, prob = transcribe_file(abs_path)
    except ValueError as e:
        elapsed = time.time() - t0
        if str(e) == "no_audio":
            # Common case: silent video funnel ads. Mark cleanly, not an error.
            write_result(conn, row["row_id"], "<NOAUDIO>", None, None)
            return "no_audio", elapsed
        log(f"asr value-error row={row['row_id']}: {e}")
        write_result(conn, row["row_id"], f"[asr_error: ValueError]", None, None)
        return "error", elapsed
    except Exception as e:
        elapsed = time.time() - t0
        log(f"asr error row={row['row_id']} {type(e).__name__}: {str(e)[:200]}")
        write_result(conn, row["row_id"], f"[asr_error: {type(e).__name__}]", None, None)
        return "error", elapsed
    elapsed = time.time() - t0

    if not text:
        write_result(conn, row["row_id"], "<NOTEXT>", lang, prob)
        return "empty", elapsed

    write_result(conn, row["row_id"], text, lang, prob)
    return "ok", elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200,
                        help="cap rows processed this run (default: 200)")
    parser.add_argument("--reset-runtime-errors", action="store_true",
                        help="reset prior RuntimeError ASR rows so they can be retried")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    if args.reset_runtime_errors:
        rc = conn.execute(
            """UPDATE media
                  SET transcript = NULL, transcript_lang = NULL, processed_at = NULL
                WHERE media_kind IN ('voice','video','audio')
                  AND transcript LIKE '[asr_error: RuntimeError]%'"""
        ).rowcount
        conn.commit()
        log(f"reset runtime-error ASR rows: {rc}")

    pending = conn.execute(
        "SELECT COUNT(*) FROM media WHERE media_kind IN ('voice','video','audio') "
        "AND (transcript IS NULL OR transcript = '')"
    ).fetchone()[0]

    log(f"start model={WHISPER_MODEL} device={WHISPER_DEVICE}/{WHISPER_COMPUTE} "
        f"cpu_fallback={WHISPER_CPU_FALLBACK} pending={pending} "
        f"limit={args.limit} dry_run={args.dry_run}")

    if args.dry_run or pending == 0:
        log("dry-run / no work; exiting")
        return

    batch = fetch_batch(conn, args.limit)
    stats = {"ok": 0, "empty": 0, "missing": 0, "error": 0, "total_elapsed": 0.0}

    for row in batch:
        status, elapsed = process_one(conn, row)
        stats[status] = stats.get(status, 0) + 1
        stats["total_elapsed"] += elapsed
        log(f"  row={row['row_id']} kind={row['media_kind']} dur={row['duration_s']}s "
            f"status={status} took={elapsed:.1f}s")

    log(f"done {stats}")
    conn.close()


if __name__ == "__main__":
    main()
