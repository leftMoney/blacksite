"""Commander fast-path maintenance operations.

Whitelist-only helper used by Telegram Commander commands. It can restart
Blacksite or rerun known maintenance jobs without turning boss DM into an
arbitrary shell.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME / "logs"
OUTBOX = RUNTIME / "cmd" / "outbox"

TZ = timezone(timedelta(hours=7))

def _first_existing(*paths: str | None) -> str | None:
    for raw in paths:
        if raw and Path(raw).exists():
            return raw
    return None


PYTHON = (
    _first_existing(
        os.environ.get("BLACKSITE_HOST_PYTHON"),
        "C:/Users/<YOUR_USERNAME>/AppData/Local/Programs/Python/Python313/python.exe",
        "C:/Users/<YOUR_USERNAME>/AppData/Local/Programs/Python/Python312/python.exe",
        "C:/Users/<YOUR_USERNAME>/AppData/Local/Programs/Python/Launcher/py.exe",
    )
    or sys.executable
)


@dataclass
class Step:
    name: str
    rel_path: str
    args: tuple[str, ...] = ()
    timeout_s: int = 600


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def log(msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{now_iso()}] [commander_ops] {msg}"
    if sys.stdout is not None:
        print(line, flush=True)
    with (LOG_DIR / f"commander_ops_{now().strftime('%Y-%m-%d')}.log").open(
        "a", encoding="utf-8"
    ) as f:
        f.write(line + "\n")


def write_outbox(title: str, body: str) -> Path:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in title)[:48]
    path = OUTBOX / f"{now().strftime('%Y-%m-%dT%H-%M-%S')}_commander_ops_{safe}.md"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    log(f"outbox wrote {path.name}")
    return path


def hist(kind: str, title: str, body: str = "") -> None:
    try:
        sys.path.insert(0, str(ROOT))
        from processors.history_log import log_event

        log_event(
            actor="commander_ops",
            kind=kind,
            scope="commander_ops",
            title=title[:118],
            body=body,
        )
    except Exception as e:
        log(f"history_log fail: {type(e).__name__}: {e}")


def run_bat(rel_path: str, timeout_s: int = 90) -> tuple[int, str]:
    cmd = ["cmd.exe", "/c", str(ROOT / rel_path)]
    log(f"run_bat {' '.join(cmd)} timeout={timeout_s}s")
    try:
        r = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout_s}s"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def run_step(step: Step) -> tuple[int, str]:
    script = ROOT / step.rel_path
    if not script.exists():
        return 2, f"missing script: {script}"
    cmd = [PYTHON, str(script), *step.args]
    log(f"run_step {step.name}: {' '.join(cmd)} timeout={step.timeout_s}s")
    try:
        r = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=step.timeout_s,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
        return r.returncode, out
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {step.timeout_s}s"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def run_daily_brief_for_date(date_str: str) -> tuple[int, str]:
    log(f"run_daily_brief_for_date {date_str}")
    try:
        sys.path.insert(0, str(ROOT))
        import processors.daily_brief as brief

        target = datetime.fromisoformat(date_str).replace(
            hour=19, minute=0, second=0, microsecond=0, tzinfo=TZ
        )
        brief.now_bkk = lambda: target  # type: ignore[assignment]
        brief.prepare(24)
        json_path = brief.QUEUE_DIR / f"pending_{date_str}.json"
        stats = json.loads(json_path.read_text(encoding="utf-8"))
        body = brief._render_brief(stats, date_str)
        md_path = brief.QUEUE_DIR / f"pending_{date_str}.md"
        md_path.write_text(body, encoding="utf-8")
        try:
            json_path.unlink()
        except Exception:
            pass
        return 0, f"daily brief queued: {md_path.relative_to(ROOT).as_posix()} ({len(body)} chars)"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


JOBS: dict[str, list[Step]] = {
    "status": [Step("session_status", "scripts/session_status.py", timeout_s=60)],
    "index": [Step("index_jsonl", "scripts/index_jsonl.py", timeout_s=600)],
    "rules": [Step("run_rules", "scripts/run_rules.py", timeout_s=900)],
    "funnel": [Step("run_funnel_edges", "scripts/run_funnel_edges.py", timeout_s=300)],
    "lead": [
        Step("lead_triage", "processors/lead_triage.py", timeout_s=300),
        Step("lead_executor", "processors/lead_executor.py", timeout_s=300),
        Step("lead_lifecycle", "processors/lead_lifecycle.py", timeout_s=300),
    ],
    "section": [Step("section_chief_eval", "processors/section_chief_eval.py", timeout_s=600)],
    "field": [Step("field_agent_self_eval", "processors/field_agent_self_eval.py", timeout_s=600)],
    "eval": [
        Step("section_chief_eval", "processors/section_chief_eval.py", timeout_s=600),
        Step("field_agent_self_eval", "processors/field_agent_self_eval.py", timeout_s=600),
    ],
    "daily": [Step("daily_brief_today", "processors/daily_brief.py", ("daily",), timeout_s=900)],
    "pulse": [
        Step("strategist_daily_pulse", "processors/strategist_daily_pulse.py", ("--force",), timeout_s=420)
    ],
    "library": [Step("library_ingest", "processors/library_ingest.py", timeout_s=300)],
    "tiktok": [Step("tiktok_listen", "agents/tiktok/tiktok_listen.py", timeout_s=1800)],
    "tg-search": [Step("tg_search", "agents/telegram/tg_search.py", ("P01",), timeout_s=1800)],
    "asr": [Step("asr_whisper", "scripts/run_asr.py", ("--limit", "30"), timeout_s=2700)],
    "asr-audit": [
        Step("asr_quality_audit", "processors/asr_quality_audit.py",
             ("--kind", "daily", "--sample-n", "4"), timeout_s=3600)
    ],
    "retention": [
        Step("retention_sweep_dry_run", "scripts/retention_sweep.py",
             ("--retain-days", "7"), timeout_s=1200)
    ],
    "cards": [Step("compose_cards_loop", "scripts/compose_cards_loop.py", timeout_s=1800)],
}

ALIASES = {
    "state": "status",
    "health": "status",
    "\u72c0\u614b": "status",
    "\u5065\u5eb7": "status",
    "\u7d22\u5f15": "index",
    "\u5165\u5eab": "library",
    "\u66f8\u5eab": "library",
    "\u65e5\u5831": "daily",
    "daily-brief": "daily",
    "\u7b56\u7565": "pulse",
    "\u7b56\u7565\u65e5\u5831": "pulse",
    "pulse": "pulse",
    "\u5c0f\u4e3b\u7ba1": "section",
    "\u60c5\u5831\u54e1": "field",
    "\u8a55\u4f30": "eval",
    "lead": "lead",
    "leads": "lead",
    "funnel": "funnel",
    "rules": "rules",
    "rule": "rules",
    "tiktok": "tiktok",
    "tg": "tg-search",
    "telegram": "tg-search",
    "voice": "asr",
    "\u6574\u7406": "retention",
    "\u6e05\u7406": "retention",
    "retention": "retention",
    "audio": "asr",
    "whisper": "asr",
    "\u8a9e\u97f3": "asr",
    "\u8f49\u9304": "asr",
    "asr audit": "asr-audit",
    "asr-audit": "asr-audit",
    "\u8a9e\u97f3\u62bd\u6e2c": "asr-audit",
    "\u8f49\u9304\u62bd\u6e2c": "asr-audit",
    "cards": "cards",
}


def normalize_job(raw: str) -> str | None:
    key = raw.strip().lower()
    return key if key in JOBS else ALIASES.get(key)


def run_job(job: str) -> int:
    norm = normalize_job(job)
    if not norm or norm not in JOBS:
        allowed = ", ".join(sorted(set(JOBS) | set(ALIASES)))
        write_outbox(
            "rerun_unknown",
            f"[COMMANDER_OP] Unknown job `{job}`\n\nAllowed: {allowed}",
        )
        return 2

    hist("milestone", f"Commander rerun started: {norm}")
    lines = [f"[COMMANDER_OP] rerun `{norm}`", f"start: `{now_iso()}`", ""]
    ok_all = True

    if norm == "daily":
        rc, out = run_daily_brief_for_date(now().strftime("%Y-%m-%d"))
        ok_all = rc == 0
        tail = out[-1200:] if out else "(no output)"
        lines.append("## daily_brief_today")
        lines.append(f"rc={rc}")
        lines.append("```")
        lines.append(tail)
        lines.append("```")
        lines.append("")
    else:
        for step in JOBS[norm]:
            rc, out = run_step(step)
            ok_all = ok_all and rc == 0
            tail = out[-1200:] if out else "(no output)"
            lines.append(f"## {step.name}")
            lines.append(f"rc={rc}")
            lines.append("```")
            lines.append(tail)
            lines.append("```")
            lines.append("")

    status = "OK" if ok_all else "FAILED"
    lines.insert(1, f"status: **{status}**")
    lines.append(f"end: `{now_iso()}`")
    write_outbox(f"rerun_{norm}", "\n".join(lines))
    hist("milestone" if ok_all else "warning", f"Commander rerun {status}: {norm}")
    return 0 if ok_all else 1


def backfill(date_str: str | None) -> int:
    if not date_str:
        date_str = (now() - timedelta(days=1)).strftime("%Y-%m-%d")
    hist("milestone", f"Commander backfill started: {date_str}")
    lines = [f"[COMMANDER_OP] backfill `{date_str}` pending/day-end chain", f"start: `{now_iso()}`", ""]
    ok_all = True

    sequence: list[Step | Callable[[], tuple[int, str]]] = [
        Step("index_jsonl", "scripts/index_jsonl.py", timeout_s=600),
        Step("asr_whisper", "scripts/run_asr.py", ("--limit", "30"), timeout_s=2700),
        Step("run_rules", "scripts/run_rules.py", timeout_s=900),
        Step("run_funnel_edges", "scripts/run_funnel_edges.py", timeout_s=300),
        Step("lead_triage", "processors/lead_triage.py", timeout_s=300),
        Step("lead_executor", "processors/lead_executor.py", timeout_s=300),
        Step("lead_lifecycle", "processors/lead_lifecycle.py", timeout_s=300),
        Step("section_chief_eval", "processors/section_chief_eval.py", timeout_s=600),
        Step("field_agent_self_eval", "processors/field_agent_self_eval.py", timeout_s=600),
        Step(
            "stage3_sonnet_strategic",
            "processors/pipeline/stage3_sonnet_strategic.py",
            ("--limit", "20"),
            timeout_s=1800,
        ),
        lambda: run_daily_brief_for_date(date_str or now().strftime("%Y-%m-%d")),
        Step("strategist_daily_pulse", "processors/strategist_daily_pulse.py", ("--date", date_str, "--force"), timeout_s=420),
        Step("library_ingest", "processors/library_ingest.py", timeout_s=300),
    ]

    for item in sequence:
        if isinstance(item, Step):
            name = item.name
            rc, out = run_step(item)
        else:
            name = "daily_brief_backfill"
            rc, out = item()
        ok_all = ok_all and rc == 0
        tail = out[-1200:] if out else "(no output)"
        lines.append(f"## {name}")
        lines.append(f"rc={rc}")
        lines.append("```")
        lines.append(tail)
        lines.append("```")
        lines.append("")

    rc2, status_out = run_step(Step("session_status", "scripts/session_status.py", timeout_s=60))
    lines.append("## session_status")
    lines.append(f"rc={rc2}")
    lines.append("```")
    lines.append((status_out or "")[-1200:])
    lines.append("```")
    lines.append(f"end: `{now_iso()}`")

    status = "OK" if ok_all else "FAILED"
    lines.insert(1, f"status: **{status}**")
    write_outbox(f"backfill_{date_str}", "\n".join(lines))
    hist("milestone" if ok_all else "warning", f"Commander backfill {status}: {date_str}")
    return 0 if ok_all else 1


def restart_daemon(delay_s: int) -> int:
    hist("milestone", f"Commander daemon restart scheduled delay={delay_s}s")
    log(f"restart_daemon sleeping {delay_s}s")
    time.sleep(max(0, delay_s))

    lines = ["[COMMANDER_OP] Blacksite daemon restart", f"start: `{now_iso()}`", ""]
    rc_stop, out_stop = run_bat("scripts/stop_daemon.bat", timeout_s=90)
    lines.extend(["## stop_daemon", f"rc={rc_stop}", "```", out_stop[-1000:] if out_stop else "(no output)", "```", ""])
    time.sleep(2)
    rc_run, out_run = run_bat("scripts/run_daemon.bat", timeout_s=90)
    lines.extend(["## run_daemon", f"rc={rc_run}", "```", out_run[-1000:] if out_run else "(no output)", "```", ""])
    time.sleep(10)
    rc_status, out_status = run_step(Step("session_status", "scripts/session_status.py", timeout_s=60))
    lines.extend(["## session_status", f"rc={rc_status}", "```", out_status[-1600:] if out_status else "(no output)", "```", ""])
    ok = rc_stop == 0 and rc_run == 0 and rc_status == 0
    lines.insert(1, f"status: **{'OK' if ok else 'FAILED'}**")
    lines.append(f"end: `{now_iso()}`")
    write_outbox("restart_daemon", "\n".join(lines))
    hist("milestone" if ok else "warning", f"Commander daemon restart {'completed' if ok else 'failed'}")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_restart = sub.add_parser("restart-daemon")
    p_restart.add_argument("--delay", type=int, default=75)

    p_rerun = sub.add_parser("rerun")
    p_rerun.add_argument("job")

    p_backfill = sub.add_parser("backfill")
    p_backfill.add_argument("--date", default=None)

    args = p.parse_args()
    if args.cmd == "restart-daemon":
        return restart_daemon(args.delay)
    if args.cmd == "rerun":
        return run_job(args.job)
    if args.cmd == "backfill":
        return backfill(args.date)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
