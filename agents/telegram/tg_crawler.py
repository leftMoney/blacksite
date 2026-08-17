"""
Blacksite — Telegram funnel-graph crawler.

For each joined dialog: pulls messages since last cursor, extracts entities
(@usernames, t.me invite links, URLs, forwarded origins) and emits
entity/edge records to runtime/funnel_graph.jsonl.

Idempotent / incremental: cursor per (persona, dialog_id) saved to
runtime/crawler_state.json.

Usage:
  py agents/telegram/tg_crawler.py                # all personas, all dialogs
  py agents/telegram/tg_crawler.py P01            # one persona
  py agents/telegram/tg_crawler.py P01 --backfill 500   # initial deep pull
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.errors import FloodWaitError

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
SESSION_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "sessions"
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

GRAPH_PATH = RUNTIME_DIR / "funnel_graph.jsonl"
STATE_PATH = RUNTIME_DIR / "crawler_state.json"

TZ = timezone(timedelta(hours=7))

DEFAULT_INCREMENTAL_LIMIT = 200
DEFAULT_BACKFILL_LIMIT = 500

# Patterns
RE_USERNAME = re.compile(r"(?:^|[\s\(\[\>\,])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
RE_TME_INVITE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_\-]{16,})")
RE_TME_PUBLIC = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_])")
RE_URL = re.compile(r"https?://[^\s<>\"\']+")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"tg_crawler_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def emit(record: dict) -> None:
    with GRAPH_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def extract_from_text(text: str) -> dict:
    """Returns {usernames, invites, public_links, urls} (deduplicated within msg)."""
    if not text:
        return {"usernames": set(), "invites": set(), "public_links": set(), "urls": set()}
    return {
        "usernames": {m.lower() for m in RE_USERNAME.findall(text)},
        "invites": set(RE_TME_INVITE.findall(text)),
        "public_links": {m.lower() for m in RE_TME_PUBLIC.findall(text)},
        "urls": set(RE_URL.findall(text)),
    }


def process_message(persona_id: str, source_chat: str, msg) -> int:
    """Emit entity + edge records for a single message. Returns edge count."""
    edges_emitted = 0
    text = msg.message or ""
    extracted = extract_from_text(text)

    # Mentions and t.me public links collapse to "tg_username"
    targets_username = extracted["usernames"] | extracted["public_links"]
    for u in targets_username:
        if u == source_chat.lower().lstrip("@"):
            continue
        emit({
            "type": "edge",
            "edge": "mention",
            "from": source_chat,
            "to": f"@{u}",
            "msg_id": msg.id,
            "via_persona": persona_id,
            "ts": now_iso(),
        })
        emit({
            "type": "entity",
            "id": f"@{u}",
            "kind": "tg_username",
            "first_seen_in": source_chat,
            "first_seen_msg_id": msg.id,
            "discovered_at": now_iso(),
        })
        edges_emitted += 1

    for h in extracted["invites"]:
        target = f"+{h}"
        emit({
            "type": "edge",
            "edge": "invite_link",
            "from": source_chat,
            "to": target,
            "msg_id": msg.id,
            "via_persona": persona_id,
            "ts": now_iso(),
        })
        emit({
            "type": "entity",
            "id": target,
            "kind": "tg_invite",
            "first_seen_in": source_chat,
            "first_seen_msg_id": msg.id,
            "discovered_at": now_iso(),
        })
        edges_emitted += 1

    for url in extracted["urls"]:
        # Skip t.me URLs (already captured)
        if "t.me/" in url:
            continue
        emit({
            "type": "edge",
            "edge": "url",
            "from": source_chat,
            "to": url,
            "msg_id": msg.id,
            "via_persona": persona_id,
            "ts": now_iso(),
        })
        emit({
            "type": "entity",
            "id": url,
            "kind": "url",
            "first_seen_in": source_chat,
            "first_seen_msg_id": msg.id,
            "discovered_at": now_iso(),
        })
        edges_emitted += 1

    # Forwarded message origin
    if msg.fwd_from is not None and msg.fwd_from.from_id is not None:
        fwd_chat = getattr(msg.fwd_from.from_id, "channel_id", None)
        fwd_user = getattr(msg.fwd_from.from_id, "user_id", None)
        fwd_target = (
            f"channel_id:{fwd_chat}" if fwd_chat else f"user_id:{fwd_user}" if fwd_user else None
        )
        if fwd_target:
            emit({
                "type": "edge",
                "edge": "forward_from",
                "from": fwd_target,
                "to": source_chat,
                "msg_id": msg.id,
                "via_persona": persona_id,
                "ts": now_iso(),
            })
            edges_emitted += 1

    return edges_emitted


def crawl_persona(persona_id: str, api_id: int, api_hash: str, backfill: int = 0) -> None:
    session_path = str(SESSION_DIR / f"{persona_id}.session")
    if not Path(session_path).exists():
        log_line(f"[{persona_id}] no session")
        return

    state = load_state()
    persona_state = state.setdefault(persona_id, {})
    limit = backfill if backfill else DEFAULT_INCREMENTAL_LIMIT
    log_line(f"[{now_iso()}] [{persona_id}] crawl start (limit={limit}{'/backfill' if backfill else ''})")

    with TelegramClient(session_path, api_id, api_hash) as client:
        if not client.is_user_authorized():
            log_line(f"[{persona_id}] not authorized")
            return

        for dialog in client.iter_dialogs(limit=200):
            if dialog.is_user:
                continue  # skip 1:1 user chats
            chat = dialog.entity
            chat_username = getattr(chat, "username", None)
            chat_id = chat.id
            source_chat = f"@{chat_username}" if chat_username else f"id:{chat_id}"

            cursor_key = f"{chat_id}"
            last_msg_id = persona_state.get(cursor_key, 0)

            try:
                msgs = list(
                    client.iter_messages(
                        chat,
                        limit=limit,
                        min_id=last_msg_id if not backfill else 0,
                    )
                )
            except FloodWaitError as e:
                log_line(f"[{persona_id}] FloodWait {e.seconds}s on {source_chat}, skipping")
                continue
            except Exception as e:
                log_line(f"[{persona_id}] iter_messages err {source_chat}: {type(e).__name__}: {e}")
                continue

            if not msgs:
                continue

            edges = 0
            max_id = last_msg_id
            for m in msgs:
                edges += process_message(persona_id, source_chat, m)
                if m.id > max_id:
                    max_id = m.id

            persona_state[cursor_key] = max_id
            log_line(
                f"[{persona_id}] {source_chat}: {len(msgs)} msgs, "
                f"{edges} edges emitted, cursor→{max_id}"
            )

    save_state(state)
    log_line(f"[{now_iso()}] [{persona_id}] crawl done")


def discover_personas() -> list[str]:
    return sorted(p.stem for p in SESSION_DIR.glob("P*.session"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("personas", nargs="*")
    parser.add_argument("--backfill", type=int, default=0,
                        help="Force pull last N msgs per dialog (ignores cursor)")
    args = parser.parse_args()

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    personas = [p.upper() for p in args.personas] or discover_personas()
    for pid in personas:
        crawl_persona(pid, api_id, api_hash, backfill=args.backfill)


if __name__ == "__main__":
    main()
