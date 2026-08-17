"""Run a collector under a Field Agent work order and mirror fresh raw rows.

The adapter keeps existing platform collectors reusable: they may keep writing
to their canonical raw directory, while this process copies rows produced in
this run into ``runtime/raw/<agent_id>/`` with work-order metadata attached.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
RAW_DIR = RUNTIME / "raw"
LOG_DIR = RUNTIME / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def raw_path(agent_id: str) -> Path:
    return RAW_DIR / agent_id / f"{now().strftime('%Y-%m-%d')}.jsonl"


def source_dir(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else RAW_DIR / p


def item_ts(item: dict) -> str:
    return str(item.get("ts") or item.get("checked_at") or item.get("created_at") or "")


def mirror_sources(
    *,
    agent_id: str,
    work_order_id: str | None,
    task_focus: str | None,
    raw_sources: list[str],
    since_iso: str,
) -> int:
    copied = 0
    out = raw_path(agent_id)
    for raw_source in raw_sources:
        directory = source_dir(raw_source)
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                ts = item_ts(item)
                if ts and ts < since_iso:
                    continue
                if item.get("agent_id") == agent_id and item.get("work_order_id") == work_order_id:
                    continue
                mirrored = dict(item)
                mirrored["agent_id"] = agent_id
                if work_order_id:
                    mirrored["work_order_id"] = work_order_id
                if task_focus:
                    mirrored["task_focus"] = task_focus
                mirrored["factory_mirrored_from"] = raw_source
                mirrored["factory_mirrored_at"] = now_iso()
                append_jsonl(out, mirrored)
                copied += 1
    return copied


def write_status(
    *,
    agent_id: str,
    work_order_id: str | None,
    task_focus: str | None,
    script: str,
    exit_code: int,
    copied: int,
    started_at: str,
    finished_at: str,
) -> None:
    record = {
        "ts": finished_at,
        "event": "collector_status",
        "kind": "collector_status",
        "platform": "factory",
        "agent_id": agent_id,
        "work_order_id": work_order_id,
        "task_focus": task_focus,
        "script": script,
        "exit_code": exit_code,
        "mirrored_records": copied,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    append_jsonl(raw_path(agent_id), record)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--work-order-id")
    parser.add_argument("--task-focus")
    parser.add_argument("--script", required=True)
    parser.add_argument("--raw-source", action="append", default=[])
    parser.add_argument("script_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    script_path = ROOT / args.script
    started_at = now_iso()
    if not script_path.exists():
        write_status(
            agent_id=args.agent_id,
            work_order_id=args.work_order_id,
            task_focus=args.task_focus,
            script=args.script,
            exit_code=127,
            copied=0,
            started_at=started_at,
            finished_at=now_iso(),
        )
        return 127

    child_args = list(args.script_args)
    if child_args and child_args[0] == "--":
        child_args = child_args[1:]
    cmd = [sys.executable, str(script_path), *child_args]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(cmd, cwd=str(ROOT), creationflags=flags)
    copied = mirror_sources(
        agent_id=args.agent_id,
        work_order_id=args.work_order_id,
        task_focus=args.task_focus,
        raw_sources=args.raw_source,
        since_iso=started_at,
    )
    finished_at = now_iso()
    write_status(
        agent_id=args.agent_id,
        work_order_id=args.work_order_id,
        task_focus=args.task_focus,
        script=args.script,
        exit_code=proc.returncode,
        copied=copied,
        started_at=started_at,
        finished_at=finished_at,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
