"""Section Chief audit for Seed Intelligence."""

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

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
SEED_DIR = RUNTIME / "seed_intelligence"
CURRENT_JSON = SEED_DIR / "current.json"
AUDIT_DIR = SEED_DIR / "audit"
AUDIT_JSON = AUDIT_DIR / "current.json"
AUDIT_MD = AUDIT_DIR / "current.md"
AUDIT_STATE_JSON = AUDIT_DIR / "persistence_state.json"
IMPROVEMENT_DIR = RUNTIME / "improvement_proposals"
QUEUE_DIR = RUNTIME / "briefs" / "queue"

# Bug A (boss 2026-05-19): same (agent_id, finding_kind) appearing in ≥N
# consecutive audits → auto-open incident, auto-escalate to strategist, suppress
# repeat boss-facing briefs. CLAUDE.md §15 incident workflow expects 7-day OR
# structural escalation; 3 cycles at 4h cadence = 12h proves chief-autonomous
# resolution didn't work without burning a full week of noise.
ESCALATE_AFTER_CONSECUTIVE = int(os.environ.get("SEED_AUDIT_ESCALATE_AFTER", "3"))


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _finding_key(finding: dict) -> str | None:
    sev = finding.get("severity")
    if sev not in {"critical", "warning"}:
        return None
    agent_id = str(finding.get("agent_id") or "_global_")
    fkind = str(finding.get("finding") or "")
    if not fkind:
        return None
    return f"{agent_id}|{fkind}"


