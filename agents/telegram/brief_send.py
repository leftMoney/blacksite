"""
M6 — TG-DM brief sender loop.

Runs INSIDE tg_listen's event loop alongside the listener event handler and
M4.5d join_loop. Polls `runtime/briefs/queue/pending_*.md` every 5 min and,
when found, sends to boss via DM through the configured brief-sender persona
(default P01). Moves sent files to runtime/briefs/sent/.

Configuration:
  BRIEF_SENDER_PERSONA   — which persona's session does the sending. Default P01.
  BOSS_TG_USER_ID        — boss's numeric TG user_id. Set after he DMs the
                           persona once and we grep his sender_external_id from
                           the raw JSONL. Without this, send is skipped (file
                           stays in queue).

Send strategy: TG DM has 4096-char limit per message. We split long briefs at
paragraph boundaries; each chunk gets a `[N/M]` prefix for ordering.

Idempotency: file → sent/ move is atomic; if listener crashes between send +
move, on restart the loop re-finds the file and re-sends. Boss may get
duplicate brief in rare crashes; acceptable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
QUEUE_DIR = RUNTIME_DIR / "briefs" / "queue"
SENT_DIR = RUNTIME_DIR / "briefs" / "sent"
DEDUPE_SKIPPED_DIR = SENT_DIR / "dedupe_skipped"
DEDUPE_INDEX_PATH = SENT_DIR / ".dedupe_index.json"
LOG_DIR = RUNTIME_DIR / "logs"

BRIEF_SENDER = os.environ.get("BRIEF_SENDER_PERSONA", "P01").upper()
POLL_INTERVAL_SEC = int(os.environ.get("BRIEF_SEND_POLL_SEC", "300"))  # 5 min default
TG_MSG_LIMIT = 4000  # safety margin under TG's 4096 hard limit
# Bug C (boss 2026-05-19): suppress repeat boss DMs whose normalized body hash
# matches one sent in the last DEDUPE_TTL_HOURS. Files skipped move to
# briefs/sent/dedupe_skipped/ so boss can audit; per-skip log_event recorded.
DEDUPE_TTL_HOURS = int(os.environ.get("BRIEF_DEDUPE_TTL_H", "24"))
# Patterns stripped before hashing so cosmetic diffs (timestamps, counter ids)
# don't break dedupe. Hand-tuned for known brief generators.
_TS_RE = re.compile(r"\d{4}-?\d{2}-?\d{2}T?\d{2}[:\-]?\d{2}[:\-]?\d{2}(?:[+\-]\d{2}:?\d{2})?")
_COUNTER_TAIL_RE = re.compile(r"_(?:audit|alert|fyi)_?\d+\.md$", re.IGNORECASE)

TZ = timezone(timedelta(hours=7))


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    line = f"[{_now()}] [brief-send] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"brief_send_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _refresh_audit_after_send(brief_name: str) -> None:
    try:
        from processors.org_task_audit_refresh import refresh_org_task_audit
        refresh_org_task_audit(f"brief_sent:{brief_name}")
    except Exception:
        pass


def _brief_dedupe_key(body: str) -> str:
    """Exact-after-timestamp-strip key. Two briefs with byte-identical content
    save for ISO timestamps collapse to one key."""
    stripped = _TS_RE.sub("<TS>", body)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return hashlib.sha1(stripped.encode("utf-8")).hexdigest()[:16]


def _brief_structural_key(body: str) -> str:
    """Looser key: also collapses digit runs to <N>, so briefs that differ only
    in counters (候選 251 vs 253) collapse. Used as 2nd-line dedupe so structurally
    identical messages don't slip past _brief_dedupe_key when counters drift."""
    stripped = _TS_RE.sub("<TS>", body)
    stripped = re.sub(r"\d+", "<N>", stripped)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return hashlib.sha1(stripped.encode("utf-8")).hexdigest()[:16]


