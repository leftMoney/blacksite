"""Seed Intelligence Pipeline.

Turns algorithm/social raw output into Seed Candidates, verifies them with
lightweight evidence rules, and maintains a watchlist/action queue. This file
does not follow/subscribe by itself; high-risk actions remain gated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
RUNTIME = INSTANCE_DIR / "runtime"
RAW_DIR = RUNTIME / "raw"
SEED_DIR = RUNTIME / "seed_intelligence"
CANDIDATES_JSONL = SEED_DIR / "candidates.jsonl"
CURRENT_JSON = SEED_DIR / "current.json"
WATCHLIST_JSON = SEED_DIR / "watchlist.json"
ACTIONS_JSON = SEED_DIR / "actions.json"
REPORT_MD = SEED_DIR / "current.md"

for directory in (SEED_DIR,):
    directory.mkdir(parents=True, exist_ok=True)

# Example (persona × platform) → vertical map. TODO: replace with YOUR instance's
# agents. Verticals map to your INSTANCE.md domain rings (yolk/white/shell).
AGENT_VERTICAL = {
    "P01_Telegram": "vertical_a",     # P01 = yolk persona
    "P01_TikTok": "vertical_a",
    "P01_FB": "vertical_a",
    "P01_IG": "vertical_a",
    "P02_YouTube": "vertical_b",      # P02 = white persona
    "P02_X": "vertical_b",
    "P02_TikTok": "vertical_b",
    "P03_Reddit": "vertical_c",       # P03 = shell persona
    "P03_Discord": "vertical_c",
}

ALGO_AGENT_IDS = set(AGENT_VERTICAL)
HIGH_RISK_AGENT_IDS = {
    "P01_FB",
    "P01_IG",
    "P01_TikTok",
    "P02_IG",
    "P02_TikTok",
    "P02_X",
}

VERTICAL_MIN_WATCHLIST = {
    "vertical_a": 8,
    "vertical_b": 8,
    "vertical_c": 5,
}

GARBLE_MARKERS = ("�", "\uf000", "\ufffd")


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def cutoff_iso(hours: int) -> str:
    return (now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def seed_id(platform: str, key: str) -> str:
    compact = key.strip().lower()
    digest = hashlib.sha1(f"{platform}:{compact}".encode("utf-8")).hexdigest()[:12]
    return f"{platform}:{digest}"


def short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def looks_unreadable(value: object) -> bool:
    text = str(value or "")
    if not text:
        return True
    if any(marker in text for marker in GARBLE_MARKERS):
        return True
    private_use = sum(1 for ch in text if "\ue000" <= ch <= "\uf8ff")
    if private_use:
        return True
    local = sum(1 for ch in text if "\u0e00" <= ch <= "\u0e7f")
    latin_digit = sum(1 for ch in text if ch.isascii() and ch.isalnum())
    cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff")
    meaningful = local + latin_digit
    return cjk >= 3 and meaningful < max(3, cjk // 2)


def unsupported_display_script(value: object) -> bool:
    """Boss-facing Seed IDs must be readable in _TEMPLATE context.

    Keep local, English/ASCII handles, and CJK. Drop names dominated by unrelated
    scripts or symbol soup; the raw line remains available via evidence refs.
    """
    text = str(value or "").strip()
    if not text:
        return True
    signal = 0
    unsupported = 0
    for ch in text:
        if ch.isspace() or ch in "_-./:@#[]()（）+&|":
            continue
        if ch.isascii():
            signal += 1
        elif "\u0e00" <= ch <= "\u0e7f":
            signal += 1
        elif "\u3400" <= ch <= "\u9fff":
            signal += 1
        else:
            unsupported += 1
    if signal == 0:
        return True
    return unsupported > signal


def discard_reason(item: dict) -> str | None:
    if looks_unreadable(item.get("display_name")):
        return "unreadable_display_name"
    if unsupported_display_script(item.get("display_name")):
        return "unsupported_display_script"
    return None


def filter_readable_candidates(candidates: list[dict]) -> tuple[list[dict], Counter]:
    kept = []
    discarded: Counter = Counter()
    for item in candidates:
        reason = discard_reason(item)
        if reason:
            discarded[reason] += 1
            continue
        kept.append(item)
    return kept, discarded


def recent_raw_records(agent_id: str, cutoff: str) -> list[tuple[Path, int, dict]]:
    out: list[tuple[Path, int, dict]] = []
    candidates = [RAW_DIR / agent_id]
    if agent_id == "P03_FB":
        candidates.append(RAW_DIR / "facebook" / "P03")
    if agent_id == "P03_IG":
        candidates.append(RAW_DIR / "instagram" / "P03")
    if agent_id == "P04_IG":
        candidates.append(RAW_DIR / "instagram" / "P04")
    for directory in candidates:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for idx, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                ts = str(item.get("ts") or item.get("scraped_at") or item.get("checked_at") or "")
                if ts and ts < cutoff:
                    continue
                out.append((path, idx, item))
    return out


def candidate_from_record(agent_id: str, path: Path, line_no: int, item: dict) -> dict | None:
    platform = str(item.get("platform") or "").lower()
    vertical = AGENT_VERTICAL.get(agent_id, "unknown")
    text = " ".join(
        str(item.get(k) or "")
        for k in (
            "title",
            "text",
            "desc",
            "card_text",
            "bio",
            "og_description",
            "task_focus",
        )
    ).strip()

    key = None
    display = None
    url = None
    kind = "account"

    if platform == "youtube" or item.get("event") == "youtube_search_result":
        key = item.get("channel_id") or item.get("channel") or item.get("uploader")
        display = item.get("channel") or item.get("uploader") or key
        url = f"https://www.youtube.com/channel/{key}" if item.get("channel_id") else item.get("url")
        platform = "youtube"
        kind = "channel"
    elif platform in {"x", "twitter"}:
        key = item.get("handle")
        display = f"@{key}" if key else None
        url = item.get("url") or (f"https://x.com/{key}" if key else None)
        platform = "x"
    elif platform == "instagram":
        key = item.get("handle")
        display = f"@{key}" if key else None
        url = f"https://www.instagram.com/{key}/" if key else None
    elif platform == "tiktok":
        key = item.get("author")
        display = f"@{key}" if key else None
        url = f"https://www.tiktok.com/@{key}" if key else item.get("url")
    elif platform in {"bigo", "nimo"}:
        key = item.get("room_id")
        display = (item.get("card_text") or key or "")[:80]
        url = item.get("url")
        kind = "room_or_streamer"
    elif platform == "facebook":
        key = item.get("page_slug") or item.get("author") or item.get("post_id")
        display = item.get("page_slug") or item.get("author") or key
        url = item.get("page_url") or item.get("permalink") or item.get("url")
        kind = "page_or_author"
    elif platform == "reddit":
        key = item.get("author") or item.get("sub")
        display = item.get("author") or f"r/{item.get('sub')}" if item.get("sub") else key
        url = item.get("permalink") or item.get("url")
    elif platform == "discord":
        key = item.get("server_id") or item.get("channel_id") or item.get("author")
        display = item.get("server_name") or item.get("channel_name") or key

    if not key:
        return None

    evidence_ref = f"{path.relative_to(ROOT).as_posix()}:{line_no}"
    event = str(item.get("event") or item.get("kind") or "raw_record")
    score = 45
    if agent_id in HIGH_RISK_AGENT_IDS:
        score += 8
    if text:
        score += min(14, len(text) // 60)
    if url:
        score += 6
    if event not in {"collector_status", "page_state_check", "verify_session"}:
        score += 8
    return {
        "seed_id": seed_id(platform, str(key)),
        "candidate_key": str(key),
        "display_name": display or str(key),
        "platform": platform,
        "kind": kind,
        "url": url,
        "source_agent": agent_id,
        "vertical": vertical,
        "first_seen_at": str(item.get("ts") or item.get("scraped_at") or now_iso()),
        "last_seen_at": str(item.get("ts") or item.get("scraped_at") or now_iso()),
        "source_event": event,
        "initial_score": score,
        "evidence_refs": [evidence_ref],
        "evidence_samples": [text[:500]] if text else [],
        "raw_record_hash": short_hash(json.dumps(item, ensure_ascii=False, sort_keys=True)),
    }


def collect_candidates(hours: int) -> list[dict]:
    cutoff = cutoff_iso(hours)
    by_key: dict[tuple[str, str, str], dict] = {}
    for agent_id in sorted(ALGO_AGENT_IDS):
        for path, line_no, item in recent_raw_records(agent_id, cutoff):
            candidate = candidate_from_record(agent_id, path, line_no, item)
            if not candidate:
                continue
            key = (candidate["source_agent"], candidate["platform"], candidate["candidate_key"])
            current = by_key.get(key)
            if not current:
                by_key[key] = candidate
                continue
            current["last_seen_at"] = max(str(current["last_seen_at"]), str(candidate["last_seen_at"]))
            current["initial_score"] = max(int(current["initial_score"]), int(candidate["initial_score"]))
            current["evidence_refs"].extend(candidate["evidence_refs"])
            current["evidence_samples"].extend(candidate.get("evidence_samples") or [])
    return list(by_key.values())


def verify_candidates(candidates: list[dict]) -> list[dict]:
    verified: list[dict] = []
    for item in candidates:
        evidence_count = len(set(item.get("evidence_refs") or []))
        sample_count = len([x for x in item.get("evidence_samples") or [] if x])
        score = int(item.get("initial_score") or 0)
        quality_flags = []
        score += min(20, evidence_count * 4)
        score += min(10, sample_count * 2)
        if item.get("platform") in {"youtube", "x", "bigo", "facebook"}:
            score += 5
        if looks_unreadable(item.get("display_name")):
            score -= 30
            quality_flags.append("unreadable_display_name")
        score = min(100, score)

        if score >= 78 and evidence_count >= 2 and "unreadable_display_name" not in quality_flags:
            status = "verified_seed"
        elif score >= 60:
            status = "needs_human_review"
        elif score < 45:
            status = "rejected_seed"
        else:
            status = "candidate"

        action = "watchlist_only"
        if status == "verified_seed":
            action = "section_chief_review" if item["source_agent"] in HIGH_RISK_AGENT_IDS else "auto_watchlist"
        elif status == "needs_human_review":
            action = "section_chief_review"

        rationale = [
            f"score={score}",
            f"evidence_count={evidence_count}",
            f"source_agent={item.get('source_agent')}",
            f"vertical={item.get('vertical')}",
        ]
        if quality_flags:
            rationale.append("quality_flags=" + ",".join(quality_flags))
        verified.append(item | {
            "verified_at": now_iso(),
            "seed_score": score,
            "status": status,
            "recommended_action": action,
            "quality_flags": quality_flags,
            "rationale": "; ".join(rationale),
        })
    verified.sort(key=lambda x: (-int(x.get("seed_score") or 0), x.get("source_agent", ""), x.get("display_name", "")))
    return verified


def _load_approved_follow_states() -> dict[str, str]:
    """Return {seed_id: follow_state} for seeds already decided by section chief."""
    path = SEED_DIR / "approved_seeds.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {s["seed_id"]: s.get("follow_state", "approved_section_chief")
                for s in data.get("approved", []) if s.get("seed_id")}
    except Exception:
        return {}


def build_watchlist(verified: list[dict]) -> list[dict]:
    approved_states = _load_approved_follow_states()
    watchlist = []
    for item in verified:
        if item.get("status") != "verified_seed":
            continue
        # Preserve section chief's decision if already processed
        follow_state = approved_states.get(
            item["seed_id"],
            "pending_section_chief_review" if item["source_agent"] in HIGH_RISK_AGENT_IDS else "not_required",
        )
        watchlist.append({
            "seed_id": item["seed_id"],
            "platform": item["platform"],
            "display_name": item["display_name"],
            "url": item.get("url"),
            "source_agent": item["source_agent"],
            "vertical": item["vertical"],
            "seed_score": item["seed_score"],
            "status": "active_watchlist",
            "added_at": now_iso(),
            "watch_cadence": "daily" if item["seed_score"] >= 85 else "weekly",
            "follow_state": follow_state,
            "evidence_refs": item.get("evidence_refs", [])[:8],
            "why_watch": item.get("rationale"),
        })
    return watchlist


TERMINAL_ACTION_STATUSES = {
    "approved_section_chief",
    "rejected_section_chief",
    "pending_strategist_review",
    "approved_strategist",
    "rejected_strategist",
}


def _load_prev_actions_by_seed() -> dict[str, dict]:
    """Index prior actions.json by seed_id so terminal decisions survive refresh.

    Without this, seed_intelligence overwrites actions.json with all entries reset to
    status=queued every cron — which makes seed_audit's pending_approval counter
    pin at the slice cap (200) forever, even though section_chief_seed_processor
    already approved them. See boss debug 2026-05-19.
    """
    data = load_json(ACTIONS_JSON, {"actions": []})
    actions = data.get("actions", []) if isinstance(data, dict) else []
    out: dict[str, dict] = {}
    for a in actions:
        sid = a.get("seed_id")
        if sid:
            out[sid] = a
    return out


def build_actions(verified: list[dict], prev_by_seed: dict[str, dict] | None = None) -> list[dict]:
    prev_by_seed = prev_by_seed or {}
    actions = []
    for item in verified:
        action = item.get("recommended_action")
        if action not in {"section_chief_review", "auto_watchlist"}:
            continue
        seed_id = item["seed_id"]
        prev = prev_by_seed.get(seed_id)
        if prev and prev.get("status") in TERMINAL_ACTION_STATUSES:
            actions.append({
                "action_id": prev.get("action_id")
                    or f"SA-{now().strftime('%Y%m%d')}-{seed_id.replace(':', '-')}",
                "created_at": prev.get("created_at", now_iso()),
                "seed_id": seed_id,
                "source_agent": item["source_agent"],
                "platform": item["platform"],
                "action": action,
                "status": prev["status"],
                "risk_gate": "section_chief_managed",
                "why": item.get("rationale"),
                "url": item.get("url"),
                "processed_at": prev.get("processed_at"),
                "processed_by": prev.get("processed_by"),
                "carried_from_prev": True,
            })
        else:
            actions.append({
                "action_id": f"SA-{now().strftime('%Y%m%d')}-{seed_id.replace(':', '-')}",
                "created_at": now_iso(),
                "seed_id": seed_id,
                "source_agent": item["source_agent"],
                "platform": item["platform"],
                "action": action,
                "status": "queued",
                "risk_gate": "section_chief_managed",
                "why": item.get("rationale"),
                "url": item.get("url"),
            })
    return actions


def summary_by(items: list[dict], field: str) -> dict[str, int]:
    return dict(Counter(str(x.get(field) or "unknown") for x in items))


def refresh(hours: int = 72) -> dict:
    raw_candidates = collect_candidates(hours)
    candidates, discarded = filter_readable_candidates(raw_candidates)
    verified = verify_candidates(candidates)
    watchlist = build_watchlist(verified)
    prev_actions_by_seed = _load_prev_actions_by_seed()
    actions = build_actions(verified, prev_actions_by_seed)
    status_counts = summary_by(verified, "status")
    vertical_counts = summary_by(watchlist, "vertical")
    gaps = {
        vertical: max(0, target - vertical_counts.get(vertical, 0))
        for vertical, target in VERTICAL_MIN_WATCHLIST.items()
    }
    payload = {
        "generated_at": now_iso(),
        "instance": ACTIVE_INSTANCE,
        "window_hours": hours,
        "summary": {
            "raw_candidates": len(raw_candidates),
            "candidates": len(candidates),
            "discarded_candidates": sum(discarded.values()),
            "discard_reasons": dict(discarded),
            "watchlist": len(watchlist),
            "actions": len(actions),
            "status_counts": status_counts,
            "vertical_watchlist_counts": vertical_counts,
            "portfolio_gaps": gaps,
        },
        "candidates": verified[:300],
        "watchlist": watchlist[:200],
        "actions": actions[:200],
    }
    write_json(CURRENT_JSON, payload)
    write_json(WATCHLIST_JSON, {"generated_at": now_iso(), "watchlist": watchlist})
    write_json(ACTIONS_JSON, {"generated_at": now_iso(), "actions": actions})
    append_jsonl(CANDIDATES_JSONL, {
        "ts": now_iso(),
        "event": "seed_pipeline_refresh",
        "summary": payload["summary"],
    })
    write_report(payload)
    try:
        from processors.history_log import log_event

        log_event(
            actor="SEED_PIPELINE",
            kind="metric",
            scope="seed_intelligence",
            title="seed intelligence refresh",
            body=json.dumps(payload["summary"], ensure_ascii=False),
            refs=[CURRENT_JSON.relative_to(ROOT).as_posix(), REPORT_MD.relative_to(ROOT).as_posix()],
        )
    except Exception:
        pass
    return payload


def write_report(payload: dict) -> None:
    summary = payload["summary"]
    lines = [
        f"# Seed Intelligence - {payload['generated_at']}",
        "",
        "## Summary",
        "",
        f"- candidates: {summary['candidates']}",
        f"- discarded_candidates: {summary.get('discarded_candidates', 0)}",
        f"- discard_reasons: {json.dumps(summary.get('discard_reasons', {}), ensure_ascii=False)}",
        f"- watchlist: {summary['watchlist']}",
        f"- actions: {summary['actions']}",
        f"- status_counts: {json.dumps(summary['status_counts'], ensure_ascii=False)}",
        f"- vertical_watchlist_counts: {json.dumps(summary['vertical_watchlist_counts'], ensure_ascii=False)}",
        f"- portfolio_gaps: {json.dumps(summary['portfolio_gaps'], ensure_ascii=False)}",
        "",
        "## Top Candidates",
        "",
    ]
    for item in payload["candidates"][:30]:
        lines.append(
            f"- [{item.get('status')}] {item.get('source_agent')} "
            f"{item.get('platform')} {item.get('display_name')} "
            f"score={item.get('seed_score')} action={item.get('recommended_action')}"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=72)
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    payload = refresh(args.hours)
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps({"summary": payload["summary"], "path": str(CURRENT_JSON)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
