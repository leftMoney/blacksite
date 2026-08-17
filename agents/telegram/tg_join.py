"""
Blacksite — Telegram channel join with stagger + jitter, idempotent, policy-aware.

Loads `agents/telegram/join_plan.yaml` (or CLI ad-hoc target list), interleaves
across personas, joins with random inter-step delay. Skips if already joined.
Handles FloodWait by sleeping the suggested duration once and retrying once.

Usage:
  py agents/telegram/tg_join.py --plan agents/telegram/join_plan.yaml
  py agents/telegram/tg_join.py P01 @example_channel_1 @example_channel_2
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UserAlreadyParticipantError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
)
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
SESSION_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "sessions"
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

JITTER_MIN_S = 90
JITTER_MAX_S = 180
TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"tg_join_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_clients(persona_ids):
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    clients = {}
    for pid in persona_ids:
        sp = str(SESSION_DIR / f"{pid}.session")
        if not Path(sp).exists():
            log_line(f"[ERR] {pid} session missing — run tg_login.py first")
            continue
        c = TelegramClient(sp, api_id, api_hash)
        c.connect()
        if not c.is_user_authorized():
            log_line(f"[ERR] {pid} session not authorized")
            c.disconnect()
            continue
        clients[pid] = c
    return clients


def already_joined(client, target: str) -> bool:
    target_lc = target.lstrip("@").lower()
    for d in client.iter_dialogs(limit=200):
        u = getattr(d.entity, "username", None)
        if u and u.lower() == target_lc:
            return True
    return False


def _join_public(client, username: str):
    try:
        if already_joined(client, username):
            return "already", f"@{username}"
        client(JoinChannelRequest(username))
        return "joined", f"@{username}"
    except UserAlreadyParticipantError:
        return "already", f"@{username}"
    except (UsernameNotOccupiedError, UsernameInvalidError):
        return "error", f"@{username} invalid_username"
    except ChannelPrivateError:
        return "error", f"@{username} private_or_banned"
    except FloodWaitError as e:
        wait = max(e.seconds, 60) + 5
        log_line(f"  ! FloodWait {e.seconds}s on @{username}; sleeping {wait}s then retry once")
        time.sleep(wait)
        try:
            client(JoinChannelRequest(username))
            return "joined_after_wait", f"@{username}"
        except Exception as e2:
            return "error", f"@{username} retry_failed: {type(e2).__name__}"
    except Exception as e:
        return "error", f"@{username} {type(e).__name__}: {e}"


def _join_invite(client, invite_hash: str):
    short = f"+{invite_hash[:10]}…"
    try:
        client(ImportChatInviteRequest(invite_hash))
        return "joined", short
    except UserAlreadyParticipantError:
        return "already", short
    except (InviteHashExpiredError, InviteHashInvalidError):
        return "error", f"{short} invalid_or_expired"
    except FloodWaitError as e:
        wait = max(e.seconds, 60) + 5
        log_line(f"  ! FloodWait {e.seconds}s on {short}; sleeping {wait}s then retry once")
        time.sleep(wait)
        try:
            client(ImportChatInviteRequest(invite_hash))
            return "joined_after_wait", short
        except Exception as e2:
            return "error", f"{short} retry_failed: {type(e2).__name__}"
    except Exception as e:
        return "error", f"{short} {type(e).__name__}: {e}"


def parse_target(target: str):
    if target.startswith(("https://t.me/+", "t.me/+")):
        return "invite", target.split("+")[-1].rstrip("/")
    if target.startswith("+") and not target[1:].isdigit():
        return "invite", target.lstrip("+")
    return "public", target.lstrip("@")


def join_one(client, target: str):
    kind, value = parse_target(target)
    if kind == "invite":
        return _join_invite(client, value)
    return _join_public(client, value)


def interleave(persona_targets):
    result = []
    if not persona_targets:
        return result
    max_len = max(len(v) for v in persona_targets.values())
    for i in range(max_len):
        for pid, ts in persona_targets.items():
            if i < len(ts):
                result.append((pid, ts[i]))
    return result


def execute(plan, clients):
    total = len(plan)
    for i, (pid, target) in enumerate(plan, 1):
        if pid not in clients:
            log_line(f"[{now_iso()}] {pid} skip {target} (no_client)")
            continue
        status, detail = join_one(clients[pid], target)
        log_line(f"[{now_iso()}] [{i}/{total}] {pid} {status:<18} {detail}")
        if i < total:
            sleep_s = random.uniform(JITTER_MIN_S, JITTER_MAX_S)
            log_line(f"  ↳ sleep {sleep_s:.0f}s")
            time.sleep(sleep_s)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", help="YAML: {persona_id: [target, ...]}")
    parser.add_argument("persona_id", nargs="?")
    parser.add_argument("targets", nargs="*")
    args = parser.parse_args()

    if args.plan:
        with Path(args.plan).open(encoding="utf-8") as f:
            plan_data = yaml.safe_load(f)
        persona_targets = {pid.upper(): list(targets) for pid, targets in plan_data.items()}
    elif args.persona_id and args.targets:
        persona_targets = {args.persona_id.upper(): list(args.targets)}
    else:
        sys.exit("Usage: --plan <yaml> | <persona_id> <target ...>")

    clients = load_clients(list(persona_targets.keys()))
    if not clients:
        sys.exit("no authorized personas — run tg_login.py first")

    plan = interleave(persona_targets)
    eta_min = len(plan) * (JITTER_MIN_S + JITTER_MAX_S) / 2 / 60
    log_line(f"[{now_iso()}] tg_join start: {len(plan)} ops, ETA ~{eta_min:.1f} min")
    try:
        execute(plan, clients)
    finally:
        for c in clients.values():
            c.disconnect()
    log_line(f"[{now_iso()}] tg_join done")


if __name__ == "__main__":
    main()
