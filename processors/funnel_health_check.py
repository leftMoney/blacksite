"""processors/funnel_health_check.py — daily liveness probe for joined edges.

Boss directive 2026-05-15: 「已加群也有可能突然死掉。死掉就不追了。只要帳號沒死
就可以一直運作才對，不該被卡住一直撞牆」.

Walks each persona's `joined` funnel_edges and probes liveness via
get_entity(handle). On permanent-death telethon errors (channel went private,
persona kicked/banned, channel deleted, peer reference broken) the edge
transitions to terminal state `joined_then_dead`, a best-effort
LeaveChannelRequest releases the persona's dialog slot, and a warning event
hits system_history. Transient errors (FloodWait, connection drops) leave
state untouched for retry on the next pass.

Runs INSIDE the tg_listen asyncio loop (mirrors funnel_join.join_loop) so the
telethon client and SQLite session lock are shared without contention.

Cadence: daily at 04:00 GMT+7 (RUN_HOUR env override). Per-edge jitter
10-30s avoids burst patterns visible to telegram anti-abuse.

Scope v1: only `tg_channel_ref` joined edges (handle re-probable via
get_entity). `tg_invite` joined edges are deferred — no resolved channel_id
column on funnel_edges yet, and current invite-join count is 0.
"""

from __future__ import annotations

import asyncio
import os
import random
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
from processors.history_log import log_event

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
BRIEF_QUEUE_DIR = RUNTIME_DIR / "briefs" / "queue"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))

PROBE_JITTER_MIN_S = int(os.environ.get("FUNNEL_HEALTH_PROBE_JITTER_MIN_S", "10"))
PROBE_JITTER_MAX_S = int(os.environ.get("FUNNEL_HEALTH_PROBE_JITTER_MAX_S", "30"))
RUN_HOUR = int(os.environ.get("FUNNEL_HEALTH_RUN_HOUR", "4"))


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_bkk().isoformat(timespec='seconds')}] [funnel-health] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"funnel_health_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# Telethon error classes that indicate the channel/persona pair is permanently
# dead. These never recover by retry; mark the edge terminal and leave.
PERMANENT_DEATH_CLASSES = (
    "ChannelPrivateError",       # channel switched to private, no access
    "ChatForbiddenError",        # persona was kicked
    "UserBannedInChannelError",  # persona was banned
    "ChannelInvalidError",       # channel deleted / id invalid
    "PeerIdInvalidError",        # peer reference broken
    "UsernameNotOccupiedError",  # username un-occupied (renamed / deleted)
    "UsernameInvalidError",
    "ChatIdInvalidError",
)

# Substring markers as backup for wrapped exceptions or version drift.
PERMANENT_DEATH_MARKERS = (
    "channel is private",
    "channel/supergroup not available",
    "user is banned",
    "you were banned",
    "chat is forbidden",
    "nobody is using this username",
    "no user has",
    "channel_invalid",
    "peer_id_invalid",
    "chat_id_invalid",
)


def _is_permanent_death(exc: Exception) -> bool:
    if type(exc).__name__ in PERMANENT_DEATH_CLASSES:
        return True
    return any(m in str(exc).lower() for m in PERMANENT_DEATH_MARKERS)


