"""Section Chief Seed Processor.

Reads queued actions from seed_intelligence/actions.json, makes autonomous
decisions, updates watchlist, and dispatches approved seeds to agent targets.

Decision chain (section chief autonomous):
  action=auto_watchlist      → approve immediately (no review needed)
  action=section_chief_review AND score >= 70 → section chief auto-approve
  action=section_chief_review AND score 55-69 → escalate to strategist
  action=section_chief_review AND score < 55  → reject

After approve: updates watchlist.json follow_state → approved_section_chief,
  writes to approved_seeds.json (persistent canonical record),
  appends dispatch record to agent_seed_dispatch.jsonl.
After escalate: queues strategist consult brief.
Sends DM summary to boss via brief queue.

Cron: every 4h, scheduled AFTER seed_intelligence (see blacksite_daemon.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
RUNTIME = INSTANCE_DIR / "runtime"
SEED_DIR = RUNTIME / "seed_intelligence"
ACTIONS_JSON = SEED_DIR / "actions.json"
CURRENT_JSON = SEED_DIR / "current.json"
WATCHLIST_JSON = SEED_DIR / "watchlist.json"
APPROVED_JSON = SEED_DIR / "approved_seeds.json"
DISPATCH_JSONL = SEED_DIR / "agent_seed_dispatch.jsonl"
BRIEF_QUEUE = RUNTIME / "briefs" / "queue"
LOG_DIR = RUNTIME / "logs"

SCORE_APPROVE_THRESHOLD = 70
SCORE_REJECT_THRESHOLD = 55


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [seed_processor] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"section_chief_seed_processor_{now().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _hist(kind: str, title: str, body: str | None = None, refs: list | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(
            actor="SECTION_CHIEF",
            kind=kind,
            scope="seed_processor",
            title=title[:118],
            body=body,
            refs=refs,
        )
    except Exception:
        return -1


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def decide(action_rec: dict, candidate: dict) -> str:
    """Return 'approve', 'reject', or 'escalate'."""
    action_type = action_rec.get("action", "")
    if action_type == "auto_watchlist":
        return "approve"
    score = int(candidate.get("seed_score") or action_rec.get("seed_score") or 0)
    if score >= SCORE_APPROVE_THRESHOLD:
        return "approve"
    if score < SCORE_REJECT_THRESHOLD:
        return "reject"
    return "escalate"


def build_candidate_index(current: dict) -> dict[str, dict]:
    return {c["seed_id"]: c for c in current.get("candidates", []) if c.get("seed_id")}


def process(dry_run: bool = False) -> dict:
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    BRIEF_QUEUE.mkdir(parents=True, exist_ok=True)

    actions_data = load_json(ACTIONS_JSON, {"actions": []})
    actions: list[dict] = actions_data.get("actions", []) if isinstance(actions_data, dict) else []

    current = load_json(CURRENT_JSON, {})
    candidates = build_candidate_index(current)

    watchlist_data = load_json(WATCHLIST_JSON, {"watchlist": []})
    watchlist: list[dict] = watchlist_data.get("watchlist", []) if isinstance(watchlist_data, dict) else []
    watchlist_by_seed: dict[str, dict] = {w["seed_id"]: w for w in watchlist if w.get("seed_id")}

    approved_data = load_json(APPROVED_JSON, {"approved": []})
    approved_seeds: list[dict] = approved_data.get("approved", []) if isinstance(approved_data, dict) else []
    already_approved: set[str] = {s["seed_id"] for s in approved_seeds if s.get("seed_id")}

    queued = [a for a in actions if a.get("status") == "queued"]
    if not queued:
        log(f"no queued actions (total={len(actions)}); nothing to do")
        return {"approved": 0, "rejected": 0, "escalated": 0, "skipped": 0}

    log(f"processing {len(queued)} queued actions (total actions={len(actions)})")

    results: dict[str, list[dict]] = {"approve": [], "reject": [], "escalate": []}

    for action_rec in queued:
        seed_id = action_rec.get("seed_id", "")
        candidate = candidates.get(seed_id, {})
        verdict = decide(action_rec, candidate)
        results[verdict].append({"action": action_rec, "candidate": candidate})

    log(f"decisions: approve={len(results['approve'])} reject={len(results['reject'])} escalate={len(results['escalate'])}")

    # --- update actions in place ---
    new_status: dict[str, str] = {}
    for item in results["approve"]:
        new_status[item["action"]["action_id"]] = "approved_section_chief"
    for item in results["reject"]:
        new_status[item["action"]["action_id"]] = "rejected_section_chief"
    for item in results["escalate"]:
        new_status[item["action"]["action_id"]] = "pending_strategist_review"

    if not dry_run:
        for a in actions:
            ns = new_status.get(a.get("action_id", ""))
            if ns:
                a["status"] = ns
                a["processed_at"] = now_iso()
                a["processed_by"] = "SECTION_CHIEF"
        write_json(ACTIONS_JSON, {"generated_at": now_iso(), "actions": actions})

    # --- update watchlist + approved_seeds ---
    new_approved: list[dict] = []
    dispatch_records: list[dict] = []

    for item in results["approve"]:
        seed_id = item["action"].get("seed_id", "")
        if seed_id in already_approved:
            continue
        cand = item["candidate"]
        w_entry = watchlist_by_seed.get(seed_id, {})
        approved_entry = {
            "seed_id": seed_id,
            "platform": item["action"].get("platform", cand.get("platform", "")),
            "display_name": cand.get("display_name") or w_entry.get("display_name", seed_id),
            "url": cand.get("url") or w_entry.get("url"),
            "source_agent": item["action"].get("source_agent", cand.get("source_agent", "")),
            "vertical": cand.get("vertical") or w_entry.get("vertical", ""),
            "seed_score": cand.get("seed_score") or w_entry.get("seed_score", 0),
            "approved_by": "SECTION_CHIEF",
            "approved_at": now_iso(),
            "watch_cadence": w_entry.get("watch_cadence", "weekly"),
            "follow_state": "approved_section_chief",
            "evidence_refs": (cand.get("evidence_refs") or w_entry.get("evidence_refs") or [])[:8],
            "why_approved": cand.get("rationale") or w_entry.get("why_watch", ""),
        }
        new_approved.append(approved_entry)
        dispatch_records.append({
            "dispatch_at": now_iso(),
            "seed_id": seed_id,
            "platform": approved_entry["platform"],
            "display_name": approved_entry["display_name"],
            "url": approved_entry["url"],
            "source_agent": approved_entry["source_agent"],
            "vertical": approved_entry["vertical"],
            "action": "monitor",
            "dispatched_by": "SECTION_CHIEF",
        })
        # Update watchlist entry in place
        if seed_id in watchlist_by_seed:
            watchlist_by_seed[seed_id]["follow_state"] = "approved_section_chief"
            watchlist_by_seed[seed_id]["approved_at"] = now_iso()

    if not dry_run and new_approved:
        approved_seeds.extend(new_approved)
        write_json(APPROVED_JSON, {"generated_at": now_iso(), "approved": approved_seeds})
        write_json(WATCHLIST_JSON, {"generated_at": now_iso(), "watchlist": list(watchlist_by_seed.values())})
        for d in dispatch_records:
            append_jsonl(DISPATCH_JSONL, d)

    # --- escalate to strategist ---
    if results["escalate"] and not dry_run:
        _queue_strategist_escalation(results["escalate"])

    # --- DM boss ---
    n_approve = len(results["approve"])
    n_reject = len(results["reject"])
    n_escalate = len(results["escalate"])
    n_new = len(new_approved)

    if not dry_run:
        _send_dm_summary(n_approve, n_reject, n_escalate, n_new, results)

    summary = {
        "processed_at": now_iso(),
        "queued_total": len(queued),
        "approved": n_approve,
        "new_approved": n_new,
        "rejected": n_reject,
        "escalated": n_escalate,
        "dispatch_records": len(dispatch_records),
    }

    if not dry_run:
        _hist(
            "metric",
            f"seed processor: +{n_approve}A {n_reject}R {n_escalate}E → {n_new} new watchlist",
            body=json.dumps(summary, ensure_ascii=False),
            refs=[
                ACTIONS_JSON.relative_to(ROOT).as_posix(),
                APPROVED_JSON.relative_to(ROOT).as_posix(),
            ],
        )

    log(f"done: {summary}")
    return summary


def _queue_strategist_escalation(escalated: list[dict]) -> None:
    if not escalated:
        return
    try:
        ts = now().strftime("%Y%m%dT%H%M%S")
        q = BRIEF_QUEUE / f"pending_{ts}_seed_escalate_strategist.md"
        lines = []
        for item in escalated[:8]:
            cand = item["candidate"]
            a = item["action"]
            lines.append(
                f"• {cand.get('display_name', a.get('seed_id', '?'))} "
                f"({a.get('platform', '?')}) score={cand.get('seed_score', '?')} "
                f"→ 灰色地帶，請策略長裁量"
            )
        q.write_text(
            f"[策略長 決策] 種子審核升級 — {len(escalated)} 條待裁量\n\n"
            + "\n".join(lines)
            + f"\n\n分數介於 {SCORE_REJECT_THRESHOLD}-{SCORE_APPROVE_THRESHOLD - 1}，"
            "小主管無法自主決定，需策略長審核後自動派工。\n",
            encoding="utf-8",
        )
        log(f"escalation brief queued: {q.name}")
    except Exception as e:
        log(f"escalation brief fail (non-fatal): {e}")


def _send_dm_summary(n_approve: int, n_reject: int, n_escalate: int,
                     n_new: int, results: dict) -> None:
    if n_approve == 0 and n_reject == 0 and n_escalate == 0:
        return
    try:
        ts = now().strftime("%Y%m%dT%H%M%S")
        q = BRIEF_QUEUE / f"pending_{ts}_seed_processor_summary.md"

        # Sample approved for boss
        approve_samples = []
        for item in results["approve"][:5]:
            cand = item["candidate"]
            a = item["action"]
            name = cand.get("display_name") or a.get("seed_id", "?")
            plat = a.get("platform", "?")
            score = cand.get("seed_score", "?")
            approve_samples.append(f"• {name} ({plat}) score={score} → 已加入監控")

        reject_samples = []
        for item in results["reject"][:3]:
            cand = item["candidate"]
            a = item["action"]
            name = cand.get("display_name") or a.get("seed_id", "?")
            reject_samples.append(f"• {name} score={cand.get('seed_score', '?')} → 拒絕（分數太低）")

        parts = [
            f"[小主管 FYI] 種子審核完成",
            "",
            f"• 批准 {n_approve} 條、拒絕 {n_reject} 條"
            + (f"、升級策略長 {n_escalate} 條" if n_escalate else "")
            + f" → {n_new} 條新加入監控清單，情報員下次 cron 即開始追蹤",
        ]
        if approve_samples:
            parts.append("")
            parts.extend(approve_samples)
        if reject_samples:
            parts.append("")
            parts.extend(reject_samples)
        if n_escalate:
            parts.append(f"\n策略長已收到 {n_escalate} 條待裁量通知，批准後自動派工。")
        parts.append("\n小主管自主處理，不需行動。")

        q.write_text("\n".join(parts), encoding="utf-8")
        log(f"DM summary queued: {q.name}")
    except Exception as e:
        log(f"DM summary fail (non-fatal): {e}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report decisions without writing")
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    summary = process(dry_run=args.dry_run)
    if args.print_json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
