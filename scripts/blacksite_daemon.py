"""
Blacksite — 24/7 orchestrator daemon.

Manages:
  - Long-running TG listener (subprocess; one per persona group)
  - Periodic TG search (every 4h)
  - Periodic TG crawler (every 1h)
  - Periodic TG classifier (every 2h)
  - Daily TG pattern miner
  - Daily archiver (03:00 Bangkok)
  - Future: YouTube monitor, Reddit listener, etc.

Schedules in Asia/Bangkok per CLAUDE.md §6.4.

Persistence: PID file at runtime/daemon.pid; restart via Windows Task Scheduler
(or systemd in WSL2/v2). Crash-safe: each scheduled job is independent.

Usage:
  py scripts/blacksite_daemon.py             # foreground (Ctrl-C to stop)
  pythonw scripts/blacksite_daemon.py        # background on Windows
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import (
    EVENT_JOB_EXECUTED,
    EVENT_JOB_ERROR,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
)
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

if sys.platform == "win32":
    # Python 3.13 + pythonw: sys.stdout/stderr are None (no console). Guard the
    # reconfigure call so daemon doesn't AttributeError-crash at module load when
    # spawned headless from PowerShell Start-Process / Task Scheduler / Startup
    # .lnk. Boss-spawned .lnk path inherits stdout via explorer.exe so it worked
    # historically; bare PS spawn doesn't. Verified 2026-05-08 16:55+07.
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LLM_PROVIDER = os.environ.get("BLACKSITE_LLM_PROVIDER", "claude").strip().lower()
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
PID_FILE = RUNTIME_DIR / "daemon.pid"
LOCK_FILE = RUNTIME_DIR / "daemon.lock"
HEARTBEAT_FILE = RUNTIME_DIR / "daemon.heartbeat"
CRON_ACTIVITY_FILE = RUNTIME_DIR / "daemon.cron_activity"

TZ_NAME = "Asia/Bangkok"
TZ = timezone(timedelta(hours=7))

PYTHON = sys.executable
listener_proc: subprocess.Popen | "_AdoptedProc" | None = None
lock_handle = None
scheduler_ref = None
manual_scheduler_started = False


def no_window_kwargs() -> dict:
    """Windows background children must not steal desktop focus."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def detached_runner_kwargs() -> dict:
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        return {"creationflags": flags, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def safe_child_log_stem(script_rel: str) -> str:
    stem = script_rel.replace("\\", "/").replace("/", "_")
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stem)


def tail_text(path: Path, limit: int = 1200) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"<tail unavailable: {type(e).__name__}: {e}>"
    return text[-limit:]


def kill_process_tree(pid: int) -> None:
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return
        except Exception as e:
            log_line(f"taskkill failed pid={pid}: {type(e).__name__}: {e}")
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception as e:
        log_line(f"kill failed pid={pid}: {type(e).__name__}: {e}")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _is_pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return str(pid) in r.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _read_registered_pid() -> int | None:
    try:
        raw = PID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def acquire_daemon_lock() -> bool:
    """Hold a process-scoped lock so patched daemons cannot double-start."""
    global lock_handle
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_FILE.open("a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt

            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(f"{os.getpid()}\n")
        lock_handle.flush()
        return True
    except OSError:
        try:
            lock_handle.close()
        except Exception:
            pass
        lock_handle = None
        return False


class _AdoptedProc:
    """Wrapper for a tg_listen process the daemon adopts at startup
    (cross-daemon handoff). Mimics the subprocess.Popen interface used by
    start_listener / supervise_listener / cleanup. Why: prior daemon's listener
    can survive its parent's exit (orphan); blindly respawning contends with
    the orphan's telethon session sqlite lock — observed 2026-05-02 17:11+
    when a new daemon respawned tg_listen 110+ times in 100 min, every spawn
    dying at client.connect() with 'database is locked'."""
    def __init__(self, pid: int):
        self.pid = pid
        self._returncode: int | None = None

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        if _is_pid_alive(self.pid):
            return None
        self._returncode = -1  # exit code unobservable from outside
        return self._returncode

    def terminate(self) -> None:
        log_line(f"_AdoptedProc.terminate() no-op: keeping adopted pid={self.pid} "
                 f"alive across daemon restart (orphan-survival design)")

    def kill(self) -> None:
        log_line(f"_AdoptedProc.kill() no-op: keeping adopted pid={self.pid} "
                 f"alive across daemon restart (orphan-survival design)")

    def wait(self, timeout: float | None = None) -> int:
        deadline = time.time() + timeout if timeout else None
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if deadline and time.time() >= deadline:
                raise subprocess.TimeoutExpired(cmd="tg_listen", timeout=timeout)
            time.sleep(0.5)


def _find_existing_tg_listen_pid() -> int | None:
    try:
        if sys.platform != "win32":
            r = subprocess.run(
                ["ps", "-eo", "pid,cmd"], capture_output=True, text=True, timeout=10,
            )
            for line in r.stdout.splitlines():
                if "tg_listen.py" in line and "blacksite_daemon" not in line:
                    parts = line.strip().split(None, 1)
                    if parts and parts[0].isdigit():
                        return int(parts[0])
            return None
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter "
            "\"Name='pythonw.exe' OR Name='python.exe'\" "
            "| Where-Object { $_.CommandLine -match 'tg_listen\\.py' } "
            "| Select-Object -First 1 -ExpandProperty ProcessId"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out = r.stdout.strip()
        if out and out.isdigit():
            return int(out)
        return None
    except Exception as e:
        log_line(f"_find_existing_tg_listen_pid failed: {type(e).__name__}: {e}")
        return None


def log_line(msg: str) -> None:
    line = f"[{now_iso()}] [daemon] {msg}"
    if sys.stdout is not None:
        print(line, flush=True)
    log_path = LOG_DIR / f"daemon_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# --- system_history bridge (schema v7, M-history) ----------------------
# Best-effort: never raises; if log_event fails, just keep going. The daemon
# still always writes plain-text via log_line above for grep/tail debugging.
def _hist(kind: str, title: str, scope: str | None = None,
          body: str | None = None, refs: list | None = None,
          parent_id: int | None = None) -> int:
    try:
        sys.path.insert(0, str(ROOT))
        from processors.history_log import log_event
        return log_event(actor="daemon", kind=kind, scope=scope,
                         title=title, body=body, refs=refs, parent_id=parent_id)
    except Exception as e:
        # never let history bring down the daemon
        log_line(f"_hist write fail ({type(e).__name__}: {e})")
        return -1


def heartbeat() -> None:
    """Sentinel cron — touch heartbeat file every 2 min via APScheduler.
    If file mtime > 5 min, scheduler is dead (zombie daemon: process alive
    but cron not firing). 0 LLM tokens, 0 subprocess, ~30-byte file write.

    Diagnostic for the 2026-04-30 15:54 zombie incident: daemon process
    stayed alive 29 hours after scheduler stopped firing all cron jobs."""
    if not ensure_pid_registration(repair=True):
        log_line(
            f"daemon pid conflict: current pid={os.getpid()} is not registered; exiting"
        )
        os._exit(0)
    HEARTBEAT_FILE.write_text(now_iso(), encoding="utf-8")
    maybe_log_scheduler_stale()


def maybe_log_scheduler_stale() -> None:
    if scheduler_ref is None or not CRON_ACTIVITY_FILE.exists():
        return
    age_s = time.time() - CRON_ACTIVITY_FILE.stat().st_mtime
    if age_s < 300:
        return
    try:
        log_line(f"scheduler cron_activity stale age={age_s:.0f}s")
    except Exception as e:
        log_line(f"scheduler stale diagnostic failed: {type(e).__name__}: {e}")


def mark_cron_activity(script_rel: str) -> None:
    CRON_ACTIVITY_FILE.write_text(f"{now_iso()} {script_rel}", encoding="utf-8")


def run_script(script_rel: str, *args: str, timeout_s: int = 60 * 30) -> None:
    """Default 30 min timeout for fast cron jobs. Slow batch jobs (OCR / ASR
    big backlog) override via per-job timeout_s in scheduler.add_job kwargs.
    Audit 2026-05-02: OCR 03:30 hit 30 min cap → daemon killed it after
    only 25/1000 photos done (Gemini API ~36-72s per photo)."""
    if LLM_PROVIDER == "codex" and script_rel in {
        "processors/oauth_keepalive.py",
        "processors/ocr_quality_audit.py",
    }:
        log_line(f"skip {script_rel}: BLACKSITE_LLM_PROVIDER=codex")
        return
    script_path = ROOT / script_rel
    if not script_path.exists():
        log_line(f"skip {script_rel}: script missing")
        mark_cron_activity(script_rel)
        _hist("warning", f"cron script missing {script_rel}",
              scope="daemon",
              body=f"path={script_path}\nargs={args}\ntimeout_s={timeout_s}")
        return
    runner_path = ROOT / "scripts" / "cron_child_runner.py"
    cmd = [
        PYTHON,
        str(runner_path),
        "--timeout-s",
        str(timeout_s),
        "--script-rel",
        script_rel,
        "--",
        *args,
    ]
    mark_cron_activity(script_rel)
    try:
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **detached_runner_kwargs(),
        )
    except Exception as e:
        log_line(f"ERR {script_rel}: {type(e).__name__}: {e}")
        _hist("crash", f"cron ERR {script_rel}: {type(e).__name__}",
              scope="daemon", body=f"args={args}\nerror: {e}")


