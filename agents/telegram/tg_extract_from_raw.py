"""
Blacksite — entity extractor, listener-output edition.

Reads runtime/raw/<persona>/*.jsonl (produced by tg_listen.py), runs the same
regex extraction as tg_crawler.py, emits entity/edge records to
runtime/funnel_graph.jsonl. NO telethon — avoids session-lock contention with
the running listener.

Idempotent via offset cursor in runtime/extractor_state.json (per-file byte
offset).

Usage:
  py agents/telegram/tg_extract_from_raw.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
RAW_DIR = RUNTIME / "raw"
GRAPH_PATH = RUNTIME / "funnel_graph.jsonl"
STATE_PATH = RUNTIME / "extractor_state.json"
LOG_DIR = RUNTIME / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

RE_USERNAME = re.compile(r"(?:^|[\s\(\[\>\,])@([A-Za-z][A-Za-z0-9_]{4,31})\b")
RE_TME_INVITE = re.compile(r"(?:https?://)?t\.me/(?:joinchat/|\+)([A-Za-z0-9_\-]{16,})")
RE_TME_PUBLIC = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?![A-Za-z0-9_])")
RE_URL = re.compile(r"https?://[^\s<>\"\']+")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log_line(msg: str) -> None:
    print(msg, flush=True)
    with (LOG_DIR / f"tg_extract_{datetime.now(TZ).strftime('%Y-%m-%d')}.log").open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(s: dict) -> None:
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def emit(record: dict) -> None:
    with GRAPH_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_record(rec: dict) -> int:
    persona = rec.get("persona", "?")
    chat_username = rec.get("chat_username")
    chat_id = rec.get("chat_id")
    source = f"@{chat_username}" if chat_username else f"id:{chat_id}"
    msg_id = rec.get("msg_id")
    text = rec.get("text", "") or ""

    edges = 0

    usernames = {m.lower() for m in RE_USERNAME.findall(text)}
    usernames |= {m.lower() for m in RE_TME_PUBLIC.findall(text)}
    self_lc = (chat_username or "").lower()
    for u in usernames:
        if u == self_lc:
            continue
        emit({"type": "edge", "edge": "mention", "from": source, "to": f"@{u}",
              "msg_id": msg_id, "via_persona": persona, "ts": now_iso()})
        emit({"type": "entity", "id": f"@{u}", "kind": "tg_username",
              "first_seen_in": source, "first_seen_msg_id": msg_id,
              "discovered_at": now_iso()})
        edges += 1

    for h in RE_TME_INVITE.findall(text):
        target = f"+{h}"
        emit({"type": "edge", "edge": "invite_link", "from": source, "to": target,
              "msg_id": msg_id, "via_persona": persona, "ts": now_iso()})
        emit({"type": "entity", "id": target, "kind": "tg_invite",
              "first_seen_in": source, "first_seen_msg_id": msg_id,
              "discovered_at": now_iso()})
        edges += 1

    for url in RE_URL.findall(text):
        if "t.me/" in url:
            continue
        emit({"type": "edge", "edge": "url", "from": source, "to": url,
              "msg_id": msg_id, "via_persona": persona, "ts": now_iso()})
        emit({"type": "entity", "id": url, "kind": "url",
              "first_seen_in": source, "first_seen_msg_id": msg_id,
              "discovered_at": now_iso()})
        edges += 1

    fwd_chat = rec.get("fwd_from_chat_id")
    fwd_user = rec.get("fwd_from_user_id")
    if fwd_chat or fwd_user:
        target = f"channel_id:{fwd_chat}" if fwd_chat else f"user_id:{fwd_user}"
        emit({"type": "edge", "edge": "forward_from", "from": target, "to": source,
              "msg_id": msg_id, "via_persona": persona, "ts": now_iso()})
        edges += 1

    return edges


def main() -> None:
    state = load_state()
    log_line(f"[{now_iso()}] tg_extract_from_raw start")
    if not RAW_DIR.exists():
        log_line("no raw dir yet")
        return
    total_msgs = 0
    total_edges = 0
    for persona_dir in sorted(RAW_DIR.iterdir()):
        if not persona_dir.is_dir():
            continue
        for jsonl in sorted(persona_dir.glob("*.jsonl")):
            key = str(jsonl.relative_to(RAW_DIR))
            offset = state.get(key, 0)
            file_size = jsonl.stat().st_size
            if offset >= file_size:
                continue
            with jsonl.open("rb") as f:
                f.seek(offset)
                chunk = f.read()
            new_offset = offset + len(chunk)
            try:
                text = chunk.decode("utf-8", errors="replace")
            except Exception:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total_msgs += 1
                total_edges += process_record(rec)
            state[key] = new_offset
            log_line(f"  {key}: +{total_edges} edges so far (offset {offset}->{new_offset})")
    save_state(state)
    log_line(f"[{now_iso()}] tg_extract_from_raw done: {total_msgs} msgs, {total_edges} edges")


if __name__ == "__main__":
    main()
