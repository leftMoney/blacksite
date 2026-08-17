"""
Funnel auto-join engine (M4.5d).

Designed to run INSIDE the tg_listen event loop (not as a separate process),
because telethon enforces single-session-per-connection on the SQLite session
file. `join_loop(client, persona_id)` is an asyncio task that:

  - Polls funnel_edges WHERE review_state='approved' AND join_state IN
    (not_attempted, failed_join, failed_resolve) every 25-90 min jittered
  - Executes ONE join per persona per cycle (daily cap enforced)
  - Pre-checks public channel title/size; post-checks invite-hash chat title
  - Updates funnel_edges.join_state in place

OPSEC parameters approved by boss 2026-04-29 「沒問題」:
  per-persona daily cap = 3 joins
  cooldown jitter = 25-90 min
  group size = 5 < members < 200,000
  blacklist tier-1 = child / drug / weapons / stolen-id / police-honeypot
  persona burn budget = acceptable

After a successful join, the EXISTING tg_listen NewMessage handler picks up
new chat msgs automatically — telethon subscription is implicit. So M4.5d's
join → M1 indexer → M4.5b funnel_edges → M4.5c review → M4.5d join recursion
is free, no extra wiring.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from db.schema import init_db

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))

PERSONA_DAILY_CAP = int(os.environ.get("FUNNEL_JOIN_DAILY_CAP", "3"))
COOLDOWN_MIN_MIN = int(os.environ.get("FUNNEL_JOIN_COOLDOWN_MIN", "25"))
COOLDOWN_MAX_MIN = int(os.environ.get("FUNNEL_JOIN_COOLDOWN_MAX", "90"))
GROUP_SIZE_MIN = int(os.environ.get("FUNNEL_JOIN_SIZE_MIN", "5"))
GROUP_SIZE_MAX = int(os.environ.get("FUNNEL_JOIN_SIZE_MAX", "200000"))
INITIAL_DELAY_SEC = int(os.environ.get("FUNNEL_JOIN_INITIAL_DELAY_SEC", "60"))

# === INSTANCE SAFETY BLACKLIST (customize per instance — see instances/_TEMPLATE/INSTANCE.md) ===
# Hard-block keywords: never join groups matching these. Per instance, ADD the
# target country's native-language terms for (a) law-enforcement / cyber-crime /
# anti-gambling task forces (honeypot risk) and (b) obviously illegal verticals
# (drugs, trafficking, weapons, CSAM). The English seeds below are universal.
BLACKLIST_KEYWORDS = [
    # English — universal
    "child", "kid ", "minor ", "underage", "loli", "cp ",
    "drug ", "cocaine", "heroin", "meth ",
    "weapon", "rifle", "pistol", "ak47", "gun for sale",
    "stolen", "identity theft", "credit card dump", "cvv ",
    # Police / honeypot (add target country's law-enforcement acronyms here)
    "dsi", "royal guard", "police investigation", "sting operation",
    # Obvious illegal verticals (add target country's native-language terms here)
    "drug trafficking", "human trafficking",
]


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_bkk().isoformat(timespec='seconds')}] [funnel-join] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"funnel_join_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------
# Cap + cooldown checks
# ----------------------------------------------------------------------

def _today_count(conn, persona_id: str) -> int:
    today = now_bkk().strftime("%Y-%m-%d")
    r = conn.execute(
        """SELECT COUNT(*) FROM funnel_edges
            WHERE join_persona = ? AND join_state = 'joined' AND join_at LIKE ?""",
        (persona_id, f"{today}%"),
    ).fetchone()
    return r[0] if r else 0


def _last_join_at(conn, persona_id: str) -> datetime | None:
    r = conn.execute(
        """SELECT MAX(join_at) FROM funnel_edges
            WHERE join_persona = ? AND join_state = 'joined'""",
        (persona_id,),
    ).fetchone()
    if not r or not r[0]:
        return None
    try:
        return datetime.fromisoformat(r[0])
    except Exception:
        return None


def _select_next_edge(conn, persona_id: str):
    # Two-level retry policy:
    #   (1) Edge-level — only `not_attempted` and `failed_resolve` (transient
    #       network/server blip on get_entity) are retryable. Permanent edge
    #       failures (failed_expired, failed_invalid, failed_full,
    #       failed_resolve_dead, failed_join, declined_*) stay terminal.
    #   (2) Target-level dedup — if ANY sibling edge (same to_target_kind +
    #       to_target) already reached a terminal state, skip this row.
    #       Sibling states that lock the target:
    #         * `joined`        — already in by another persona; re-joining
    #                             wastes cap and creates an OPSEC correlation
    #                             axis between personas in the same chat
    #         * permanent fails — `failed_resolve_dead`, `failed_expired`,
    #                             `failed_invalid`, `failed_bad_hash`,
    #                             `failed_join`, `failed_full`,
    #                             `declined_blacklist`, `declined_size`,
    #                             `declined_unsupported`, `failed_unknown_kind`
    #       Sibling states that do NOT lock: `not_attempted`, `failed_resolve`,
    #       `queued`, `failed_persona_at_capacity`, `failed_flood_wait` —
    #       transient OR persona-specific, leave room for another attempt.
    # Rationale: same dead target was being discovered repeatedly from
    # different source chats, creating multiple edge rows that each tried
    # once and wasted persona cap on a known-dead destination (boss 5/15
    # directive: 「死掉就不追了，不該被卡住一直撞牆」).
    return conn.execute(
        """SELECT row_id, to_target_kind, to_target, edge_kind, push_count,
                  bait_intent, from_chat_id
             FROM funnel_edges fe
            WHERE fe.review_state = 'approved'
              AND fe.join_state IN ('not_attempted', 'failed_resolve')
              AND (fe.join_persona IS NULL OR fe.join_persona = ?)
              AND NOT EXISTS (
                    SELECT 1 FROM funnel_edges sibling
                     WHERE sibling.to_target_kind = fe.to_target_kind
                       AND sibling.to_target = fe.to_target
                       AND sibling.row_id != fe.row_id
                       AND sibling.join_state IN (
                            'joined', 'joined_then_dead',
                            'failed_resolve_dead', 'failed_expired',
                            'failed_invalid', 'failed_bad_hash',
                            'failed_join', 'failed_full',
                            'declined_blacklist', 'declined_size',
                            'declined_unsupported', 'failed_unknown_kind'
                       )
              )
            ORDER BY fe.push_count DESC, fe.avg_amplification DESC
            LIMIT 1""",
        (persona_id,),
    ).fetchone()


def _matches_blacklist(text: str) -> str | None:
    """Return matching keyword or None."""
    if not text:
        return None
    low = text.lower()
    for kw in BLACKLIST_KEYWORDS:
        if kw.lower() in low:
            return kw
    return None


# ----------------------------------------------------------------------
# Telethon ops
# ----------------------------------------------------------------------

_INVITE_HASH_RE = re.compile(r"t\.me/(?:joinchat/|\+)([A-Za-z0-9_-]+)")


async def _join_invite_hash(client, target: str) -> tuple[str, str]:
    """t.me/+HASH or t.me/joinchat/HASH. Returns (state, info)."""
    m = _INVITE_HASH_RE.search(target)
    if not m:
        return "failed_bad_hash", f"could not extract hash from {target}"
    invite_hash = m.group(1)
    try:
        from telethon.tl.functions.messages import ImportChatInviteRequest
        updates = await client(ImportChatInviteRequest(invite_hash))
    except Exception as e:
        err = str(e)
        low = err.lower()
        # Telethon raises both raw error codes (INVITE_HASH_EXPIRED) and
        # friendly messages ("...has expired and is not valid anymore").
        # Match either form.
        if "INVITE_HASH_EXPIRED" in err or "expired and is not valid" in low or "has expired" in low:
            return "failed_expired", err[:200]
        if "INVITE_HASH_INVALID" in err or "invalid" in low and "invite" in low:
            return "failed_invalid", err[:200]
        if "USER_ALREADY_PARTICIPANT" in err or "already a participant" in low:
            return "joined", "already_member"
        if "USERS_TOO_MUCH" in err or "too many users" in low:
            return "failed_full", err[:200]
        if "FLOOD_WAIT" in err.upper() or "flood" in low:
            return "failed_flood_wait", err[:200]
        return "failed_join", err[:200]

    chat = updates.chats[0] if getattr(updates, "chats", None) else None
    if chat:
        title = getattr(chat, "title", "") or ""
        member_count = getattr(chat, "participants_count", None)
        kw = _matches_blacklist(title)
        if kw:
            try:
                from telethon.tl.functions.channels import LeaveChannelRequest
                await client(LeaveChannelRequest(chat))
            except Exception:
                pass
            return "declined_blacklist", f"post-join blacklist '{kw}' in title={title[:80]}"
        return "joined", f"title={title[:80]} members={member_count}"
    return "joined", "joined but no chat metadata"


async def _join_channel_ref(client, target: str) -> tuple[str, str]:
    """t.me/<username> or bare <username>. Probe via get_entity, then join."""
    handle = target.split("/")[-1] if "/" in target else target
    try:
        entity = await client.get_entity(handle)
    except Exception as e:
        err = str(e)
        err_low = err.lower()
        cls = type(e).__name__
        # Permanent: username doesn't exist / is malformed. Re-resolving will
        # never recover; mark terminal so retries don't burn the daily cap.
        permanent_classes = ("UsernameNotOccupiedError", "UsernameInvalidError")
        permanent_markers = (
            "nobody is using this username",
            "no user has",
            "username is unacceptable",
            "username_not_occupied",
            "username_invalid",
        )
        if cls in permanent_classes or any(m in err_low for m in permanent_markers):
            return "failed_resolve_dead", err[:200]
        return "failed_resolve", err[:200]

    title = (getattr(entity, "title", None) or "")[:200]
    member_count = getattr(entity, "participants_count", None)

    kw = _matches_blacklist(title)
    if kw:
        return "declined_blacklist", f"pre-join blacklist '{kw}' in title={title[:80]}"
    if member_count is not None:
        if member_count < GROUP_SIZE_MIN:
            return "declined_size", f"too small ({member_count})"
        if member_count > GROUP_SIZE_MAX:
            return "declined_size", f"too large ({member_count})"

    try:
        from telethon.tl.functions.channels import JoinChannelRequest
        await client(JoinChannelRequest(entity))
    except Exception as e:
        err = str(e)
        if "USER_ALREADY_PARTICIPANT" in err:
            return "joined", f"already_member title={title[:80]}"
        if "FLOOD_WAIT" in err.upper():
            return "failed_flood_wait", err[:200]
        if "CHANNELS_TOO_MUCH" in err:
            return "failed_persona_at_capacity", err[:200]
        return "failed_join", err[:200]
    return "joined", f"title={title[:80]} members={member_count}"


async def _execute_join(client, edge: dict) -> tuple[str, str]:
    kind = edge["to_target_kind"]
    target = edge["to_target"]
    if kind == "tg_invite":
        return await _join_invite_hash(client, target)
    if kind == "tg_channel_ref":
        return await _join_channel_ref(client, target)
    if kind == "tg_bot_deeplink":
        return "declined_unsupported", "bot deeplinks deferred to v2"
    return "failed_unknown_kind", kind


# ----------------------------------------------------------------------
# Cycle runner (one attempt per call)
# ----------------------------------------------------------------------

async def execute_pending_join(client, persona_id: str) -> dict:
    init_db()
    conn = get_connection()
    try:
        cap_count = _today_count(conn, persona_id)
        if cap_count >= PERSONA_DAILY_CAP:
            return {"persona_id": persona_id, "skip_reason": f"daily_cap ({cap_count}/{PERSONA_DAILY_CAP})"}

        last_join = _last_join_at(conn, persona_id)
        if last_join is not None:
            elapsed_min = (now_bkk() - last_join).total_seconds() / 60
            min_cooldown = COOLDOWN_MIN_MIN
            if elapsed_min < min_cooldown:
                return {"persona_id": persona_id,
                        "skip_reason": f"cooldown ({elapsed_min:.0f}/{min_cooldown} min)"}

        # Atomic claim — BEGIN IMMEDIATE locks writers so the SELECT+UPDATE
        # pair below cannot interleave with another persona's join_loop.
        conn.execute("BEGIN IMMEDIATE")
        try:
            edge = _select_next_edge(conn, persona_id)
            if not edge:
                conn.execute("COMMIT")
                return {"persona_id": persona_id, "skip_reason": "no_eligible_edge"}
            edge_d = dict(edge)
            conn.execute(
                "UPDATE funnel_edges SET join_persona=?, join_state='queued' WHERE row_id=?",
                (persona_id, edge_d["row_id"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        log(f"[{persona_id}] joining edge#{edge_d['row_id']} {edge_d['to_target_kind']} {edge_d['to_target']}")
        try:
            state, info = await _execute_join(client, edge_d)
        except Exception as e:
            state, info = "failed_join", f"{type(e).__name__}: {str(e)[:200]}"

        join_error = None if state == "joined" else info
        conn.execute(
            """UPDATE funnel_edges
                  SET join_state=?, join_persona=?, join_at=?, join_error=?
                WHERE row_id=?""",
            (state, persona_id, now_bkk().isoformat(timespec="seconds"),
             join_error, edge_d["row_id"]),
        )
        log(f"[{persona_id}] edge#{edge_d['row_id']} → {state} | {info[:120]}")
        # system_history: log every join attempt (joined/failed/already_member)
        try:
            from processors.history_log import log_event
            kind = "milestone" if state == "joined" else "warning"
            log_event(
                actor=f"{persona_id.lower()}_join",
                kind=kind,
                scope="funnel",
                title=f"[{persona_id}] {state} {edge_d['to_target_kind']} {edge_d['to_target']}",
                body=f"info: {info[:400]}\nfrom_chat: {edge_d.get('from_chat_username') or edge_d.get('from_chat_id')}",
                refs=[f"funnel_edges#{edge_d['row_id']}"],
            )
        except Exception as e:
            log(f"  history_log fail: {type(e).__name__}: {e}")
        return {"persona_id": persona_id, "edge_row_id": edge_d["row_id"],
                "state": state, "info": info,
                "to_target": edge_d["to_target"]}
    finally:
        conn.close()


# ----------------------------------------------------------------------
# Long-running task (injected into tg_listen event loop)
# ----------------------------------------------------------------------

async def join_loop(client, persona_id: str) -> None:
    """Inject via `asyncio.create_task(join_loop(client, persona_id))` from
    inside run_persona() before client.run_until_disconnected()."""
    log(f"[{persona_id}] join_loop started "
        f"(cap={PERSONA_DAILY_CAP}/day, cooldown {COOLDOWN_MIN_MIN}-{COOLDOWN_MAX_MIN} min, "
        f"size {GROUP_SIZE_MIN}-{GROUP_SIZE_MAX})")
    await asyncio.sleep(INITIAL_DELAY_SEC)
    while True:
        try:
            result = await execute_pending_join(client, persona_id)
            if result.get("skip_reason"):
                log(f"[{persona_id}] cycle skipped: {result['skip_reason']}")
        except Exception as e:
            log(f"[{persona_id}] cycle err: {type(e).__name__}: {str(e)[:200]}")
        sleep_min = random.uniform(COOLDOWN_MIN_MIN, COOLDOWN_MAX_MIN)
        await asyncio.sleep(sleep_min * 60)


# ----------------------------------------------------------------------
# CLI: standalone diagnostic mode (only safe when listener is paused)
# ----------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Standalone join diagnostic. ⚠ Stops listener implicitly via session lock.")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_status = sub.add_parser("status", help="show queue + per-persona stats")
    p_dry = sub.add_parser("dry-run", help="show next eligible edge per persona")
    p_dry.add_argument("--persona", default="P01")

    args = parser.parse_args()
    init_db()
    conn = get_connection()

    if args.mode == "status":
        print("=== funnel_edges by join_state ===")
        for r in conn.execute("SELECT join_state, COUNT(*) FROM funnel_edges GROUP BY join_state").fetchall():
            print(f"  {r[0]:<28} {r[1]}")
        print()
        print("=== per-persona today + last_join ===")
        for p in ["P01", "P02"]:
            print(f"  {p}: today={_today_count(conn, p)} last_at={_last_join_at(conn, p)}")
        print()
        print("=== approved + not_attempted (queue ahead) ===")
        for r in conn.execute("""SELECT row_id, to_target_kind, to_target, push_count, bait_intent
                                   FROM funnel_edges
                                  WHERE review_state='approved'
                                    AND join_state IN ('not_attempted','failed_join','failed_resolve')
                                  ORDER BY push_count DESC""").fetchall():
            print(f"  edge#{r[0]} {r[1]:<16} push={r[3]} bait={r[4]} → {r[2]}")
    elif args.mode == "dry-run":
        edge = _select_next_edge(conn, args.persona)
        if edge:
            print(f"next for {args.persona}: edge#{edge[0]} {edge[1]} {edge[2]} push={edge[4]}")
        else:
            print(f"nothing eligible for {args.persona}")
    conn.close()


if __name__ == "__main__":
    main()
