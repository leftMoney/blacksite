"""Provider switch for Blacksite LLM calls.

This module keeps subscription-backed model paths swappable:

  BLACKSITE_LLM_PROVIDER=claude | codex | auto

`claude` preserves the existing Claude Code subscription path. `codex` uses
`codex exec`, which consumes the signed-in Codex/ChatGPT subscription session
instead of an OpenAI API key. `auto` tries Codex first, then lets the caller
fall back to its legacy Claude implementation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from processors import llm_profiles  # noqa: E402  (after load_dotenv so env is hot)

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

PROVIDER_ENV = "BLACKSITE_LLM_PROVIDER"
FALLBACK_PROVIDER_ENV = "BLACKSITE_LLM_FALLBACK_PROVIDER"

# Aliased tier names used by Stage 2 / Stage 3 callers — they map to the
# canonical fast/strategic tiers in config/llm_providers.yaml.
_TIER_ALIASES = {"stage2": "fast", "stage3": "strategic"}


def _canonical_tier(tier: str) -> str:
    return _TIER_ALIASES.get(tier, tier)

_CODEX_EXE_CACHE: str | None = None

CODEX_ENV_STRIP_PREFIXES = (
    "OPENAI_",
    "AZURE_OPENAI_",
    "ANTHROPIC_",
)
CODEX_ENV_STRIP_KEYS = {
    "GEMINI_API_KEY",
}


@dataclass
class LLMResult:
    ok: bool
    text: str = ""
    provider: str = ""
    model: str = ""
    duration_ms: int = 0
    error: str | None = None
    usage: dict | None = None
    raw: dict | None = None

    def meta(self) -> dict:
        out = {
            "_provider": self.provider,
            "_model": self.model,
            "_duration_ms": self.duration_ms,
        }
        if self.error:
            out["_error"] = self.error
        if self.usage:
            out["_usage"] = self.usage
        if self.raw:
            out["_raw"] = self.raw
        return out


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [llm_router] {msg}"
    print(line, flush=True, file=sys.stderr)
    with (LOG_DIR / f"llm_router_{datetime.now(TZ).strftime('%Y-%m-%d')}.log").open(
        "a", encoding="utf-8"
    ) as f:
        f.write(line + "\n")


def selected_provider() -> str:
    """Active provider per env / YAML default. Always returns a registered name."""
    raw = (os.environ.get(PROVIDER_ENV) or "").strip().lower()
    if raw == "auto":
        return "auto"
    if raw in llm_profiles.list_providers():
        return raw
    return llm_profiles.default_provider()


def fallback_provider() -> str:
    """Optional provider used only after the active provider fails."""
    raw = (os.environ.get(FALLBACK_PROVIDER_ENV) or "").strip().lower()
    if not raw or raw == "none":
        return ""
    if raw in llm_profiles.list_providers() and raw != selected_provider():
        return raw
    return ""


def should_try_codex_fallback() -> bool:
    return fallback_provider() == "codex"


def codex_model_for_tier(tier: str) -> str:
    """Resolve the model id `codex.exe -m` should use for this tier.

    Always resolves against the `codex` profile (it would be wrong to send
    a Claude model id to codex.exe just because BLACKSITE_LLM_<TIER>
    currently holds a Claude value — those env vars track the *active*
    provider, which may not be codex).

    Override: set BLACKSITE_LLM_CODEX_<TIER> to a specific id to bypass YAML.
    """
    canonical = _canonical_tier(tier)
    explicit = os.environ.get(f"BLACKSITE_LLM_CODEX_{canonical.upper()}")
    if explicit:
        return explicit
    return llm_profiles.tier_model("codex", canonical)


def codex_login_status(timeout_s: int = 15) -> LLMResult:
    codex_exe = find_codex_exe()
    if not codex_exe:
        return LLMResult(
            ok=False,
            provider="codex",
            error="codex.exe not found on PATH",
        )
    t0 = time.time()
    spawn_env = clean_codex_env()
    try:
        proc = subprocess.run(
            [codex_exe, "login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=str(ROOT),
            env=spawn_env,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
        )
    except Exception as e:
        return LLMResult(
            ok=False,
            provider="codex",
            duration_ms=int((time.time() - t0) * 1000),
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    ok = proc.returncode == 0 and "not logged in" not in text.lower()
    return LLMResult(
        ok=ok,
        text=text,
        provider="codex",
        duration_ms=int((time.time() - t0) * 1000),
        error=None if ok else text[:300] or f"rc={proc.returncode}",
    )


def find_codex_exe() -> str | None:
    """Locate the Windows executable, not the extensionless shim.

    WindowsApps exposes both `codex` and `codex.exe`. Background CreateProcess
    can misinterpret the extensionless file and raise the classic "unsupported
    16-bit application" dialog. Always prefer `codex.exe`.
    """
    global _CODEX_EXE_CACHE
    if _CODEX_EXE_CACHE:
        return _CODEX_EXE_CACHE
    explicit = os.environ.get("CODEX_EXE")
    if explicit and Path(explicit).is_file():
        _CODEX_EXE_CACHE = explicit
        return _CODEX_EXE_CACHE
    if os.name == "nt":
        local_appdata = Path(
            os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        )
        for candidate in (
            local_appdata / "OpenAI" / "Codex" / "bin" / "codex.exe",
            local_appdata / "Microsoft" / "WinGet" / "Links" / "codex.exe",
        ):
            if candidate.is_file():
                _CODEX_EXE_CACHE = str(candidate)
                return _CODEX_EXE_CACHE
    found = shutil.which("codex.exe")
    if found:
        _CODEX_EXE_CACHE = found
        return _CODEX_EXE_CACHE
    found = shutil.which("codex")
    if found and found.lower().endswith(".exe"):
        _CODEX_EXE_CACHE = found
        return _CODEX_EXE_CACHE
    return None


def codex_available(timeout_s: int = 15) -> bool:
    return codex_login_status(timeout_s=timeout_s).ok


def clean_codex_env() -> dict:
    spawn_env = os.environ.copy()
    for key in list(spawn_env):
        if key in CODEX_ENV_STRIP_KEYS or key.startswith(CODEX_ENV_STRIP_PREFIXES):
            spawn_env.pop(key, None)
    return spawn_env


def run_codex(
    prompt: str,
    *,
    tier: str,
    model: str | None = None,
    image_path: str | Path | None = None,
    output_schema: str | Path | None = None,
    timeout_s: int = 300,
    sandbox: str = "read-only",
) -> LLMResult:
    """Run a single non-interactive Codex subscription call."""
    codex_exe = find_codex_exe()
    if not codex_exe:
        return LLMResult(
            ok=False,
            provider="codex",
            model=model or codex_model_for_tier(tier),
            error="codex.exe not found on PATH",
        )
    model = model or codex_model_for_tier(tier)
    out_dir = RUNTIME_DIR / "tmp" / "llm_router"
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        "w", delete=False, encoding="utf-8", suffix=".txt", dir=out_dir
    ) as f:
        out_path = Path(f.name)

    cmd = [
        codex_exe, "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "-C", str(ROOT),
        "-m", model,
        "--sandbox", sandbox,
        "--color", "never",
        "--output-last-message", str(out_path),
    ]
    if image_path:
        cmd.extend(["--image", str(image_path)])
    if output_schema:
        cmd.extend(["--output-schema", str(output_schema)])

    no_window_kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    spawn_env = clean_codex_env()

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            cwd=str(ROOT),
            env=spawn_env,
            **no_window_kw,
        )
    except subprocess.TimeoutExpired:
        return LLMResult(
            ok=False,
            provider="codex",
            model=model,
            duration_ms=int((time.time() - t0) * 1000),
            error=f"timeout after {timeout_s}s",
        )
    except Exception as e:
        return LLMResult(
            ok=False,
            provider="codex",
            model=model,
            duration_ms=int((time.time() - t0) * 1000),
            error=f"{type(e).__name__}: {str(e)[:300]}",
        )

    text = ""
    try:
        text = out_path.read_text(encoding="utf-8").strip()
    except Exception:
        text = (proc.stdout or "").strip()

    duration_ms = int((time.time() - t0) * 1000)
    if proc.returncode != 0:
        err = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        return LLMResult(
            ok=False,
            text=text,
            provider="codex",
            model=model,
            duration_ms=duration_ms,
            error=err[:600] or f"rc={proc.returncode}",
        )

    return LLMResult(
        ok=bool(text),
        text=text,
        provider="codex",
        model=model,
        duration_ms=duration_ms,
        error=None if text else "empty codex output",
    )


def should_try_codex(tier: str) -> bool:
    provider = selected_provider()
    return provider == "codex" or provider == "auto"


def should_use_claude_fallback() -> bool:
    provider = selected_provider()
    return provider == "claude" or provider == "auto"


def json_schema_file(name: str, schema: dict) -> Path:
    schema_dir = RUNTIME_DIR / "tmp" / "llm_schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    p = schema_dir / f"{name}.schema.json"
    p.write_text(json.dumps(schema, ensure_ascii=True, indent=2), encoding="utf-8")
    return p
