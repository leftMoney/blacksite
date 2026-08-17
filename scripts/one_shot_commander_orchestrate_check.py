"""One-shot DM to boss verifying section_chief_orchestrate idempotency fix
(5/17 patch). Sleeps until just after the next 5-min cron tick, reads the
latest orchestrate log tick, classifies result, drops a .md into
runtime/cmd/outbox/ — cmd_send_loop picks it up within 30s and DMs via P01.

Usage:
  pythonw scripts/one_shot_commander_orchestrate_check.py [HH:MM-deadline-GMT+7]

If deadline omitted, defaults to next 5-min mark + 60s buffer. The script
fires once and exits; no daemon hook needed.
"""
from __future__ import annotations

import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ = timezone(timedelta(hours=7))
ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "instances/_TEMPLATE/runtime/logs/cron_processors_section_chief_orchestrate.py_{date}.log"
OUTBOX = ROOT / "instances/_TEMPLATE/runtime/cmd/outbox"
SELF_LOG = ROOT / "instances/_TEMPLATE/runtime/logs/one_shot_commander_orchestrate_check.log"


def now() -> datetime:
    return datetime.now(TZ)


def _self_log(line: str) -> None:
    try:
        SELF_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SELF_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{now().isoformat(timespec='seconds')}] {line}\n")
    except Exception:
        pass


def _next_target() -> datetime:
    n = now()
    # cron fires at xx:34:41 / :39:41 / :44:41... (every 5 min, ~:39 offset).
    # Target = next 5-min boundary that has already passed by >=60s.
    minute = (n.minute // 5) * 5 + 5
    if minute >= 60:
        target = n.replace(hour=(n.hour + 1) % 24, minute=0,
                           second=0, microsecond=0)
    else:
        target = n.replace(minute=minute, second=0, microsecond=0)
    # Wait 1m 30s past the cron fire to ensure log line is flushed.
    target = target + timedelta(seconds=90)
    return target


def _read_last_tick() -> tuple[int, int, int, str]:
    """Read orchestrate log → return (n_skip, n_fire, n_spawn, raw_tail).

    Looks at the last 'starting orchestration tick' block.
    """
    log_path = Path(str(LOG).replace("{date}", now().strftime("%Y-%m-%d")))
    if not log_path.exists():
        return 0, 0, 0, f"(log file missing: {log_path.name})"
    text = log_path.read_text(encoding="utf-8", errors="replace")
    parts = text.split("starting orchestration tick")
    if len(parts) < 2:
        return 0, 0, 0, "(no orchestrate tick found in log)"
    last = parts[-1]
    n_skip = len(re.findall(r"\[SKIP\].*already fired", last))
    n_fire = len(re.findall(r"\[FIRE\]", last))
    n_spawn = len(re.findall(r"\[SPAWN\].*pid=\d+", last))
    # Grab raw skip/fire lines for body
    raw_lines = []
    for line in last.splitlines():
        if "[SKIP]" in line or "[FIRE]" in line or "[SPAWN]" in line or "tick complete" in line:
            raw_lines.append(line.strip())
    return n_skip, n_fire, n_spawn, "\n".join(raw_lines[-12:])


def _build_message() -> str:
    n_skip, n_fire, n_spawn, raw_tail = _read_last_tick()
    ts = now().strftime("%H:%M")
    if n_skip > 0 and n_fire == n_spawn:
        verdict = f"✅ idempotency 生效。本 tick 已 fire 過的 {n_skip} 個視窗自動 SKIP，新 spawn={n_spawn}（=新視窗）。"
    elif n_skip == 0 and n_fire == n_spawn:
        verdict = f"✅ 本 tick 沒有已 fire 過的視窗碰到（spawn={n_spawn}）；下一個 tick 應該會看到 SKIP。"
    elif n_skip == 0 and n_fire == 0 and n_spawn == 0:
        verdict = "⚪ 本 tick 沒有任何視窗（沒到任何 in_window/starting_soon），沒事。"
    else:
        verdict = f"⚠ 異常：SKIP={n_skip} FIRE={n_fire} SPAWN={n_spawn}。請看 log。"
    body = (
        f"orchestrate idempotency 驗證 @ {ts}\n\n"
        f"{verdict}\n\n"
        f"tick log tail：\n```\n{raw_tail or '(empty)'}\n```"
    )
    return body


def main() -> int:
    try:
        target = _next_target()
        _self_log(f"start; target_dm_at={target.isoformat(timespec='seconds')}")
        while now() < target:
            time.sleep(15)
        msg = _build_message()
        OUTBOX.mkdir(parents=True, exist_ok=True)
        ts_compact = now().strftime("%Y-%m-%dT%H-%M-%S")
        out_path = OUTBOX / f"{ts_compact}_engine_orchestrate_check.md"
        out_path.write_text(msg, encoding="utf-8")
        _self_log(f"wrote {out_path.name} ({len(msg)} chars)")
        return 0
    except Exception:
        _self_log(f"FATAL: {traceback.format_exc()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
