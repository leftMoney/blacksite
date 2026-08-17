"""
Blacksite — Gmail IMAP OTP / verification-code poller.

Each persona's Gmail inbox is monitored via IMAP using the persona-specific
APP PASSWORD (NOT the regular Gmail password — Google requires app-
specific passwords for IMAP since 2022 deprecation of "less secure apps").

Boss task (one-time per persona):
  1. Sign in to https://myaccount.google.com/apppasswords with the
     persona's Gmail
  2. Generate an app password named "Blacksite IMAP poller"
  3. Paste the 16-char string into .env as PERSONA_<id>_GMAIL_APP_PWD

Engine then calls poll_otp() with persona_id + sender_filter to retrieve
the latest unread verification code from that sender domain.

Usage:
  from agents._common.email_otp_poller import poll_otp
  code = poll_otp("P03", sender_contains="bigo.tv", timeout_s=120)
"""

from __future__ import annotations

import imaplib
import os
import re
import time
from email import message_from_bytes
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Common verification-code patterns — 4 to 8 digits, optionally with -
_CODE_RE = re.compile(r"\b(\d{4,8})\b")
# Stronger pattern: looks for "code" / "verification" / "verify" near digits
# TODO: set UI markers for your instance's language — add the target language's
# words for "code" / "confirm" to this alternation.
_CONTEXT_CODE_RE = re.compile(
    r"(?:code|verification|verify|confirm)[^\n]{0,40}?(\d{4,8})",
    re.IGNORECASE,
)

# X suspicious-login emails may send alphanumeric single-use codes in the
# subject/body (for example 38kkvzbt). Keep numeric fallback below, but make
# context-aware extraction accept letters so X does not return stale digits.
_CONTEXT_CODE_RE = re.compile(
    r"\b(?:confirmation code|single-use code|verification code|code|verify|confirm)\b[^\n]{0,80}?\b((?=[a-z0-9]*\d)[a-z0-9]{4,10})\b",
    re.IGNORECASE,
)


def _persona_creds(persona_id: str) -> tuple[str, str]:
    """Read persona's Gmail + app password from .env."""
    user = os.environ.get(f"PERSONA_{persona_id}_GMAIL")
    pwd = os.environ.get(f"PERSONA_{persona_id}_GMAIL_APP_PWD")
    if not user:
        raise RuntimeError(f"PERSONA_{persona_id}_GMAIL not set in .env")
    if not pwd or pwd == "__GENERATE_BOSS_TASK__":
        raise RuntimeError(
            f"PERSONA_{persona_id}_GMAIL_APP_PWD not generated yet. Boss "
            f"task: https://myaccount.google.com/apppasswords (5 min)"
        )
    return user, pwd


def _imap_login(persona_id: str) -> imaplib.IMAP4_SSL:
    user, pwd = _persona_creds(persona_id)
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(user, pwd)
    return M


def _iter_recent_messages(M: imaplib.IMAP4_SSL, max_count: int = 30) -> Iterator[dict]:
    """Yield recent INBOX messages, newest first."""
    M.select("INBOX")
    typ, data = M.search(None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return
    ids = data[0].split()
    for raw_id in reversed(ids[-max_count:]):  # newest first
        typ, msg_data = M.fetch(raw_id, "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            continue
        raw = msg_data[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            continue
        msg = message_from_bytes(raw)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += payload.decode("utf-8", errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="replace")
        try:
            ts = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
        except Exception:
            ts = None
        yield {
            "id": raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id),
            "from": msg.get("From", ""),
            "subject": msg.get("Subject", ""),
            "ts": ts,
            "body": body,
        }


def poll_otp(
    persona_id: str,
    sender_contains: str = "",
    subject_contains: str = "",
    body_contains: str = "",
    timeout_s: int = 120,
    poll_interval_s: int = 5,
    after_ts=None,
) -> str | None:
    """Poll persona's Gmail inbox for a verification code matching the
    filter criteria. Returns the code string or None on timeout.

    Filters are AND-combined (case-insensitive substring match). after_ts
    can be a datetime — only messages newer than this are considered (use
    `datetime.now()` before triggering registration to avoid stale codes).
    """
    deadline = time.monotonic() + timeout_s
    sender_q = sender_contains.lower()
    subject_q = subject_contains.lower()
    body_q = body_contains.lower()

    while time.monotonic() < deadline:
        try:
            M = _imap_login(persona_id)
        except Exception as e:
            print(f"[email_otp] IMAP login failed for {persona_id}: {e}")
            time.sleep(poll_interval_s)
            continue

        try:
            for msg in _iter_recent_messages(M, max_count=30):
                if after_ts and msg["ts"] and msg["ts"] < after_ts:
                    continue
                if sender_q and sender_q not in msg["from"].lower():
                    continue
                if subject_q and subject_q not in msg["subject"].lower():
                    continue
                if body_q and body_q not in msg["body"].lower():
                    continue
                # Try context-aware code pattern first, then plain digits
                m = _CONTEXT_CODE_RE.search(msg["body"]) or _CONTEXT_CODE_RE.search(msg["subject"])
                if not m:
                    m = _CODE_RE.search(msg["body"]) or _CODE_RE.search(msg["subject"])
                if m:
                    code = m.group(1)
                    print(f"[email_otp] {persona_id} got code from {msg['from'][:40]}: {code}")
                    return code
        finally:
            try:
                M.logout()
            except Exception:
                pass

        time.sleep(poll_interval_s)

    print(f"[email_otp] {persona_id} timeout after {timeout_s}s")
    return None


if __name__ == "__main__":
    # Self-test: try to poll P03's inbox for any recent Gmail security alert
    import sys
    persona = sys.argv[1] if len(sys.argv) > 1 else "P03"
    print(f"polling {persona} inbox for any recent verification email (60s timeout)...")
    code = poll_otp(persona, sender_contains="", timeout_s=60)
    print(f"result: {code}")
