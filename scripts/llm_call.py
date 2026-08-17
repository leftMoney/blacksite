"""
Generic LLM dispatcher — Commander M7.2 派工窗口.

Boss directive 5/8: Commander 應能自己選 model 跑 task。三個 tier:

  --model 7b      Qwen 2.5VL 7B via Ollama (free, ~3-8s, 0 計費)
                  Use for: noise filter, 視覺辨識, 大量低風險判斷
  --model haiku   Claude Haiku 4.5 via OAuth Bearer + api.anthropic.com
                  Use for: 精準結構化判斷, 中量精準, 中等成本
  --model sonnet  Claude Sonnet 4.6 via claude.exe host OAuth path
                  Use for: 策略解讀, cross-case pattern, 高層判斷

Usage:
  py scripts/llm_call.py --model 7b "<prompt>"
  py scripts/llm_call.py --model haiku "<prompt>"
  py scripts/llm_call.py --model sonnet "<prompt>"
  echo "<long prompt>" | py scripts/llm_call.py --model sonnet --stdin
  py scripts/llm_call.py --model haiku --image path/to/img.jpg "describe"

Stdout: just the text response (so Commander can pipe / capture).
Stderr: log + errors. Exit 0 on success, 1 on failure.

Per-request safety caps:
  - prompt length ≤ 50_000 chars (Commander 不會灌整本 KB)
  - response max_tokens 4096 (boss 要更大手動 --max-tokens)
  - timeout 120s default

🔴 Audit: every call writes one row to system_history (kind=metric scope=commander_llm)
so boss can grep cost/usage trends.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))
from processors import llm_profiles  # noqa: E402
from processors.claude_auth import current_oauth_access_token, is_claude_auth_error  # noqa: E402
from processors.llm_router import (  # noqa: E402
    codex_model_for_tier,
    fallback_provider,
    run_codex,
    selected_provider,
    should_try_codex,
    should_use_claude_fallback,
)
TZ = timezone(timedelta(hours=7))

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Local Ollama vision model: prefer LLM_CALL_QWEN_MODEL, then OCR_LOCAL_MODEL
# (same model used by Stage 1 noise filter), then YAML `local.vision` if a
# `local` provider is registered. No hardcoded fallback past that.
QWEN_MODEL_TAG = (
    os.environ.get("LLM_CALL_QWEN_MODEL")
    or os.environ.get("OCR_LOCAL_MODEL")
    or llm_profiles.tier_model("local", "vision")
)
# Haiku full id (API endpoint requires full id, not alias)
HAIKU_MODEL_ID = (
    os.environ.get("LLM_CALL_HAIKU_MODEL")
    or llm_profiles.tier_model("claude", "fast")
)
# Sonnet alias for claude.exe --model flag (alias is shorter in logs)
SONNET_MODEL_ALIAS = (
    os.environ.get("LLM_CALL_SONNET_MODEL")
    or llm_profiles.tier_model_for_claude_exe("claude", "strategic")
)

PROMPT_MAX_CHARS = int(os.environ.get("LLM_CALL_PROMPT_MAX", "50000"))
DEFAULT_MAX_TOKENS = int(os.environ.get("LLM_CALL_MAX_TOKENS", "4096"))
DEFAULT_TIMEOUT_S = int(os.environ.get("LLM_CALL_TIMEOUT_S", "120"))


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _err(msg: str) -> None:
    print(f"[llm_call {_now_iso()}] {msg}", file=sys.stderr, flush=True)


def _audit(model: str, prompt_len: int, response_len: int,
           duration_ms: int, success: bool, error: str | None = None) -> None:
    """Log one usage row to system_history. Soft-fail."""
    try:
        sys.path.insert(0, str(ROOT))
        from processors.history_log import log_event
        title = (f"llm_call model={model} prompt={prompt_len}c "
                 f"resp={response_len}c {duration_ms}ms "
                 f"{'ok' if success else 'fail'}")
        body = (f"caller=commander_or_main\nmodel={model}\nprompt_chars={prompt_len}\n"
                f"response_chars={response_len}\nduration_ms={duration_ms}\n"
                f"success={success}\n")
        if error:
            body += f"error={error[:300]}\n"
        log_event(actor="commander", kind="metric", scope="commander_llm",
                  title=title, body=body)
    except Exception as e:
        _err(f"audit log fail: {type(e).__name__}: {e}")


def _read_image(path: str) -> tuple[bytes, str]:
    """Return (bytes, mime_type)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"image not found: {path}")
    data = p.read_bytes()
    suffix = p.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp",
                ".gif": "image/gif"}
    return data, mime_map.get(suffix, "image/jpeg")


# ---------------------------------------------------------------------
# 7b — Ollama local
# ---------------------------------------------------------------------

def call_qwen(prompt: str, image_path: str | None,
              max_tokens: int, timeout_s: int) -> str:
    """POST Ollama /api/generate. Returns response text."""
    body: dict = {
        "model": QWEN_MODEL_TAG,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30s",  # CLAUDE.md §5070 VRAM mgmt rule
        "options": {"num_predict": max_tokens},
    }
    if image_path:
        img_bytes, _mime = _read_image(image_path)
        body["images"] = [base64.b64encode(img_bytes).decode("ascii")]

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read())
    return data.get("response", "").strip()


# ---------------------------------------------------------------------
# haiku — direct OAuth Bearer + api.anthropic.com
# ---------------------------------------------------------------------