def _list_joined_edges(conn, persona_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT row_id, to_target_kind, to_target, join_at
             FROM funnel_edges
            WHERE join_state = 'joined'
              AND join_persona = ?
              AND to_target_kind = 'tg_channel_ref'""",
        (persona_id,),
    ).fetchall()
    return [dict(r) for r in rows]


async def _probe_one(client, edge: dict) -> tuple[str, str | None]:
    """Returns ('alive', None) | ('dead', reason) | ('transient', err_text)."""
    handle = edge["to_target"]
    try:
        entity = await client.get_entity(handle)
    except Exception as e:
        if _is_permanent_death(e):
            return ("dead", f"{type(e).__name__}: {str(e)[:200]}")
        return ("transient", f"{type(e).__name__}: {str(e)[:200]}")
    if entity is None:
        return ("dead", "get_entity returned None")
    return ("alive", None)


async def _leave_dead_channel(client, target: str) -> str:
    """Best-effort LeaveChannelRequest to free the persona's dialog slot.
    Returns a short status string for the audit log."""
    try:
        from telethon.tl.functions.channels import LeaveChannelRequest
        entity = await client.get_entity(target)
        await client(LeaveChannelRequest(entity))
        return "left"
    except Exception as e:
        return f"leave_failed:{type(e).__name__}"


async def health_check_joined(client, persona_id: str) -> dict:
    init_db()
    conn = get_connection()
    try:
        edges = _list_joined_edges(conn, persona_id)
        log(f"[{persona_id}] daily health-check start: {len(edges)} joined edges")
        n_alive = n_dead = n_transient = 0
        for i, edge in enumerate(edges):
            try:
                state, info = await _probe_one(client, edge)
            except Exception as e:
                err = str(e)
                if "FLOOD_WAIT" in err.upper() or "flood" in err.lower():
                    log(f"[{persona_id}] FloodWait — abort pass: {err[:200]}")
                    log_event(
                        actor=f"{persona_id.lower()}_health",
                        kind="warning", scope="funnel",
                        title=f"[{persona_id}] health_check aborted: FloodWait",
                        body=f"checked {i}/{len(edges)}; err={err[:300]}",
                    )
                    break
                log(f"[{persona_id}] edge#{edge['row_id']} unexpected: "
                    f"{type(e).__name__}: {err[:120]}")
                n_transient += 1
                continue

            if state == "alive":
                n_alive += 1
            elif state == "transient":
                n_transient += 1
                log(f"[{persona_id}] edge#{edge['row_id']} {edge['to_target']} "
                    f"→ transient: {info[:120]}")
            else:  # dead
                n_dead += 1
                leave_status = await _leave_dead_channel(client, edge["to_target"])
                conn.execute(
                    """UPDATE funnel_edges
                          SET join_state='joined_then_dead',
                              join_error=?,
                              join_at=?
                        WHERE row_id=?""",
                    (
                        f"died_at_probe: {info[:150]} | leave={leave_status}",
                        now_bkk().isoformat(timespec="seconds"),
                        edge["row_id"],
                    ),
                )
                conn.commit()
                log(f"[{persona_id}] edge#{edge['row_id']} {edge['to_target']} "
                    f"→ DEAD ({info[:80]}) leave={leave_status}")
                log_event(
                    actor=f"{persona_id.lower()}_health",
                    kind="warning", scope="funnel",
                    title=f"[{persona_id}] joined_then_dead {edge['to_target']}",
                    body=f"reason: {info[:300]} | leave: {leave_status}",
                    refs=[f"funnel_edges#{edge['row_id']}"],
                )

            if i < len(edges) - 1:
                await asyncio.sleep(
                    random.uniform(PROBE_JITTER_MIN_S, PROBE_JITTER_MAX_S)
                )

        summary = {
            "persona_id": persona_id,
            "checked": n_alive + n_dead + n_transient,
            "alive": n_alive,
            "dead": n_dead,
            "transient": n_transient,
            "total_joined": len(edges),
        }
        log(f"[{persona_id}] done: {summary}")
        log_event(
            actor=f"{persona_id.lower()}_health",
            kind="metric", scope="funnel",
            title=f"[{persona_id}] daily joined-edge health-check",
            body=(f"alive={n_alive} dead={n_dead} transient={n_transient} "
                  f"of {len(edges)} joined edges"),
        )
        if n_dead > 0:
            try:
                BRIEF_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
                ts = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
                q = BRIEF_QUEUE_DIR / f"pending_{ts}_funnel_health_{persona_id.lower()}.md"
                q.write_text(
                    f"[小主管 FYI] 漏斗頻道失效（{persona_id}）\n\n"
                    f"• {n_dead} 個頻道失效 → 那些目標群已無法採集訊息"
                    f"（存活 {n_alive}，失效 {n_dead}，短暫 {n_transient}，共 {len(edges)} 條）\n"
                    f"• 已標記 joined_then_dead，不重試 → §14 規定，死掉不追\n\n"
                    f"小主管已知悉，視需要補充 seed。",
                    encoding="utf-8",
                )
            except Exception:
                pass
        return summary
    finally:
        conn.close()


async def _sleep_until_next_run() -> None:
    """Sleep until next RUN_HOUR:00 GMT+7."""
    now = now_bkk()
    target = now.replace(hour=RUN_HOUR, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    delta_s = (target - now).total_seconds()
    log(f"sleeping {delta_s/3600:.1f}h until {target.isoformat(timespec='minutes')}")
    await asyncio.sleep(delta_s)


async def health_check_loop(client, persona_id: str) -> None:
    """Inject via `asyncio.create_task(health_check_loop(client, persona_id))`
    from inside tg_listen run_persona()."""
    log(f"[{persona_id}] health_check_loop started "
        f"(daily {RUN_HOUR:02d}:00 GMT+7, probe jitter "
        f"{PROBE_JITTER_MIN_S}-{PROBE_JITTER_MAX_S}s)")
    while True:
        await _sleep_until_next_run()
        try:
            await health_check_joined(client, persona_id)
        except Exception as e:
            log(f"[{persona_id}] cycle err: {type(e).__name__}: {str(e)[:200]}")


def main() -> None:
    """Standalone diagnostic. Cannot run probe while listener holds session;
    `status` and `dry-run` are read-only DB queries that work anytime."""
    import argparse
    parser = argparse.ArgumentParser(
        description="Funnel joined-edge health-check diagnostic (read-only).")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("status", help="summarize joined-state distribution + recent deaths")
    p_dry = sub.add_parser("dry-run", help="list edges that WOULD be probed")
    p_dry.add_argument("--persona", default="P01")

    args = parser.parse_args()
    init_db()
    conn = get_connection()
    if args.mode == "status":
        print("=== joined / joined_then_dead by persona ===")
        for r in conn.execute(
            """SELECT join_persona, join_state, COUNT(*)
                 FROM funnel_edges
                WHERE join_state IN ('joined','joined_then_dead')
                GROUP BY join_persona, join_state
                ORDER BY join_persona, join_state"""
        ).fetchall():
            print(f"  {r[0]} {r[1]:<22} {r[2]}")
        print()
        print("=== recent joined_then_dead transitions (last 20) ===")
        rows = conn.execute(
            """SELECT row_id, join_persona, to_target, join_at, join_error
                 FROM funnel_edges
                WHERE join_state='joined_then_dead'
                ORDER BY join_at DESC LIMIT 20"""
        ).fetchall()
        if not rows:
            print("  (none yet)")
        for r in rows:
            print(f"  edge#{r[0]:<6} {r[1]} {r[2]:<28} @ {r[3]}  "
                  f"err={(r[4] or '')[:100]}")
    elif args.mode == "dry-run":
        edges = _list_joined_edges(conn, args.persona)
        print(f"would probe {len(edges)} edges for {args.persona}:")
        for e in edges[:30]:
            print(f"  edge#{e['row_id']:<6} {e['to_target']:<30} "
                  f"joined_at={e['join_at']}")
        if len(edges) > 30:
            print(f"  ... + {len(edges) - 30} more")
    conn.close()


if __name__ == "__main__":
    main()
