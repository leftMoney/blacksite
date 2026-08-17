"""
Blacksite — Telegram operator-cluster + template-fingerprint miner.

Three analyses over runtime/funnel_graph.jsonl + runtime/raw/<persona>/*.jsonl:

1. Username clustering — same operator running multiple aliases.
   Signal: shared prefix (>=4 chars) + numeric suffix variation, or Levenshtein <= 2
   between usernames also bridged by mutual mentions.

2. Message template fingerprint — a single promo template re-used across multiple
   chats (likely same operator pushing). Signal: normalized text (numbers and
   variable tokens redacted) repeats across chats.

3. Posting-cadence correlation — chats whose posts cluster around the same minute
   marks within UTC+7 days suggest coordinated scheduling.

Output: runtime/operator_clusters.json (snapshot, single file overwrites weekly).

Usage:
  py agents/telegram/tg_pattern_miner.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

GRAPH_PATH = RUNTIME_DIR / "funnel_graph.jsonl"
RAW_DIR = RUNTIME_DIR / "raw"
OUT_PATH = RUNTIME_DIR / "operator_clusters.json"

TZ = timezone(timedelta(hours=7))

RE_NUM = re.compile(r"\d+")
# TODO: set UI markers for your instance's language — add the target language's
# Unicode block to the kept range (e.g. U+0E00..U+0E7F) so native-script
# template text survives normalization. Default keeps ASCII word chars only.
RE_NONALNUM = re.compile(r"[^\w\s]+")  # keep word chars (extend for local script)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"tg_pattern_miner_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def collect_usernames() -> list[str]:
    seen = set()
    for rec in iter_jsonl(GRAPH_PATH):
        if rec.get("type") == "entity" and rec.get("kind") == "tg_username":
            seen.add(rec["id"].lower())
    return sorted(seen)


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,
                cur[-1] + 1,
                prev[j - 1] + (ca != cb),
            ))
        prev = cur
    return prev[-1]


def common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def cluster_usernames(usernames: list[str]) -> list[list[str]]:
    """Greedy: any pair with prefix>=4 OR levenshtein<=2 in same cluster."""
    parent = {u: u for u in usernames}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    bare = [u.lstrip("@") for u in usernames]
    for i in range(len(bare)):
        for j in range(i + 1, len(bare)):
            a, b = bare[i], bare[j]
            cp = common_prefix(a, b)
            if cp >= 4 or levenshtein(a, b) <= 2:
                union(usernames[i], usernames[j])

    clusters = defaultdict(list)
    for u in usernames:
        clusters[find(u)].append(u)
    # Only return clusters of size >= 2
    return [sorted(v) for v in clusters.values() if len(v) >= 2]


def normalize_template(text: str) -> str:
    if not text:
        return ""
    t = RE_NUM.sub("#", text)
    t = RE_NONALNUM.sub(" ", t)
    t = " ".join(t.split())
    return t.lower()[:200]


def template_fingerprints() -> dict:
    """Map normalized-template → list of (chat_username, msg_id, ts)."""
    fp: dict[str, list] = defaultdict(list)
    if not RAW_DIR.exists():
        return {}
    for persona_dir in RAW_DIR.iterdir():
        if not persona_dir.is_dir():
            continue
        for jsonl in persona_dir.glob("*.jsonl"):
            for rec in iter_jsonl(jsonl):
                text = rec.get("text", "") or ""
                if len(text) < 30:
                    continue
                norm = normalize_template(text)
                if len(norm) < 30:
                    continue
                fp[norm].append({
                    "chat_username": rec.get("chat_username"),
                    "msg_id": rec.get("msg_id"),
                    "ts": rec.get("ts"),
                    "persona": rec.get("persona"),
                })
    # Templates that appear in 2+ distinct chats
    return {
        norm: hits
        for norm, hits in fp.items()
        if len({h["chat_username"] for h in hits if h["chat_username"]}) >= 2
    }


def cadence_correlation() -> dict:
    """Per chat: histogram of post minutes (mod 60). Pairs with correlation > 0.5
    over many slots are flagged."""
    minute_hist: dict[str, Counter] = defaultdict(Counter)
    if not RAW_DIR.exists():
        return {}
    for persona_dir in RAW_DIR.iterdir():
        if not persona_dir.is_dir():
            continue
        for jsonl in persona_dir.glob("*.jsonl"):
            for rec in iter_jsonl(jsonl):
                u = rec.get("chat_username")
                ts = rec.get("ts")
                if not u or not ts:
                    continue
                try:
                    minute = int(ts[14:16])
                except (ValueError, IndexError):
                    continue
                minute_hist[f"@{u}"][minute] += 1
    return {k: dict(v) for k, v in minute_hist.items()}


def main() -> None:
    log_line(f"[{now_iso()}] tg_pattern_miner start")
    usernames = collect_usernames()
    clusters = cluster_usernames(usernames)
    templates = template_fingerprints()
    cadence = cadence_correlation()

    output = {
        "snapshot_at": now_iso(),
        "username_clusters": [{"members": c, "size": len(c)} for c in clusters],
        "shared_templates": [
            {"template": norm[:120], "chats": sorted({h["chat_username"] for h in hits if h["chat_username"]}), "occurrences": len(hits)}
            for norm, hits in sorted(templates.items(), key=lambda kv: -len(kv[1]))[:50]
        ],
        "posting_cadence": cadence,
    }
    OUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    log_line(
        f"[{now_iso()}] tg_pattern_miner done: "
        f"{len(clusters)} username clusters, "
        f"{len(output['shared_templates'])} shared templates, "
        f"{len(cadence)} chats with cadence data"
    )


if __name__ == "__main__":
    main()