def call_haiku(prompt: str, image_path: str | None,
               max_tokens: int, timeout_s: int) -> str:
    token = current_oauth_access_token()
    if not token:
        raise RuntimeError("Claude OAuth access token unavailable")

    content: list[dict] = []
    if image_path:
        img_bytes, mime = _read_image(image_path)
        content.append({
            "type": "image",
            "source": {"type": "base64",
                       "media_type": mime,
                       "data": base64.b64encode(img_bytes).decode("ascii")},
        })
    content.append({"type": "text", "text": prompt})

    body = {
        "model": HAIKU_MODEL_ID,
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": content}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read())
    parts = data.get("content", [])
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()


def call_fast(prompt: str, image_path: str | None,
              max_tokens: int, timeout_s: int) -> str:
    provider = selected_provider()
    if should_try_codex("fast"):
        res = run_codex(
            prompt,
            tier="fast",
            model=codex_model_for_tier("fast"),
            image_path=image_path,
            timeout_s=timeout_s,
        )
        if res.ok:
            return res.text
        if provider == "codex" or not should_use_claude_fallback():
            raise RuntimeError(res.error or "codex fast failed")
        _err(f"codex fast failed, falling back to haiku: {res.error}")
    try:
        return call_haiku(prompt, image_path, max_tokens, timeout_s)
    except Exception as e:
        if fallback_provider() == "codex" and is_claude_auth_error(e):
            _err(f"haiku auth failed, trying codex fallback: {e}")
            res = run_codex(
                prompt,
                tier="fast",
                model=codex_model_for_tier("fast"),
                image_path=image_path,
                timeout_s=timeout_s,
            )
            if res.ok:
                return res.text
            _err(f"codex fallback failed: {res.error}")
        raise


# ---------------------------------------------------------------------
# sonnet — claude.exe host OAuth path (per reference_engine_llm_paths.md)
# ---------------------------------------------------------------------

def call_sonnet(prompt: str, image_path: str | None,
                max_tokens: int, timeout_s: int) -> str:
    """Use processors._llm_synth.claude_run with pass_model_flag=True.
    Image path embedded in prompt as 「@<path>」 marker — claude.exe Read tool
    can fetch it. Pure-text prompts work directly."""
    if image_path:
        prompt = f"{prompt}\n\n附圖: @{image_path} (請用 Read tool 讀)"
    sys.path.insert(0, str(ROOT))
    from processors._llm_synth import claude_run
    success, out = claude_run(
        task=prompt,
        skill_prefix=False,
        extra_system="",
        allowed_tools="Read",
        permission_mode="bypassPermissions",
        model=SONNET_MODEL_ALIAS,
        pass_model_flag=True,
        timeout_s=float(timeout_s),
        max_retries=2,
    )
    if not success:
        raise RuntimeError(f"claude_run sonnet failed: {out[:300]}")
    return out.strip()


def call_strategic(prompt: str, image_path: str | None,
                   max_tokens: int, timeout_s: int) -> str:
    provider = selected_provider()
    if should_try_codex("strategic"):
        res = run_codex(
            prompt,
            tier="strategic",
            model=codex_model_for_tier("strategic"),
            image_path=image_path,
            timeout_s=timeout_s,
        )
        if res.ok:
            return res.text
        if provider == "codex" or not should_use_claude_fallback():
            raise RuntimeError(res.error or "codex strategic failed")
        _err(f"codex strategic failed, falling back to sonnet: {res.error}")
    try:
        return call_sonnet(prompt, image_path, max_tokens, timeout_s)
    except Exception as e:
        if fallback_provider() == "codex" and is_claude_auth_error(e):
            _err(f"sonnet auth failed, trying codex fallback: {e}")
            res = run_codex(
                prompt,
                tier="strategic",
                model=codex_model_for_tier("strategic"),
                image_path=image_path,
                timeout_s=timeout_s,
            )
            if res.ok:
                return res.text
            _err(f"codex fallback failed: {res.error}")
        raise


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Generic LLM dispatcher (Commander M7.2)")
    p.add_argument("--model", required=True, choices=["7b", "haiku", "sonnet"],
                   help="model tier — see docstring for selection guideline")
    p.add_argument("prompt", nargs="?", default=None,
                   help="prompt text (positional). If omitted, --stdin or fail.")
    p.add_argument("--stdin", action="store_true",
                   help="read prompt from stdin instead of positional arg")
    p.add_argument("--image", default=None, metavar="PATH",
                   help="optional image path for vision models (7b/haiku/sonnet)")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                   help=f"response max tokens (default {DEFAULT_MAX_TOKENS})")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                   help=f"per-request timeout in seconds (default {DEFAULT_TIMEOUT_S})")
    args = p.parse_args()

    # Prompt source
    if args.stdin:
        prompt = sys.stdin.read()
    elif args.prompt:
        prompt = args.prompt
    else:
        _err("no prompt given (positional or --stdin)")
        return 2

    prompt = prompt.strip()
    if not prompt:
        _err("empty prompt")
        return 2
    if len(prompt) > PROMPT_MAX_CHARS:
        _err(f"prompt {len(prompt)}c exceeds cap {PROMPT_MAX_CHARS}c — "
             f"split or set LLM_CALL_PROMPT_MAX env")
        return 2

    # Dispatch
    t0 = time.time()
    response = ""
    error = None
    try:
        if args.model == "7b":
            response = call_qwen(prompt, args.image, args.max_tokens, args.timeout)
        elif args.model == "haiku":
            response = call_fast(prompt, args.image, args.max_tokens, args.timeout)
        elif args.model == "sonnet":
            response = call_strategic(prompt, args.image, args.max_tokens, args.timeout)
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)[:300]}"
        _err(f"call failed: {error}")
    duration_ms = int((time.time() - t0) * 1000)

    _audit(args.model, len(prompt), len(response), duration_ms,
           success=bool(response and not error), error=error)

    if error:
        return 1
    if not response:
        _err("empty response")
        return 1

    print(response, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