def _update_persistence_state(
    findings: list[dict], verdict: str
) -> tuple[dict, list[tuple[str, str]], set[str]]:
    """Track per-(agent, finding) consecutive count + auto-open incidents on persistence.

    Returns (new_state, new_escalations, pending_brief_keys).
      new_escalations: (key, incident_id) for incidents opened THIS run
      pending_brief_keys: keys still allowed in boss-facing brief (pre-escalation
        every cycle; post-escalation exactly one final notification, then suppressed)
    """
    prev_state: dict = {}
    try:
        prev_state = json.loads(AUDIT_STATE_JSON.read_text(encoding="utf-8"))
    except Exception:
        prev_state = {}
    prev_by_key: dict = prev_state.get("by_key", {}) if isinstance(prev_state, dict) else {}

    new_by_key: dict = {}
    new_escalations: list[tuple[str, str]] = []
    pending_brief_keys: set[str] = set()
    current_keys: set[str] = set()

    for finding in findings:
        key = _finding_key(finding)
        if not key:
            continue
        current_keys.add(key)
        prev = prev_by_key.get(key, {})
        consecutive = int(prev.get("consecutive_count", 0)) + 1
        first_seen = prev.get("first_seen") or now_iso()
        incident_id = prev.get("incident_id")
        already_notified = bool(prev.get("boss_notified_at_escalation"))
        agent_id = finding.get("agent_id") or ""
        fkind = finding.get("finding") or ""
        detail = finding.get("detail") or ""

        if (
            consecutive >= ESCALATE_AFTER_CONSECUTIVE
            and not incident_id
            and agent_id
        ):
            try:
                from processors.agent_incidents import open_incident, transition
                inc_id = open_incident(
                    agent_id=agent_id,
                    kind=fkind,
                    hypothesis=(
                        f"Seed audit reports '{fkind}' for {consecutive} consecutive cycles "
                        f"since {first_seen}. Section Chief autonomous handling did not "
                        f"resolve. Auto-escalated to strategist per CLAUDE.md §15 + boss "
                        f"2026-05-19 directive."
                    ),
                    evidence=[detail, f"first_seen={first_seen}", f"consecutive={consecutive}"],
                    severity="yellow",
                )
                transition(
                    inc_id,
                    "escalated_strategist",
                    note=(
                        f"auto-escalated by cron_seed_audit: {consecutive} consecutive "
                        "identical findings; chief autonomous handling did not resolve"
                    ),
                    actor="cron_seed_audit",
                )
                incident_id = inc_id
                new_escalations.append((key, inc_id))
            except Exception as e:
                print(
                    f"[seed_audit] open_incident fail for {key}: "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )

        if not incident_id:
            pending_brief_keys.add(key)
        elif incident_id and not already_notified:
            pending_brief_keys.add(key)

        new_by_key[key] = {
            "consecutive_count": consecutive,
            "first_seen": first_seen,
            "last_seen": now_iso(),
            "incident_id": incident_id,
            "severity": finding.get("severity"),
            "agent_id": agent_id,
            "finding": fkind,
            "boss_notified_at_escalation": bool(incident_id),
        }

    for old_key, old_data in prev_by_key.items():
        if old_key in current_keys:
            continue
        inc_id = old_data.get("incident_id")
        if inc_id:
            try:
                from processors.agent_incidents import transition
                transition(
                    inc_id,
                    "resolved",
                    note=f"seed audit no longer reports {old_key} at {now_iso()}",
                    actor="cron_seed_audit",
                )
            except Exception as e:
                print(
                    f"[seed_audit] resolve incident {inc_id} fail: "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )

    new_state = {"updated_at": now_iso(), "verdict": verdict, "by_key": new_by_key}
    try:
        AUDIT_STATE_JSON.write_text(
            json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[seed_audit] state write fail: {type(e).__name__}: {e}", flush=True)
    return new_state, new_escalations, pending_brief_keys


def audit() -> dict:
    seed = load_json(CURRENT_JSON, {"summary": {}, "candidates": [], "watchlist": [], "actions": []})
    candidates = seed.get("candidates", []) if isinstance(seed, dict) else []
    watchlist = seed.get("watchlist", []) if isinstance(seed, dict) else []
    actions = seed.get("actions", []) if isinstance(seed, dict) else []
    findings = []

    status_counts = Counter(str(c.get("status") or "unknown") for c in candidates)
    by_agent = Counter(str(c.get("source_agent") or "unknown") for c in candidates)
    high_risk_auto = [
        a for a in actions
        if a.get("action") == "boss_approve_follow" and a.get("status") not in {"queued", "pending_section_chief_review"}
    ]
    if high_risk_auto:
        findings.append({
            "severity": "critical",
            "finding": "high_risk_follow_not_queued",
            "detail": f"{len(high_risk_auto)} 高風險追蹤動作未正確進入小主管審核隊列。",
        })
    if status_counts.get("verified_seed", 0) == 0 and candidates:
        findings.append({
            "severity": "warning",
            "finding": "no_verified_seed",
            "detail": "Candidate flow exists but no seed passed verification.",
        })
    if not candidates:
        findings.append({
            "severity": "warning",
            "finding": "candidate_flow_empty",
            "detail": "No Seed Candidate was found in the current evidence window.",
        })
    unreadable = [
        c for c in candidates
        if "unreadable_display_name" in (c.get("quality_flags") or [])
    ]
    if unreadable:
        findings.append({
            "severity": "warning",
            "finding": "unreadable_seed_display_names",
            "detail": f"{len(unreadable)} seed candidates have unreadable/mojibake display names and must not be auto-watchlisted.",
        })
    for agent_id, count in by_agent.items():
        if count >= 20:
            verified = sum(1 for c in candidates if c.get("source_agent") == agent_id and c.get("status") == "verified_seed")
            if verified == 0:
                findings.append({
                    "severity": "warning",
                    "finding": "agent_many_candidates_zero_verified",
                    "agent_id": agent_id,
                    "detail": f"{agent_id} produced {count} candidates but zero verified seeds.",
                })

    pending_approval = [a for a in actions if a.get("status") in {"pending_boss_approval", "pending_section_chief_review", "queued"}]
    if len(pending_approval) > 10:
        findings.append({
            "severity": "info",
            "finding": "section_chief_queue_large",
            "detail": f"{len(pending_approval)} 個種子動作在小主管審核隊列中，小主管將自主處理。",
        })

    level_order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda x: (level_order.get(x.get("severity"), 9), x.get("finding", "")))
    verdict = "pass"
    if any(f.get("severity") == "critical" for f in findings):
        verdict = "critical"
    elif any(f.get("severity") == "warning" for f in findings):
        verdict = "warning"

    payload = {
        "generated_at": now_iso(),
        "instance": ACTIVE_INSTANCE,
        "auditor": "SECTION_CHIEF.seed_audit",
        "verdict": verdict,
        "summary": {
            "candidate_count": len(candidates),
            "watchlist_count": len(watchlist),
            "action_count": len(actions),
            "status_counts": dict(status_counts),
            "pending_approval": len(pending_approval),
            "findings": len(findings),
        },
        "findings": findings,
    }
    write_json(AUDIT_JSON, payload)
    write_report(payload)
    if verdict in {"critical", "warning"}:
        write_improvement_proposal(payload)
    try:
        from processors.history_log import log_event

        log_event(
            actor="SECTION_CHIEF",
            kind="metric",
            scope="seed_audit",
            title=f"seed audit {verdict}",
            body=json.dumps(payload["summary"], ensure_ascii=False),
            refs=[AUDIT_JSON.relative_to(ROOT).as_posix(), AUDIT_MD.relative_to(ROOT).as_posix()],
        )
    except Exception:
        pass
    # Bug A: persistence tracking + auto-escalation. Updates AUDIT_STATE_JSON,
    # opens + escalates incidents for findings persisting ≥ ESCALATE_AFTER_CONSECUTIVE
    # cycles, returns the subset of finding keys still eligible for boss-facing brief.
    new_state, new_escalations, pending_brief_keys = _update_persistence_state(findings, verdict)
    payload["persistence_state"] = {
        "escalate_after_consecutive": ESCALATE_AFTER_CONSECUTIVE,
        "tracked_keys": len(new_state.get("by_key", {})),
        "new_escalations": [{"key": k, "incident_id": i} for k, i in new_escalations],
        "pending_brief_keys": sorted(pending_brief_keys),
    }
    write_json(AUDIT_JSON, payload)

    if verdict in {"critical", "warning"}:
        eligible_findings = [
            f for f in findings
            if _finding_key(f) is None or _finding_key(f) in pending_brief_keys
        ]
        # Drop info-only findings if nothing critical/warning is eligible
        nontrivial = [f for f in eligible_findings if f.get("severity") in {"critical", "warning"}]
        if not nontrivial:
            try:
                from processors.history_log import log_event
                log_event(
                    actor="SECTION_CHIEF", kind="config_change", scope="seed_audit",
                    title="seed audit brief suppressed (all findings already escalated)",
                    body=json.dumps({
                        "verdict": verdict,
                        "tracked_keys": len(new_state.get("by_key", {})),
                        "all_findings": [_finding_key(f) for f in findings if _finding_key(f)],
                    }, ensure_ascii=False),
                    refs=[AUDIT_JSON.relative_to(ROOT).as_posix()],
                )
            except Exception:
                pass
        else:
            try:
                ts = now().strftime("%Y%m%dT%H%M%S")
                if new_escalations:
                    prefix = "[策略長 決策]"
                    tag = "seed_strategist_persistent"
                    verdict_zh = "結構性問題（自動升級）"
                    handler_zh = (
                        f"策略長已接手 {len(new_escalations)} 條結構性問題"
                        f"（小主管 ≥{ESCALATE_AFTER_CONSECUTIVE} 週期內未解決，已開 incident "
                        f"+ 自動升級策略長）"
                    )
                elif verdict == "critical":
                    prefix = "[策略長 決策]"
                    tag = "seed_strategist"
                    verdict_zh = "嚴重"
                    handler_zh = "策略長已自主介入處理"
                else:
                    prefix = "[小主管 FYI]"
                    tag = "seed_sc_fyi"
                    verdict_zh = "警告"
                    handler_zh = "小主管已自主處理，你不需要行動"

                q = QUEUE_DIR / f"pending_{ts}_{tag}.md"
                finding_labels = {"critical": "嚴重", "warning": "警告", "info": "資訊"}
                top_bullets = []
                for f in eligible_findings[:3]:
                    sev_zh = finding_labels.get(f.get("severity", ""), f.get("severity", ""))
                    top_bullets.append(f"• [{sev_zh}] {str(f.get('detail',''))[:80]}")
                top = "\n".join(top_bullets) if top_bullets else "• （無具體發現）"

                incident_lines = []
                for k, i in new_escalations:
                    incident_lines.append(f"• {k} → `{i}`")
                incident_block = ""
                if incident_lines:
                    incident_block = "\n\n新開 incident：\n" + "\n".join(incident_lines)

                impact_zh = (
                    "情況嚴重，種子可能已嚴重偏移目標"
                    if verdict == "critical" else "種子品質影響情報覆蓋廣度"
                )
                q.write_text(
                    f"{prefix} 種子情報 {verdict_zh}\n\n"
                    f"• 候選 {payload['summary']['candidate_count']} 條，"
                    f"發現 {len(nontrivial)} 個未升級問題，"
                    f"待審 {payload['summary']['pending_approval']} 條 → {impact_zh}\n"
                    f"{top}"
                    f"{incident_block}\n\n"
                    f"{handler_zh}。完整報告：`{AUDIT_MD.relative_to(ROOT).as_posix()}`\n",
                    encoding="utf-8",
                )
            except Exception as e:
                print(f"[seed_audit] brief write fail: {type(e).__name__}: {e}", flush=True)
    return payload


