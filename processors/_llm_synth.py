"""
processors/_llm_synth.py — Spawn claude.exe as the analyst (per boss 5/2 directive).

⚠ boss correction 5/2 PM: 不要走 Anthropic SDK 直連 — OAuth token 被 server 拒
("OAuth authentication is currently not supported"). 走 tg_bridge 同 path:
spawn `claude.exe --print` with full agent harness (tools + skill + permission).

Architecture (per boss):
  - claude IS the analyst (not text-completion endpoint)
  - skill loaded via --append-system-prompt → analyst persona on entry
  - input data path passed in prompt → claude uses Read tool to fetch
  - claude writes output via Write/Edit tool → KB markdown / DB / brief queue
  - we just verify output path exists after process exits

Tier routing (per kb/DESIGN.md §23.2):
  - cross-day coherence / Manager Pack → claude-opus-4-7
  - per-signal / mid-tier insight       → claude-sonnet-4-6
  - batch translate / dedup             → claude-haiku-4-5

Cost: boss subscription OAuth token, marginal cost ~= 0 within rate limits.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from processors import llm_profiles  # noqa: E402
from processors.claude_auth import claude_host_oauth_env, is_claude_auth_error  # noqa: E402
from processors.llm_router import (  # noqa: E402
    codex_model_for_tier,
    run_codex,
    selected_provider,
    should_try_codex_fallback,
)

SKILL_PATH = ROOT / "personas" / "skills" / "SECTION_CHIEF.md"  # was BUSINESS_ANALYST.md (renamed 5/2 §15 reorg; alias kept at old path)
INSTANCE_NAME = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / INSTANCE_NAME / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Claude-tier model ids resolved from config/llm_providers.yaml (no hardcoded
# IDs here — edit the YAML to swap model versions). Constants kept for
# backward-compat with callers importing MODEL_FOR_COHERENCE / MODEL_FOR_BATCH
# directly; new code should use llm_profiles.resolve(tier, provider="claude").
MODEL_FOR_COHERENCE  = llm_profiles.tier_model("claude", "coherence")
MODEL_FOR_PER_SIGNAL = llm_profiles.tier_model("claude", "strategic")
MODEL_FOR_BATCH      = llm_profiles.tier_model("claude", "fast")

# Legacy aliases — some callers import these names. Keep them resolving to
# the same source-of-truth values.
MODEL_OPUS_4_7   = MODEL_FOR_COHERENCE
MODEL_SONNET_4_6 = MODEL_FOR_PER_SIGNAL
MODEL_HAIKU_4_5  = MODEL_FOR_BATCH

# Mirror agents/telegram/tg_bridge.py CLAUDE_APP_DIR (Roaming, not Local)
CLAUDE_APP_DIR = Path(os.environ.get(
    "CLAUDE_APP_DIR",
    str(Path.home() / "AppData" / "Roaming" / "Claude" / "claude-code"),
))

TZ = timezone(timedelta(hours=7))


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    line = f"[{_now_iso()}] [_llm_synth] {msg}"
    print(line, flush=True, file=sys.stderr)
    try:
        with (LOG_DIR / f"llm_synth_{datetime.now(TZ).strftime('%Y-%m-%d')}.log").open(
            "a", encoding="utf-8"
        ) as f:
            f.write(line + "\n")
    except Exception:
        pass


def _codex_tier_for_model(model: str | None) -> str:
    tier = "strategic"
    model_l = (model or "").lower()
    if "haiku" in model_l:
        tier = "fast"
    elif "opus" in model_l or model == MODEL_FOR_COHERENCE:
        tier = "coherence"
    return tier


# ---------------------------------------------------------------------------
# Skill prefix loading (cached per process)
# ---------------------------------------------------------------------------

_skill_cache: str | None = None


def load_skill() -> str:
    global _skill_cache
    if _skill_cache is None:
        try:
            _skill_cache = SKILL_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            _log(f"skill not found at {SKILL_PATH}")
            _skill_cache = ""
    return _skill_cache


# ---------------------------------------------------------------------------
# claude.exe locator (per-spawn re-detect, mirrors tg_bridge 5/2 fix)
# ---------------------------------------------------------------------------


def find_claude_exe() -> str | None:
    """Locate the latest claude.exe. Returns None if Claude Code not installed."""
    explicit = os.environ.get("CLAUDE_EXE")
    if explicit and Path(explicit).is_file():
        return explicit
    if not CLAUDE_APP_DIR.exists():
        return None
    candidates = [p for p in CLAUDE_APP_DIR.glob("*/claude.exe") if p.is_file()]
    if not candidates:
        return None

    def vkey(p: Path):
        try:
            return tuple(int(x) for x in p.parent.name.split(".") if x.isdigit())
        except Exception:
            return (0,)

    candidates.sort(key=vkey, reverse=True)
    return str(candidates[0])


# ---------------------------------------------------------------------------
# Core: spawn claude as analyst with tools (per boss 5/2 directive)
# ---------------------------------------------------------------------------

# Default tools — mirrors tg_bridge but broader (analyst needs to read DB, write
# cards / brief markdown, run sqlite3 queries via Bash).
DEFAULT_TOOLS = "Read,Write,Edit,Bash,Grep,Glob"


def claude_run(
    task: str,
    *,
    skill_prefix: bool = True,
    extra_system: str = "",
    allowed_tools: str = DEFAULT_TOOLS,
    permission_mode: str = "acceptEdits",
    add_dirs: list[str] | None = None,
    model: str = MODEL_FOR_PER_SIGNAL,
    pass_model_flag: bool = False,
    timeout_s: float = 300.0,
    max_retries: int = 3,
    agent_memory_id: str | None = None,
) -> tuple[bool, str]:
    """Spawn claude.exe as analyst. claude reads data + writes output via tools.

    Returns (success_bool, final_stdout_text). Side effects (file writes, DB
    updates) happen during the spawn via tool use.

    max_retries (default 3) = retry up to 3 times with exponential backoff
    (5s, 30s, 120s). Observed 5/2 19:47-54 + 21:09 transient claude.exe OAuth
    verify race fails for ~10 min windows then auto-recover. 3 retries spans
    ~3 min so daily_brief usually rides out the blip.

    agent_memory_id (boss 5/3 §15.Y): when set, prepends the agent's memory
    file (with banner) to extra_system. Skill (skill_prefix) stays separate
    — they have separate token budgets per CLAUDE.md §15.Y. Auto-compacts
    memory in-place if over tier budget before injection.
    """
    system_parts: list[str] = []
    if skill_prefix:
        skill = load_skill()
        if skill:
            system_parts.append(skill)
    # agent_memory injection (§15.Y) — separate budget from skill
    if agent_memory_id:
        try:
            sys.path.insert(0, str(ROOT))
            from agents._common.agent_memory import inject_into_extra_system
            extra_system = inject_into_extra_system(agent_memory_id, extra_system)
        except Exception as e:
            _log(f"agent_memory inject fail for {agent_memory_id}: {type(e).__name__}: {e}")
    if extra_system:
        system_parts.append(extra_system)

    if selected_provider() == "codex":
        tier = _codex_tier_for_model(model)
        codex_prompt = task
        if system_parts:
            codex_prompt = "\n\n".join(system_parts) + "\n\n" + task
        result = run_codex(
            codex_prompt,
            tier=tier,
            model=codex_model_for_tier(tier),
            timeout_s=int(timeout_s),
            sandbox=os.environ.get("BLACKSITE_CODEX_SANDBOX", "workspace-write"),
        )
        if result.ok:
            _log(f"codex ok 繚 {result.duration_ms/1000:.1f}s 繚 "
                 f"tier={tier} model={result.model} 繚 out_chars={len(result.text)}")
            return True, result.text
        _log(f"codex fail 繚 tier={tier} model={result.model} 繚 {result.error}")
        return False, result.text or (result.error or "")

    claude_exe = find_claude_exe()
    if not claude_exe:
        _log("claude.exe not found")
        return False, ""

    # NOTE 5/2 PM (now stale): "do NOT pass --model with OAuth" was based on
    # --bare mode behavior. 5/8 verified: under host OAuth env (i.e. caller
    # NOT setting CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=0 and not using --bare),
    # claude.exe 2.1.128 honors --model sonnet/opus/haiku flags correctly,
    # routing to the named model on Pro plan quota. Default still applies
    # when pass_model_flag=False (legacy callers preserve behavior).
    cmd = [
        claude_exe,
        "--print", task,
        "--add-dir", str(ROOT),
        "--no-session-persistence",
        "--output-format", "text",
        "--allowed-tools", allowed_tools,
        "--permission-mode", permission_mode,
    ]
    if pass_model_flag and model:
        cmd.extend(["--model", model])
    _sys_tmp_path = None
    if system_parts:
        combined_sys = "\n\n".join(system_parts)
        # Windows CreateProcess limit is 32767 chars total. If combined system prompt
        # exceeds 20K, write to a temp file and use --append-system-prompt-file to
        # avoid WinError 206 (e.g. CHIEF_STRATEGIST skill 24K + memory 12K = 36K+).
        if len(combined_sys) > 20_000:
            import tempfile
            tmp = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".txt", delete=False
            )
            tmp.write(combined_sys)
            tmp.close()
            _sys_tmp_path = tmp.name
            cmd.extend(["--append-system-prompt-file", _sys_tmp_path])
        else:
            cmd.extend(["--append-system-prompt", combined_sys])
    for d in (add_dirs or []):
        cmd.extend(["--add-dir", d])

    # spawn env: keep CLAUDE_CODE_* / CLAUDE_AGENT_SDK / CLAUDECODE so claude.exe
    # can use host OAuth context (CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1 +
    # CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH=1 are NEEDED for OAuth refresh — proven
    # 5/2 22:40 by bridge smoke test PASS while _llm_synth with strip FAIL on
    # same token / same claude.exe path. OAuth refresh token (sk-ant-oat01-)
    # cannot be used as a plain API key; must ride host auth path).
    # Only strip vars that would actively conflict (already-set API keys or
    # unrelated noise).
    # 2026-05-25: centralized host OAuth env strips ANTHROPIC_* and SDK
    # inheritance markers; credentials.json is the source of truth.
    spawn_env = claude_host_oauth_env(os.environ)

    # 🔴 boss 5/19 memory reference_claude_exe_entrypoint_oauth.md:
    # daemon-spawned cron (pythonw / DETACHED_PROCESS) inherits
    # ENTRYPOINT=sdk-cli from the SDK context. claude.exe then refuses
    # OAuth with "Invalid API key · Fix external API key" (server-side
    # reject of sdk-cli-context Bearer). Force claude-desktop entrypoint
    # AND strip CC SDK marker env vars — mirror oauth_keepalive.py:242-257.
    # Without this, any 06:00 / cron-class call to Sonnet/Opus dies with
    # Invalid API key (observed 5/25 06:02-09:05 four-attempt-retry loops).
    spawn_env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
    for k in (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDECODE",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_EXECPATH",
        # NOTE: keep CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST and
        # CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH per 5/2 finding (claude.exe
        # OAuth refresh path reads these to know host OAuth is available).
    ):
        spawn_env.pop(k, None)

    import time as _time
    BACKOFFS = [5, 30, 120]  # exponential: 5s, 30s, 120s between attempts
    last_out, last_err, last_rc, last_elapsed = "", "", -1, 0.0
    # Suppress new console window: daemon (pythonw GUI) spawning claude.exe
    # (console subsystem) otherwise pops a fresh cmd window every LLM synth
    # call (daily brief / strategist / section chief eval all hit this).
    # 5/3 boss directive: 「不要 focus 最上層」.
    no_window_kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    for attempt in range(max_retries + 1):
        started = datetime.now(TZ)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                env=spawn_env,
                timeout=timeout_s,
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                **no_window_kw,
            )
        except subprocess.TimeoutExpired:
            _log(f"timeout after {timeout_s}s · model={model} · attempt={attempt+1}")
            if attempt < max_retries:
                _time.sleep(BACKOFFS[min(attempt, len(BACKOFFS) - 1)])
                continue
            return False, ""
        except Exception as e:
            _log(f"spawn err {type(e).__name__}: {e} · attempt={attempt+1}")
            if attempt < max_retries:
                _time.sleep(BACKOFFS[min(attempt, len(BACKOFFS) - 1)])
                continue
            return False, ""

        last_elapsed = (datetime.now(TZ) - started).total_seconds()
        last_out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        last_err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        last_rc = proc.returncode
        if last_rc == 0:
            if attempt > 0:
                _log(f"ok on retry {attempt} · {last_elapsed:.1f}s · model={model} · out_chars={len(last_out)}")
            else:
                _log(f"ok · {last_elapsed:.1f}s · model={model} · out_chars={len(last_out)}")
            if _sys_tmp_path:
                try:
                    os.unlink(_sys_tmp_path)
                except OSError:
                    pass
            return True, last_out
        # rc != 0 — log and maybe retry
        _log(
            f"non-zero exit {last_rc} · {last_elapsed:.1f}s · model={model} · "
            f"attempt={attempt+1}/{max_retries+1} · stdout_head={last_out[:80]!r} · "
            f"stderr_head={last_err[:200]!r}"
        )
        if attempt < max_retries:
            _time.sleep(BACKOFFS[min(attempt, len(BACKOFFS) - 1)])
    if should_try_codex_fallback() and is_claude_auth_error(last_out, last_err):
        tier = _codex_tier_for_model(model)
        codex_prompt = task
        if system_parts:
            codex_prompt = "\n\n".join(system_parts) + "\n\n" + task
        result = run_codex(
            codex_prompt,
            tier=tier,
            model=codex_model_for_tier(tier),
            timeout_s=int(timeout_s),
            sandbox=os.environ.get("BLACKSITE_CODEX_SANDBOX", "workspace-write"),
        )
        if result.ok:
            _log(f"codex fallback ok after Claude auth error · "
                 f"tier={tier} model={result.model} · out_chars={len(result.text)}")
            if _sys_tmp_path:
                try:
                    os.unlink(_sys_tmp_path)
                except OSError:
                    pass
            return True, result.text
        _log(f"codex fallback failed after Claude auth error · "
             f"tier={tier} model={result.model} · {result.error}")

    if _sys_tmp_path:
        try:
            os.unlink(_sys_tmp_path)
        except OSError:
            pass
    return False, last_out


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="claude.exe analyst smoke test")
    p.add_argument("--model", default=MODEL_HAIKU_4_5)
    p.add_argument("--no-skill", action="store_true")
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("task", nargs="?", default="用繁體中文回一句 ≤30 字：今天系統 OK 嗎？")
    args = p.parse_args()
    ok, txt = claude_run(
        args.task,
        model=args.model,
        skill_prefix=not args.no_skill,
        timeout_s=args.timeout,
    )
    print("---success---")
    print(ok)
    print("---reply---")
    print(txt or "(empty)")
