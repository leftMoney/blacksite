"""LLM provider profile loader — single source of truth for tier→model mapping.

The registry lives at `config/llm_providers.yaml` (override path via
`BLACKSITE_LLM_PROFILES` env). Production code MUST NOT hardcode model
identifiers; every model id resolves through this module.

Resolution priority for a tier's model id (most-specific wins):
  1. `BLACKSITE_LLM_<TIER>` env (set by `scripts/switch_llm_provider.py`)
  2. profile registry default for the active provider
  3. KeyError — fail loudly; no fallback hardcoded.

Active provider is set via `BLACKSITE_LLM_PROVIDER`. When set to `auto`, the
`default_provider` field in YAML is used for tier lookups.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILES_PATH = ROOT / "config" / "llm_providers.yaml"


def profiles_path() -> Path:
    explicit = os.environ.get("BLACKSITE_LLM_PROFILES")
    return Path(explicit) if explicit else DEFAULT_PROFILES_PATH


@lru_cache(maxsize=1)
def _load() -> dict:
    path = profiles_path()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "providers" not in data:
        raise ValueError(f"{path}: missing top-level `providers` key")
    return data


def reload() -> None:
    """Drop the cached YAML — call after editing the file in-process."""
    _load.cache_clear()


def list_providers(*, switchable_only: bool = False) -> list[str]:
    """Names of all providers in the registry (excludes `auto`).

    `switchable_only=True` filters out entries with `switchable: false`
    (e.g. `local` Ollama models — registered for code reference but not
    selectable as the active cloud LLM).
    """
    providers = _load()["providers"]
    if not switchable_only:
        return list(providers.keys())
    return [name for name, block in providers.items()
            if block.get("switchable", True)]


def is_switchable(provider: str) -> bool:
    return _load()["providers"][provider].get("switchable", True)


def default_provider() -> str:
    declared = _load().get("default_provider")
    if declared:
        return declared
    return list_providers(switchable_only=True)[0]


def selected_provider() -> str:
    """Currently active provider per env. `auto` collapses to default_provider."""
    raw = (os.environ.get("BLACKSITE_LLM_PROVIDER") or default_provider()).strip().lower()
    if raw == "auto":
        return default_provider()
    if raw not in list_providers():
        # Fall back to default — but never silently invent a provider.
        return default_provider()
    return raw


def all_tiers(provider: str | None = None) -> list[str]:
    p = provider or selected_provider()
    return list(_load()["providers"][p]["tiers"].keys())


def tier_model(provider: str, tier: str) -> str:
    """Canonical model id for (provider, tier). Raises KeyError if missing."""
    return _load()["providers"][provider]["tiers"][tier]


def tier_model_for_claude_exe(provider: str, tier: str) -> str:
    """Short alias preferred by claude.exe `--model` (e.g. `sonnet` / `haiku`),
    falling back to the canonical tier model id when no alias is configured."""
    block = _load()["providers"][provider]
    aliases = block.get("claude_exe_alias") or {}
    return aliases.get(tier) or block["tiers"][tier]


def resolve(tier: str, *, provider: str | None = None) -> str:
    """Resolve a tier to its model id, env override taking precedence.

    Env key: `BLACKSITE_LLM_<TIER>` (e.g. BLACKSITE_LLM_FAST).
    """
    env_val = os.environ.get(f"BLACKSITE_LLM_{tier.upper()}")
    if env_val:
        return env_val
    return tier_model(provider or selected_provider(), tier)


def resolve_claude_exe(tier: str, *, provider: str | None = None) -> str:
    """Like resolve(), but prefers the short claude.exe alias when available."""
    env_val = os.environ.get(f"BLACKSITE_LLM_{tier.upper()}_ALIAS")
    if env_val:
        return env_val
    return tier_model_for_claude_exe(provider or selected_provider(), tier)


def env_updates_for(provider: str) -> dict[str, str]:
    """All env updates needed when switching to `provider`. Used by
    `scripts/switch_llm_provider.py` to rewrite .env safely.

    Raises ValueError if the provider is registered but not `switchable`.
    """
    target = default_provider() if provider == "auto" else provider
    if not is_switchable(target):
        raise ValueError(
            f"provider {target!r} is not switchable; only registered for code reference"
        )
    updates: dict[str, str] = {"BLACKSITE_LLM_PROVIDER": provider}
    for tier, model_id in _load()["providers"][target]["tiers"].items():
        updates[f"BLACKSITE_LLM_{tier.upper()}"] = model_id
    return updates


def describe(provider: str | None = None) -> dict:
    """Return a flat dict suitable for printing / logging the active config."""
    p = provider or selected_provider()
    return {
        "provider": p,
        "profiles_path": str(profiles_path()),
        "default_provider": default_provider(),
        "tiers": {t: resolve(t, provider=p) for t in all_tiers(p)},
    }