def write_report(payload: dict) -> None:
    lines = [
        f"# Seed Audit - {payload['generated_at']}",
        "",
        f"verdict: {payload['verdict']}",
        f"summary: {json.dumps(payload['summary'], ensure_ascii=False)}",
        "",
        "## Findings",
        "",
    ]
    if not payload["findings"]:
        lines.append("- none")
    for finding in payload["findings"]:
        lines.append(
            f"- [{finding.get('severity')}] {finding.get('finding')}: "
            f"{finding.get('detail')}"
        )
    AUDIT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_improvement_proposal(payload: dict) -> None:
    date = now().strftime("%Y-%m-%d")
    path = IMPROVEMENT_DIR / f"{date}_seed_audit_{payload['verdict']}.md"
    lines = [
        f"# Seed Audit Improvement Proposal - {payload['generated_at']}",
        "",
        f"verdict: {payload['verdict']}",
        "",
        "## Problem",
        "",
    ]
    for finding in payload["findings"][:8]:
        lines.append(f"- [{finding.get('severity')}] {finding.get('finding')}: {finding.get('detail')}")
    lines.extend([
        "",
        "## Proposed Fix",
        "",
        "- If candidates are empty: repair raw extraction/candidate extractor for the affected agents.",
        "- If zero verified: tune seed scoring thresholds or require richer evidence samples.",
        "- If pending approval queue is large: strategist should prioritize portfolio lanes before follow approval.",
        "",
        "## Validation",
        "",
        "- Re-run `processors/seed_intelligence.py`, then `processors/seed_audit.py`.",
        "- Expected: critical=0, warning count reduced, no high-risk follow outside boss approval.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    payload = audit()
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps({"verdict": payload["verdict"], "summary": payload["summary"], "path": str(AUDIT_JSON)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
