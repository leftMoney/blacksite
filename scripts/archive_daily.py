"""
Blacksite — daily archiver. Compresses raw jsonl + log files older than N days
into runtime/archive/<YYYY-MM>/.

Default: keep 30 days uncompressed; compress + move older to archive.
"""

from __future__ import annotations

import gzip
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
RAW_DIR = RUNTIME_DIR / "raw"
LOG_DIR = RUNTIME_DIR / "logs"
ARCHIVE_DIR = RUNTIME_DIR / "archive"

TZ = timezone(timedelta(hours=7))
RAW_RETAIN_DAYS = 30
LOG_RETAIN_DAYS = 14


def now() -> datetime:
    return datetime.now(TZ)


def archive_file(src: Path, ym_label: str) -> None:
    dest_dir = ARCHIVE_DIR / ym_label
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (src.name + ".gz")
    with src.open("rb") as f_in, gzip.open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    src.unlink()
    print(f"  archived {src.relative_to(ROOT)} -> {dest.relative_to(ROOT)}", flush=True)


def parse_iso_date_from_name(p: Path) -> datetime | None:
    # raw/<persona>/YYYY-MM-DD.jsonl ; logs/<agent>_YYYY-MM-DD.log
    name = p.stem
    for token in name.split("_"):
        try:
            return datetime.strptime(token, "%Y-%m-%d").replace(tzinfo=TZ)
        except ValueError:
            continue
    try:
        return datetime.strptime(name, "%Y-%m-%d").replace(tzinfo=TZ)
    except ValueError:
        return None


def sweep(root: Path, retain_days: int) -> int:
    if not root.exists():
        return 0
    cutoff = now() - timedelta(days=retain_days)
    count = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix == ".gz":
            continue
        d = parse_iso_date_from_name(p)
        if d is None or d >= cutoff:
            continue
        archive_file(p, d.strftime("%Y-%m"))
        count += 1
    return count


def main() -> None:
    print(f"[{now().isoformat(timespec='seconds')}] archive_daily start", flush=True)
    raw_archived = sweep(RAW_DIR, RAW_RETAIN_DAYS)
    log_archived = sweep(LOG_DIR, LOG_RETAIN_DAYS)
    print(
        f"[{now().isoformat(timespec='seconds')}] archive_daily done: "
        f"raw={raw_archived} log={log_archived}",
        flush=True,
    )


if __name__ == "__main__":
    main()
