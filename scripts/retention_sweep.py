"""Blacksite runtime artifact retention and scratch organizer.

Default is dry-run. Use --commit to move/delete.

Retention contract:
  - runtime/media: keep 7 days for OCR/ASR audit, then delete files
  - runtime/raw: keep 7 days for sampling/audit, then delete files
  - runtime/screenshots: keep 7 days for login/debug evidence, then delete files
  - runtime/artifacts: keep 7 days for organized scratch artifacts, then delete files
  - repo-root scratch media/tmp dirs: move into runtime/artifacts/root_scratch/

The script never touches DB files, persona storage_state, code, .env, or System docs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
REPORT_DIR = RUNTIME / "reports" / "retention"
ARTIFACTS_DIR = RUNTIME / "artifacts"
TZ = timezone(timedelta(hours=7))

MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    ".mp4", ".mov", ".webm", ".m4a", ".mp3", ".wav", ".ogg",
}
RAW_EXTS = {".jsonl", ".json", ".ndjson"}
DATE_RE = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")
DATE_DIR_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")

ROOT_SCRATCH_DIR_PATTERNS = (
    ".codex_tmp", ".codex_temp", ".tmp", ".imgcrops",
    "_tmp", "_ocr", "tmp", "tmpocr", "tmp_ocr", "tmp_crops",
)
ROOT_SCRATCH_FILE_PREFIXES = (
    "crop", "_crop", "tmp", "_tmp", "ocr", "_ocr", "seg",
    "tl_", "tr_", "bl_", "br_", "line", "watermark", "sticker",
    "blacksite_", "bag_", "bottom_", "top_", "left_", "right_",
    "center_", "foreground_", "grid_", "label_", "ribbon_",
)


@dataclass
class Action:
    action: str
    path: str
    dest: str | None
    bytes: int
    age_days: float | None
    reason: str


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def rel(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def ensure_inside_root(path: Path) -> Path:
    resolved = path.resolve()
    root = ROOT.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"refusing outside-root path: {path}")
    return resolved


def parse_date(path: Path) -> datetime | None:
    for part in path.parts:
        if DATE_DIR_RE.match(part):
            return datetime.strptime(part, "%Y-%m-%d").replace(tzinfo=TZ)
    m = DATE_RE.search(path.name)
    if m:
        try:
            y, mo, d = m.groups()
            return datetime(int(y), int(mo), int(d), tzinfo=TZ)
        except ValueError:
            return None
    return None


def file_dt(path: Path) -> datetime:
    parsed = parse_date(path)
    if parsed:
        return parsed
    return datetime.fromtimestamp(path.stat().st_mtime, TZ)


def age_days(path: Path) -> float:
    return max(0.0, (now() - file_dt(path)).total_seconds() / 86400)


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += file_size(child)
    return total


def unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.stem
    suffix = dest.suffix
    for i in range(1, 10_000):
        candidate = dest.with_name(f"{stem}__{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate unique destination for {dest}")


def should_move_root_file(path: Path) -> bool:
    if path.parent != ROOT:
        return False
    if path.suffix.lower() not in MEDIA_EXTS:
        return False
    name = path.name.lower()
    return name.startswith(ROOT_SCRATCH_FILE_PREFIXES)


def should_move_root_dir(path: Path, min_age_hours: float) -> bool:
    if path.parent != ROOT or not path.is_dir():
        return False
    name = path.name.lower()
    if not name.startswith(ROOT_SCRATCH_DIR_PATTERNS):
        return False
    age_h = (now() - datetime.fromtimestamp(path.stat().st_mtime, TZ)).total_seconds() / 3600
    return age_h >= min_age_hours


def collect_delete_actions(root: Path, retain_days: int, suffixes: set[str], reason: str) -> list[Action]:
    actions: list[Action] = []
    if not root.exists():
        return actions
    cutoff = now() - timedelta(days=retain_days)
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in suffixes:
            continue
        dt = file_dt(path)
        if dt >= cutoff:
            continue
        actions.append(Action(
            action="delete",
            path=rel(path),
            dest=None,
            bytes=file_size(path),
            age_days=round((now() - dt).total_seconds() / 86400, 2),
            reason=reason,
        ))
    return actions


def collect_root_moves(min_age_hours: float) -> list[Action]:
    actions: list[Action] = []
    for path in ROOT.iterdir():
        if path.is_file() and should_move_root_file(path):
            dt = file_dt(path)
            dest_dir = ARTIFACTS_DIR / "root_scratch" / dt.strftime("%Y-%m-%d")
            dest = unique_dest(dest_dir / path.name)
            actions.append(Action(
                action="move",
                path=rel(path),
                dest=rel(dest),
                bytes=file_size(path),
                age_days=round(age_days(path), 2),
                reason="repo-root scratch media should live under runtime/artifacts/root_scratch/<date>",
            ))
        elif path.is_dir() and should_move_root_dir(path, min_age_hours):
            dt = datetime.fromtimestamp(path.stat().st_mtime, TZ)
            dest_dir = ARTIFACTS_DIR / "root_scratch_dirs" / dt.strftime("%Y-%m-%d")
            dest = unique_dest(dest_dir / path.name)
            actions.append(Action(
                action="move_dir",
                path=rel(path),
                dest=rel(dest),
                bytes=dir_size(path),
                age_days=round((now() - dt).total_seconds() / 86400, 2),
                reason="repo-root scratch directory should live under runtime/artifacts/root_scratch_dirs/<date>",
            ))
    return actions


def execute(actions: list[Action], commit: bool) -> tuple[int, int]:
    moved = 0
    deleted = 0
    if not commit:
        return moved, deleted
    for action in actions:
        src = ensure_inside_root(ROOT / action.path)
        if action.action in {"move", "move_dir"}:
            if not action.dest:
                continue
            dest = ensure_inside_root(ROOT / action.dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved += 1
        elif action.action == "delete":
            if src.exists() and src.is_file():
                src.unlink()
                deleted += 1
    return moved, deleted


def prune_empty_dirs(root: Path, commit: bool) -> int:
    if not commit or not root.exists():
        return 0
    removed = 0
    for directory in sorted([p for p in root.rglob("*") if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def write_report(actions: list[Action], commit: bool, retain_days: int, moved: int, deleted: int, empty_dirs: int) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = now().strftime("%Y-%m-%dT%H-%M-%S")
    summary = {
        "ts": now_iso(),
        "active_instance": ACTIVE_INSTANCE,
        "mode": "commit" if commit else "dry_run",
        "retain_days": retain_days,
        "actions": len(actions),
        "bytes_total": sum(a.bytes for a in actions),
        "moved": moved,
        "deleted": deleted,
        "empty_dirs_removed": empty_dirs,
        "by_action": {},
    }
    for action in actions:
        summary["by_action"].setdefault(action.action, 0)
        summary["by_action"][action.action] += 1
    payload = {"summary": summary, "actions": [asdict(a) for a in actions]}
    path = REPORT_DIR / f"retention_{ts}_{summary['mode']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retain-days", type=int, default=7)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run even when BLACKSITE_RETENTION_COMMIT=1")
    parser.add_argument("--no-organize-root", action="store_true")
    parser.add_argument("--min-root-dir-age-hours", type=float, default=1.0)
    parser.add_argument("--max-print", type=int, default=40)
    args = parser.parse_args()

    actions: list[Action] = []
    actions.extend(collect_delete_actions(RUNTIME / "media", args.retain_days, MEDIA_EXTS, "runtime media retained 7d for OCR/ASR audit"))
    actions.extend(collect_delete_actions(RUNTIME / "raw", args.retain_days, RAW_EXTS, "runtime raw retained 7d for sampling/audit"))
    actions.extend(collect_delete_actions(RUNTIME / "screenshots", args.retain_days, MEDIA_EXTS, "runtime screenshots retained 7d for account/debug audit"))
    actions.extend(collect_delete_actions(ARTIFACTS_DIR, args.retain_days, MEDIA_EXTS | RAW_EXTS, "organized artifacts retained 7d"))
    actions.extend(collect_delete_actions(RUNTIME / "archive", args.retain_days, {".gz"}, "legacy archived raw/log gzip retained 7d"))
    if not args.no_organize_root:
        actions.extend(collect_root_moves(args.min_root_dir_age_hours))

    env_commit = os.environ.get("BLACKSITE_RETENTION_COMMIT", "").strip() == "1"
    commit = (args.commit or env_commit) and not args.dry_run

    moved, deleted = execute(actions, commit)
    empty_dirs = 0
    if commit:
        for root in (RUNTIME / "media", RUNTIME / "raw", RUNTIME / "screenshots", ARTIFACTS_DIR):
            empty_dirs += prune_empty_dirs(root, commit=True)
    report = write_report(actions, commit, args.retain_days, moved, deleted, empty_dirs)

    mode = "COMMIT" if commit else "DRY-RUN"
    total_mb = sum(a.bytes for a in actions) / 1024 / 1024
    print(f"[{now_iso()}] retention_sweep {mode} actions={len(actions)} bytes={total_mb:.1f}MB report={rel(report)}")
    for action in actions[: args.max_print]:
        dest = f" -> {action.dest}" if action.dest else ""
        print(f"  {action.action}: {action.path}{dest} ({action.bytes} bytes, age={action.age_days}d) {action.reason}")
    if len(actions) > args.max_print:
        print(f"  ... {len(actions) - args.max_print} more; see {rel(report)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
