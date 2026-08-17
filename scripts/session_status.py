"""
Blacksite — session status diagnostic. Prints a structured snapshot of running
processes, raw-data freshness, and recent intel size. Called by §4 Bootstrap
step 4 every session start (including post-/clear).

Output sections (machine-readable lines + human-readable narrative):
  daemon: alive | dead | unknown
  listener: alive (last_evt=ISO) | stale | dead
  raw_today: <persona> <bytes> <modified>
  funnel_graph: <lines>
  classified_entities: <count>
  pending_harvest: <count> (from CHECKPOINT.md)

Exit code: 0 if all healthy, 1 if any issue (so a wrapper can shell-check).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME / "logs"
SESSION_DIR = RUNTIME / "sessions"
RAW_DIR = RUNTIME / "raw"
PID_FILE = RUNTIME / "daemon.pid"
HEARTBEAT_FILE = RUNTIME / "daemon.heartbeat"
CRON_ACTIVITY_FILE = RUNTIME / "daemon.cron_activity"
CHECKPOINT = ROOT / "instances" / ACTIVE_INSTANCE / "CHECKPOINT.md"
SCHEDULE_PATH = ROOT / "instances" / ACTIVE_INSTANCE / "policy" / "persona_warmup_schedule.yaml"

TZ = timezone(timedelta(hours=7))

LISTENER_STALE_MIN = 5    # listener log line; tight — listener writes on every msg + per-startup
RAW_STALE_MIN = 30        # per-persona raw jsonl; loose — quiet channels OK
WINDOW_GRACE_MIN = 30
HEARTBEAT_STALE_MIN = 5   # daemon sentinel cron */2 min; 5 min = 2 missed beats = APScheduler zombie
CRON_ACTIVITY_STALE_MIN = 20


def now() -> datetime:
    return datetime.now(TZ)


def is_pid_alive(pid: int) -> bool:
    """Windows + POSIX. Returns True if process exists."""
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in r.stdout
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def count_blacksite_processes() -> dict:
    try:
        import psutil
    except Exception:
        return {"daemons": [], "listeners": []}
    daemons = []
    listeners = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or []).replace("\\", "/").lower()
        except Exception:
            continue
        if "d:/blacksite/" not in cmd:
            continue
        if "scripts/blacksite_daemon.py" in cmd:
            daemons.append(proc.pid)
        elif "agents/telegram/tg_listen.py" in cmd:
            listeners.append(proc.pid)
    return {"daemons": sorted(daemons), "listeners": sorted(listeners)}


def check_daemon() -> dict:
    """Combined check: process alive AND APScheduler firing (heartbeat fresh).
    Catches zombie daemon (process up but scheduler dead — 2026-04-30 incident)."""
    if not PID_FILE.exists():
        return {"status": "no_pid_file", "alive": False, "pid": None,
                "scheduler_alive": False, "heartbeat_age_min": None}
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        return {"status": "pid_file_unreadable", "alive": False, "pid": None,
                "scheduler_alive": False, "heartbeat_age_min": None}
    proc_alive = is_pid_alive(pid)
    hb_age = None
    cron_age = None
    sched_alive = False
    cron_alive = False
    if HEARTBEAT_FILE.exists():
        hb_age = (datetime.now(TZ).timestamp() - HEARTBEAT_FILE.stat().st_mtime) / 60
        sched_alive = hb_age < HEARTBEAT_STALE_MIN
    if CRON_ACTIVITY_FILE.exists():
        cron_age = (datetime.now(TZ).timestamp() - CRON_ACTIVITY_FILE.stat().st_mtime) / 60
        cron_alive = cron_age < CRON_ACTIVITY_STALE_MIN
    if not proc_alive:
        status = "dead_stale_pid"
    elif not sched_alive:
        status = "ZOMBIE_scheduler_dead" if hb_age is not None else "no_heartbeat_file"
    elif not cron_alive:
        status = "ZOMBIE_cron_activity_stalled" if cron_age is not None else "no_cron_activity_file"
    else:
        status = "alive"
    return {
        "status": status,
        "alive": proc_alive and sched_alive and cron_alive,
        "pid": pid,
        "scheduler_alive": sched_alive,
        "heartbeat_age_min": round(hb_age, 1) if hb_age is not None else None,
        "cron_activity_age_min": round(cron_age, 1) if cron_age is not None else None,
    }


def check_listener() -> dict:
    """Latest event line in tg_listen_subproc.log; freshness vs threshold."""
    log_files = sorted(LOG_DIR.glob("tg_listen_subproc*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not log_files:
        return {"status": "no_log", "last_event": None}
    log = log_files[0]
    last_iso = None
    try:
        with log.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace").splitlines()
        for line in reversed(tail):
            m = re.search(r"\[(\d{4}-\d{2}-\d{2}T[\d:.]+(?:\+\d{2}:\d{2})?)\]", line)
            if m:
                last_iso = m.group(1)
                break
    except Exception:
        pass
    if not last_iso:
        return {"status": "log_empty", "last_event": None}
    try:
        last_dt = datetime.fromisoformat(last_iso)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=TZ)
    except Exception:
        return {"status": "unparseable_ts", "last_event": last_iso}
    age_min = (now() - last_dt).total_seconds() / 60
    return {
        "status": "alive" if age_min < LISTENER_STALE_MIN else "stale",
        "last_event": last_iso,
        "age_min": round(age_min, 1),
    }


def _raw_aid(persona: str, platform: str) -> str:
    platform_key = platform.lower()
    mapping = {
        "facebook": "FB",
        "instagram": "IG",
        "tiktok": "TikTok",
        "pantip": "Pantip",
        "discord": "Discord",
        "reddit": "Reddit",
        "twitter_x": "X",
        "youtube": "YouTube_sports",
    }
    return f"{persona}_{mapping.get(platform_key, platform.title())}"


def _load_daily_windows() -> dict[str, dict]:
    if not SCHEDULE_PATH.exists():
        return {}
    out: dict[str, dict] = {}
    current: dict | None = None
    in_daily = False
    for line in SCHEDULE_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        if re.match(r"^daily_windows:\s*$", line):
            in_daily = True
            continue
        if in_daily and re.match(r"^[A-Za-z_].*:\s*$", line):
            break
        if not in_daily:
            continue
        m = re.match(r"\s*-\s+hh:\s*[\"']?(\d{2}:\d{2}-\d{2}:\d{2})[\"']?", line)
        if m:
            current = {"hh": m.group(1)}
            continue
        if current is None:
            continue
        for key in ("persona", "platform", "agent_id"):
            m = re.match(rf"\s+{key}:\s*[\"']?([^\"'#]+)", line)
            if m:
                current[key] = m.group(1).strip()
        if {"hh", "persona", "platform", "agent_id"} <= set(current):
            aid = _raw_aid(current["persona"], current["platform"])
            out[aid] = dict(current, raw_aid=aid)
            current = None
    return out


def _window_times(hh: str) -> tuple[datetime, datetime]:
    start_s, end_s = hh.split("-")
    today = now().date()
    sh, sm = [int(x) for x in start_s.split(":")]
    eh, em = [int(x) for x in end_s.split(":")]
    return (
        datetime(today.year, today.month, today.day, sh, sm, tzinfo=TZ),
        datetime(today.year, today.month, today.day, eh, em, tzinfo=TZ),
    )


def _read_auth_state(path: Path) -> dict:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return {}
    for line in reversed(lines[-50:]):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        event = rec.get("event")
        if event == "session_recovery":
            return {
                "logged_in": bool(rec.get("recovery_success")),
                "human_action_required": bool(rec.get("human_action_required")),
                "recovery_attempted": bool(rec.get("attempted")),
                "recovery_reason": rec.get("reason"),
                "action_hint": rec.get("action_hint"),
            }
        if event == "verify_session" and "logged_in" in rec:
            return {
                "logged_in": bool(rec.get("logged_in")),
                "human_action_required": False,
                "recovery_attempted": bool(rec.get("recovered_from_not_logged_in")),
                "recovery_reason": "recovered" if rec.get("recovered_from_not_logged_in") else None,
                "action_hint": None,
            }
    return {}


def check_raw_freshness() -> list[dict]:
    out = []
    today = now().strftime("%Y-%m-%d")
    if not RAW_DIR.exists():
        return out
    windows = _load_daily_windows()
    raw_names = {
        p.name for p in RAW_DIR.iterdir()
        if p.is_dir() and p.name.startswith("P")
    }
    for persona in sorted(raw_names | set(windows)):
        if not persona.startswith("P"):
            continue
        schedule = windows.get(persona)
        if windows and schedule is None:
            continue
        persona_dir = RAW_DIR / persona
        path = persona_dir / f"{today}.jsonl"
        start = end = None
        due = True
        if schedule:
            start, end = _window_times(schedule["hh"])
            due = now() >= end + timedelta(minutes=WINDOW_GRACE_MIN)
        if not path.exists():
            out.append({
                "persona": persona,
                "exists": False,
                "window": schedule.get("hh") if schedule else None,
                "issue": due,
                "status": "missing_after_window" if due else "not_due",
            })
            continue
        st = path.stat()
        age_min = (datetime.now(TZ).timestamp() - st.st_mtime) / 60
        mtime = datetime.fromtimestamp(st.st_mtime, TZ)
        auth = _read_auth_state(path)
        login_ok = auth.get("logged_in")
        if schedule:
            window_ok = mtime >= start - timedelta(minutes=5)
            status = "window_ok" if window_ok else "stale_for_window"
            issue = due and not window_ok
        else:
            window_ok = age_min < RAW_STALE_MIN
            status = "fresh" if window_ok else "stale"
            issue = not window_ok
        if auth.get("human_action_required"):
            status = "manual_login_required"
            issue = True
        elif login_ok is False:
            status = "not_logged_in"
            issue = True
        out.append({
            "persona": persona,
            "exists": True,
            "size": st.st_size,
            "age_min": round(age_min, 1),
            "fresh": window_ok,
            "window": schedule.get("hh") if schedule else None,
            "issue": issue,
            "status": status,
            "logged_in": login_ok,
            "recovery_attempted": bool(auth.get("recovery_attempted")),
            "recovery_reason": auth.get("recovery_reason"),
            "action_hint": auth.get("action_hint"),
        })
    return out


def check_intel_size() -> dict:
    fg = RUNTIME / "funnel_graph.jsonl"
    ec = RUNTIME / "entities_classified.jsonl"
    fg_lines = sum(1 for _ in fg.open(encoding="utf-8")) if fg.exists() else 0
    ec_lines = sum(1 for _ in ec.open(encoding="utf-8")) if ec.exists() else 0
    return {"funnel_graph_lines": fg_lines, "entities_classified": ec_lines}


def count_listener_crashes_recent(window_min: int = 30) -> int:
    """Count 'listener died, restarting' events in today's daemon log within
    last N minutes. > 5 = crash storm — supervisor may be false-positive
    death-detecting an orphan tg_listen process (2026-05-02 17:11+: 110+
    crashes in 100 min while orphan PID 11192 was actually working fine)."""
    today = now().strftime("%Y-%m-%d")
    log = LOG_DIR / f"daemon_{today}.log"
    if not log.exists():
        return 0
    cutoff = (now() - timedelta(minutes=window_min)).isoformat(timespec="seconds")
    count = 0
    try:
        with log.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "listener died, restarting" not in line:
                    continue
                m = re.search(r"\[(\d{4}-\d{2}-\d{2}T[\d:.]+(?:[+-]\d{2}:\d{2})?)\]", line)
                if m and m.group(1) >= cutoff:
                    count += 1
    except Exception:
        return 0
    return count


def count_pending_harvest() -> int:
    """Count harvest items: bullet `- ` lines OR table rows starting `| Q\\d+ |`."""
    if not CHECKPOINT.exists():
        return 0
    text = CHECKPOINT.read_text(encoding="utf-8")
    m = re.search(r"# Pending harvest.*?(?=\n# |\Z)", text, re.DOTALL)
    if not m:
        return 0
    section = m.group(0)
    bullets = len(re.findall(r"^- ", section, re.MULTILINE))
    table_rows = len(re.findall(r"^\| Q\d+\s*\|", section, re.MULTILINE))
    return bullets + table_rows


def main() -> int:
    daemon = check_daemon()
    listener = check_listener()
    raw = check_raw_freshness()
    intel = check_intel_size()
    harvest = count_pending_harvest()
    crashes_30m = count_listener_crashes_recent(30)
    procs = count_blacksite_processes()

    print(f"=== Blacksite session status @ {now().isoformat(timespec='seconds')} ===")
    print(f"instance         : {ACTIVE_INSTANCE}")
    hb_str = (f", heartbeat={daemon['heartbeat_age_min']}min"
              if daemon['heartbeat_age_min'] is not None else ", heartbeat=missing")
    cron_str = (f", cron_activity={daemon['cron_activity_age_min']}min"
                if daemon.get("cron_activity_age_min") is not None else ", cron_activity=missing")
    print(f"daemon           : {daemon['status']} (pid={daemon['pid']}{hb_str}{cron_str})")
    print(f"daemon_processes : {len(procs['daemons'])} {procs['daemons']}")
    print(f"listener_processes: {len(procs['listeners'])} {procs['listeners']}")
    crash_str = f", crashes_30m={crashes_30m}" if crashes_30m else ""
    print(f"listener         : {listener['status']} (last_evt={listener.get('last_event')}, age={listener.get('age_min')} min{crash_str})")
    for r in raw:
        window = f", window={r['window']}" if r.get("window") else ""
        auth_note = ""
        if r.get("status") == "manual_login_required" and r.get("recovery_reason"):
            auth_note = f", reason={r['recovery_reason']}"
        elif r.get("status") == "not_logged_in" and r.get("recovery_attempted"):
            auth_note = ", recovery_attempted=true"
        if not r["exists"]:
            print(f"raw[{r['persona']}]      : MISSING today's jsonl{window} ({r.get('status')})")
        else:
            print(
                f"raw[{r['persona']}]      : {r['size']}B, {r['age_min']} min ago "
                f"({r.get('status')}){window}{auth_note}"
            )
    print(f"funnel_graph     : {intel['funnel_graph_lines']} lines")
    print(f"entities_classified: {intel['entities_classified']}")
    print(f"pending_harvest  : {harvest} item(s) — see CHECKPOINT § Pending harvest")

    issues = []
    if len(procs["daemons"]) != 1:
        issues.append(f"daemon process count != 1: {procs['daemons']}")
    if len(procs["listeners"]) > 1:
        issues.append(f"listener process count > 1: {procs['listeners']}")
    if daemon["status"] == "ZOMBIE_scheduler_dead":
        issues.append(
            f"🔴 daemon ZOMBIE: process pid={daemon['pid']} alive but APScheduler "
            f"dead (heartbeat stale {daemon['heartbeat_age_min']}min) → "
            f"run scripts\\stop_daemon.bat then scripts\\run_daemon.bat"
        )
    elif daemon["status"] == "ZOMBIE_cron_activity_stalled":
        issues.append(
            f"daemon CRON STALLED: heartbeat fresh but no run_script activity for "
            f"{daemon['cron_activity_age_min']}min; restart daemon"
        )
    elif not daemon["alive"]:
        issues.append("daemon dead → run scripts\\run_daemon.bat")
    if listener["status"] in ("stale", "dead_stale_pid", "no_log"):
        issues.append(f"listener {listener['status']} → restart daemon if persists")
    if crashes_30m > 5:
        issues.append(
            f"🔴 LISTENER CRASH STORM: {crashes_30m} 'listener died' events in last 30 min "
            f"(threshold 5) — supervisor may be false-positive death-detecting an orphan "
            f"tg_listen. Check `tasklist /FI \"IMAGENAME eq pythonw.exe\"` for tg_listen.py; "
            f"if found AND raw events still flowing → daemon needs stop+restart so it can "
            f"adopt the orphan via _find_existing_tg_listen_pid()"
        )
    for r in raw:
        if r.get("issue"):
            if r.get("status") == "manual_login_required":
                issues.append(
                    f"raw {r['persona']} manual login required "
                    f"({r.get('recovery_reason') or 'recovery failed'})"
                )
            elif r.get("status") == "not_logged_in":
                issues.append(f"raw {r['persona']} verify_session not logged in")
            elif r["exists"]:
                issues.append(f"raw {r['persona']} not updated for scheduled window {r.get('window')}")
            else:
                issues.append(f"raw {r['persona']} missing after scheduled window {r.get('window')}")

    if issues:
        print("\nISSUES:")
        for i in issues:
            print(f"  ⚠ {i}")
        ret = 1
    else:
        print("\nALL GREEN")
        ret = 0

    # SQLite index summary (ingestion progress)
    try:
        sys.path.insert(0, str(ROOT))
        from db.connection import get_connection
        conn = get_connection()
        msg_n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        ent_n = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        med_n = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        med_sz = conn.execute("SELECT COALESCE(SUM(file_size),0) FROM media").fetchone()[0]
        last_idx = conn.execute(
            "SELECT MAX(last_indexed_at) FROM ingestion_runs"
        ).fetchone()[0]
        conn.close()
        sz_mb = med_sz / (1024 * 1024)
        print(f"\nSQLite index : {msg_n} msgs / {ent_n} entities / {med_n} media ({sz_mb:.1f} MB)")
        print(f"last indexed : {last_idx}")
    except Exception as e:
        print(f"\nSQLite index : (err: {type(e).__name__}: {e})")

    print(
        "\n下一步建議："
        "\n  - 看夜班簡報：py scripts/night_brief.py --hours 8"
        "\n  - 看更長窗口：py scripts/night_brief.py --hours 24"
    )
    return ret


if __name__ == "__main__":
    sys.exit(main())
