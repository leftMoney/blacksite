"""Blacksite daemon process guard.

Commands:
  list        print matching daemon/listener processes
  ensure-one keep one daemon and one listener, kill extras, repair daemon.pid
  stop-all   kill all daemon/listener processes and remove daemon.pid

This is intentionally scoped to command lines containing Blacksite's
blacksite_daemon.py / tg_listen.py. It never kills generic Python processes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
PID_FILE = RUNTIME / "daemon.pid"


def _cmdline(proc: psutil.Process) -> str:
    try:
        info = getattr(proc, "info", {}) or {}
        cached = info.get("cmdline")
        if cached:
            return " ".join(cached)
        return " ".join(proc.cmdline())
    except Exception:
        return ""


def _is_blacksite_proc(proc: psutil.Process, needle: str) -> bool:
    cmd = _cmdline(proc).replace("\\", "/").lower()
    return "d:/blacksite/" in cmd and needle in cmd


def find_daemons() -> list[psutil.Process]:
    current = os.getpid()
    out = []
    for proc in psutil.process_iter(["pid", "name", "create_time", "cmdline"]):
        if proc.pid == current:
            continue
        if _is_blacksite_proc(proc, "scripts/blacksite_daemon.py"):
            out.append(proc)
    return sorted(out, key=lambda p: (p.create_time(), p.pid))


def find_listeners() -> list[psutil.Process]:
    current = os.getpid()
    out = []
    for proc in psutil.process_iter(["pid", "name", "create_time", "ppid", "cmdline"]):
        if proc.pid == current:
            continue
        if _is_blacksite_proc(proc, "agents/telegram/tg_listen.py"):
            out.append(proc)
    return sorted(out, key=lambda p: (p.create_time(), p.pid))


def find_runners() -> list[psutil.Process]:
    current = os.getpid()
    out = []
    for proc in psutil.process_iter(["pid", "name", "create_time", "cmdline"]):
        if proc.pid == current:
            continue
        if _is_blacksite_proc(proc, "scripts/cron_child_runner.py"):
            out.append(proc)
    return sorted(out, key=lambda p: (p.create_time(), p.pid))


def read_pidfile() -> int | None:
    try:
        raw = PID_FILE.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except Exception:
        return None


def write_pidfile(pid: int) -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{pid}\n", encoding="utf-8")


def alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:
        return False


def kill_tree(proc: psutil.Process, timeout_s: float = 5.0) -> None:
    try:
        children = proc.children(recursive=True)
    except Exception:
        children = []
    victims = children + [proc]
    for p in victims:
        try:
            p.terminate()
        except Exception:
            pass
    _, alive_procs = psutil.wait_procs(victims, timeout=timeout_s)
    for p in alive_procs:
        try:
            p.kill()
        except Exception:
            pass
    psutil.wait_procs(alive_procs, timeout=timeout_s)


def choose_keeper(daemons: list[psutil.Process]) -> psutil.Process | None:
    if not daemons:
        return None
    registered = read_pidfile()
    if registered is not None:
        for proc in daemons:
            if proc.pid == registered:
                return proc
    # Prefer newest: a manual restart should supersede orphaned old daemons.
    return sorted(daemons, key=lambda p: (p.create_time(), p.pid), reverse=True)[0]


def list_processes() -> int:
    for label, procs in (
        ("daemon", find_daemons()),
        ("listener", find_listeners()),
        ("runner", find_runners()),
    ):
        for proc in procs:
            ppid = proc.ppid() if proc.is_running() else None
            print(f"{label} pid={proc.pid} ppid={ppid} cmd={_cmdline(proc)}")
    return 0


def ensure_one(no_kill: bool = False) -> int:
    daemons = find_daemons()
    if not daemons:
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except Exception:
                pass
        print("no blacksite daemon running")
        return 1

    keeper = choose_keeper(daemons)
    assert keeper is not None
    extras = [p for p in daemons if p.pid != keeper.pid]
    if extras and no_kill:
        print(f"multiple daemons found; keeper={keeper.pid}; extras={[p.pid for p in extras]}")
        return 2
    for proc in extras:
        print(f"killing extra daemon pid={proc.pid}")
        kill_tree(proc)

    write_pidfile(keeper.pid)

    listeners = find_listeners()
    preferred_listener = None
    for proc in listeners:
        try:
            if proc.ppid() == keeper.pid:
                preferred_listener = proc
                break
        except Exception:
            pass
    if preferred_listener is None and listeners:
        preferred_listener = sorted(listeners, key=lambda p: (p.create_time(), p.pid), reverse=True)[0]

    for proc in listeners:
        if preferred_listener is not None and proc.pid == preferred_listener.pid:
            continue
        if no_kill:
            continue
        print(f"killing extra tg_listen pid={proc.pid}")
        kill_tree(proc)

    if preferred_listener is not None:
        print(f"Blacksite daemon running: {keeper.pid}; listener={preferred_listener.pid}")
    else:
        print(f"Blacksite daemon running: {keeper.pid}; listener=none")
    return 0


def stop_all() -> int:
    daemons = find_daemons()
    listeners = find_listeners()
    runners = find_runners()
    for proc in daemons:
        print(f"killing daemon pid={proc.pid}")
        kill_tree(proc)
    # Kill orphan listeners not already killed as daemon children.
    time.sleep(0.5)
    for proc in find_listeners():
        print(f"killing tg_listen pid={proc.pid}")
        kill_tree(proc)
    for proc in find_runners():
        print(f"killing cron_child_runner pid={proc.pid}")
        kill_tree(proc)
    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except Exception as e:
            print(f"warning: cannot remove pidfile: {type(e).__name__}: {e}")
    print(f"stopped daemons={len(daemons)} listeners={len(listeners)} runners={len(runners)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_ensure = sub.add_parser("ensure-one")
    p_ensure.add_argument("--no-kill", action="store_true")
    sub.add_parser("stop-all")
    args = parser.parse_args()
    if args.cmd == "list":
        return list_processes()
    if args.cmd == "ensure-one":
        return ensure_one(no_kill=args.no_kill)
    if args.cmd == "stop-all":
        return stop_all()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
