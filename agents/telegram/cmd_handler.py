"""
M6.5 — Bidirectional TG-DM command handler.

Boss directive 2026-04-29: "我能不能在 TG 發訊息給 Commander，你這邊偵測到然後回我".

Architecture (mirror of M6 brief flow but with bidirectional + faster cadence):

  1. Boss DMs P01 ("Commander") on TG
  2. tg_listen's boss_dm_capturer detects sender_id == BOSS_TG_USER_ID,
     writes a JSON record to runtime/cmd/inbox/<ts>_<msg_id>.json
  3. Scheduled-task `blacksite-tg-cmd` (cron every 2 min) reads inbox,
     interprets each command/question, composes Claude reply, writes
     to runtime/cmd/outbox/<inbox-name>.md, moves inbox to processed/
  4. tg_listen's cmd_send_loop (poll every 30s) sees outbox file,
     DMs reply to boss via P01, moves to sent/

End-to-end latency: 0-3 min depending on where the cron fires relative
to the DM arrival. Acceptable for "give Claude orders via TG" feel.

Cost: cron fires ~720/day at base ~1K tokens (empty inbox bash check
exits fast); content fires use 10-30K tokens. On boss's Claude Max 5X
plan, well within token budget.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"

INBOX_DIR = RUNTIME_DIR / "cmd" / "inbox"
PROCESSED_DIR = RUNTIME_DIR / "cmd" / "processed"
OUTBOX_DIR = RUNTIME_DIR / "cmd" / "outbox"
SENT_DIR = RUNTIME_DIR / "cmd" / "sent"
LOG_DIR = RUNTIME_DIR / "logs"
REPORT_DIR = RUNTIME_DIR / "reports"

CMD_SEND_PERSONA = os.environ.get("CMD_SEND_PERSONA", "P01").upper()
POLL_INTERVAL_SEC = int(os.environ.get("CMD_SEND_POLL_SEC", "30"))
TG_MSG_LIMIT = 4000
ATTACH_MARKER_RE = re.compile(r"<!--BLACKSITE_ATTACH_FILE:(.*?)-->")

TZ = timezone(timedelta(hours=7))


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    line = f"[{_now()}] [cmd] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"cmd_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------
# Inbox writer — called from tg_listen.boss_dm_capturer
# ----------------------------------------------------------------------

def write_inbox(boss_id: int, msg_id: int, text: str,
                sender_username: str | None = None,
                received_via_persona: str = "P01",
                reply_to_msg_id: int | None = None,
                reply_to_text: str | None = None) -> Path:
    """Try fast-path first (Python-handled, 0 Claude tokens, sub-second).
    On match → write reply DIRECTLY to outbox/, log fast-path hit, done.
    No-match → drop into inbox/ for the (lower-frequency) scheduled-task to
    pick up and reason about as freeform."""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    ts_compact = datetime.now(TZ).strftime("%Y-%m-%dT%H-%M-%S")

    # ----- Fast-path attempt -----
    try:
        from agents.telegram.cmd_fast_path import try_fast_path
        fp = try_fast_path(text)
    except Exception as e:
        _log(f"fast-path import/exec err: {type(e).__name__}: {e}")
        fp = None

    if fp:
        reply_md, intent = fp
        out_path = OUTBOX_DIR / f"{ts_compact}_{msg_id}.md"
        out_path.write_text(reply_md, encoding="utf-8")
        _log(f"fast-path:{intent} → outbox/{out_path.name} text={(text or '')[:60]!r}")
        return out_path

    # ----- No fast-path match → freeform queue -----
    path = INBOX_DIR / f"{ts_compact}_{msg_id}.json"
    payload = {
        "received_at": _now(),
        "received_via_persona": received_via_persona,
        "boss_id": boss_id,
        "msg_id": msg_id,
        "sender_username": sender_username,
        "text": text,
        "freeform": True,
        "reply_to_msg_id": reply_to_msg_id,
        "reply_to_text": reply_to_text,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    _log(f"freeform→ inbox/{path.name} text={(text or '')[:80]!r}")
    return path


# ----------------------------------------------------------------------
# Outbox sender loop — runs inside tg_listen event loop
# ----------------------------------------------------------------------

def _split_for_tg(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current = ""
    for p in paragraphs:
        if not p.strip():
            continue
        candidate = (current + "\n\n" + p) if current else p
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(p) <= limit:
            current = p
        else:
            for line in p.split("\n"):
                if len(current) + len(line) + 1 > limit:
                    if current:
                        chunks.append(current)
                    current = line[:limit]
                else:
                    current = (current + "\n" + line) if current else line
    if current:
        chunks.append(current)
    return chunks


def _is_allowed_attachment(path: Path) -> bool:
    try:
        resolved = path.resolve()
        report_root = REPORT_DIR.resolve()
    except Exception:
        return False
    return resolved == report_root or report_root in resolved.parents


def _extract_attachments(body: str) -> tuple[str, list[Path]]:
    paths: list[Path] = []

    def repl(match: re.Match) -> str:
        raw = match.group(1).strip()
        if raw:
            paths.append(Path(raw))
        return ""

    cleaned = ATTACH_MARKER_RE.sub(repl, body).strip()
    return cleaned, paths


async def _send_one(client, boss_id: int, reply_path: Path) -> bool:
    body, attachments = _extract_attachments(reply_path.read_text(encoding="utf-8"))
    if body:
        chunks = _split_for_tg(body)
        n = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            prefix = f"[{i}/{n}] " if n > 1 else ""
            await client.send_message(boss_id, prefix + chunk, parse_mode="md")
            if i < n:
                await asyncio.sleep(0.5)
    for path in attachments:
        if not _is_allowed_attachment(path):
            _log(f"blocked attachment outside report dir: {path}")
            await client.send_message(boss_id, f"附件被擋下：`{path}`", parse_mode="md")
            continue
        if not path.exists() or not path.is_file():
            await client.send_message(boss_id, f"附件不存在：`{path}`", parse_mode="md")
            continue
        await client.send_file(boss_id, str(path), caption=f"Blacksite report: {path.name}")
        await asyncio.sleep(0.5)
    return True


async def cmd_send_loop(client, persona_id: str) -> None:
    """Long-running task injected from tg_listen.run_persona().
    Polls outbox every POLL_INTERVAL_SEC; sends each reply via P01."""
    if persona_id.upper() != CMD_SEND_PERSONA:
        _log(f"[{persona_id}] not cmd-sender (CMD_SEND_PERSONA={CMD_SEND_PERSONA}); idle")
        return

    _log(f"[{persona_id}] cmd_send_loop started (poll {POLL_INTERVAL_SEC}s, "
         f"queue={OUTBOX_DIR}, limit={TG_MSG_LIMIT}c/msg)")
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    SENT_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        try:
            boss_id_raw = os.environ.get("BOSS_TG_USER_ID")
            if not boss_id_raw:
                continue
            try:
                boss_id = int(boss_id_raw)
            except ValueError:
                continue

            replies = sorted(OUTBOX_DIR.glob("*.md"))
            if not replies:
                continue

            for reply in replies:
                try:
                    await _send_one(client, boss_id, reply)
                except Exception as e:
                    _log(f"send err on {reply.name}: {type(e).__name__}: {str(e)[:200]}")
                    continue
                target = SENT_DIR / reply.name
                try:
                    shutil.move(str(reply), str(target))
                    _log(f"[{persona_id}] DM'd cmd reply {reply.name}")
                except Exception as e:
                    _log(f"move err: {type(e).__name__}: {e}")
        except Exception as e:
            _log(f"loop err: {type(e).__name__}: {str(e)[:200]}")
