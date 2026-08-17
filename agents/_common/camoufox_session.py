"""
Blacksite — Camoufox session wrapper for Meta-family personas (FB+IG).

Provides per-persona, per-platform isolated browser sessions via Camoufox
(anti-detect Firefox fork). Each persona gets a unique fingerprint mix
(canvas/webgl/timezone/device class) and a separate user_data_dir to keep
cookies+localStorage isolated per platform.

Per fb_ig_strategy.md §0 (Route B locked) + §6.1 (camoufox_session.py spec)
+ §7 (fingerprint mitigation): persona ↔ device class deliberately differs
to make 3 personas behind one shared FlyVPN IP look like 3 residential
users behind a NAT, not 3 bots.

Per persona declared device class (ref strategy §4.4):
  P03 -> iPhone Safari mobile profile
  P04 -> Android Chrome mobile profile
  P05 -> Firefox profile, forced to mobile automation viewport

Per CLAUDE.md §6.4 all timestamps are GMT+7-aware.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
PERSONAS_DIR = ROOT / "personas"
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

from agents._common.browser_viewport import MOBILE_WINDOW, mobile_viewport  # noqa: E402


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _log(persona_id: str, msg: str) -> None:
    line = f"[{now_iso()}] [camoufox] [{persona_id}] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"meta_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# Per-persona declared device class (fb_ig_strategy.md §4.4)
PERSONA_DEVICE_CLASS: dict[str, dict[str, Any]] = {
    "P03": {
        "os": "macos",
        "humanize": True,
        "i_know_what_im_doing": False,
        "screen_resolution": MOBILE_WINDOW,
        "user_agent_family": "safari_ios",
    },
    "P04": {
        "os": "linux",                         # Android maps to linux for fingerprint
        "humanize": True,
        "i_know_what_im_doing": False,
        "screen_resolution": MOBILE_WINDOW,
        "user_agent_family": "chrome_android",
    },
    "P05": {
        "os": "windows",
        "humanize": True,
        "i_know_what_im_doing": False,
        "screen_resolution": MOBILE_WINDOW,
        "user_agent_family": "firefox_desktop",
    },
}


def _automation_window(persona_id: str, declared_window: tuple[int, int]) -> tuple[int, int]:
    override = os.environ.get("CAMOUFOX_WINDOW", "").strip()
    if override and "x" in override.lower():
        try:
            w_s, h_s = override.lower().split("x", 1)
            return int(w_s), int(h_s)
        except ValueError:
            pass

    stable = {
        "P03": MOBILE_WINDOW,
        "P04": MOBILE_WINDOW,
        "P05": MOBILE_WINDOW,
    }
    if persona_id in stable:
        return stable[persona_id]

    return MOBILE_WINDOW


def _profile_yaml_path(persona_id: str) -> Path:
    return PERSONAS_DIR / persona_id / "profile.yaml"


def load_persona_profile(persona_id: str) -> dict[str, Any]:
    path = _profile_yaml_path(persona_id)
    if not path.exists():
        raise FileNotFoundError(f"No profile.yaml at {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def browser_dir(persona_id: str, platform: str) -> Path:
    """Per-persona, per-platform user_data_dir (cookies+localStorage isolation)."""
    d = PERSONAS_DIR / persona_id / "browser" / platform
    d.mkdir(parents=True, exist_ok=True)
    return d


def storage_state_path(persona_id: str, platform: str) -> Path:
    """Authoritative storage_state.json for a (persona, platform). Used by
    headless agents that don't need a full user_data_dir to save startup time;
    register.py exports here at end of register success."""
    d = PERSONAS_DIR / persona_id / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{platform}_storage_state.json"


@asynccontextmanager
async def launch_persona(
    persona_id: str,
    platform: str,
    *,
    headless: bool = True,
    use_storage_state: bool = True,
    proxy: dict[str, str] | None = None,
):
    """Async context manager that yields (browser, context, page) for a persona.

    `platform` is one of "facebook" | "instagram" (also accepts other names
    for future Meta-family surfaces). Used to scope the user_data_dir.

    `use_storage_state=True` loads <persona>/state/<platform>_storage_state.json
    if it exists (post-register handoff). False = fresh session (register flow).

    `headless=False` for register.py boss-in-loop visual mode; True for daemon.
    """
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError as e:
        raise RuntimeError(
            "Camoufox not installed. Run scripts/install_camoufox.bat first."
        ) from e

    if persona_id not in PERSONA_DEVICE_CLASS:
        raise ValueError(f"Unknown persona_id={persona_id}")

    device = PERSONA_DEVICE_CLASS[persona_id]
    user_data_dir = browser_dir(persona_id, platform)

    storage_state_arg: str | None = None
    if use_storage_state:
        sp = storage_state_path(persona_id, platform)
        if sp.exists():
            storage_state_arg = str(sp)
            _log(persona_id, f"loading storage_state from {sp}")
        else:
            _log(persona_id, f"no storage_state at {sp} (fresh session)")

    proxy_arg = None
    if proxy:
        proxy_arg = proxy
    elif os.environ.get("CAMOUFOX_PROXY"):
        # Single-string fallback: env var like "http://user:pass@host:port"
        proxy_arg = {"server": os.environ["CAMOUFOX_PROXY"]}

    # Windows: Camoufox default install at \AppData\Local\camoufox\camoufox\Cache\
    # triggers Windows SxS Activation Context fail (REGISTER_LESSONS §1.1).
    # Workaround: xcopy to ~\Camoufox and pass executable_path here.
    exec_path = os.environ.get("CAMOUFOX_EXECUTABLE_PATH")
    if not exec_path and sys.platform == "win32":
        candidate = Path.home() / "Camoufox" / "camoufox.exe"
        if candidate.exists():
            exec_path = str(candidate)

    _log(persona_id, f"launching platform={platform} headless={headless} device={device['os']}"
                     + (f" exec={Path(exec_path).name}" if exec_path else ""))
    # Camoufox 0.4 API: use `window=` (NOT `screen=` — tuple unsupported; bug verified
    # 5/6 with `'tuple' object has no attribute 'is_set'`). `timezone=` also unsupported
    # in 0.4 (use locale + geoip for tz spoof). `i_know_what_im_doing` removed.
    #
    # Mode selection (5/6 bug fix):
    #   storage_state path exists  → ephemeral context loads cookies (no user_data_dir
    #                                — hotel kit shipped state JSON only, not 1.7GB user_data_dir)
    #   storage_state absent       → persistent_context + user_data_dir (register flow)
    use_persistent_mode = not storage_state_arg

    common_kwargs = dict(
        headless=headless,
        humanize=device["humanize"],
        os=device["os"],
        window=_automation_window(persona_id, device["screen_resolution"]),
        proxy=proxy_arg,
        locale="en-US",
        geoip=True,
    )
    if exec_path:
        common_kwargs["executable_path"] = exec_path

    if use_persistent_mode:
        # Register / fresh-session mode: persistent_context retains full Firefox
        # profile (history, bookmarks, etc.) at user_data_dir.
        common_kwargs["persistent_context"] = True
        common_kwargs["user_data_dir"] = str(user_data_dir)
        async with AsyncCamoufox(**common_kwargs) as browser:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()
            await page.set_viewport_size(mobile_viewport())
            try:
                yield browser, context, page
            finally:
                try:
                    sp = storage_state_path(persona_id, platform)
                    await context.storage_state(path=str(sp))
                    _log(persona_id, f"storage_state persisted -> {sp.name}")
                except Exception as e:
                    _log(persona_id, f"storage_state persist FAILED: {e}")
    else:
        # Warmup-session mode: load cookies via storage_state, ephemeral context.
        async with AsyncCamoufox(**common_kwargs) as browser:
            context = await browser.new_context(
                storage_state=storage_state_arg,
                viewport=mobile_viewport(),
            )
            page = await context.new_page()
            await page.set_viewport_size(mobile_viewport())
            try:
                yield browser, context, page
            finally:
                try:
                    sp = storage_state_path(persona_id, platform)
                    await context.storage_state(path=str(sp))
                    _log(persona_id, f"storage_state persisted -> {sp.name}")
                except Exception as e:
                    _log(persona_id, f"storage_state persist FAILED: {e}")


async def export_storage_state(persona_id: str, platform: str) -> Path:
    """Export current persistent-context storage_state to canonical path.

    Called from register.py end-of-flow. Headless agents use the resulting
    file directly without spinning up a full user_data_dir.
    """
    sp = storage_state_path(persona_id, platform)
    async with launch_persona(persona_id, platform, headless=True,
                              use_storage_state=True) as (browser, context, page):
        await context.storage_state(path=str(sp))
    _log(persona_id, f"export_storage_state -> {sp}")
    return sp
