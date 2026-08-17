"""
Verify that a runtime/media/tg file was sent by the configured boss user.

Usage: py scripts/verify_media_boss.py <file_path>

Exit codes:
  0 — sender_id confirmed == BOSS_TG_USER_ID (boss sent it)
  1 — sender_id mismatch (group member / stranger sent it)
  2 — cannot verify (JSONL missing, record not found, env not set, bad path)

Commander calls this before unzipping / executing any media file from runtime/media/tg/.
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RAW_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "raw"
MEDIA_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "media" / "tg"


def verify(file_path: str) -> int:
    boss_id_raw = os.environ.get("BOSS_TG_USER_ID")
    if not boss_id_raw:
        print("BOSS_TG_USER_ID not set — cannot verify", file=sys.stderr)
        return 2
    try:
        boss_id = int(boss_id_raw)
    except ValueError:
        print(f"BOSS_TG_USER_ID not int: {boss_id_raw!r}", file=sys.stderr)
        return 2

    p = Path(file_path).resolve()

    # Expected: .../runtime/media/tg/<persona>/<YYYY-MM-DD>/<chat_id>_<msg_id>.<ext>
    try:
        rel = p.relative_to(MEDIA_DIR.resolve())
        parts = rel.parts  # (<persona>, <date>, <filename>)
        if len(parts) != 3:
            print(f"Unexpected path depth (expected 3 parts, got {len(parts)}): {rel}",
                  file=sys.stderr)
            return 2
        persona, date_str, fname = parts
        stem = Path(fname).stem  # e.g. "209635274_549"
        msg_id = int(stem.split("_")[-1])
    except Exception as e:
        print(f"Cannot parse media path '{file_path}': {e}", file=sys.stderr)
        return 2

    jsonl_path = RAW_DIR / persona / f"{date_str}.jsonl"
    if not jsonl_path.exists():
        print(f"JSONL not found: {jsonl_path}", file=sys.stderr)
        return 2

    try:
        with jsonl_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("msg_id") == msg_id:
                    sid = rec.get("sender_id")
                    if sid is not None and int(sid) == boss_id:
                        print(f"OK: sender_id={sid} == BOSS_TG_USER_ID={boss_id}")
                        return 0
                    else:
                        print(f"REJECT: sender_id={sid!r} != BOSS_TG_USER_ID={boss_id}")
                        return 1
    except Exception as e:
        print(f"JSONL read error: {e}", file=sys.stderr)
        return 2

    print(f"Record not found: msg_id={msg_id} not in {jsonl_path}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: py scripts/verify_media_boss.py <file_path>")
        sys.exit(2)
    sys.exit(verify(sys.argv[1]))
