"""Switch Blacksite subscription LLM provider.

Usage:
  py scripts/switch_llm_provider.py <provider>          # provider name from YAML
  py scripts/switch_llm_provider.py auto                # uses default_provider for env
  py scripts/switch_llm_provider.py --list              # list registered providers
  py scripts/switch_llm_provider.py --show              # show resolved env for active

Provider names come from `config/llm_providers.yaml` (override path via env
`BLACKSITE_LLM_PROFILES`). To register a new provider or change a tier model,
edit that YAML — no code change required here.

After switching: restart `scripts/blacksite_daemon.py` so cron jobs inherit
the new env.

Optional flags:
  --no-default-models     only flip BLACKSITE_LLM_PROVIDER; leave tier env vars
  --env <path>            target a different .env file (default $BLACKSITE_ENV_PATH
                          or <repo>/.env)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from processors.llm_profiles import (  # noqa: E402
    default_provider,
    describe,
    env_updates_for,
    list_providers,
    profiles_path,
)

ENV_PATH_DEFAULT = Path(os.environ.get("BLACKSITE_ENV_PATH", str(ROOT / ".env")))
TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def set_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [k for k in updates if k not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        out.append(f"# Blacksite LLM provider switch, updated {now_iso()}")
        for key in missing:
            out.append(f"{key}={updates[key]}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _print_listing() -> None:
    print(f"# profiles: {profiles_path()}")
    print(f"# default_provider: {default_provider()}")
    for name in list_providers(switchable_only=False):
        snap = describe(name)
        from processors.llm_profiles import is_switchable
        tag = "" if is_switchable(name) else "  (not switchable — code-ref only)"
        print(f"\n[{name}]{tag}")
        for tier, model in snap["tiers"].items():
            print(f"  {tier:<10} = {model}")


def main() -> int:
    providers = list_providers(switchable_only=True)
    choices = providers + ["auto"]

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("provider", nargs="?",
                        help=f"one of {choices}, or omit with --list / --show")
    parser.add_argument("--list", action="store_true",
                        help="list registered providers and their tier models")
    parser.add_argument("--show", action="store_true",
                        help="show resolved env for the currently active provider")
    parser.add_argument("--no-default-models", action="store_true",
                        help="only update BLACKSITE_LLM_PROVIDER, leave tier env vars")
    parser.add_argument("--env", default=str(ENV_PATH_DEFAULT),
                        help=f"target .env file (default: {ENV_PATH_DEFAULT})")
    args = parser.parse_args()

    if args.list:
        _print_listing()
        return 0

    if args.show:
        snap = describe()
        print(f"# active provider: {snap['provider']}")
        print(f"# profiles: {snap['profiles_path']}")
        for tier, model in snap["tiers"].items():
            print(f"  {tier:<10} = {model}")
        return 0

    if not args.provider:
        parser.error("provider required (or use --list / --show)")
    if args.provider not in choices:
        parser.error(f"unknown provider {args.provider!r}; available: {choices}")

    if args.no_default_models:
        updates = {"BLACKSITE_LLM_PROVIDER": args.provider}
    else:
        updates = env_updates_for(args.provider)

    env_path = Path(args.env)
    set_env_values(env_path, updates)
    print(f"# updated {env_path} @ {now_iso()}")
    for k, v in updates.items():
        print(f"{k}={v}")
    print("# restart scripts/blacksite_daemon.py for cron jobs to inherit this.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
