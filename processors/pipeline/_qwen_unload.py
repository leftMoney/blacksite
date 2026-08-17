"""Qwen model unload helper — explicit force-unload from Ollama VRAM.

Boss directive 2026-05-08: long-running GPU work MUST actively unload after
completion. Ollama `keep_alive=30s` is passive release and not trustworthy —
issue explicit `keep_alive=0` to force the model out of VRAM immediately.

Usage:
    from processors.pipeline._qwen_unload import unload_qwen
    unload_qwen()  # idempotent, fast (sub-second), safe to call always

Or from CLI for ad-hoc cleanup:
    py -m processors.pipeline._qwen_unload
    py -m processors.pipeline._qwen_unload --model qwen2.5vl:32b-iq4_xs
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=7))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("STAGE1_QWEN_MODEL", "qwen2.5vl:7b")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def unload_qwen(model: str = DEFAULT_MODEL, *, log_fn=None) -> dict:
    """Force-unload `model` from Ollama VRAM. Returns status dict.

    Mechanism: POST /api/generate with keep_alive=0 + empty prompt + no
    images. Ollama treats keep_alive=0 as "unload immediately after this
    request returns" (its own docs / source). Fast (no actual inference
    when prompt is empty).

    Idempotent — if model is already unloaded, the call is still cheap.
    """
    import requests
    out = {"model": model, "ok": False, "elapsed_ms": 0, "error": None}
    msg = f"unload_qwen model={model}"
    if log_fn:
        log_fn(msg)
    else:
        print(f"[{now_iso()}] [qwen_unload] {msg}", flush=True)
    import time
    t0 = time.time()
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": "",
                "keep_alive": 0,
                "stream": False,
            },
            timeout=15,
        )
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        out["http"] = resp.status_code
        out["ok"] = resp.ok
        if not resp.ok:
            out["error"] = resp.text[:200]
    except Exception as e:
        out["elapsed_ms"] = int((time.time() - t0) * 1000)
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    msg2 = f"unload_qwen result {out}"
    if log_fn:
        log_fn(msg2)
    else:
        print(f"[{now_iso()}] [qwen_unload] {msg2}", flush=True)
    return out


def list_loaded() -> list:
    """Returns Ollama /api/ps output (currently-loaded models)."""
    import requests
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/ps", timeout=10)
        if resp.ok:
            return resp.json().get("models", [])
    except Exception:
        pass
    return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--all", action="store_true",
                        help="unload all currently-loaded models")
    parser.add_argument("--list", action="store_true",
                        help="just list currently-loaded models")
    args = parser.parse_args()

    if args.list:
        loaded = list_loaded()
        print(f"loaded models ({len(loaded)}):")
        for m in loaded:
            name = m.get("name") or m.get("model") or "?"
            size = m.get("size_vram") or m.get("size") or 0
            size_gb = size / (1024 ** 3)
            print(f"  - {name} ({size_gb:.1f} GB VRAM)")
        return

    if args.all:
        loaded = list_loaded()
        if not loaded:
            print("no models loaded — nothing to do")
            return
        for m in loaded:
            name = m.get("name") or m.get("model")
            if name:
                unload_qwen(name)
        return

    unload_qwen(args.model)


if __name__ == "__main__":
    main()
