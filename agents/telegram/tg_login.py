"""
Blacksite — Telegram first-time login (two-stage, AI-driveable).

Stage 1 (no --code): connects to TG, sends SMS code to persona's phone, saves
the phone_code_hash to disk, exits. The AI driver asks the human for the SMS code.

Stage 2 (with --code <CODE>): reads phone_code_hash from disk, calls sign_in
with the code, falls back to 2FA cloud password if required, saves session.

Creates: instances/<active>/runtime/sessions/<PXX>.session

Prerequisites:
  - Python 3.10+
  - py -m pip install -r requirements.txt
  - .env contains:
      TG_API_ID, TG_API_HASH         (one-time, from https://my.telegram.org/apps)
      TG_PERSONA_NN_PHONE            (per persona)
      TG_PERSONA_NN_PASSWORD         (per persona, if 2FA cloud password set)

Usage:
  Stage 1:  py agents/telegram/tg_login.py P01
  Stage 2:  py agents/telegram/tg_login.py P01 --code 12345
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
SESSION_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)


def resolve_persona(persona_id: str) -> tuple[str, str, str | None]:
    persona_id = persona_id.upper()
    if not (persona_id.startswith("P") and persona_id[1:].isdigit()):
        sys.exit(f"persona id must look like P01/P02, got {persona_id!r}")
    persona_num = persona_id[1:]
    phone = os.environ.get(f"TG_PERSONA_{persona_num}_PHONE")
    password = os.environ.get(f"TG_PERSONA_{persona_num}_PASSWORD")
    if not phone:
        sys.exit(f"TG_PERSONA_{persona_num}_PHONE not set in .env")
    return persona_id, phone, password


def get_api_creds() -> tuple[int, str]:
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        sys.exit(
            "Missing TG_API_ID / TG_API_HASH in .env.\n"
            "Get them once from https://my.telegram.org/apps "
            "(login with any of your TG accounts, create an application — free, ~1 min)."
        )
    return int(api_id), api_hash


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("persona_id", help="e.g. P01, P02")
    parser.add_argument("--code", default=None, help="SMS code received (stage 2)")
    args = parser.parse_args()

    persona_id, phone, password = resolve_persona(args.persona_id)
    api_id, api_hash = get_api_creds()

    session_path = str(SESSION_DIR / f"{persona_id}.session")
    hash_path = SESSION_DIR / f".{persona_id}.code_hash"

    client = TelegramClient(session_path, api_id, api_hash)
    client.connect()

    try:
        if client.is_user_authorized():
            me = client.get_me()
            print(
                f"[ALREADY_AUTH] {persona_id} session is valid. "
                f"Logged in as: {me.first_name or ''} (id={me.id}, "
                f"username=@{me.username or '<none>'})"
            )
            return

        if args.code is None:
            # Stage 1: send code
            sent = client.send_code_request(phone)
            hash_path.write_text(sent.phone_code_hash, encoding="utf-8")
            print(f"[CODE_SENT] persona={persona_id} phone={phone}")
            print(f"[CODE_SENT] phone_code_hash saved -> {hash_path}")
            print(
                f"[NEXT] py agents/telegram/tg_login.py {persona_id} "
                f"--code <SMS_CODE>"
            )
            return

        # Stage 2: verify code
        if not hash_path.exists():
            sys.exit(
                f"No phone_code_hash on disk for {persona_id}. "
                f"Run stage 1 first (without --code)."
            )
        phone_code_hash = hash_path.read_text(encoding="utf-8").strip()

        try:
            client.sign_in(
                phone=phone,
                code=args.code,
                phone_code_hash=phone_code_hash,
            )
        except SessionPasswordNeededError:
            if not password:
                sys.exit(
                    f"2FA cloud password required for {persona_id} but "
                    f"TG_PERSONA_{persona_id[1:]}_PASSWORD not set in .env."
                )
            client.sign_in(password=password)

        # Cleanup ephemeral hash file
        try:
            hash_path.unlink()
        except FileNotFoundError:
            pass

        me = client.get_me()
        print(
            f"[OK] {persona_id} logged in as: {me.first_name or ''} "
            f"(id={me.id}, username=@{me.username or '<none>'})"
        )
        print(f"[OK] session saved -> {session_path}")
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()