def _manual_run_job(entry: dict, scheduled_time: datetime) -> None:
    try:
        log_line(
            f"manual EXECUTE job={entry['id']} "
            f"scheduled={scheduled_time.isoformat()}"
        )
        run_script(*entry["args"], **entry["kwargs"])
    except Exception as e:
        log_line(f"manual JOB_ERROR job={entry['id']}: {type(e).__name__}: {e}")
    finally:
        entry["running"] = False


def _manual_scheduler_loop(entries: list[dict]) -> None:
    log_line(f"manual scheduler started jobs={len(entries)}")
    while True:
        now = datetime.now(TZ)
        for entry in entries:
            next_run = entry.get("next_run_time")
            if next_run is None:
                try:
                    next_run = entry["trigger"].get_next_fire_time(None, now)
                except Exception as e:
                    log_line(
                        f"manual next_run error job={entry['id']}: "
                        f"{type(e).__name__}: {e}"
                    )
                    next_run = None
                entry["next_run_time"] = next_run
            if next_run is None or now < next_run:
                continue
            if entry.get("running"):
                log_line(f"manual MAX_INSTANCES job={entry['id']} scheduled={next_run}")
            else:
                entry["running"] = True
                threading.Thread(
                    target=_manual_run_job,
                    args=(entry, next_run),
                    daemon=True,
                    name=f"manual-cron-{entry['id']}",
                ).start()
            try:
                entry["next_run_time"] = entry["trigger"].get_next_fire_time(next_run, now)
            except Exception as e:
                log_line(
                    f"manual reschedule error job={entry['id']}: "
                    f"{type(e).__name__}: {e}"
                )
                entry["next_run_time"] = None
        time.sleep(5)


def handoff_run_script_jobs_to_manual(sched) -> None:
    """Bypass APScheduler executors for subprocess jobs.

    2026-05-13 Windows/pythonw failure mode: APScheduler keeps heartbeat alive
    but run_script jobs get stuck at SUBMITTED and never enter the executor.
    The trigger math remains useful; dispatch must be owned by a local thread.
    """
    global manual_scheduler_started
    if manual_scheduler_started:
        return
    now = datetime.now(TZ)
    entries: list[dict] = []
    for job in list(sched.get_jobs()):
        if getattr(job, "func", None) is not run_script:
            continue
        next_run = getattr(job, "next_run_time", None)
        if next_run is None:
            try:
                next_run = job.trigger.get_next_fire_time(None, now)
            except Exception:
                next_run = None
        entries.append({
            "id": job.id,
            "trigger": job.trigger,
            "args": tuple(job.args),
            "kwargs": dict(job.kwargs),
            "next_run_time": next_run,
            "running": False,
        })
        sched.remove_job(job.id)
    if not entries:
        return
    manual_scheduler_started = True
    log_line(f"handoff run_script jobs to manual scheduler count={len(entries)}")
    threading.Thread(
        target=_manual_scheduler_loop,
        args=(entries,),
        daemon=True,
        name="blacksite-manual-scheduler",
    ).start()


def log_scheduler_event(event) -> None:
    if event.code == EVENT_JOB_SUBMITTED:
        if event.job_id not in {"heartbeat", "supervise_tg_listener"}:
            log_line(
                f"scheduler SUBMITTED job={event.job_id} "
                f"scheduled={getattr(event, 'scheduled_run_times', None)}"
            )
        return
    if event.code == EVENT_JOB_EXECUTED:
        if event.job_id not in {"heartbeat", "supervise_tg_listener"}:
            log_line(
                f"scheduler EXECUTED job={event.job_id} "
                f"scheduled={getattr(event, 'scheduled_run_time', None)}"
            )
        return
    if event.code == EVENT_JOB_MISSED:
        log_line(
            f"scheduler MISSED job={event.job_id} "
            f"scheduled={getattr(event, 'scheduled_run_time', None)}"
        )
        return
    if event.code == EVENT_JOB_MAX_INSTANCES:
        log_line(
            f"scheduler MAX_INSTANCES job={event.job_id} "
            f"scheduled={getattr(event, 'scheduled_run_times', None)}"
        )
        return
    if event.code == EVENT_JOB_ERROR:
        log_line(
            f"scheduler JOB_ERROR job={event.job_id} "
            f"scheduled={getattr(event, 'scheduled_run_time', None)} "
            f"exception={getattr(event, 'exception', None)}"
        )


