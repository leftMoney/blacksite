"""
Blacksite — YouTube channel monitor.

For a list of channel handles in policy/youtube_channels.yaml, polls each
for new uploads since last cursor. Output: runtime/raw/youtube/channels/<handle>/<date>.jsonl

Usage:
  py agents/youtube/yt_channel_monitor.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from yt_dlp import YoutubeDL
except ImportError:
    sys.exit("yt-dlp not installed. py -m pip install yt-dlp")

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
RAW_OUT = RUNTIME / "raw" / "youtube" / "channels"
RAW_OUT.mkdir(parents=True, exist_ok=True)
STATE_PATH = RUNTIME / "yt_channel_state.json"
POLICY_PATH = ROOT / "instances" / ACTIVE_INSTANCE / "policy" / "youtube_channels.yaml"
LOG_DIR = RUNTIME / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"yt_channel_monitor_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_policy() -> list[str]:
    if not POLICY_PATH.exists():
        return []
    data = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8")) or {}
    return data.get("channels", [])


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(s: dict) -> None:
    STATE_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")


def fetch_recent(channel_url: str, limit: int = 30) -> list[dict]:
    opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
    }
    with YoutubeDL(opts) as y:
        info = y.extract_info(channel_url, download=False)
    return info.get("entries", []) or []


def handle_to_dir(handle: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in handle)
    return RAW_OUT / safe


def main() -> None:
    channels = load_policy()
    if not channels:
        log_line(f"[{now_iso()}] no channels in {POLICY_PATH}; skip")
        return
    state = load_state()
    log_line(f"[{now_iso()}] yt_channel_monitor start: {len(channels)} channels")
    new_total = 0
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    for ch in channels:
        try:
            entries = fetch_recent(ch)
        except Exception as e:
            log_line(f"  FAIL {ch}: {type(e).__name__}: {e}")
            continue
        last_id = state.get(ch)
        new_for_channel = []
        for e in entries:
            vid = e.get("id")
            if vid is None:
                continue
            if vid == last_id:
                break
            new_for_channel.append(e)
        if entries:
            state[ch] = entries[0].get("id")
        out_dir = handle_to_dir(ch)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{today}.jsonl"
        with out_path.open("a", encoding="utf-8") as f:
            for e in new_for_channel:
                rec = {
                    "ts": now_iso(),
                    "channel": ch,
                    "id": e.get("id"),
                    "title": e.get("title"),
                    "url": e.get("url") or e.get("webpage_url"),
                    "duration": e.get("duration"),
                    "view_count": e.get("view_count"),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        log_line(f"  {ch}: +{len(new_for_channel)} new")
        new_total += len(new_for_channel)
    save_state(state)
    log_line(f"[{now_iso()}] yt_channel_monitor done: +{new_total} new videos")


if __name__ == "__main__":
    main()
