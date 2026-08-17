"""Detached cron child supervisor for blacksite_daemon.py.

The daemon must stay a scheduler, not a long-running subprocess waiter. This
runner owns per-job locking, child stdout logs, timeout, and process-tree kill.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import psutil
except Exception:  # pragma: no cover - fallback only
    psutil = None


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
LOCK_DIR = RUNTIME_DIR / "locks"
TZ = timezone(timedelta(hours=7))
PYTHON = sys.executable


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def safe_stem(value: str) -> str:
    value = value.replace("\\", "/").replace("/", "_")
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def log_line(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{now_iso()}] [cron_runner] {msg}"
    path = LOG_DIR / f"daemon_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with path.open("a", encoding="utf-8", errors="replace") as f:
        f.write(line + "\n")


def tail_text(path: Path, limit: int = 1200) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except Exception as e:
        return f"<tail unavailable: {type(e).__name__}: {e}>"


def no_window_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def pid_alive(pid: int) -> bool:
    if psutil is not None:
        try:
            return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
        except Exception:
            return False
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=10,
                **no_window_kwargs(),
            )
            return str(pid) in r.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def kill_tree(pid: int) -> None:
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            victims = proc.children(recursive=True) + [proc]
            for victim in victims:
                try:
                    victim.terminate()
                except Exception:
                    pass
            _, alive = psutil.wait_procs(victims, timeout=5)
            for victim in alive:
                try:
                    victim.kill()
                except Exception:
                    pass
            psutil.wait_procs(alive, timeout=5)
            return
        except Exception as e:
            log_line(f"psutil kill_tree failed pid={pid}: {type(e).__name__}: {e}")
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                **no_window_kwargs(),
            )
        except Exception as e:
            log_line(f"taskkill failed pid={pid}: {type(e).__name__}: {e}")
        return
    try:
        os.kill(pid, 9)
    except Exception as e:
        log_line(f"kill failed pid={pid}: {type(e).__name__}: {e}")


def read_lock_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").splitlines()[0].strip()
        return int(raw)
    except Exception:
        return None


def acquire_lock(path: Path, timeout_s: int) -> bool:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    existing = read_lock_pid(path)
    if existing and pid_alive(existing):
        age = time.time() - path.stat().st_mtime
        log_line(
            f"skip locked job lock={path.name} pid={existing} "
            f"age={age:.0f}s timeout={timeout_s}s"
        )
        return False
    path.write_text(f"{os.getpid()}\n{now_iso()}\n", encoding="utf-8")
    return True


def release_lock(path: Path) -> None:
    if read_lock_pid(path) == os.getpid():
        try:
            path.unlink()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-s", type=int, required=True)
    parser.add_argument("--script-rel", required=True)
    parser.add_argument("child_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    child_args = list(args.child_args)
    if child_args and child_args[0] == "--":
        child_args = child_args[1:]

    script_path = ROOT / args.script_rel
    key = " ".join([args.script_rel, *child_args])
    digest = hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:10]
    lock_path = LOCK_DIR / f"cron_{safe_stem(args.script_rel)}_{digest}.lock"
    if not acquire_lock(lock_path, args.timeout_s):
        return 0

    child_log = LOG_DIR / (
        f"cron_{safe_stem(args.script_rel)}_"
        f"{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    )
    cmd = [PYTHON, str(script_path), *child_args]
    started = time.time()
    try:
        with child_log.open("a", encoding="utf-8", errors="replace") as out:
            out.write(f"\n[{now_iso()}] run {' '.join(cmd)}\n")
            out.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **no_window_kwargs(),
            )
            try:
                rc = proc.wait(timeout=args.timeout_s)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                elapsed = time.time() - started
                log_line(
                    f"TIMEOUT {args.script_rel} after {args.timeout_s}s "
                    f"elapsed={elapsed:.1f}s child_log={child_log.name}"
                )
                return 124

        elapsed = time.time() - started
        if rc != 0:
            tail = tail_text(child_log)
            log_line(
                f"FAIL {args.script_rel}: rc={rc} elapsed={elapsed:.1f}s "
                f"child_log={child_log.name} tail={tail[-400:]}"
            )
            return rc
        log_line(
            f"ok {args.script_rel} elapsed={elapsed:.1f}s "
            f"child_log={child_log.name}"
        )
        return 0
    except Exception as e:
        log_line(f"ERR {args.script_rel}: {type(e).__name__}: {e}")
        return 1
    finally:
        release_lock(lock_path)


if __name__ == "__main__":
    raise SystemExit(main())