def _load_dedupe_index() -> dict:
    try:
        data = json.loads(DEDUPE_INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_dedupe_index(index: dict) -> None:
    cutoff = (datetime.now(TZ) - timedelta(hours=DEDUPE_TTL_HOURS * 2)).isoformat(timespec="seconds")
    trimmed = {
        k: v for k, v in index.items()
        if isinstance(v, dict) and v.get("last_sent", "") >= cutoff
    }
    try:
        DEDUPE_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEDUPE_INDEX_PATH.write_text(
            json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        _log(f"dedupe index save fail: {type(e).__name__}: {e}")


def _check_dedupe(brief_path: Path) -> tuple[str, str, dict | None, float | None, str]:
    """Return (dkey, skey, prev_entry, age_hours, match_kind).
    match_kind ∈ {'exact', 'structural', ''}. If age < TTL with non-empty match_kind
    → caller should suppress."""
    try:
        body = brief_path.read_text(encoding="utf-8")
    except Exception:
        return ("", "", None, None, "")
    dkey = _brief_dedupe_key(body)
    skey = _brief_structural_key(body)
    index = _load_dedupe_index()
    for key, kind in ((dkey, "exact"), (skey, "structural")):
        entry = index.get(key)
        if not entry or not isinstance(entry, dict):
            continue
        last_sent_iso = entry.get("last_sent", "")
        try:
            last_sent = datetime.fromisoformat(last_sent_iso)
        except Exception:
            continue
        age_h = (datetime.now(TZ) - last_sent).total_seconds() / 3600
        return (dkey, skey, entry, age_h, kind)
    return (dkey, skey, None, None, "")


def _record_dedupe_send(dkey: str, skey: str, brief_name: str) -> None:
    index = _load_dedupe_index()
    now_iso_str = datetime.now(TZ).isoformat(timespec="seconds")
    for key in {dkey, skey} - {""}:
        prev = index.get(key, {})
        index[key] = {
            "last_sent": now_iso_str,
            "last_brief_name": brief_name,
            "send_count": int((prev or {}).get("send_count", 0)) + 1,
        }
    _save_dedupe_index(index)


def _archive_dedupe_skipped(brief_path: Path, dkey: str, prev_entry: dict, age_h: float) -> Path | None:
    try:
        DEDUPE_SKIPPED_DIR.mkdir(parents=True, exist_ok=True)
        target = DEDUPE_SKIPPED_DIR / f"skipped_{brief_path.name.replace('pending_', '')}"
        shutil.move(str(brief_path), str(target))
        return target
    except Exception as e:
        _log(f"dedupe archive fail for {brief_path.name}: {type(e).__name__}: {e}")
        return None


def _split_for_tg(text: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    """Split markdown at paragraph boundaries to stay under TG msg limit."""
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
            # Single paragraph too big — hard split on lines, then chars
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


async def _send_one(client, boss_id: int, brief_path: Path) -> bool:
    body = brief_path.read_text(encoding="utf-8")
    chunks = _split_for_tg(body)
    n = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        prefix = f"[{i}/{n}] " if n > 1 else ""
        await client.send_message(boss_id, prefix + chunk, parse_mode="md")
        if i < n:
            await asyncio.sleep(0.5)  # gentle pacing
    return True


async def brief_send_loop(client, persona_id: str) -> None:
    """Long-running task. Inject from tg_listen.run_persona() via
    asyncio.create_task(brief_send_loop(client, persona_id))."""
    if persona_id.upper() != BRIEF_SENDER:
        # Only the configured brief-sender persona runs the actual send;
        # other personas no-op (still inject the task so config is uniform).
        _log(f"[{persona_id}] not brief-sender (BRIEF_SENDER_PERSONA={BRIEF_SENDER}); idle")
        return

    _log(f"[{persona_id}] brief_send_loop started "
         f"(poll {POLL_INTERVAL_SEC}s, queue={QUEUE_DIR}, limit={TG_MSG_LIMIT}c/msg)")
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    SENT_DIR.mkdir(parents=True, exist_ok=True)

    while True:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        try:
            # Quiet hours: 23:00-09:00 GMT+7 (= 00:00-10:00 GMT+8). Defer
            # proactive DMs so boss is not woken up. Boss-initiated replies
            # (cmd_handler) are unaffected — only this brief queue is gated.
            now_h = datetime.now(TZ).hour
            if now_h >= 23 or now_h < 9:
                continue

            boss_id_raw = os.environ.get("BOSS_TG_USER_ID")
            if not boss_id_raw:
                # Quietly skip — boss hasn't given us his ID yet
                continue
            try:
                boss_id = int(boss_id_raw)
            except ValueError:
                _log(f"BOSS_TG_USER_ID not int: {boss_id_raw!r}; skipping")
                continue

            briefs = sorted(QUEUE_DIR.glob("pending_*.md"))
            if not briefs:
                continue

            for brief_path in briefs:
                dkey, skey, prev_entry, age_h, match_kind = _check_dedupe(brief_path)
                if match_kind and prev_entry and age_h is not None and age_h < DEDUPE_TTL_HOURS:
                    archived = _archive_dedupe_skipped(brief_path, dkey, prev_entry, age_h)
                    send_count = int(prev_entry.get("send_count", 0))
                    _log(
                        f"[{persona_id}] SKIP {brief_path.name} match={match_kind} "
                        f"dkey={dkey} skey={skey} age={age_h:.1f}h "
                        f"prev_sends={send_count} "
                        f"prev_name={prev_entry.get('last_brief_name','?')}"
                    )
                    try:
                        from processors.history_log import log_event
                        log_event(
                            actor="brief_send", kind="config_change", scope="brief_dedupe",
                            title=f"deduped brief {brief_path.name} ({match_kind})"[:118],
                            body=(
                                f"match={match_kind}\ndkey={dkey}\nskey={skey}\n"
                                f"age_h={age_h:.1f}\nprev_sends={send_count}\n"
                                f"prev_brief={prev_entry.get('last_brief_name')}\n"
                                f"archived_to={archived.name if archived else 'fail'}"
                            ),
                            refs=[
                                (archived.relative_to(ROOT).as_posix()
                                 if archived else brief_path.name)
                            ],
                        )
                    except Exception:
                        pass
                    continue

                try:
                    sent = await _send_one(client, boss_id, brief_path)
                except Exception as e:
                    _log(f"send err on {brief_path.name}: {type(e).__name__}: {str(e)[:200]}")
                    continue
                if sent:
                    target = SENT_DIR / brief_path.name.replace("pending_", "sent_")
                    try:
                        shutil.move(str(brief_path), str(target))
                        if dkey or skey:
                            _record_dedupe_send(dkey, skey, target.name)
                        _log(f"[{persona_id}] DM'd brief {brief_path.name} → boss ({boss_id})")
                        _refresh_audit_after_send(target.name)
                    except Exception as e:
                        _log(f"move err: {type(e).__name__}: {e}")
        except Exception as e:
            _log(f"loop err: {type(e).__name__}: {str(e)[:200]}")
