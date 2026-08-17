"""
Blacksite — Telegram entity classifier.

Reads runtime/funnel_graph.jsonl + runtime/raw/<persona>/*.jsonl, computes
signatures per entity, applies role + risk tagging from policy/funnel_scan.yaml,
outputs runtime/entities_classified.jsonl (one line per entity, latest wins).

Roles: bait | funnel | brand_public | operator_private | victim_pool | unknown
Risk:  low | medium | high | extreme_skip

Usage:
  py agents/telegram/tg_classifier.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
POLICY_PATH = ROOT / "instances" / ACTIVE_INSTANCE / "policy" / "funnel_scan.yaml"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

GRAPH_PATH = RUNTIME_DIR / "funnel_graph.jsonl"
RAW_DIR = RUNTIME_DIR / "raw"
OUT_PATH = RUNTIME_DIR / "entities_classified.jsonl"

TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"tg_classifier_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_policy() -> dict:
    if not POLICY_PATH.exists():
        return {}
    return yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8")) or {}


def iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if line_no <= 3:
                    log_line(f"[{now_iso()}] tg_classifier skip bad jsonl line {path.name}:{line_no}")
                continue


def collect_entity_evidence() -> dict:
    """Aggregate edge counts per entity from funnel_graph."""
    seen_at: dict[str, str] = {}
    edge_counts: dict[str, Counter] = defaultdict(Counter)
    out_edges: dict[str, Counter] = defaultdict(Counter)  # this entity → others
    in_edges: dict[str, Counter] = defaultdict(Counter)   # others → this entity
    kinds: dict[str, str] = {}
    for rec in iter_jsonl(GRAPH_PATH):
        if rec.get("type") == "entity":
            eid = rec["id"]
            kinds[eid] = rec.get("kind", "unknown")
            seen_at.setdefault(eid, rec.get("discovered_at", ""))
        elif rec.get("type") == "edge":
            etype = rec.get("edge", "unknown")
            f, t = rec.get("from"), rec.get("to")
            if f:
                out_edges[f][etype] += 1
            if t:
                in_edges[t][etype] += 1
                edge_counts[t][etype] += 1
    return {
        "kinds": kinds,
        "seen_at": seen_at,
        "in_edges": in_edges,
        "out_edges": out_edges,
    }


def collect_chat_stats() -> dict:
    """For chats we've actually joined and listened to: sender diversity + link density."""
    stats: dict[str, dict] = defaultdict(lambda: {
        "msg_count": 0,
        "senders": set(),
        "promo_link_msgs": 0,
        "msg_text_concat": [],
    })
    if not RAW_DIR.exists():
        return stats
    for persona_dir in RAW_DIR.iterdir():
        if not persona_dir.is_dir():
            continue
        for jsonl in persona_dir.glob("*.jsonl"):
            for rec in iter_jsonl(jsonl):
                username = rec.get("chat_username")
                if not username:
                    continue
                key = f"@{username}"
                s = stats[key]
                s["msg_count"] += 1
                if rec.get("sender_id"):
                    s["senders"].add(rec["sender_id"])
                text = rec.get("text", "") or ""
                if "t.me/" in text or "http" in text:
                    s["promo_link_msgs"] += 1
                if len(s["msg_text_concat"]) < 200:
                    s["msg_text_concat"].append(text[:200])
    return stats


def score_entity(eid: str, kind: str, in_edges, out_edges, chat_stats: dict, policy: dict) -> dict:
    """Return {role, risk, signatures, confidence}."""
    sigs = {}
    role = "unknown"
    risk = "low"

    cfg = policy.get("classify", {})
    risk_cfg = policy.get("risk", {})
    bait_cfg = cfg.get("bait_signatures", {})
    funnel_cfg = cfg.get("funnel_signatures", {})
    brand_cfg = cfg.get("brand_public_signatures", {})

    s = chat_stats.get(eid)
    if s and s["msg_count"] > 0:
        sigs["msg_count"] = s["msg_count"]
        sigs["sender_diversity"] = (
            len(s["senders"]) / s["msg_count"] if s["msg_count"] else 0
        )
        sigs["promo_link_density"] = s["promo_link_msgs"] / s["msg_count"]

        text_blob = " ".join(s["msg_text_concat"]).lower()
        # Risk signals
        for kw in risk_cfg.get("high_risk_immediate_skip", []):
            if kw.lower() in text_blob:
                risk = "extreme_skip"
                sigs["high_risk_keyword"] = kw
                break
        if risk != "extreme_skip":
            for kw in risk_cfg.get("police_adjacent_keywords", []):
                if kw.lower() in text_blob:
                    risk = "high"
                    sigs["police_adjacent"] = kw
                    break
        if risk == "low":
            for kw in risk_cfg.get("scam_certain_keywords", []):
                if kw.lower() in text_blob:
                    risk = "medium"
                    sigs["scam_signal"] = kw
                    break

    # Role inference (rule-based v0)
    invite_link_outs = out_edges.get(eid, Counter()).get("invite_link", 0)
    mention_outs = out_edges.get(eid, Counter()).get("mention", 0)
    in_count = sum(in_edges.get(eid, Counter()).values())

    sigs["invite_links_dropped"] = invite_link_outs
    sigs["mentions_made"] = mention_outs
    sigs["in_references"] = in_count

    # Heuristic ladder
    if invite_link_outs >= funnel_cfg.get("invite_link_extract_count_min", 3):
        role = "funnel"
    elif (
        sigs.get("sender_diversity", 1.0) <= bait_cfg.get("sender_diversity_max", 0.05)
        and sigs.get("promo_link_density", 0) >= bait_cfg.get("promo_link_density_min", 0.5)
        and sigs.get("msg_count", 0) >= 20
    ):
        role = "bait"
    elif kind == "tg_invite":
        role = "operator_private"   # private channels reachable only via invite
    elif in_count >= 2 and kind == "tg_username":
        role = "brand_public"       # repeatedly mentioned across multiple sources

    return {
        "id": eid,
        "kind": kind,
        "role": role,
        "risk": risk,
        "signatures": sigs,
        "classified_at": now_iso(),
    }


def main() -> None:
    log_line(f"[{now_iso()}] tg_classifier start")
    policy = load_policy()
    evidence = collect_entity_evidence()
    chat_stats = collect_chat_stats()

    out_lines = []
    for eid, kind in evidence["kinds"].items():
        rec = score_entity(eid, kind, evidence["in_edges"], evidence["out_edges"], chat_stats, policy)
        out_lines.append(json.dumps(rec, ensure_ascii=False))

    OUT_PATH.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
    role_counts = Counter(json.loads(l)["role"] for l in out_lines) if out_lines else Counter()
    risk_counts = Counter(json.loads(l)["risk"] for l in out_lines) if out_lines else Counter()
    log_line(
        f"[{now_iso()}] tg_classifier done: {len(out_lines)} entities, "
        f"roles={dict(role_counts)} risks={dict(risk_counts)}"
    )


if __name__ == "__main__":
    main()
