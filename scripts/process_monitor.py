"""
process_monitor.py — periodic snapshot of Blacksite + Meridian processes.

Boss 2026-05-02 directive: 24/7 boot uptime requirement, must detect memory
growth leaks before OS OOM kills (per 5/2 morning Claude Code crash).

Snapshots taken via PowerShell Get-Process (no psutil dependency). Output:
  instances/<active>/runtime/process_monitor.jsonl  (append-only)

Each line: {ts, daemon_pid, processes: [{Id, ProcessName, Mem, VM,
Threads, Started}]}.

Daily brief reads last 96 entries (24h × 4 per hour) to compute trends:
  - max(Mem) - min(Mem) per (PID, ProcessName) = potential leak signal
  - process disappear/restart count

Daemon cron: every 15 min. Pure-Python in-process not used (PowerShell call
needs subprocess), so this is a real subprocess but very cheap (<2s).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_PATH = RUNTIME_DIR / "process_monitor.jsonl"
PID_FILE = RUNTIME_DIR / "daemon.pid"
TZ = timezone(timedelta(hours=7))

POWERSHELL_CMD = (
    "Get-Process pythonw,python,node -ErrorAction SilentlyContinue | "
    "Select-Object Id, ProcessName, "
    "@{n='Mem';e={$_.WorkingSet64}}, "
    "@{n='VM';e={$_.VirtualMemorySize64}}, "
    "@{n='Threads';e={$_.Threads.Count}}, "
    "@{n='Started';e={$_.StartTime.ToString('o')}} | "
    "ConvertTo-Json -Compress -Depth 3"
)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def snapshot_processes() -> list[dict]:
    # Suppress console window pop: cron fires every 15 min from daemon
    # (pythonw GUI subsystem); spawning powershell.exe (console subsystem)
    # otherwise creates a fresh cmd window each fire and steals focus
    # (5/3 boss directive: 「不要 focus 最上層」).
    import os as _os
    no_window_kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if _os.name == "nt" else {}
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", POWERSHELL_CMD],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            **no_window_kw,
        )
        out = (result.stdout or "").strip()
        if not out:
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception as e:
        return [{"_err": f"{type(e).__name__}: {e}"}]


def read_daemon_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def main() -> None:
    procs = snapshot_processes()
    snapshot = {
        "ts": now_iso(),
        "daemon_pid": read_daemon_pid(),
        "process_count": len([p for p in procs if "_err" not in p]),
        "processes": procs,
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    # Brief stdout for daemon log
    if procs and "_err" not in procs[0]:
        max_mem = max((p.get("Mem", 0) or 0) for p in procs)
        print(f"[{snapshot['ts']}] [proc-mon] {snapshot['process_count']} procs / "
              f"max_mem={max_mem/1024/1024:.1f}MB / daemon={snapshot['daemon_pid']}",
              flush=True)
    else:
        print(f"[{snapshot['ts']}] [proc-mon] err: {procs}", flush=True)


if __name__ == "__main__":
    main()
