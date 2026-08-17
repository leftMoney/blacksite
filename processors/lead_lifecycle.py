"""processors/lead_lifecycle.py — resolve executed kb_leads (P3).

Cron daily 18:55 (just before daily_brief 19:00) via blacksite_daemon.

For each lead in state='executed', classify the evidence and transition to
final state:
  resolved_closed   — evidence confirms noise / no-action-needed
  resolved_escalate — evidence supports escalation (next brief surfaces)
  re_queued         — evidence ambiguous; retry in 7 days
  conflict_flag     — evidence contradicts initial confidence by >0.3 (boss must review)

Classification:
  Pass 1 — rule-based (deterministic, free):
    - sql_sample with row_count=0 → re_queued (no data to judge)
    - sql_sample with empty/whitespace text in all rows → resolved_closed (spam/noise)
    - sql_sample where all texts are pure digits/repeats → resolved_closed
    - whois_lookup error → re_queued
    - whois_lookup with creation_date < 90d → resolved_escalate (fresh grey domain)
    - tier_upgrade with updated_count > 0 → resolved_closed (work done; spillover propose
      tier_upgrade follow-up if new_tier=yolk and confidence was low → conflict_flag)
    - code_fix_regex queued=true → resolved_closed (handed off)
    - code_fix_regex refused=true → escalated_with_resolution
    - card_builder_check healthy=false → resolved_escalate
    - card_builder_check healthy=true → resolved_closed
    - SUBAGENT_DISPATCH executed → re_queued (boss / next session needed)
    - AUTO_SCHEDULE executed → resolved_closed (queued; lifecycle done)
  Pass 2 — Haiku LLM (only for ambiguous evidence; <=20% of leads typical)
    NOTE: v1 we keep rule-based only. Haiku tier-2 reranker is a v2 enhancement
    (boss can flip on by setting LIFECYCLE_LLM=1 in .env).

Per CLAUDE.md §6.4: timestamps ISO 8601 with +07:00.
Per CLAUDE.md §13.6: log_event 'milestone' for normal resolutions; 'warning' for
conflict_flag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

RE_QUEUE_DAYS = 7


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [lead_lifecycle] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"lead_lifecycle_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _hist(kind: str, title: str, body: str | None = None,
          refs: list | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(
            actor="cron_lead_lifecycle", kind=kind, scope="lead_pipeline",
            title=title[:118], body=body, refs=refs,
        )
    except Exception as e:
        log(f"history_log fail: {type(e).__name__}: {e}")
        return -1


# --------------------------------------------------------------------
# Per-type classifiers — return (final_state, resolution_summary)
# --------------------------------------------------------------------

def _classify_sql_sample(lead: dict, ev: dict) -> tuple[str, str]:
    rc = ev.get("row_count", 0)
    rows = ev.get("rows") or []
    if rc == 0:
        return ("re_queued", f"sql returned 0 rows; retry in {RE_QUEUE_DAYS}d")
    # texts content analysis
    texts = [(r.get("text") or "").strip() for r in rows]
    if all(not t for t in texts):
        return ("resolved_closed", "all rows empty text — confirmed noise")
    # spam pattern: repeated short tokens (e.g. '999' 'aa' 'ok' 'local')
    nonempty = [t for t in texts if t]
    if len(nonempty) >= 3 and len(set(nonempty)) <= max(1, len(nonempty) // 3):
        return ("resolved_closed", "rows show high repetition — confirmed spam noise")
    # all-digit content (lottery number spam pattern)
    if nonempty and all(t.replace(" ", "").replace(",", "").replace(".", "").isdigit()
                         and len(t) <= 30 for t in nonempty):
        return ("resolved_closed", "rows are pure-digit content — number spam noise")
    # grey-market promo keyword detection in any row.
    # === INSTANCE: append the target country's native-language promo / lottery /
    # gambling / deposit-withdraw terms to this tuple. ===
    promo_kw = ("promo", "bonus", "free credit", "lottery", "gambling",
                "casino", "bet", "slot", "deposit", "withdraw")
    joined = " ".join(nonempty).lower()
    if any(k in joined for k in promo_kw):
        return ("resolved_escalate",
                "evidence shows promo/grey-market keywords — supports escalation")
    return ("re_queued",
            f"sample text neutral (no clear noise/promo); retry in {RE_QUEUE_DAYS}d for richer evidence")


def _classify_whois(lead: dict, ev: dict) -> tuple[str, str]:
    if ev.get("error"):
        return ("re_queued", f"whois error: {ev.get('error', 'unknown')[:80]}")
    creation = ev.get("creation_date")
    if creation:
        try:
            # whois date format varies wildly; try common ones
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S",
                        "%d-%b-%Y", "%Y/%m/%d"):
                try:
                    dt = datetime.strptime(creation.strip()[:19], fmt)
                    age_days = (datetime.now() - dt).days
                    if age_days < 90:
                        return ("resolved_escalate",
                                f"domain age {age_days}d (<90d) — fresh grey domain, escalate")
                    return ("resolved_closed",
                            f"domain age {age_days}d (>90d) — established, lower risk")
                except ValueError:
                    continue
        except Exception:
            pass
    return ("resolved_closed",
            f"whois ok registrar={ev.get('registrar', '?')[:40]}; no age signal")


def _classify_tier_upgrade(lead: dict, ev: dict) -> tuple[str, str]:
    if ev.get("error"):
        return ("re_queued", f"tier_upgrade error: {ev.get('error')[:80]}")
    n = ev.get("updated_count", 0)
    if n == 0:
        return ("re_queued", "no entities updated (entity not in DB?)")
    new_tier = ev.get("new_tier")
    conf = lead.get("confidence") or 0.5
    # conflict: low confidence (<0.5) but action committed AND elevated to yolk
    if conf < 0.5 and new_tier == "yolk":
        return ("conflict_flag",
                f"confidence={conf:.2f} but escalated {n} rows to yolk — boss review")
    return ("resolved_closed",
            f"tier_upgrade complete: {n} rows → {new_tier}; reversibility audit in evidence")


def _classify_code_fix_regex(lead: dict, ev: dict) -> tuple[str, str]:
    if ev.get("refused"):
        # already escalated by executor; lifecycle just logs final state
        return ("resolved_escalate", f"regex refused: {ev.get('reason', '?')[:100]}")
    if ev.get("queued"):
        return ("resolved_closed",
                f"code_fix_regex queued for boss/session apply: {ev.get('regex_name')}")
    return ("re_queued", "unexpected evidence shape")


def _classify_card_builder_check(lead: dict, ev: dict) -> tuple[str, str]:
    if ev.get("error"):
        return ("re_queued", f"check error: {ev['error'][:80]}")
    if not ev.get("healthy"):
        return ("resolved_escalate",
                f"card_builder unhealthy: last_built={ev.get('last_built_at')} "
                f"age_h={ev.get('age_hours')}")
    return ("resolved_closed",
            f"card_builder healthy: last_built {ev.get('age_hours')}h ago")


def _classify_subagent_dispatched(lead: dict, ev: dict) -> tuple[str, str]:
    return ("re_queued",
            f"subagent task queued at {ev.get('spec_path')}; "
            f"awaiting boss/session pickup; retry in {RE_QUEUE_DAYS}d")


def _classify_auto_schedule(lead: dict, ev: dict) -> tuple[str, str]:
    return ("resolved_closed",
            f"observation cron queued at {ev.get('queued_to')}; "
            "lifecycle complete")


def classify(lead: dict) -> tuple[str, str]:
    """Returns (next_state, resolution_summary)."""
    raw = lead.get("evidence") or "{}"
    try:
        ev = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return ("conflict_flag", f"evidence JSON malformed: {raw[:100]!r}")

    lane = lead.get("triage_lane")
    t = lead.get("type")

    # Lane-level routes first
    if lane == "AUTO_SCHEDULE":
        return _classify_auto_schedule(lead, ev)
    if lane == "SUBAGENT_DISPATCH":
        return _classify_subagent_dispatched(lead, ev)

    # Type-level routes
    if t == "sql_sample":
        return _classify_sql_sample(lead, ev)
    if t == "whois_lookup":
        return _classify_whois(lead, ev)
    if t == "tier_upgrade":
        return _classify_tier_upgrade(lead, ev)
    if t == "code_fix_regex":
        return _classify_code_fix_regex(lead, ev)
    if t == "card_builder_check":
        return _classify_card_builder_check(lead, ev)

    # Fallback
    return ("re_queued", f"no classifier for type={t!r} lane={lane!r}")


# --------------------------------------------------------------------
# Re-queue revival — bring re_queued leads back to pending after expiry
# --------------------------------------------------------------------

def revive_expired_requeues(conn) -> int:
    """Move re_queued leads whose re_queued_until is past back to pending."""
    ts = now_iso()
    rows = conn.execute(
        "SELECT lead_id FROM kb_leads WHERE state='re_queued' AND re_queued_until <= ?",
        (ts,),
    ).fetchall()
    if not rows:
        return 0
    for r in rows:
        conn.execute(
            """UPDATE kb_leads
                  SET state='pending',
                      triage_lane=NULL,
                      re_queued_until=NULL
                WHERE lead_id=?""",
            (r["lead_id"],),
        )
    conn.commit()
    log(f"revived {len(rows)} re_queued leads to pending")
    return len(rows)


# --------------------------------------------------------------------
# Main pass
# --------------------------------------------------------------------

def run_pass(dry_run: bool = False) -> dict:
    conn = get_connection()
    try:
        revived = 0 if dry_run else revive_expired_requeues(conn)

        cur = conn.execute(
            """SELECT lead_id, type, target, confidence, actionability,
                      reversibility, triage_lane, evidence
                 FROM kb_leads
                WHERE state='executed'
                ORDER BY emitted_at"""
        )
        cols = [d[0] for d in cur.description]
        leads = [dict(zip(cols, r)) for r in cur.fetchall()]

        if not leads:
            log("no executed leads to resolve")
            return {"total": 0, "revived": revived}

        counts = {"resolved_closed": 0, "resolved_escalate": 0,
                  "re_queued": 0, "conflict_flag": 0}
        ts = now_iso()
        re_q_until = (datetime.now(TZ) + timedelta(days=RE_QUEUE_DAYS)).isoformat(timespec="seconds")

        for lead in leads:
            next_state, summary = classify(lead)
            counts[next_state] = counts.get(next_state, 0) + 1
            if dry_run:
                log(f"DRY {next_state}: {lead['lead_id']} ({lead['type']}) — {summary}")
                continue
            try:
                if next_state == "re_queued":
                    conn.execute(
                        """UPDATE kb_leads
                              SET state='re_queued',
                                  resolution=?,
                                  resolution_at=?,
                                  re_queued_until=?
                            WHERE lead_id=?""",
                        (summary, ts, re_q_until, lead["lead_id"]),
                    )
                else:
                    conn.execute(
                        """UPDATE kb_leads
                              SET state=?,
                                  resolution=?,
                                  resolution_at=?
                            WHERE lead_id=?""",
                        (next_state, summary, ts, lead["lead_id"]),
                    )
                conn.commit()
            except Exception as e:
                log(f"FAIL update {lead['lead_id']}: {e}")
                continue

            kind = "warning" if next_state == "conflict_flag" else "milestone"
            _hist(kind,
                  f"lifecycle {next_state}: {lead['lead_id']} ({lead['type']})",
                  body=f"summary: {summary}",
                  refs=[f"lead:{lead['lead_id']}"])
            log(f"{next_state}: {lead['lead_id']} ({lead['type']}) — {summary}")

        log(f"pass: {counts} revived={revived}")
        if not dry_run and sum(counts.values()) > 0:
            _hist("metric",
                  f"lifecycle pass: {counts['resolved_closed']}closed / "
                  f"{counts['resolved_escalate']}esc / "
                  f"{counts['re_queued']}requeue / "
                  f"{counts['conflict_flag']}conflict",
                  body=f"counts={counts} revived={revived}")
        return {**counts, "total": sum(counts.values()), "revived": revived}
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run_pass(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
