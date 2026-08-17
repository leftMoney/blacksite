"""
Blacksite — Telegram message listener daemon.

Long-running listener; one client per persona. Subscribes to all dialogs and
writes new messages to JSONL raw store, partitioned by persona/date (Bangkok TZ).

Captures (per message):
  - text + chat/sender metadata (chat_id, username, sender_id, ...)
  - engagement signals: views, reactions (count + emoji breakdown), forwards,
    replies count, edit_date
  - forward chain: fwd_from_chat_id, fwd_from_user_id
  - media: photo / voice / document / video / sticker — DOWNLOADED to
    runtime/media/tg/<persona>/<YYYY-MM-DD>/<chat_id>_<msg_id>.<ext>
    (subject to size caps in MEDIA_LIMITS); media_files[] in JSONL records
    sha256, file_size, mime_type, duration, width/height when available.

Output:
  instances/_TEMPLATE/runtime/raw/<persona>/<YYYY-MM-DD>.jsonl
  instances/_TEMPLATE/runtime/media/tg/<persona>/<YYYY-MM-DD>/...

Usage:
  py agents/telegram/tg_listen.py                # all personas with sessions
  py agents/telegram/tg_listen.py P01 P02        # specific personas

Termination:
  Ctrl-C (or SIGTERM in service mode). Persistent: wrap with Task Scheduler /
  pythonw / nssm / WSL2 systemd in v2.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    DocumentAttributeImageSize,
    DocumentAttributeVideo,
    MessageMediaDocument,
    MessageMediaPhoto,
)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))   # so `from processors.funnel_join import join_loop` resolves
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
SESSION_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "sessions"
RAW_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "raw"
MEDIA_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "media" / "tg"
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))  # Asia/Bangkok per CLAUDE.md §6.4

# Per-kind size caps (bytes). Skip download if media exceeds. Photos always
# downloaded (small); voice always downloaded (small + AI-transcribable);
# documents capped at 30MB (most config/credentials/wallet artifacts fit);
# videos capped at 80MB (most TG forum videos are short clips; long videos
# typically rebroadcast YouTube anyway).
MEDIA_LIMITS = {
    "photo":    1024 * 1024 * 20,    # 20 MB (ultra-rare to exceed)
    "voice":    1024 * 1024 * 30,    # 30 MB (~50 min audio)
    "document": 1024 * 1024 * 30,
    "video":    1024 * 1024 * 80,
    "sticker":  1024 * 1024 * 5,
    "gif":      1024 * 1024 * 20,
}


def now_bkk() -> datetime:
    return datetime.now(TZ)


def classify_media_kind(media) -> str | None:
    """Bucket telethon media object into our kind taxonomy."""
    if media is None:
        return None
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc is None:
            return "document"
        attrs = getattr(doc, "attributes", []) or []
        for a in attrs:
            if isinstance(a, DocumentAttributeAudio):
                return "voice" if getattr(a, "voice", False) else "audio"
            if isinstance(a, DocumentAttributeVideo):
                return "video"
        mime = (getattr(doc, "mime_type", None) or "").lower()
        if mime.startswith("image/"):
            if "gif" in mime:
                return "gif"
            return "photo"
        if mime == "image/webp":
            return "sticker"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        return "document"
    return type(media).__name__


def serialize_reactions(reactions) -> tuple[int, list[dict]]:
    """telethon MessageReactions → (total, [{emoji, count}, ...])."""
    if reactions is None:
        return 0, []
    results = getattr(reactions, "results", None) or []
    total = 0
    out = []
    for r in results:
        cnt = getattr(r, "count", 0) or 0
        total += cnt
        emoji = None
        rk = getattr(r, "reaction", None)
        if rk is not None:
            emoji = getattr(rk, "emoticon", None) or getattr(rk, "document_id", None)
        out.append({"emoji": str(emoji) if emoji is not None else None, "count": cnt})
    return total, out


def serialize_message(persona_id: str, event) -> dict:
    msg = event.message
    chat = msg.chat

    chat_id = getattr(chat, "id", None)
    if chat_id is None and msg.peer_id is not None:
        chat_id = (
            getattr(msg.peer_id, "channel_id", None)
            or getattr(msg.peer_id, "chat_id", None)
            or getattr(msg.peer_id, "user_id", None)
        )
    chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None)
    chat_username = getattr(chat, "username", None)

    sender = msg.sender
    sender_id = getattr(sender, "id", None)
    sender_name = (
        getattr(sender, "first_name", None) or getattr(sender, "title", None)
    )
    sender_username = getattr(sender, "username", None)

    media_kind = classify_media_kind(msg.media)

    fwd_chat = None
    fwd_user = None
    if msg.fwd_from is not None and msg.fwd_from.from_id is not None:
        fwd_chat = getattr(msg.fwd_from.from_id, "channel_id", None) or getattr(
            msg.fwd_from.from_id, "chat_id", None
        )
        fwd_user = getattr(msg.fwd_from.from_id, "user_id", None)

    reactions_total, reactions_breakdown = serialize_reactions(msg.reactions)

    replies_count = None
    if msg.replies is not None:
        replies_count = getattr(msg.replies, "replies", None)

    edit_ts = None
    if msg.edit_date is not None:
        try:
            edit_ts = msg.edit_date.astimezone(TZ).isoformat(timespec="seconds")
        except Exception:
            pass

    return {
        "ts": now_bkk().isoformat(timespec="seconds"),
        "persona": persona_id,
        "chat_id": chat_id,
        "chat_title": chat_title,
        "chat_username": chat_username,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "sender_username": sender_username,
        "msg_id": msg.id,
        "text": msg.message or "",
        "media_kind": media_kind,
        "fwd_from_chat_id": fwd_chat,
        "fwd_from_user_id": fwd_user,
        "reply_to_msg_id": getattr(msg.reply_to, "reply_to_msg_id", None) if msg.reply_to else None,
        # NEW engagement signals
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "replies": replies_count,
        "reactions_total": reactions_total,
        "reactions": reactions_breakdown,
        "edit_ts": edit_ts,
        # media_files filled async by download_media_for_message; default empty
        "media_files": [],
    }


def media_size_for(media) -> int:
    """Best-effort size hint from telethon media (0 if unknown)."""
    if isinstance(media, MessageMediaPhoto):
        # Photos: largest size variant
        photo = media.photo
        sizes = getattr(photo, "sizes", []) or []
        max_s = 0
        for s in sizes:
            sz = getattr(s, "size", 0) or 0
            if sz > max_s:
                max_s = sz
        return max_s
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc is None:
            return 0
        return getattr(doc, "size", 0) or 0
    return 0


def media_dimensions(media) -> tuple[int | None, int | None, float | None]:
    """Return (width, height, duration_s) when available."""
    w = h = None
    dur = None
    if isinstance(media, MessageMediaPhoto):
        sizes = getattr(media.photo, "sizes", []) or []
        # Pick max-area variant with w/h attributes
        for s in sizes:
            sw = getattr(s, "w", None)
            sh = getattr(s, "h", None)
            if sw and sh:
                if w is None or sw * sh > (w or 0) * (h or 0):
                    w, h = sw, sh
    elif isinstance(media, MessageMediaDocument):
        doc = media.document
        attrs = getattr(doc, "attributes", []) or []
        for a in attrs:
            if isinstance(a, DocumentAttributeImageSize):
                w, h = a.w, a.h
            elif isinstance(a, DocumentAttributeVideo):
                w, h = a.w, a.h
                dur = float(getattr(a, "duration", 0) or 0) or None
            elif isinstance(a, DocumentAttributeAudio):
                dur = float(getattr(a, "duration", 0) or 0) or None
    return w, h, dur


def media_filename_hint(media, msg_id: int, chat_id, kind: str) -> str:
    """Return relative filename (without dir prefix). Stable per (chat,msg)."""
    ext = ""
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        attrs = getattr(doc, "attributes", []) or []
        for a in attrs:
            if isinstance(a, DocumentAttributeFilename) and a.file_name:
                _, e = os.path.splitext(a.file_name)
                if e:
                    ext = e
                    break
        if not ext:
            mime = (getattr(doc, "mime_type", None) or "").lower()
            ext = mimetypes.guess_extension(mime) or ""
    if not ext:
        ext = {"photo": ".jpg", "voice": ".ogg", "video": ".mp4",
               "sticker": ".webp", "gif": ".mp4", "audio": ".mp3"}.get(kind, "")
    return f"{chat_id}_{msg_id}{ext}"


async def download_media_for_message(client, msg, persona_id: str, kind: str) -> dict | None:
    """Download a single message's media to disk; return media_files entry."""
    if msg.media is None or kind is None:
        return None
    cap = MEDIA_LIMITS.get(kind, 0)
    size_hint = media_size_for(msg.media)
    if cap and size_hint and size_hint > cap:
        return {
            "media_kind": kind,
            "skipped": "size_over_cap",
            "file_size": size_hint,
            "cap": cap,
        }
    today = now_bkk().strftime("%Y-%m-%d")
    chat_id = getattr(msg.chat, "id", None) or getattr(msg.peer_id, "channel_id", None) or 0
    out_dir = MEDIA_DIR / persona_id / today
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = media_filename_hint(msg.media, msg.id, chat_id, kind)
    out_path = out_dir / fname
    try:
        saved = await client.download_media(msg, file=str(out_path))
        if saved is None:
            return {"media_kind": kind, "skipped": "download_returned_none"}
    except Exception as e:
        return {"media_kind": kind, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    saved_path = Path(saved)
    if not saved_path.exists():
        return {"media_kind": kind, "skipped": "file_missing_after_save"}
    file_size = saved_path.stat().st_size
    sha = hashlib.sha256()
    try:
        with saved_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha.update(chunk)
        sha_hex = sha.hexdigest()
    except Exception:
        sha_hex = None
    w, h, dur = media_dimensions(msg.media)
    mime = None
    if isinstance(msg.media, MessageMediaDocument) and msg.media.document is not None:
        mime = getattr(msg.media.document, "mime_type", None)
    rel = str(saved_path.relative_to(ROOT)).replace("\\", "/")
    return {
        "media_kind": kind,
        "file_path": rel,
        "file_size": file_size,
        "mime_type": mime,
        "duration_s": dur,
        "width": w,
        "height": h,
        "sha256": sha_hex,
    }


def write_jsonl(persona_id: str, record: dict) -> None:
    today = now_bkk().strftime("%Y-%m-%d")
    out_dir = RAW_DIR / persona_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{today}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"tg_listen_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


async def run_persona(persona_id: str, api_id: int, api_hash: str) -> None:
    session_path = str(SESSION_DIR / f"{persona_id}.session")
    if not Path(session_path).exists():
        log_line(f"[{persona_id}] NO_SESSION at {session_path}")
        return

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        log_line(f"[{persona_id}] NOT_AUTHORIZED")
        await client.disconnect()
        return

    me = await client.get_me()
    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] [{persona_id}] "
        f"listener up: name={me.first_name!r} id={me.id}"
    )

    # M4.5d funnel auto-join — runs alongside listener inside the same
    # asyncio loop & shares the telethon client (no session-lock conflict).
    try:
        from processors.funnel_join import join_loop
        asyncio.create_task(join_loop(client, persona_id))
        log_line(f"[{persona_id}] funnel join_loop scheduled")
    except Exception as e:
        log_line(f"[{persona_id}] funnel join_loop NOT started: {type(e).__name__}: {e}")

    # Daily joined-edge liveness probe — detects channels that died after
    # we joined (channel went private / persona kicked / channel deleted)
    # and transitions them to terminal `joined_then_dead`. Boss directive
    # 2026-05-15: 「死掉就不追了，不該被卡住一直撞牆」.
    try:
        from processors.funnel_health_check import health_check_loop
        asyncio.create_task(health_check_loop(client, persona_id))
        log_line(f"[{persona_id}] funnel health_check_loop scheduled")
    except Exception as e:
        log_line(f"[{persona_id}] funnel health_check_loop NOT started: {type(e).__name__}: {e}")

    # M6 daily-brief DM sender — only fires on the configured BRIEF_SENDER_PERSONA.
    try:
        from agents.telegram.brief_send import brief_send_loop
        asyncio.create_task(brief_send_loop(client, persona_id))
        log_line(f"[{persona_id}] brief_send_loop scheduled")
    except Exception as e:
        log_line(f"[{persona_id}] brief_send_loop NOT started: {type(e).__name__}: {e}")

    # M6.5 cmd outbox sender — only fires on configured CMD_SEND_PERSONA.
    try:
        from agents.telegram.cmd_handler import cmd_send_loop
        asyncio.create_task(cmd_send_loop(client, persona_id))
        log_line(f"[{persona_id}] cmd_send_loop scheduled")
    except Exception as e:
        log_line(f"[{persona_id}] cmd_send_loop NOT started: {type(e).__name__}: {e}")

    # M7 TG bridge — replaces scheduled-task `blacksite-tg-cmd` for freeform
    # queries. Spawns claude.exe --print on each pending inbox JSON, captures
    # reply, writes outbox. 0-token baseline (only fires on freeform).
    try:
        from agents.telegram.tg_bridge import bridge_loop
        asyncio.create_task(bridge_loop(client, persona_id))
        log_line(f"[{persona_id}] tg_bridge bridge_loop scheduled")
    except Exception as e:
        log_line(f"[{persona_id}] tg_bridge bridge_loop NOT started: {type(e).__name__}: {e}")

    # Capture boss's DM sender_id when he first messages this persona.
    # Logs to runtime/logs/inbound_dm_<date>.log so the M6 setup-flow can grep
    # the user_id without polluting the main listener log. ALSO writes any
    # message from the configured boss user_id to the cmd handler inbox so
    # the scheduled-task can compose a reply.
    @client.on(events.NewMessage(incoming=True))
    async def boss_dm_capturer(event):
        try:
            if not event.is_private:
                return
            # OPSEC 5/2 fix (boss observation: Commander 對話框永遠不顯示已讀，
            # 像假帳號): mark inbound DM as read. A real person 開 TG
            # 看到 DM 會自然 trigger her client's read receipt. Without
            # this, sender 永遠看到「送達未讀」, 即時 reply 都救不回來。
            # mark_read failure should not break the rest of the handler.
            try:
                await event.message.mark_read()
            except Exception as e:
                log_line(f"[{persona_id}] mark_read err (non-fatal): {type(e).__name__}: {e}")

            sender = await event.get_sender()
            sid = getattr(sender, "id", None)
            uname = getattr(sender, "username", None) or ""
            first = getattr(sender, "first_name", None) or ""
            text = event.message.message or ""
            preview = text[:80]
            line = (f"[{datetime.now(TZ).isoformat(timespec='seconds')}] "
                    f"[{persona_id}] inbound_DM from sender_id={sid} "
                    f"username=@{uname} name={first!r}: {preview!r}")
            with (LOG_DIR / f"inbound_dm_{datetime.now(TZ).strftime('%Y-%m-%d')}.log").open("a", encoding="utf-8") as f:
                f.write(line + "\n")

            # If the inbound DM is from the configured boss user, write
            # the message to the cmd handler inbox so the scheduled-task
            # can interpret + respond. Only the configured CMD_SEND_PERSONA
            # writes to inbox to avoid duplicate processing if boss DMs both
            # personas (rare but possible).
            boss_id_raw = os.environ.get("BOSS_TG_USER_ID")
            cmd_persona = os.environ.get("CMD_SEND_PERSONA", "P01").upper()
            if (sid and boss_id_raw and str(sid) == boss_id_raw
                    and persona_id.upper() == cmd_persona
                    and text.strip()):
                try:
                    from agents.telegram.cmd_handler import write_inbox
                    # Fetch replied-to message text while still in async context.
                    reply_to_id: int | None = None
                    reply_to_text: str | None = None
                    if event.message.is_reply:
                        try:
                            reply_msg = await event.message.get_reply_message()
                            if reply_msg:
                                reply_to_id = reply_msg.id
                                reply_to_text = (reply_msg.message or "").strip() or None
                        except Exception as re:
                            reply_to_id = getattr(getattr(event.message, "reply_to", None), "reply_to_msg_id", None)
                            log_line(f"[{persona_id}] reply fetch err (non-fatal): {type(re).__name__}: {re}")
                    write_inbox(boss_id=sid, msg_id=event.message.id,
                                text=text, sender_username=uname,
                                received_via_persona=persona_id,
                                reply_to_msg_id=reply_to_id,
                                reply_to_text=reply_to_text)
                except Exception as e:
                    log_line(f"[{persona_id}] cmd inbox write err: {type(e).__name__}: {e}")
        except Exception as e:
            log_line(f"[{persona_id}] boss_dm_capturer err: {type(e).__name__}: {e}")

    # Silent-disconnect watchdog (5/3 fix for "Commander 沒已讀" recurrence).
    # Telethon `run_until_disconnected()` does NOT return when the server
    # silently stops pushing updates — client.is_connected() reports True
    # but no events fire. Witnessed 5/3 16:14-16:21 P01 channel silent for
    # 7+ min while P02 kept receiving in same subprocess. Boss DM commander →
    # mark_read never fired → 「沒已讀」symptom. supervise_listener saw the
    # process alive and didn't restart.
    #
    # Fix: track last update wall-clock per persona; every 60s check idle.
    # If idle > 10 min (P01/P02 high-traffic chats easily see msgs every
    # few min during waking hours), probe with client.get_me() — if probe
    # times out / errors, the connection is dead → os._exit(2) the WHOLE
    # subprocess so supervise_listener restarts both personas with clean
    # telethon state. Probe success resets the timer (genuinely quiet
    # channels at 4am don't trigger false restart).
    WATCHDOG_IDLE_SEC = int(os.environ.get("TG_LISTEN_WATCHDOG_IDLE_SEC", "600"))
    WATCHDOG_PROBE_TIMEOUT = int(os.environ.get("TG_LISTEN_WATCHDOG_PROBE_TIMEOUT", "10"))
    state = {"last_update_ts": now_bkk()}

    async def _watchdog():
        while True:
            await asyncio.sleep(60)
            idle = (now_bkk() - state["last_update_ts"]).total_seconds()
            if idle < WATCHDOG_IDLE_SEC:
                continue
            try:
                await asyncio.wait_for(client.get_me(), timeout=WATCHDOG_PROBE_TIMEOUT)
                state["last_update_ts"] = now_bkk()
                log_line(f"[{persona_id}] watchdog probe OK after {idle:.0f}s idle (quiet channels)")
            except Exception as e:
                log_line(
                    f"[{persona_id}] WATCHDOG silent_disconnect detected "
                    f"(idle={idle:.0f}s probe_err={type(e).__name__}: {str(e)[:80]}) — "
                    f"os._exit(2) for supervise_listener full restart"
                )
                os._exit(2)

    asyncio.create_task(_watchdog())

    @client.on(events.NewMessage)
    async def handler(event):
        state["last_update_ts"] = now_bkk()
        try:
            record = serialize_message(persona_id, event)
            # Best-effort media download — failure does not block the JSONL write
            kind = record.get("media_kind")
            if kind and event.message.media is not None:
                try:
                    entry = await asyncio.wait_for(
                        download_media_for_message(client, event.message, persona_id, kind),
                        timeout=120,
                    )
                    if entry:
                        record["media_files"] = [entry]
                except asyncio.TimeoutError:
                    record["media_files"] = [{"media_kind": kind, "error": "download_timeout"}]
                except Exception as e:
                    record["media_files"] = [{"media_kind": kind, "error": f"{type(e).__name__}: {str(e)[:120]}"}]
            write_jsonl(persona_id, record)
            preview = (record["text"] or "")[:80].replace("\n", " ")
            mfs = record.get("media_files") or []
            mtag = ""
            if mfs:
                e0 = mfs[0]
                if e0.get("file_path"):
                    mtag = f" [📎{e0['media_kind']} {e0.get('file_size',0)}B]"
                elif e0.get("error"):
                    mtag = f" [📎{e0['media_kind']}!{e0['error'][:30]}]"
                elif e0.get("skipped"):
                    mtag = f" [📎{e0['media_kind']}~{e0['skipped']}]"
            engagement = ""
            if record.get("views") is not None or record.get("reactions_total"):
                engagement = (
                    f" 👁{record.get('views') or 0}"
                    f" ❤{record.get('reactions_total') or 0}"
                )
            log_line(
                f"[{record['ts']}] {persona_id} "
                f"@{record['chat_username'] or record['chat_id']} "
                f"<{record['sender_name'] or '?'}>:{engagement} {preview}{mtag}"
            )
        except Exception as e:
            log_line(f"[{persona_id}] handler_err: {type(e).__name__}: {e}")

    await client.run_until_disconnected()


async def main() -> None:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    if len(sys.argv) > 1:
        persona_ids = [p.upper() for p in sys.argv[1:]]
    else:
        persona_ids = sorted(p.stem for p in SESSION_DIR.glob("P*.session"))
    if not persona_ids:
        sys.exit("no personas found — run tg_login.py first")
    log_line(f"[{now_bkk().isoformat()}] tg_listen starting personas={persona_ids}")
    await asyncio.gather(*(run_persona(pid, api_id, api_hash) for pid in persona_ids))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
