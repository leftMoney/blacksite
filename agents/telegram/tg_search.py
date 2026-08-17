"""
Blacksite — Telegram public-channel discovery.

Searches Telegram global directory for public channels/groups matching a list
of the client brand-relevant local keywords. Outputs a deduplicated, ranked candidate list.
RECON ONLY — does not join anything.

Notes:
  - Only returns PUBLIC entities (those with @username). Private channels need
    invite links found via other OSINT (FB/X/Pantip mentions).
  - Telegram throttles new accounts on global search; we sleep between queries.
  - Scam / fake flags from Telegram's directory are surfaced.

Usage:
  py agents/telegram/tg_search.py P01                        # default seed keywords
  py agents/telegram/tg_search.py P01 example_keyword_1 example_keyword_2   # custom keywords
"""

from __future__ import annotations

import os
import sys
import asyncio
import shutil
import time
from pathlib import Path

# Force UTF-8 stdout/stderr so local/Chinese chars survive the Windows cp950 console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import SearchRequest

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
SESSION_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "sessions"
SEARCH_SESSION_DIR = SESSION_DIR / "_search"

# TODO: set search seeds for your instance (see instances/_TEMPLATE/policy/*.yaml)
DEFAULT_SEED_KEYWORDS = [
    "example_keyword_1",
    "example_keyword_2",
    "example_keyword_3",
]

PER_QUERY_LIMIT = 30
INTER_QUERY_SLEEP = 3.0  # be gentle to fresh accounts


def make_isolated_session(persona_id: str) -> Path:
    """Use a per-run Telethon session copy so search never locks tg_listen."""
    source = SESSION_DIR / f"{persona_id}.session"
    if not source.exists():
        raise FileNotFoundError(f"base session missing: {source}")
    SEARCH_SESSION_DIR.mkdir(parents=True, exist_ok=True)
    dest = SEARCH_SESSION_DIR / f"{persona_id}_search_{os.getpid()}.session"
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            shutil.copy2(source, dest)
            return dest
        except Exception as e:
            last_err = e
            time_s = 0.5 * (attempt + 1)
            print(f"[search] session copy retry {attempt+1}/5 after {time_s:.1f}s: {e}", file=sys.stderr)
            time.sleep(time_s)
    raise RuntimeError(f"cannot copy session {source} -> {dest}: {last_err}")


def cleanup_isolated_session(path: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            p = Path(str(path) + suffix)
            if p.exists():
                p.unlink()
        except Exception:
            pass


async def search_one(client: TelegramClient, keyword: str) -> list[dict]:
    try:
        result = await client(SearchRequest(q=keyword, limit=PER_QUERY_LIMIT))
    except FloodWaitError as e:
        print(f"  [FLOODWAIT] '{keyword}': sleep {e.seconds}s", file=sys.stderr)
        await asyncio.sleep(e.seconds + 1)
        result = await client(SearchRequest(q=keyword, limit=PER_QUERY_LIMIT))

    items = []
    for chat in result.chats:
        if not getattr(chat, "username", None):
            continue  # private channel — skip in v1
        items.append(
            {
                "title": chat.title,
                "username": chat.username,
                "participants": getattr(chat, "participants_count", None),
                "is_channel": getattr(chat, "broadcast", False),
                "is_megagroup": getattr(chat, "megagroup", False),
                "verified": getattr(chat, "verified", False),
                "scam": getattr(chat, "scam", False),
                "fake": getattr(chat, "fake", False),
                "matched_kw": [keyword],
            }
        )
    return items


async def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("Usage: py agents/telegram/tg_search.py <persona_id> [keyword ...]", file=sys.stderr)
        return 2
    persona_id = args[0].upper()
    keywords = args[1:] or DEFAULT_SEED_KEYWORDS

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    isolated_session = make_isolated_session(persona_id)
    session_path = str(isolated_session)

    print(f"[search] persona={persona_id} keywords={len(keywords)}\n")
    seen: dict[str, dict] = {}
    try:
        async with TelegramClient(session_path, api_id, api_hash) as client:
            for kw in keywords:
                results = await search_one(client, kw)
                print(f"  '{kw}' -> {len(results)} public hits")
                for r in results:
                    key = r["username"].lower()
                    if key in seen:
                        if kw not in seen[key]["matched_kw"]:
                            seen[key]["matched_kw"].append(kw)
                    else:
                        seen[key] = r
                await asyncio.sleep(INTER_QUERY_SLEEP)
    finally:
        cleanup_isolated_session(isolated_session)

    print(f"\n[total unique public entities] {len(seen)}\n")

    sorted_items = sorted(
        seen.values(),
        key=lambda x: (-(x["participants"] or 0), x["title"]),
    )

    print(
        f"{'kind':6s} {'flag':5s} {'members':>9s}  "
        f"{'@username':28s}  title  [matched_kw]"
    )
    print("-" * 120)
    for r in sorted_items:
        if r["is_channel"]:
            kind = "chan"
        elif r["is_megagroup"]:
            kind = "mega"
        else:
            kind = "grp"
        flag = ""
        if r["scam"]:
            flag = "SCAM"
        elif r["fake"]:
            flag = "FAKE"
        elif r["verified"]:
            flag = "VRFY"
        members = str(r["participants"] or "?").rjust(9)
        username = f"@{r['username']}".ljust(28)
        title = r["title"][:55]
        kws = ",".join(r["matched_kw"])
        print(f"{kind:6s} {flag:5s} {members}  {username}  {title}  [{kws}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
