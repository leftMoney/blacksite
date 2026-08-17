"""processors/section_chief_orchestrate.py ??Field Agent daily orchestration.

Boss 5/6 directive 1+3: schedule 11 LIVE persona-driven Field Agents per
`instances/_TEMPLATE/policy/persona_warmup_schedule.yaml` daily_windows.

CURRENT STATE: live window scanner. Reads schedule every tick, enforces
anti-overlap, and spawns only when the platform warmup script exists and the
agent KPI status is live.

Cron registration: hourly `* :00` in scripts/blacksite_daemon.py (TODO).

v1.1 ship requirements:
1. 6 new scripts: agents/{tiktok,pantip,discord,reddit,twitter_x,youtube}/warmup_session.py
   (FB / IG already have feed_harvest / warmup_loop infrastructure to reuse)
2. Replace `_spawn_agent_dry_run()` with real subprocess.Popen + DETACHED_PROCESS
3. Anti-overlap enforcement: check `runtime/agent_running/<aid>.lock` before spawn
4. Cron register in blacksite_daemon.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
import yaml

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
POLICY = ROOT / "instances" / ACTIVE_INSTANCE / "policy"
SCHEDULE_PATH = POLICY / "persona_warmup_schedule.yaml"
BASELINE_PATH = POLICY / "agent_kpi_baseline.yaml"
LOG_DIR = RUNTIME / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RUNNING_DIR = RUNTIME / "agent_running"
RUNNING_DIR.mkdir(parents=True, exist_ok=True)

# Per-window idempotency state: prevent re-firing the same agent inside the
# same daily window. cron runs every 5 min; without this guard a 25-min
# window would spawn 5x verify_only sessions = persona OPSEC red flag
# (multi-login fingerprint per CLAUDE.md §9). Keyed by
# f"{agent_id}|{window_start_iso}"; pruned entries > 2 days old.
STATE_DIR = RUNTIME / "section_chief"
STATE_DIR.mkdir(parents=True, exist_ok=True)
FIRED_STATE_PATH = STATE_DIR / "orchestrate_fired.json"

TZ = timezone(timedelta(hours=7))


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [section_chief_orchestrate] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"section_chief_orchestrate_{now().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _load_fired_state() -> dict:
    """Load per-window idempotency state. Tolerant of missing/corrupt file."""
    if not FIRED_STATE_PATH.exists():
        return {}
    try:
        return json.loads(FIRED_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"WARN: fired-state load failed ({e}); resetting to empty")
        return {}


def _save_fired_state(state: dict) -> None:
    """Atomic write via tempfile.replace to avoid partial-file corruption."""
    tmp = FIRED_STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(FIRED_STATE_PATH)


def _fired_key(agent_id: str, window_start: datetime) -> str:
    """Key combines agent + day-anchored window start so the next day's
    same-hh window is a fresh entry (and prune-able)."""
    return f"{agent_id}|{window_start.isoformat(timespec='minutes')}"


def _prune_fired_state(state: dict, *, keep_days: int = 2) -> dict:
    """Drop entries older than `keep_days` to keep the file bounded."""
    cutoff = (now() - timedelta(days=keep_days)).isoformat()
    return {k: v for k, v in state.items() if v >= cutoff}


def parse_window(hh: str) -> tuple[datetime, datetime]:
    """'07:00-07:25' ??(today 07:00 GMT+7, today 07:25 GMT+7)."""
    start_s, end_s = hh.split("-")
    today = now().date()
    sh, sm = [int(x) for x in start_s.split(":")]
    eh, em = [int(x) for x in end_s.split(":")]
    start = datetime(today.year, today.month, today.day, sh, sm, tzinfo=TZ)
    end = datetime(today.year, today.month, today.day, eh, em, tzinfo=TZ)
    return start, end


def is_agent_running(aid: str) -> bool:
    """Check anti-overlap lock file."""
    lock = RUNNING_DIR / f"{aid}.lock"
    if not lock.exists():
        return False
    try:
        pid = int(lock.read_text().strip())
        # cheap check: PID file exists. v1.1 should psutil-verify alive.
        return True
    except Exception:
        return False


def is_persona_or_platform_busy(persona: str, platform: str) -> tuple[bool, str]:
    """Anti-overlap check per boss 5/6 strict rule."""
    for lock in RUNNING_DIR.glob("*.lock"):
        aid = lock.stem
        # Parse aid like "P03_FB" ??persona=P03, platform=FB
        parts = aid.split("_", 1)
        if len(parts) != 2:
            continue
        p_other, plat_other = parts
        if p_other == persona:
            return True, f"persona {persona} busy on {plat_other}"
        if plat_other.lower() == platform.lower():
            return True, f"platform {platform} busy with {p_other}"
    return False, ""


# platform ??script directory (most match name; X is at agents/twitter/)
PLATFORM_TO_SCRIPT_DIR = {
    "twitter_x": "twitter",
}


def _agent_baseline(aid: str) -> dict:
    try:
        data = yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data.get("field_agent", {}).get(aid, {}) or {}


def _agent_status_is_live(aid: str) -> bool:
    """Check KPI yaml status ??only spawn when live (skip yellow / red)."""
    import yaml
    kpi_dir = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "agent_kpi"
    f = kpi_dir / f"{aid}.yaml"
    if not f.exists():
        return False
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        status = str(data.get("status", "")).strip().lower()
        if status in {"red", "paused", "retired", "decommissioned", "burned"}:
            return False
        if data.get("live_status"):
            return status in {"", "live", "green", "yellow"}
        return status == "live"
    except Exception:
        return False


def _build_spawn_cmd(window: dict) -> tuple[list[str], str]:
    aid = window["agent_id"]
    persona = window["persona"]
    platform = window["platform"]
    plat_dir = PLATFORM_TO_SCRIPT_DIR.get(platform, platform.lower())
    baseline = _agent_baseline(aid)
    is_verify_only = bool(baseline.get("is_verify_only", True))

    if not is_verify_only:
        harvest_script = ROOT / "agents" / plat_dir / "feed_harvest.py"
        if harvest_script.exists():
            return (
                [sys.executable, str(harvest_script), "--persona", persona, "--duration-min", "8"],
                "active_feed_harvest",
            )

    script = ROOT / "agents" / plat_dir / "warmup_session.py"
    mode = "verify_only" if is_verify_only else "active"
    return [sys.executable, str(script), "--persona", persona, "--mode", mode], f"warmup_{mode}"


def _spawn_agent(window: dict) -> bool:
    """v1.1 real spawn ??subprocess.Popen DETACHED_PROCESS Camoufox session.

    Returns True iff a subprocess was actually launched (so the caller can
    mark the per-window fired state). False = status-not-live or missing
    script; the window stays unfired and may retry on the next tick if the
    block is transient.
    """
    import subprocess
    aid = window["agent_id"]
    if not _agent_status_is_live(aid):
        log(f"[SKIP] {aid} status != live ??not spawning")
        return False

    cmd, launch_kind = _build_spawn_cmd(window)
    script = Path(cmd[1])
    if not script.exists():
        log(f"[ERR] {aid}: script missing {script}")
        hist("warning", title=f"{aid} spawn FAIL ??script missing",
             body=f"expected {script}")
        return False

    log_path = LOG_DIR / f"warmup_{aid}_{now().strftime('%Y%m%d')}.log"
    log(f"[SPAWN] {aid} kind={launch_kind} cmd={' '.join(cmd)} -> log={log_path.name}")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )

    with log_path.open("a", encoding="utf-8") as logf:
        proc = subprocess.Popen(cmd, stdout=logf, stderr=logf,
                                stdin=subprocess.DEVNULL,
                                cwd=str(ROOT), creationflags=creationflags)
    log(f"[SPAWN] {aid} pid={proc.pid}")
    hist("metric", title=f"orchestrate spawn {aid}",
         body=f"pid={proc.pid} launch_kind={launch_kind} window={window['hh']}",
         scope="section_chief")
    return True


def hist(kind: str, title: str, body: str = "", scope: str = "section_chief",
         refs: list | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(actor="section_chief_orchestrate", kind=kind, scope=scope,
                         title=title, body=body, refs=refs)
    except Exception as e:
        log(f"hist log failed: {e}")
        return -1


def main() -> int:
    log("starting orchestration tick (window scan)")
    if not SCHEDULE_PATH.exists():
        log(f"ERROR: schedule not found: {SCHEDULE_PATH}")
        return 1

    schedule = yaml.safe_load(SCHEDULE_PATH.read_text(encoding="utf-8"))
    windows = schedule.get("daily_windows", [])
    log(f"loaded {len(windows)} daily windows from schedule v{schedule.get('schedule_version')}")

    fired_state = _load_fired_state()
    n = now()
    fired = 0
    skipped_dup = 0
    state_dirty = False
    for w in windows:
        start, end = parse_window(w["hh"])
        # Window starts in next 5 min OR currently within window
        if start <= n <= end:
            phase = "in_window"
        elif start - timedelta(minutes=5) <= n < start:
            phase = "starting_soon"
        else:
            continue

        # Per-window idempotency: same (agent_id, window_start) fires once.
        # Without this, cron-every-5min × window-25min = 5 spawns per window,
        # which platform anti-bot systems flag as repeated short-lived sessions
        # (CLAUDE.md §9 OPSEC red line for cold/yellow personas).
        key = _fired_key(w["agent_id"], start)
        if key in fired_state:
            log(f"  [SKIP] {w['agent_id']} window={w['hh']} already fired @ {fired_state[key]}")
            skipped_dup += 1
            continue

        # Anti-overlap check
        busy, reason = is_persona_or_platform_busy(w["persona"], w["platform"])
        if busy:
            log(f"  [SKIP] {w['agent_id']} blocked: {reason}")
            continue

        log(f"  [FIRE] {w['agent_id']} window={w['hh']} phase={phase}")
        if _spawn_agent(w):
            fired_state[key] = now().isoformat(timespec="seconds")
            state_dirty = True
            fired += 1

    if state_dirty:
        fired_state = _prune_fired_state(fired_state)
        _save_fired_state(fired_state)

    log(f"tick complete: {fired} agents fired, {skipped_dup} skipped (already-fired this window)")
    if fired:
        hist("metric",
             title=f"orchestrate tick: {fired} agents fired (v1.1 real spawn)",
             body=f"real subprocess.Popen DETACHED_PROCESS. fired_count={fired} "
                  f"skipped_already_fired={skipped_dup}",
             scope="section_chief")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