def start_listener() -> None:
    global listener_proc
    if listener_proc and listener_proc.poll() is None:
        log_line("listener already running")
        return

    existing_pid = _find_existing_tg_listen_pid()
    if existing_pid is not None and existing_pid != os.getpid():
        log_line(f"adopting existing tg_listen pid={existing_pid} (cross-daemon handoff)")
        _hist("milestone", f"adopted orphan tg_listen pid={existing_pid}",
              scope="daemon",
              body="prior daemon's listener survived its parent's exit; "
                   "adopting instead of respawning to avoid telethon session "
                   "lock contention loop")
        listener_proc = _AdoptedProc(existing_pid)
        return

    log_line("starting tg_listen…")
    listener_proc = subprocess.Popen(
        [PYTHON, str(ROOT / "agents" / "telegram" / "tg_listen.py")],
        cwd=str(ROOT),
        stdout=open(LOG_DIR / "tg_listen_subproc.log", "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        **no_window_kwargs(),
    )


def supervise_listener() -> None:
    global listener_proc
    if listener_proc is None or listener_proc.poll() is not None:
        log_line("listener died, restarting")
        _hist("crash", "tg_listen subprocess died — restarting",
              scope="daemon",
              body=f"prev_pid={listener_proc.pid if listener_proc else 'never_started'} "
                   f"returncode={listener_proc.poll() if listener_proc else 'n/a'}")
        start_listener()


def ensure_pid_registration(*, repair: bool = False) -> bool:
    current_pid = os.getpid()
    registered_pid = _read_registered_pid()
    if registered_pid == current_pid:
        return True
    if registered_pid is not None and _is_pid_alive(registered_pid):
        if repair:
            log_line(
                f"daemon.pid conflict: keeping live registered pid={registered_pid}, "
                f"current pid={current_pid}"
            )
        return False

    PID_FILE.write_text(f"{current_pid}\n", encoding="utf-8")
    if repair:
        prev = "missing" if registered_pid is None else f"stale pid={registered_pid}"
        log_line(f"daemon.pid repaired -> {current_pid} ({prev})")
        _hist("warning", f"daemon.pid repaired -> {current_pid}",
              scope="daemon", body=f"previous={prev}")
    return True


def cleanup() -> None:
    log_line("shutdown")
    _hist("milestone", "daemon shutdown", scope="daemon",
          body=f"pid={os.getpid()}")
    if listener_proc and listener_proc.poll() is None:
        listener_proc.terminate()
        try:
            listener_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            listener_proc.kill()
    if _read_registered_pid() == os.getpid():
        PID_FILE.unlink()
    if lock_handle is not None:
        try:
            lock_handle.close()
        except Exception:
            pass


def main() -> None:
    global scheduler_ref
    if not acquire_daemon_lock():
        log_line("another daemon holds daemon.lock; exiting without starting scheduler")
        _hist("warning", f"daemon lock held; bootstrap skipped pid={os.getpid()}",
              scope="daemon")
        return
    if not ensure_pid_registration():
        log_line("another daemon already registered; exiting without starting scheduler")
        _hist("warning", f"daemon bootstrap skipped pid={os.getpid()}",
              scope="daemon",
              body=f"registered_pid={_read_registered_pid()} current_pid={os.getpid()}")
        return
    log_line(f"daemon up; instance={ACTIVE_INSTANCE} tz={TZ_NAME} python={PYTHON}")
    _hist("milestone", f"daemon up pid={os.getpid()}", scope="daemon",
          body=f"instance={ACTIVE_INSTANCE} tz={TZ_NAME} python={PYTHON}")

    # job_defaults applied to every cron handler:
    #   max_instances=1     — same job_id can't double-fire while previous still
    #                         running; prevents thread-pool saturation if a
    #                         single handler hangs (2026-04-30 zombie hypothesis)
    #   misfire_grace_time=60 — runs late by > 60s are skipped, not queued
    #   coalesce=True       — multiple missed runs collapse into one
    sched = BackgroundScheduler(
        timezone=TZ_NAME,
        executors={"default": ThreadPoolExecutor(max_workers=20)},
        job_defaults={
            "max_instances": 1,
            "misfire_grace_time": 60,
            "coalesce": True,
        },
    )
    scheduler_ref = sched
    sched.add_listener(
        log_scheduler_event,
        EVENT_JOB_SUBMITTED
        | EVENT_JOB_EXECUTED
        | EVENT_JOB_MISSED
        | EVENT_JOB_MAX_INSTANCES
        | EVENT_JOB_ERROR,
    )

    # Sentinel heartbeat — pure-Python in-process, NO subprocess, NO LLM,
    # ~30-byte file write every 2 min. If APScheduler dies, this stops
    # firing → file mtime goes stale → session_status.py flags zombie.
    # next_run_time=now ensures first beat fires immediately on daemon start.
    sched.add_job(heartbeat, IntervalTrigger(minutes=2),
                  id="heartbeat", replace_existing=True,
                  next_run_time=datetime.now(TZ))

    # Process memory monitor (boss 2026-05-02 directive: 24/7 boot, detect
    # leak before OS OOM). Snapshots all pythonw / python / node processes
    # via PowerShell Get-Process every 15 min → runtime/process_monitor.jsonl.
    # daily_brief reads last 96 entries (24h) for leak trend.
    sched.add_job(
        run_script, IntervalTrigger(minutes=15),
        args=("scripts/process_monitor.py",),
        id="process_monitor", replace_existing=True,
        next_run_time=datetime.now(TZ),
    )

    # Boss opinion extractor — tail conversation.jsonl, classify new boss
    # turns into boss_opinions table for cross-session institutional memory.
    # Commander doesn't modify code (M7.1 sandbox), so this sidecar runs in
    # daemon. Boss queries via scripts/commander_history.py.
    sched.add_job(
        run_script, IntervalTrigger(minutes=120),
        args=("processors/commander_opinion_extractor.py",),
        id="commander_opinion_extractor", replace_existing=True,
    )

    # Milestone alert runner — fire pre-scheduled probes per
    # runtime/milestone_alerts.jsonl, DM boss via brief queue. Boss directive
    # 5/2 21:40 PM 「主管反饋的時間點也飆出來」: each milestone in 48h schedule
    # fires automatic boss DM (PASS/FAIL + evidence) at probe time.
    sched.add_job(
        run_script, IntervalTrigger(minutes=30),
        args=("processors/milestone_runner.py",),
        id="milestone_runner", replace_existing=True,
    )

    # Library ingest — Phase A 5/5 fix for boss directive 「週報沒入庫，沒有進
    # 書庫」. Pre-fix kb_chunks=0/kb_documents=0 confirmed never ingested. Daily
    # 20:00 GMT+7 (after 19:00 daily_brief composition; before Sun 21:00
    # strategist runs that may want fresh library state). Idempotent via
    # UNIQUE(source_kind, source_row_id) so re-runs are no-ops. Sources:
    # strategy_memos / briefs/sent / boss_opinions / resolved kb_leads.
    sched.add_job(
        run_script, CronTrigger(hour=20, minute=0, timezone=TZ_NAME),
        args=("processors/library_ingest.py",),
        kwargs={"timeout_s": 300},
        id="library_ingest", replace_existing=True,
    )

    # OAuth setup-token keepalive — fix for 5/3 「commander 接線員炸了」 root cause.
    # `sk-ant-oat01-` setup-token is server-side invalidated when boss's
    # 5/24 RE-DESIGN — TTL-aware keepalive. The previous 3h cadence with
    # ANTHROPIC_API_KEY=<sk-ant-oat01-...> env was a NOOP: it did not refresh
    # credentials.json (verified by mtime not advancing across keepalive
    # calls). On 5/24 the new sk-ant-oat01- token died at exact 8h match with
    # credentials.json access expiry, proving OAuth chain coupling.
    #
    # New design: cron runs every 30min. Each run checks credentials.json
    # access TTL. If TTL > REFRESH_THRESHOLD_H (default 1.5h) → NOOP. If TTL
    # ≤ threshold → spawn claude.exe via host OAuth path (NO ANTHROPIC_API_KEY
    # env) → claude.exe sees near-expiry, calls Anthropic refresh endpoint,
    # writes new credentials.json. Token chain stays warm indefinitely as
    # long as cron runs.
    # See processors/oauth_keepalive.py for full root-cause analysis.
    sched.add_job(
        run_script, IntervalTrigger(minutes=30),
        args=("processors/oauth_keepalive.py",),
        kwargs={"timeout_s": 120},  # 60s spawn timeout + 60s scheduler buffer
        id="oauth_keepalive", replace_existing=True,
        next_run_time=datetime.now(TZ),
    )

    # Long-running: TG listener (start now + supervise)
    start_listener()
    sched.add_job(supervise_listener, IntervalTrigger(minutes=2),
                  id="supervise_tg_listener", replace_existing=True)

    # Periodic TG ops
    sched.add_job(
        run_script, CronTrigger(minute=15, hour="*/4", timezone=TZ_NAME),
        args=("agents/telegram/tg_search.py", "P01"),
        id="tg_search", replace_existing=True,
    )
    # NOTE: tg_crawler.py uses telethon and would contend with the listener's
    # SQLite session. Schedule the *raw-extractor* (regex-only, no telethon) to
    # process listener JSONL output instead. Use tg_crawler only for one-shot
    # backfill before daemon starts.
    sched.add_job(
        run_script, IntervalTrigger(hours=1),
        args=("agents/telegram/tg_extract_from_raw.py",),
        id="tg_extract_from_raw", replace_existing=True,
    )
    sched.add_job(
        run_script, IntervalTrigger(hours=2),
        args=("agents/telegram/tg_classifier.py",),
        id="tg_classifier", replace_existing=True,
    )
    sched.add_job(
        run_script, CronTrigger(hour=4, minute=0, timezone=TZ_NAME),
        args=("agents/telegram/tg_pattern_miner.py",),
        id="tg_pattern_miner", replace_existing=True,
    )

    # Daily archiver
    sched.add_job(
        run_script, CronTrigger(hour=3, minute=0, timezone=TZ_NAME),
        args=("scripts/archive_daily.py",),
        id="archive_daily", replace_existing=True,
    )

    # Runtime retention sweep — keeps OCR/ASR audit materials and raw JSONL for
    # 7 days, and organizes repo-root scratch OCR crops under runtime/artifacts.
    # Default is dry-run. Set BLACKSITE_RETENTION_COMMIT=1 only after boss
    # approval to enable actual move/delete.
    retention_args = ["scripts/retention_sweep.py", "--retain-days", "7"]
    if os.environ.get("BLACKSITE_RETENTION_COMMIT", "").strip() == "1":
        retention_args.append("--commit")
    sched.add_job(
        run_script, CronTrigger(hour=2, minute=40, timezone=TZ_NAME),
        args=tuple(retention_args),
        kwargs={"timeout_s": 60 * 20},
        id="retention_sweep", replace_existing=True,
    )

    # YouTube periodic
    sched.add_job(
        run_script, CronTrigger(minute=30, hour="*/6", timezone=TZ_NAME),
        args=("agents/youtube/yt_search.py",),
        id="yt_search", replace_existing=True,
    )
    sched.add_job(
        run_script, CronTrigger(minute=45, hour="*/2", timezone=TZ_NAME),
        args=("agents/youtube/yt_channel_monitor.py",),
        id="yt_channel_monitor", replace_existing=True,
    )

    # New platforms (added 2026-04-27). All anonymous read-only at v1; full
    # yield gated on TH SOCKS5 proxy (BLACKSITE_TH_PROXY env). Listeners
    # auto-detect proxy + gracefully degrade when missing or rate-limited.
    sched.add_job(
        run_script, CronTrigger(minute="*/30", timezone=TZ_NAME),
        args=("agents/pantip/pantip_listen.py",),
        id="pantip_scan", replace_existing=True,
    )
    sched.add_job(
        run_script, CronTrigger(minute="20,50", timezone=TZ_NAME),
        args=("agents/twitter/x_listen.py",),
        kwargs={"timeout_s": 60 * 15},
        id="x_metadata_scan", replace_existing=True,
    )
    sched.add_job(
        run_script, CronTrigger(minute="10,40", timezone=TZ_NAME),
        args=("agents/tiktok/tiktok_listen.py",),
        id="tiktok_scan", replace_existing=True,
    )
    sched.add_job(
        run_script, CronTrigger(minute=5, hour="*/2", timezone=TZ_NAME),
        args=("agents/reddit/reddit_listen.py",),
        id="reddit_scan", replace_existing=True,
    )

    # M9 v1.0 — TH-local livestream / KOL platform agents (added 2026-04-30).
    # All anonymous read-only at v1; FB Pages list stub-empty until Q6
    # Gemini DR harvest populates it. Stagger schedules to avoid IP-collision
    # when FlyVPN TH endpoint shared with pantip / tiktok.

    # 5/8 fb_page_scan DEREGISTERED — Meta killed mbasic ~4/30, m/www anon
    # paths return SPA shell with 0 server-rendered posts. fb_page_anon agent
    # marked dormant in agent_kpi (not decommissioned, in case Meta reverts).
    # Replaced by fb_og_meta_scan below using sanctioned facebookexternalhit
    # endpoint for og:* metadata-tier monitoring. system_history #1327 #1328.
    # OLD:
    # sched.add_job(
    #     run_script, CronTrigger(minute=25, timezone=TZ_NAME),
    #     args=("agents/facebook/fb_page_scan.py",),
    #     id="fb_page_scan", replace_existing=True,
    # )

    # NEW (5/8) — fb_og_meta_anon Field Agent (Tier 1 anonymous_web).
    # Hourly :30 — staggered after old :25 slot (avoids burst on shared IP),
    # gives 5-min headroom should fb_page_scan ever come back live for canary.
    # ~15 Pages × ~7 sec/fetch = ~2 min runtime; well under hour interval.
    sched.add_job(
        run_script, CronTrigger(minute=30, timezone=TZ_NAME),  # :30 every hour
        args=("agents/facebook/fb_og_meta_scan.py",),
        kwargs={"timeout_s": 60 * 10},  # 10 min ceiling (15 Pages × 7s + buffer)
        id="fb_og_meta_scan", replace_existing=True,
    )
    sched.add_job(
        run_script, CronTrigger(minute="15,45", timezone=TZ_NAME),  # :15 :45 every 30min
        args=("agents/bigo/bigo_lobby_scan.py",),
        id="bigo_lobby_scan", replace_existing=True,
    )
    # M9 v1.5 — Nimo TV (added 2026-04-30 post-Q6 Gemini DR; Tencent gaming
    # livestream + Bullet Screen high-velocity tip mechanic).
    sched.add_job(
        run_script, CronTrigger(minute="0,30", timezone=TZ_NAME),  # :00 :30 every 30min
        args=("agents/nimo/nimo_lobby_scan.py",),
        id="nimo_lobby_scan", replace_existing=True,
    )
    sched.add_job(
        run_script, CronTrigger(minute=35, timezone=TZ_NAME),  # :35 every 45min approx via every-hour
        args=("agents/trueid/trueid_listen.py",),
        id="trueid_scan", replace_existing=True,
    )
    # 5/7 Fleet rebalance (CLAUDE.md §1.1 #4 — Fleet by ROI not coverage):
    # Decommissioned 4 OTT scanners (oneD / ch3plus / aisplay / noice) per audit
    # memo §6.1. trueid_anon + sanook_anon retained as #8 shell-tier representatives.
    # KPI yaml + memory archived to runtime/agent_kpi/_retired/ + agent_memory/_retired/.
    # Scan scripts agents/<plat>/ kept on disk for re-enable optionality.
    # See system_history #1087-1090 + #1094.

    # FB+IG sock-puppet personas (P03/P04/P05) — gated by META_AGENTS_ENABLED env.
    # Set META_AGENTS_ENABLED=1 in .env AFTER first persona register success
    # (per fb_ig_strategy.md §6.2). Until then jobs would no-op anyway (scripts
    # bail when no FB c_user / IG sessionid cookies present), but skipping
    # registration avoids unnecessary subprocess spawn churn.
    if os.environ.get("META_AGENTS_ENABLED", "0") == "1":
        log_line("META_AGENTS_ENABLED=1 — registering FB+IG cron jobs")
        # Per-persona session windows in BKK time. Jitter prevents same-IP
        # same-time login (Meta's #1 cluster signal — see fb_ig_strategy.md §7).
        # P03 yolk = 3 sessions/day; P04 white = 2; P05 shell = 2.
        for persona, session_kind, hour, minute, jitter_s in [
            ("P03", "morning",   8, 30, 600),
            ("P03", "afternoon", 13, 30, 600),
            ("P03", "evening",   20, 0,  600),
            ("P04", "lunch",     12, 0,  600),
            ("P04", "evening",   21, 0,  600),
            ("P05", "lunch",     12, 0,  900),
            ("P05", "evening",   22, 0,  900),
        ]:
            sched.add_job(
                run_script,
                CronTrigger(hour=hour, minute=minute, timezone=TZ_NAME, jitter=jitter_s),
                args=("agents/facebook/warmup_loop.py", "--persona", persona,
                      "--session", session_kind),
                id=f"fb_warmup_{persona}_{session_kind}", replace_existing=True,
            )
            sched.add_job(
                run_script,
                CronTrigger(hour=hour, minute=minute + 5, timezone=TZ_NAME,
                            jitter=jitter_s),
                args=("agents/instagram/warmup_loop.py", "--persona", persona,
                      "--session", session_kind),
                id=f"ig_warmup_{persona}_{session_kind}", replace_existing=True,
            )

        # Daily account-health probe per persona (04:30 / 04:45 / 05:00 BKK).
        # Detects burn signals — pauses persona via lifecycle JSON if any.
        for persona, hour, minute in [("P03", 4, 30), ("P04", 4, 45), ("P05", 5, 0)]:
            sched.add_job(
                run_script, CronTrigger(hour=hour, minute=minute, timezone=TZ_NAME),
                args=("agents/facebook/account_health.py", "--persona", persona),
                id=f"meta_health_{persona}", replace_existing=True,
            )

        # IG Story sweep — defer until calibration phase (script self-gates;
        # checks lifecycle.current_stage and skips if register/limited).
        # 3× daily during BKK active hours.
        for hour in (10, 16, 22):
            for persona in ("P03", "P04"):  # P05 shell skips Story sweep
                sched.add_job(
                    run_script, CronTrigger(hour=hour, minute=15, timezone=TZ_NAME,
                                            jitter=600),
                    args=("agents/instagram/story_sweep.py", "--persona", persona),
                    id=f"ig_stories_{persona}_{hour:02d}", replace_existing=True,
                )

        # Daily lifecycle reset (23:50 BKK) — zeros today's engagement counter,
        # recomputes budget for stage+tier. Tiny script via meta_lifecycle CLI.
        for persona in ("P03", "P04", "P05"):
            sched.add_job(
                run_script, CronTrigger(hour=23, minute=50, timezone=TZ_NAME),
                args=("agents/_common/meta_lifecycle.py", persona, "--reset-today"),
                id=f"meta_lifecycle_reset_{persona}", replace_existing=True,
            )
    else:
        log_line("META_AGENTS_ENABLED unset — FB+IG cron jobs skipped "
                 "(set META_AGENTS_ENABLED=1 in .env after first persona register)")

    # JSONL → SQLite indexer (every 15 min). Projects every platform's raw
    # JSONL into queryable tables (messages/entities/media). Incremental
    # via ingestion_runs.last_offset cursor; idempotent.
    sched.add_job(
        run_script, IntervalTrigger(minutes=15),
        args=("scripts/index_jsonl.py",),
        id="index_jsonl", replace_existing=True,
    )

    # Rules-layer classifier (every 30 min). Reads messages with
    # processed_at_rules IS NULL; writes intent / topic / tone / lang_detected
    # / content_hash + extracts phone/lineid/promo/wallet identifier entities.
    # Idempotent. Runs after indexer so it always has the freshest atoms.
    sched.add_job(
        run_script, IntervalTrigger(minutes=30),
        args=("scripts/run_rules.py",),
        id="rules_layer", replace_existing=True,
    )

    # Gemini OCR (M2): every 3h. Each fire processes ~50 photos in 30-50 min
    # (Gemini Flash Lite ~36-72s per photo). 8 fires/day × ~50 = ~400/day
    # throughput within 1000 RPD self-cap. Audit 2026-05-02: OLD daily 03:30
    # cron hit 30 min subprocess timeout repeatedly (only 25/1000 succeeded
    # before kill). NEW: explicit timeout_s=4h kwarg + every-3h cadence.
    # 5/6 5070 上路 + boss directive: night-only cron to avoid daytime
    # CC contention. qwen2.5vl:7b on 5070 Ti 16GB ~3-5 sec/img; 200-photo
    # backlog clears in ~15 min. Single 03:00 GMT+7 daily fire = 04:00 Taiwan
    # boss desktop time = boss off-hours. ASR fires 04:00 — OCR finishes by
    # 03:30, model auto-unloads via keep_alive=30s, ASR has full VRAM.
    sched.add_job(
        run_script, CronTrigger(hour=3, minute=0, timezone=TZ_NAME),
        args=("scripts/run_ocr.py",),
        # 5/7 audit: 5/7 03:00 cron processed 420/576 in 60min then got
        # cron-killed (rate degraded 0.2→0.1 img/s mid-batch, likely Ollama
        # VRAM hiccups). Bumped 1h → 3h so 1000-photo backlog can complete.
        # ASR still fires 04:00 — qwen2.5vl unloads via keep_alive=30s after
        # last call so VRAM is free; if OCR still running at 04:00, ASR may
        # OOM-defer (acceptable, retries next day).
        kwargs={"timeout_s": 60 * 60 * 3},
        id="ocr_local", replace_existing=True,
    )

    # Whisper ASR (M3): daily 04:00 Bangkok, after pattern_miner. 5070 Ti
    # production route is faster-whisper large-v3 cuda/float16. Keep the batch
    # small enough to finish cleanly; backlog catch-up should be launched
    # deliberately so ASR does not collide with OCR/stage1 VRAM windows.
    sched.add_job(
        run_script, CronTrigger(hour=4, minute=0, timezone=TZ_NAME),
        args=("scripts/run_asr.py", "--limit", "30"),
        kwargs={"timeout_s": 60 * 45},
        id="asr_whisper", replace_existing=True,
    )

    # ASR quality audit: reference-free LLM accuracy proxy. Samples recent
    # voice/video transcripts, re-decodes with Whisper audit settings, and has
    # Codex audit tier judge language plausibility / transcript agreement /
    # commercial usability. Runs after OCR + ASR + 06:00 image audits.
    sched.add_job(
        run_script, CronTrigger(hour=7, minute=45, timezone=TZ_NAME),
        args=("processors/asr_quality_audit.py", "--kind", "daily"),
        kwargs={"timeout_s": 60 * 60},
        id="asr_quality_audit_daily", replace_existing=True,
    )
    sched.add_job(
        run_script,
        CronTrigger(day_of_week="mon", hour=8, minute=30, timezone=TZ_NAME),
        args=("processors/asr_quality_audit.py", "--kind", "weekly"),
        kwargs={"timeout_s": 60 * 60 * 2},
        id="asr_quality_audit_weekly", replace_existing=True,
    )

    # OCR quality audit (boss 5/6 directive: CC 智商抽檢): daily 06:00 GMT+7,
    # after OCR (03:00) and ASR (04:00) finish. Random-samples 10 rows from
    # last 30h, sends to claude.exe vision via OAuth, scores accuracy 0-100,
    # writes runtime/agent_kpi/ocr_audit/<date>.yaml + system_history metric.
    # Auto-warning if avg<75, ≥3 high-concern, or any loop/hallucination.
    # NOTE: superseded post-5/8 by `pipeline/audit_sonnet.py` per CLAUDE.md §2.1
    # — kept temporarily until audit_sonnet has 1 week of stable history.
    sched.add_job(
        run_script, CronTrigger(hour=6, minute=0, timezone=TZ_NAME),
        args=("processors/ocr_quality_audit.py",),
        kwargs={"timeout_s": 60 * 30},  # 30 min ceiling (10 calls × ~20s avg)
        id="ocr_audit", replace_existing=True,
    )

    # 3-stage hybrid OCR/KB-decision pipeline (CLAUDE.md §2.1, post-5/8).
    # Stage 1 (Qwen 7B local) — every 30 min, batch 100 (~16 min worst case
    # @ 9.4s/img). Filters ~75% noise. Sub-cron stage2 picks up signal rows.
    sched.add_job(
        run_script, IntervalTrigger(minutes=30),
        args=("processors/pipeline/stage1_qwen_filter.py", "--limit", "100"),
        kwargs={"timeout_s": 60 * 25},
        id="pipeline_stage1_qwen", replace_existing=True,
    )

    # Stage 2 (Codex/GPT subscription) — every 30 min @ minute 25/55 so Stage 1
    # completes first. Codex image calls vary widely (10-50s/req, occasional
    # 120s timeout), so keep batches bounded and let the script stop gracefully
    # before daemon timeout instead of being hard-killed mid-run.
    sched.add_job(
        run_script, CronTrigger(minute="25,55", timezone=TZ_NAME),
        args=("processors/pipeline/stage2_haiku_precision.py", "--limit", "15",
              "--max-runtime-sec", "720"),
        kwargs={"timeout_s": 60 * 15},
        id="pipeline_stage2_haiku", replace_existing=True,
    )

    # Stage 3 (Sonnet via claude.exe) — daily 19:00 GMT+7 (after Stage 2 has
    # fed all day; before strategist daily pulse 20:00 so memo can cite Stage 3
    # commercial actions). Batch 20 high-value (kb_value_score >= 70).
    # Sonnet ~60s/req → 20 min worst case. Pro plan quota.
    sched.add_job(
        run_script, CronTrigger(hour=19, minute=0, timezone=TZ_NAME),
        args=("processors/pipeline/stage3_sonnet_strategic.py", "--limit", "20"),
        kwargs={"timeout_s": 60 * 30},
        id="pipeline_stage3_sonnet", replace_existing=True,
    )

    # Audit Sonnet daily — 06:00 GMT+7 N=20 (5 noise/5 low/5 mid/5 high).
    # Drives auto-improvement loop on accuracy floor breach.
    sched.add_job(
        run_script, CronTrigger(hour=6, minute=0, timezone=TZ_NAME),
        args=("processors/pipeline/audit_sonnet.py", "--kind", "daily"),
        kwargs={"timeout_s": 60 * 35},  # 20 samples × ~60s + buffer
        id="pipeline_audit_daily", replace_existing=True,
    )

    # Audit Sonnet weekly — Monday 07:00 GMT+7 N=100 cross-7-day mix.
    sched.add_job(
        run_script,
        CronTrigger(day_of_week="mon", hour=7, minute=0, timezone=TZ_NAME),
        args=("processors/pipeline/audit_sonnet.py", "--kind", "weekly"),
        kwargs={"timeout_s": 60 * 60 * 2},  # 100 × ~60s = 100 min
        id="pipeline_audit_weekly", replace_existing=True,
    )

    # KB promotion (boss 5/8 「整理入庫」directive): hourly @ minute 40
    # — runs after Stage 2's :25/:55 fires, so admit rows from the latest
    # Haiku batch flow into cards table within the hour. Pure SQL write
    # (~410 rows/sec); 0 LLM cost. Idempotent via media_kb_decision.promoted_at.
    sched.add_job(
        run_script, CronTrigger(minute=40, timezone=TZ_NAME),
        args=("processors/pipeline/promote_to_kb.py", "--commit", "--limit", "500"),
        kwargs={"timeout_s": 60 * 5},
        id="pipeline_promote_to_kb", replace_existing=True,
    )

    # KB refresh-stage3 — hourly @ minute 45 (after promote @ :40 + Stage 3
    # cron at 19:00). Updates body_md of existing media_admit cards whose
    # Stage 3 commercial_action / cross_case_pattern arrived AFTER initial
    # promotion. Pure SQL write; 0 LLM cost.
    sched.add_job(
        run_script, CronTrigger(minute=45, timezone=TZ_NAME),
        args=("processors/pipeline/promote_to_kb.py", "--refresh-stage3",
              "--commit", "--limit", "1000"),
        kwargs={"timeout_s": 60 * 5},
        id="pipeline_promote_refresh_stage3", replace_existing=True,
    )

    # Funnel-edges builder (M4.5b): every 30 min. Synthesizes directed edges
    # from messages × messages_entities × entities (where kind ∈ tg_invite /
    # tg_channel_ref / tg_bot_deeplink) into the funnel_edges table. Sub-
    # second SQL aggregation, idempotent UPSERT preserves review/join state.
    sched.add_job(
        run_script, IntervalTrigger(minutes=30),
        args=("scripts/run_funnel_edges.py",),
        id="funnel_edges", replace_existing=True,
    )

    # Auto-review (boss directive 2026-05-01「能加就加」): every 30 min.
    # Classifies pending edges via rule-based policy:
    #   - REJECT: tg_invite + desperate tone, out-of-scope topic, known noise
    #   - APPROVE: in-scope intent+topic, bait_intent + multi-sender,
    #              grey-brand name pattern in target
    #   - else stays pending for boss manual review.
    # Replaces the manual review gate for funnel-push edges; join_loop in
    # tg_listen will pick up newly-approved edges on next cycle.
    sched.add_job(
        run_script, IntervalTrigger(minutes=30),
        args=("processors/funnel_auto_review.py",),
        id="funnel_auto_review", replace_existing=True,
    )

    # Entity decay (M5): daily 02:00 Bangkok, before archive_daily. Scans
    # all entities, transitions stale ones (per time_decay_class window) to
    # `dormant` / `superseded`. Mirrors state to cards. Skips manual states
    # (`noise`, `contradicted`).
    sched.add_job(
        run_script, CronTrigger(hour=2, minute=0, timezone=TZ_NAME),
        args=("scripts/run_entity_decay.py",),
        id="entity_decay", replace_existing=True,
    )

    # Lead pipeline (P1-P4 — kb/DESIGN.md §22 §23 / boss 5/2 PM directive).
    # daily_brief analyst emits leads.jsonl sidecar → ingested into kb_leads.
    # lead_triage classifies pending → triaged + lane every 15 min.
    # lead_executor dispatches AUTO_SAFE_EXEC / AUTO_SCHEDULE / SUBAGENT_DISPATCH
    # every 30 min. lead_lifecycle resolves executed → resolved_* daily 18:55.
    sched.add_job(
        run_script, IntervalTrigger(minutes=15),
        args=("processors/lead_triage.py",),
        id="lead_triage", replace_existing=True,
    )
    sched.add_job(
        run_script, IntervalTrigger(minutes=30),
        args=("processors/lead_executor.py",),
        id="lead_executor", replace_existing=True,
    )
    sched.add_job(
        run_script, CronTrigger(hour=18, minute=55, timezone=TZ_NAME),
        args=("processors/lead_lifecycle.py",),
        id="lead_lifecycle", replace_existing=True,
    )

    # Daily brief (M6 — pure-Python, replaces former scheduled-task path).
    # Runs prepare + compose in one shot. Boss-time target = 20:00 Taipei
    # (UTC+8) = 19:00 Bangkok (UTC+7). Daemon TZ_NAME is Bangkok so we use
    # hour=19. brief_send_loop in tg_listen polls queue/*.md every 5 min and
    # DMs to boss via P01. ZERO Anthropic OAuth dependency, ZERO LLM tokens.
    sched.add_job(
        run_script, CronTrigger(hour=19, minute=0, timezone=TZ_NAME),
        args=("processors/daily_brief.py", "daily"),
        id="daily_brief", replace_existing=True,
    )

    # ====================================================================
    # CLAUDE.md §15 multi-agent intelligence organization (3-tier)
    # ====================================================================
    # Strategy directive applier — daily 16:30 (BEFORE section_chief_eval at 17:00).
    # Processes unapplied yaml under runtime/strategy_directives/ from past 7d.
    # Handles the 7 org-adjustment directive kinds (chief_create / dissolve /
    # agent_reassign / metric_redefine / monitoring_track_open / org_meta_review
    # / agent_kpi_adjust). Per CLAUDE.md §15.W (boss 5/3 directive).
    # chief_dissolve still requires explicit boss_approved: true field.
    sched.add_job(
        run_script, CronTrigger(hour=16, minute=30, timezone=TZ_NAME),
        args=("processors/strategy_directive_apply.py",),
        id="strategy_directive_apply", replace_existing=True,
    )

    # Tier 2 Section Chief Field Agent orchestrator — every 5 min.
    # Reads instances/<inst>/policy/persona_warmup_schedule.yaml daily_windows;
    # for any window starting in next 5 min, spawns Field Agent warmup_session
    # via subprocess.Popen DETACHED_PROCESS. Per CLAUDE.md §15 + boss 5/6
    # directive 1 (anti-overlap algorithm-shape). Skips agents with status != live.
    # Currently fires --mode verify_only (smoke); v1.2 will introduce active mode.
    sched.add_job(
        run_script, IntervalTrigger(minutes=5),
        args=("processors/section_chief_orchestrate.py",),
        id="section_chief_orchestrate", replace_existing=True,
    )

    # Field Agent factory loop: converts chief repair tasks into work orders,
    # dispatches only explicit safe collectors, and grades 4h mission check-ins.
    # Token-free unless a future work order explicitly opts into an LLM lane.
    #
    # Persona Activity Governor is token-free and writes the boss/audit-facing
    # gate snapshot. The dispatch gate itself is enforced inside
    # field_agent_factory.py before a browser/process can launch.
    sched.add_job(
        run_script, IntervalTrigger(hours=1),
        args=("processors/persona_activity_governor.py",),
        kwargs={"timeout_s": 60 * 5},
        id="persona_activity_governor", replace_existing=True,
    )
    sched.add_job(
        run_script, IntervalTrigger(minutes=15),
        args=("processors/field_agent_factory.py", "--dispatch"),
        kwargs={"timeout_s": 60 * 20},
        id="field_agent_factory", replace_existing=True,
    )

    # Seed Intelligence factory: candidate -> verified seed -> watchlist/action
    # queue. No follow/subscribe/like happens here; high-risk actions remain
    # boss-approved and are later guarded by Persona Activity Governor.
    sched.add_job(
        run_script, IntervalTrigger(hours=4),
        args=("processors/seed_intelligence.py", "--hours", "96"),
        kwargs={"timeout_s": 60 * 10},
        id="seed_intelligence", replace_existing=True,
    )
    sched.add_job(
        run_script, IntervalTrigger(hours=4),
        args=("processors/seed_audit.py",),
        kwargs={"timeout_s": 60 * 5},
        id="section_chief_seed_audit", replace_existing=True,
    )
    # Section Chief seed processor: reads queued actions → auto-approve/reject/escalate
    # → updates approved_seeds.json + watchlist + dispatch records. Runs after seed_audit.
    sched.add_job(
        run_script, IntervalTrigger(hours=4),
        args=("processors/section_chief_seed_processor.py",),
        kwargs={"timeout_s": 60 * 5},
        id="section_chief_seed_processor", replace_existing=True,
    )
    sched.add_job(
        run_script, CronTrigger(hour=20, minute=35, timezone=TZ_NAME),
        args=("processors/seed_strategy_portfolio.py",),
        kwargs={"timeout_s": 60 * 5},
        id="chief_strategist_seed_portfolio", replace_existing=True,
    )

    # Tier 2 小主管 KPI evaluator — daily 17:00 (2h before 19:00 brief).
    # Updates runtime/agent_kpi/<id>.yaml for every Field Agent in
    # agent_kpi_baseline.yaml; opens incidents on red transitions.
    # Per boss 5/2 Q5: NEVER auto-pauses agents — only opens incidents.
    # Multi-chief (boss 5/3 §15.Z): iterates over all SECTION_CHIEF*.md
    # memory files; each chief filtered to their managed Field Agents.
    sched.add_job(
        run_script, CronTrigger(hour=17, minute=0, timezone=TZ_NAME),
        args=("processors/section_chief_eval.py",),
        id="section_chief_eval", replace_existing=True,
    )

    # Incident auto-escalator — daily 03:00. Lightweight pass through
    # runtime/agent_incidents/*.md; transitions in_review/open incidents
    # older than 7d to escalated_strategist (boss 5/2 Q5 chain-review SOP).
    sched.add_job(
        run_script, CronTrigger(hour=3, minute=0, timezone=TZ_NAME),
        args=("processors/agent_incidents.py", "escalate-aged"),
        id="incident_escalator", replace_existing=True,
    )

    # Tier 3 策略長 weekly synthesizer — Sunday 21:00 GMT+7. Reads past 7d
    # KB cards / leads / opinions / digest / open incidents; spawns Claude
    # Opus 4.7 1M with CHIEF_STRATEGIST.md skill; writes strategy memo +
    # directive yaml + brief queue [STRATEGY] insert. Boss-trigger via
    # cmd_fast_path 「策略長 上工」 calls --force separately (detached Popen).
    sched.add_job(
        run_script, CronTrigger(day_of_week='sun', hour=21, minute=0, timezone=TZ_NAME),
        args=("processors/chief_strategist.py",),
        id="chief_strategist", replace_existing=True,
    )

    # 5/7 audit fix — Synthesis 層 watchdog: queue file 超 6h 未 compose = LLM compose
    # 環節斷電。直接觸發 compose_cards_loop 自動處理 backlog（boss 5/7 directive：不再
    # 依賴 Claude scheduled task `blacksite-cards-build`，因為 5/2 queue 5 天沒人 compose
    # 證明它沒在運轉）。每 4h 跑一次，跟原本 cron 對齊。
    sched.add_job(
        run_script, CronTrigger(hour="2,6,10,14,18,22", minute=15, timezone=TZ_NAME),
        args=("scripts/compose_cards_loop.py",),
        id="compose_cards_loop", replace_existing=True,
    )

    # Tier 1 Field Agent DAILY SELF-EVAL — 18:00 GMT+7 (boss 5/7 ritual cadence Layer 1).
    # Each agent writes 1 line into its `# 我的經驗` memory section: status + yield +
    # smoke + anomaly flags. Template-driven (no LLM per agent — 25 agents/day too costly).
    # Runs BEFORE 19:00 Section Chief eval so chief sees fresh self-eval breadcrumbs.
    sched.add_job(
        run_script, CronTrigger(hour=18, minute=0, timezone=TZ_NAME),
        args=("processors/field_agent_self_eval.py",),
        id="field_agent_self_eval", replace_existing=True,
    )

    # Tier 3 strategist DAILY PULSE — 20:00 GMT+7 (boss 5/7 ritual cadence design).
    # 200-字 strategist 短 memo + brief queue 3-line DM commander. Bridges between
    # weekly memo (Sun 21:00) and rest of week — solves the strategist 「reactive
    # only」 antipattern (7 days only 1 directive issued = 停擺 between Sundays).
    # Outputs:
    #   runtime/strategy_memos/daily_pulse_<date>.md  (full memo)
    #   runtime/briefs/queue/pending_<date>_strategist_pulse.md  (3-line DM)
    # Threshold alerts auto-prepend [ALERT] to brief queue.
    sched.add_job(
        run_script, CronTrigger(hour=20, minute=0, timezone=TZ_NAME),
        args=("processors/strategist_daily_pulse.py",),
        id="strategist_daily_pulse", replace_existing=True,
    )

    # Note: tg_crawler.py one-shot backfill is run MANUALLY *before* daemon starts
    # (or while listener is paused) — it would session-lock conflict if scheduled here.

    handoff_run_script_jobs_to_manual(sched)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        sched.start()
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        cleanup()


if __name__ == "__main__":
    main()
