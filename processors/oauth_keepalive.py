"""
Blacksite — OAuth setup-token keepalive (5/3 fix for 「commander 接線員炸了」)

Root cause (verified 5/3):
  Bridge spawns `claude.exe --print` with ANTHROPIC_API_KEY = `sk-ant-oat01-`
  setup-token from .env. Server-side keeps that token alive only while
  SOMETHING in boss's account is exercising the OAuth chain. When boss closes
  every Claude Desktop / CLI window for ~18-20h straight, the parent OAuth
  session goes idle → server invalidates derived setup-tokens → next bridge
  spawn returns "Invalid API key · Fix external API key".

  Empirical timeline that proved it:
    5/2 13:56 +08  boss reboot from Start Menu (Windows event id 1074)
    5/2 16:52 +08  last interactive Desktop session start (4032.json)
    5/2 .. 5/3     22h with NO active Desktop session
    5/3 11:34 +07  msg_id=129 first "Invalid API key" — ~19h 40m after last
    5/3 14:09 +08  boss opens new session (11648.json), token still dead
    5/3 16:17 +08  boss runs `claude` interactive in cmd → token recovers

How this fixes it:
  Cron-fire every 3h: spawn `claude.exe --print "1"` with the same env the
  bridge uses (ANTHROPIC_API_KEY = oauth_token, no ANTHROPIC_BASE_URL). One
  successful exchange touches the OAuth chain server-side and resets the
  inactivity timer. 3h cadence gives ~6× safety margin vs the observed
  ~18-20h invalidation window.

  ~30-50K subscription tokens per fire (one Claude Code session boot + a
  trivial 1-char turn). Negligible vs daily brief / strategist passes.

Failure handling:
  - rc=0  : log "ok" to runtime/logs/oauth_keepalive_<date>.log
  - "Invalid API key" detected : escalate via log_event(kind='warning',
    scope='bridge') + fire Telethon DM to boss via P01 (bridge is dead so
    direct Telethon is the only reliable path).
  - Other transient (timeout / network) : log warn but don't escalate;
    next 3h fire will retry.

5/23 fix: AUTH_DEATH now sends direct TG DM via Telethon (P01) to boss.
  Previous path (system_history only) was unreliable — bridge is dead when
  OAuth is dead, so boss never saw the warning. Telethon session is
  independent of Anthropic OAuth.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from processors.claude_auth import claude_host_oauth_env, read_credentials  # noqa: E402

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME / "logs"

TZ = timezone(timedelta(hours=7))
TIMEOUT_SEC = int(os.environ.get("OAUTH_KEEPALIVE_TIMEOUT_SEC", "60"))

CLAUDE_APP_DIR = Path(os.environ.get(
    "CLAUDE_APP_DIR",
    "C:/Users/<YOUR_USERNAME>/AppData/Roaming/Claude/claude-code",
))


def _now() -> datetime:
    return datetime.now(TZ)


def _log(msg: str) -> None:
    line = f"[{_now().isoformat(timespec='seconds')}] [oauth_keepalive] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / f"oauth_keepalive_{_now().strftime('%Y-%m-%d')}.log"
    with log.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def _tg_notify_boss(msg: str) -> bool:
    """Send a direct TG message to boss via P01 Telethon session.
    Independent of Anthropic OAuth — works even when bridge is dead.
    Returns True on success, False on any failure (non-fatal)."""
    try:
        from telethon import TelegramClient
    except ImportError:
        _log("TG notify skip: telethon not installed")
        return False

    api_id_raw = os.environ.get("TG_API_ID", "")
    api_hash = os.environ.get("TG_API_HASH", "")
    boss_id_raw = os.environ.get("BOSS_TG_USER_ID", "")
    if not (api_id_raw and api_hash and boss_id_raw):
        _log("TG notify skip: TG_API_ID / TG_API_HASH / BOSS_TG_USER_ID not set")
        return False

    try:
        api_id = int(api_id_raw)
        boss_id = int(boss_id_raw)
    except ValueError:
        _log("TG notify skip: TG_API_ID or BOSS_TG_USER_ID not int")
        return False

    active_instance = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
    session_path = str(ROOT / "instances" / active_instance / "runtime" / "sessions" / "P01.session")
    if not Path(session_path).exists():
        _log(f"TG notify skip: P01 session not found at {session_path}")
        return False

    try:
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            _log("TG notify skip: P01 session not authorized")
            await client.disconnect()
            return False
        await client.send_message(boss_id, msg)
        await client.disconnect()
        _log(f"TG notify sent to boss_id={boss_id}")
        return True
    except Exception as e:
        _log(f"TG notify fail: {type(e).__name__}: {str(e)[:200]}")
        return False


def _fire_tg_notify(msg: str) -> bool:
    """Sync wrapper for _tg_notify_boss — runs in a fresh event loop."""
    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(_tg_notify_boss(msg))
        loop.close()
        return result
    except Exception as e:
        _log(f"TG notify event loop fail: {type(e).__name__}: {str(e)[:200]}")
        return False


def find_claude_exe() -> str:
    """Mirror agents/telegram/tg_bridge.find_claude_exe to stay in sync with
    the path the bridge actually uses. Re-detect each fire (Claude Code
    auto-update may have moved/removed binaries since last fire)."""
    if env := os.environ.get("CLAUDE_EXE"):
        return env
    if not CLAUDE_APP_DIR.exists():
        raise FileNotFoundError(f"claude code app dir not found: {CLAUDE_APP_DIR}")
    versions = [p for p in CLAUDE_APP_DIR.glob("*/claude.exe") if p.is_file()]
    if not versions:
        raise FileNotFoundError(f"no claude.exe under {CLAUDE_APP_DIR}/*/")

    def vkey(p: Path):
        try:
            return tuple(int(x) for x in p.parent.name.split(".") if x.isdigit())
        except Exception:
            return (0,)

    return str(sorted(versions, key=vkey, reverse=True)[0])


CREDENTIALS_JSON = Path(
    os.environ.get("CLAUDE_CREDENTIALS_PATH", "")
    or Path.home() / ".claude" / ".credentials.json"
)
# Refresh trigger threshold (hours). When credentials.json access_token TTL
# falls below this, call claude.exe host OAuth to force refresh. Defaults to
# 1.5h — gives at least 1 cron tick of safety margin if cron is 30min.
REFRESH_THRESHOLD_H = float(os.environ.get("OAUTH_REFRESH_THRESHOLD_H", "1.5"))


def _read_credentials_ttl_h() -> tuple[float | None, float | None]:
    """Return (ttl_hours_remaining, mtime_epoch). None if file unreadable.

    Used by the smart keepalive to decide whether to actually fire claude.exe.
    Calling claude.exe when TTL is still high is wasted — claude.exe will not
    refresh until access is near expiry (empirical: 7.96h TTL → no refresh).
    """
    snap = read_credentials()
    return snap.get("ttl_h"), snap.get("mtime")


def main() -> int:
    provider = os.environ.get("BLACKSITE_LLM_PROVIDER", "claude").strip().lower()
    if provider != "claude":
        _log(f"SKIP: BLACKSITE_LLM_PROVIDER={provider}; Codex/ChatGPT path must not use Anthropic API key keepalive")
        return 0

    # 5/24 RE-DESIGN — TTL-aware refresh trigger.
    #
    # PROBLEM with prior implementation: it called claude.exe with
    # ANTHROPIC_API_KEY=<sk-ant-oat01-...> every 3h. This path does NOT
    # trigger OAuth refresh — claude.exe treats the env token as a Bearer and
    # never touches credentials.json. Result: credentials.json access_token
    # expires at 8h TTL, server-side OAuth session collapses, sk-ant-oat01-
    # in .env also dies (server-side TTL coupling, verified 5/24 by exact-
    # 8h match between credentials write time and bridge AUTH_DEATH).
    #
    # NEW DESIGN: keepalive watches credentials.json TTL. Only when access is
    # within REFRESH_THRESHOLD_H of expiry, fire claude.exe via host OAuth
    # path (no ANTHROPIC_API_KEY env). claude.exe internally detects near-
    # expiry and calls Anthropic's refresh endpoint with the refresh_token
    # from credentials.json → writes new credentials.json with extended TTL.
    # Server-side OAuth chain stays warm → sk-ant-oat01- bridge token stays
    # alive (assuming the TTL-coupling hypothesis holds; if not, bridge will
    # need its own fix in tg_bridge.py).
    ttl_h, mt_before = _read_credentials_ttl_h()
    if ttl_h is None:
        _log(f"ABORT: cannot read {CREDENTIALS_JSON}; run `claude setup-token` to seed")
        return 1
    if ttl_h > REFRESH_THRESHOLD_H:
        _log(f"NOOP: credentials.json access TTL={ttl_h:.2f}h > "
             f"{REFRESH_THRESHOLD_H}h threshold; nothing to refresh")
        return 0
    if ttl_h <= 0:
        _log(f"WARN: credentials.json access already expired ({ttl_h:.2f}h). "
             "refresh_token may still work — attempting refresh.")

    try:
        claude_exe = find_claude_exe()
    except FileNotFoundError as e:
        _log(f"ABORT: {e}")
        return 1

    spawn_env = claude_host_oauth_env(os.environ)
    spawn_env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
    for k in (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDECODE",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
        "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
        "CLAUDE_CODE_EXECPATH",
        # CRITICAL: strip ANTHROPIC_API_KEY + ANTHROPIC_OAUTH_TOKEN so claude.exe
        # uses host OAuth (credentials.json) and triggers refresh, NOT the
        # Bearer-token shortcut that bypasses refresh.
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    ):
        spawn_env.pop(k, None)
    _log(f"REFRESH_TRIGGER: TTL={ttl_h:.2f}h ≤ {REFRESH_THRESHOLD_H}h threshold; "
         "calling claude.exe via host OAuth (no API_KEY env)")

    cmd = [
        claude_exe,
        "--print", "1",
        "--no-session-persistence",
        "--output-format", "text",
    ]

    # Suppress console window: daemon (pythonw GUI) spawning claude.exe
    # (console subsystem) otherwise pops a cmd window every 3h.
    no_window_kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    t0 = time.monotonic()
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            timeout=TIMEOUT_SEC,
            env=spawn_env,
            cwd=str(ROOT),
            **no_window_kw,
        )
        elapsed = time.monotonic() - t0
        stdout = r.stdout.decode("utf-8", errors="replace").strip() if r.stdout else ""
        stderr = r.stderr.decode("utf-8", errors="replace").strip() if r.stderr else ""
    except subprocess.TimeoutExpired:
        _log(f"TIMEOUT after {TIMEOUT_SEC}s — transient, will retry next fire")
        return 0  # don't escalate transient timeout
    except Exception as e:
        _log(f"EXC {type(e).__name__}: {str(e)[:200]} — transient, will retry next fire")
        return 0

    if r.returncode == 0 and stdout:
        # Verify the call actually triggered a refresh (mtime advanced).
        # If mtime didn't change, claude.exe didn't refresh — either threshold
        # is even tighter than ours, or refresh path is broken.
        new_ttl_h, mt_after = _read_credentials_ttl_h()
        refreshed = bool(mt_after and mt_before and mt_after > mt_before)
        _log(
            f"ok {elapsed:.1f}s reply={stdout[:60]!r} "
            f"refreshed={refreshed} ttl_before={ttl_h:.2f}h "
            f"ttl_after={(new_ttl_h or 0):.2f}h "
            f"claude={claude_exe}"
        )
        if not refreshed:
            _log("WARN: claude.exe call succeeded but credentials.json mtime "
                 "did NOT advance — refresh threshold may be tighter than "
                 f"{REFRESH_THRESHOLD_H}h, or refresh failed silently. "
                 "Consider lowering OAUTH_REFRESH_THRESHOLD_H.")
        return 0

    # Failure path — distinguish auth death from transient
    blob = stdout + "\n" + stderr
    is_auth_death = (
        "Invalid API key" in blob
        or "Fix external API key" in blob
        or "401" in blob
        or "authentication_error" in blob
    )

    if is_auth_death:
        _log(f"AUTH_DEATH rc={r.returncode} {elapsed:.1f}s "
             f"stdout_head={stdout[:200]!r} stderr_head={stderr[:200]!r}")

        # 1. Direct TG DM to boss via Telethon P01 (bridge is dead, this is
        #    the only reliable notification path when OAuth is gone).
        tg_msg = (
            "⚠️ Commander 橋接死了！Anthropic OAuth token 失效。\n\n"
            "你 TG 傳訊息給 Commander 不會有回應，要修才能恢復。\n\n"
            "修法（在桌機跑）：\n"
            "1. 開 cmd / Windows Terminal\n"
            "2. 跑：claude setup-token\n"
            "3. 完成瀏覽器 OAuth\n"
            "4. 把新的 sk-ant-oat01-... 貼進 .env 的 ANTHROPIC_OAUTH_TOKEN=\n"
            "5. 不需重啟 daemon，bridge 自動讀新 token\n\n"
            f"偵測時間：{_now().isoformat(timespec='seconds')}"
        )
        tg_ok = _fire_tg_notify(tg_msg)
        if not tg_ok:
            _log("TG direct notify failed — boss may not see this alert")

        # 2. Also write to system_history as secondary record.
        try:
            from processors.history_log import log_event
            log_event(
                actor="oauth_keepalive",
                kind="warning",
                scope="bridge",
                title="OAuth setup-token REJECTED — bridge dead, boss must re-mint",
                body=(
                    "Cron keepalive spawn returned 'Invalid API key'. The "
                    "ANTHROPIC_OAUTH_TOKEN in .env is no longer accepted by "
                    "Anthropic API. Commander bridge will fail every freeform "
                    "DM until token is refreshed.\n\n"
                    f"TG direct notify: {'sent' if tg_ok else 'FAILED'}\n\n"
                    "Fix: on the desktop where Claude Code is logged in:\n"
                    "  1. open cmd / Windows Terminal\n"
                    "  2. run: claude setup-token\n"
                    "  3. complete browser OAuth\n"
                    "  4. paste the new sk-ant-oat01-... token into "
                    ".env line ANTHROPIC_OAUTH_TOKEN=...\n"
                    "  5. (no daemon restart needed — bridge re-reads env "
                    "per spawn)\n\n"
                    f"Spawn detail:\n  rc={r.returncode}\n  elapsed={elapsed:.1f}s\n"
                    f"  stdout: {stdout[:400]}\n  stderr: {stderr[:400]}"
                ),
                refs=[".env", "processors/oauth_keepalive.py", "agents/telegram/tg_bridge.py"],
            )
        except Exception as e:
            _log(f"history_log fail (non-fatal): {type(e).__name__}: {e}")
        return 2  # distinct rc so daemon log_line shows FAIL

    # Other non-zero (rare): not auth death, log but don't escalate
    _log(f"NON_AUTH_FAIL rc={r.returncode} {elapsed:.1f}s "
         f"stdout_head={stdout[:200]!r} stderr_head={stderr[:200]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
