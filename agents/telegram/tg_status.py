"""
Blacksite — Telegram persona status check.

Reads .session files for the requested personas and prints:
  - Logged-in account identity (name, id, username)
  - Total dialogs count
  - First N dialogs by kind (channel / group / user)

Usage:
  py agents/telegram/tg_status.py             # all personas in instance
  py agents/telegram/tg_status.py P01 P02     # specific personas
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon.sync import TelegramClient

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
SESSION_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "sessions"

LIST_LIMIT = 30


def discover_personas() -> list[str]:
    return sorted(p.stem for p in SESSION_DIR.glob("P*.session"))


def inspect(persona_id: str, api_id: int, api_hash: str) -> None:
    session_path = str(SESSION_DIR / f"{persona_id}.session")
    if not Path(session_path).exists():
        print(f"\n=== {persona_id} ===\n  [NO_SESSION] {session_path}")
        return

    with TelegramClient(session_path, api_id, api_hash) as client:
        if not client.is_user_authorized():
            print(f"\n=== {persona_id} ===\n  [NOT_AUTH] session expired or invalid")
            return
        me = client.get_me()
        dialogs = list(client.iter_dialogs(limit=None))
        kind_count = {"channel": 0, "group": 0, "user": 0, "other": 0}
        for d in dialogs:
            if d.is_channel and not d.is_group:
                kind_count["channel"] += 1
            elif d.is_group:
                kind_count["group"] += 1
            elif d.is_user:
                kind_count["user"] += 1
            else:
                kind_count["other"] += 1

        print(
            f"\n=== {persona_id} === "
            f"name={me.first_name or ''!r} "
            f"id={me.id} "
            f"username=@{me.username or '<none>'} "
            f"phone={me.phone or '<hidden>'}"
        )
        print(
            f"  total dialogs: {len(dialogs)} "
            f"(channels={kind_count['channel']}, "
            f"groups={kind_count['group']}, "
            f"users={kind_count['user']})"
        )

        for d in dialogs[:LIST_LIMIT]:
            if d.is_channel and not d.is_group:
                kind = "channel"
            elif d.is_group:
                kind = "group  "
            elif d.is_user:
                kind = "user   "
            else:
                kind = "other  "
            uname = f"@{d.entity.username}" if getattr(d.entity, "username", None) else ""
            print(f"  [{kind}] {d.name!r:40s} {uname}")
        if len(dialogs) > LIST_LIMIT:
            print(f"  ...({len(dialogs) - LIST_LIMIT} more)")


def main() -> None:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    personas = sys.argv[1:] or discover_personas()
    if not personas:
        sys.exit("no personas found — log in via tg_login.py first")
    for pid in personas:
        inspect(pid.upper(), api_id, api_hash)


if __name__ == "__main__":
    main()
