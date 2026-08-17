"""Shared Claude OAuth helpers for Blacksite.

Claude is the primary subscription LLM path. Headless claude.exe calls should
use the host OAuth credentials, not a static setup token masquerading as an API
key. Direct Anthropic OAuth API calls may use the current access token from
credentials.json, falling back to ANTHROPIC_OAUTH_TOKEN only when no live access
token is readable.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[1]

CLAUDE_APP_DIR = Path(os.environ.get(
    "CLAUDE_APP_DIR",
    str(Path.home() / "AppData" / "Roaming" / "Claude" / "claude-code"),
))

AUTH_ERROR_PATTERNS = (
    "invalid api key",
    "invalid authentication credentials",
    "failed to authenticate",
    "api error: 401",
    "401",
    "unauthorized",
)


def find_claude_exe() -> str | None:
    """Locate the newest installed claude.exe."""
    if os.name != "nt":
        found = Path(os.environ.get("CLAUDE_EXE", ""))
        return str(found) if found.is_file() else None
    explicit = os.environ.get("CLAUDE_EXE")
    if explicit and Path(explicit).is_file():
        return explicit
    if not CLAUDE_APP_DIR.exists():
        return None
    candidates = [p for p in CLAUDE_APP_DIR.glob("*/claude.exe") if p.is_file()]
    if not candidates:
        return None
    return str(sorted(candidates, key=lambda p: p.parent.name)[-1])


def credentials_path() -> Path:
    return Path(os.environ.get(
        "CLAUDE_CREDENTIALS_PATH",
        str(Path.home() / ".claude" / ".credentials.json"),
    ))


def _expires_to_datetime(raw: object) -> datetime | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw) / 1000, timezone.utc)
        text = str(raw).strip()
        if text.isdigit():
            return datetime.fromtimestamp(int(text) / 1000, timezone.utc)
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def read_credentials() -> dict:
    path = credentials_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth") or {}
        exp = _expires_to_datetime(oauth.get("expiresAt"))
        ttl_h = None
        if exp:
            ttl_h = (exp - datetime.now(timezone.utc)).total_seconds() / 3600
        return {
            "path": str(path),
            "mtime": path.stat().st_mtime,
            "access_token": oauth.get("accessToken") or "",
            "refresh_token": oauth.get("refreshToken") or "",
            "expires_at": exp,
            "ttl_h": ttl_h,
            "subscription_type": oauth.get("subscriptionType") or "",
            "rate_limit_tier": oauth.get("rateLimitTier") or "",
        }
    except Exception:
        return {
            "path": str(path),
            "mtime": None,
            "access_token": "",
            "refresh_token": "",
            "expires_at": None,
            "ttl_h": None,
            "subscription_type": "",
            "rate_limit_tier": "",
        }


def current_oauth_access_token() -> str:
    """Return a bearer token for direct Anthropic OAuth API calls."""
    snap = read_credentials()
    token = snap.get("access_token") or ""
    ttl_h = snap.get("ttl_h")
    if token and (ttl_h is None or ttl_h > 0):
        return token
    return os.environ.get("ANTHROPIC_OAUTH_TOKEN", "")


def claude_host_oauth_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build an env that forces claude.exe to use host OAuth credentials."""
    env = dict(base_env or os.environ)
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "BAGGAGE",
        "AI_AGENT",
        "DEFAULT_LLM_MODEL",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDECODE",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
        "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
        "CLAUDE_CODE_EXECPATH",
    ):
        env.pop(key, None)
    env["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
    return env


def is_claude_auth_error(*parts: object) -> bool:
    blob = " ".join(str(p or "") for p in parts).lower()
    return any(marker in blob for marker in AUTH_ERROR_PATTERNS)
