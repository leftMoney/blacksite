"""
Blacksite — YouTube keyword search via yt-dlp (no auth required).

Searches YouTube for the client brand-relevant local keywords, captures channel + recent video
metadata for each hit. Output: runtime/raw/youtube/searches/<YYYY-MM-DD>.jsonl

Usage:
  py agents/youtube/yt_search.py
  py agents/youtube/yt_search.py "example_keyword_1" "example_keyword_2"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    from yt_dlp import YoutubeDL
except ImportError:
    sys.exit("yt-dlp not installed. py -m pip install yt-dlp")

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RAW_OUT = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "raw" / "youtube" / "searches"
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
RAW_OUT.mkdir(parents=True, exist_ok=True)
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

# TODO: set search seeds for your instance (see instances/_TEMPLATE/policy/*.yaml)
DEFAULT_QUERIES = [
    "example_keyword_1",
    "example_keyword_2",
    "example_keyword_3",
    "ExampleGovWallet example_keyword_4",
]

PER_QUERY_LIMIT = 20


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"yt_search_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def agent_raw_path(agent_id: str | None) -> Path | None:
    if not agent_id:
        return None
    d = RUNTIME / "raw" / agent_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"


def search(query: str, limit: int) -> list[dict]:
    opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "default_search": f"ytsearch{limit}",
    }
    with YoutubeDL(opts) as y:
        res = y.extract_info(f"ytsearch{limit}:{query}", download=False)
    out = []
    for entry in res.get("entries", []) or []:
        out.append({
            "ts": now_iso(),
            "event": "youtube_search_result",
            "kind": "youtube_search_result",
            "platform": "youtube",
            "query": query,
            "id": entry.get("id"),
            "title": entry.get("title"),
            "channel": entry.get("channel"),
            "channel_id": entry.get("channel_id"),
            "url": entry.get("url") or entry.get("webpage_url"),
            "duration": entry.get("duration"),
            "view_count": entry.get("view_count"),
            "uploader": entry.get("uploader"),
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("queries", nargs="*", default=None)
    parser.add_argument("--limit", type=int, default=PER_QUERY_LIMIT)
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--work-order-id", default=None)
    parser.add_argument("--task-focus", default=None)
    args = parser.parse_args()
    queries = args.queries or DEFAULT_QUERIES
    out_path = RAW_OUT / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"
    agent_out = agent_raw_path(args.agent_id)
    log_line(f"[{now_iso()}] yt_search start: {len(queries)} queries")
    total = 0
    with out_path.open("a", encoding="utf-8") as f:
        agent_f = agent_out.open("a", encoding="utf-8") if agent_out else None
        try:
            if agent_f:
                agent_f.__enter__()
            for q in queries:
                try:
                    results = search(q, args.limit)
                except Exception as e:
                    log_line(f"  FAIL '{q}': {type(e).__name__}: {e}")
                    continue
                for r in results:
                    if args.agent_id:
                        r["agent_id"] = args.agent_id
                    if args.work_order_id:
                        r["work_order_id"] = args.work_order_id
                    if args.task_focus:
                        r["task_focus"] = args.task_focus
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    if agent_f:
                        agent_f.write(json.dumps(r, ensure_ascii=False) + "\n")
                total += len(results)
                log_line(f"  '{q}' -> {len(results)} videos")
        finally:
            if agent_f:
                agent_f.close()
    log_line(f"[{now_iso()}] yt_search done: {total} videos written -> {out_path}")


if __name__ == "__main__":
    main()
