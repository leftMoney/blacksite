"""processors/lead_triage.py — assign triage_lane to pending kb_leads (P2).

Cron */15 min via blacksite_daemon. Reads kb_leads WHERE state='pending', applies
rules from instances/<active>/policy/lead_triage_rules.yaml in priority order,
UPDATEs state='triaged' + triage_lane + triaged_at.

Lanes (per v9 schema CHECK constraint):
  AUTO_SAFE_EXEC | AUTO_SCHEDULE | SUBAGENT_DISPATCH | STRATEGIST_DISPATCH | BOSS_ESCALATE | CLOSE_AS_NOISE

Per CLAUDE.md §6.4: timestamps ISO 8601 with +07:00.
Per CLAUDE.md §13.6: log_event 'milestone' per pass; 'warning' on boss_escalate;
'decision' on per-lead lane assignment skipped (too noisy — only aggregate metric).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RULES_PATH = ROOT / "instances" / ACTIVE_INSTANCE / "policy" / "lead_triage_rules.yaml"
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [lead_triage] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"lead_triage_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_rules() -> dict:
    if not RULES_PATH.exists():
        log(f"WARN rules file missing: {RULES_PATH} — using empty rules (all leads → BOSS_ESCALATE)")
        return {}
    with RULES_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# Reversibility ordering for "<= reversible" rule
_REV_RANK = {"safe": 0, "reversible": 1, "medium": 2, "destructive": 3}


def _rev_le(actual: str | None, threshold: str) -> bool:
    """Return True if actual reversibility <= threshold rank.
    None treated as 'reversible' (mid-default)."""
    a = _REV_RANK.get((actual or "reversible").lower(), 1)
    t = _REV_RANK.get((threshold or "reversible").lower(), 1)
    return a <= t


def classify(lead: dict, rules: dict) -> tuple[str, str]:
    """Return (lane, reason). Priority order matters."""
    t = lead.get("type") or ""
    conf = lead.get("confidence") or 0.0
    act = lead.get("actionability") or 0.0
    rev = lead.get("reversibility")
    auto_safe = bool(lead.get("auto_safe"))

    # 1. close_as_noise — drop early
    cn = rules.get("close_as_noise") or {}
    if (
        "confidence_max" in cn and conf <= cn["confidence_max"]
        and "actionability_max" in cn and act <= cn["actionability_max"]
    ):
        return ("CLOSE_AS_NOISE",
                f"confidence={conf} <= {cn['confidence_max']} AND actionability={act} <= {cn['actionability_max']}")

    # 2. boss_escalate — destructive only (per CLAUDE.md §14: agent_strategy_change → strategist)
    be = rules.get("boss_escalate") or {}
    if t in (be.get("always_types") or []):
        return ("BOSS_ESCALATE", f"type={t} in always_types")
    if rev and rev in (be.get("reversibility_in") or []):
        return ("BOSS_ESCALATE", f"reversibility={rev} requires boss")

    # 3. strategist_dispatch — agent_strategy_change → 策略長 auto-resolves (CLAUDE.md §14)
    std = rules.get("strategist_dispatch") or {}
    if t in (std.get("types") or []):
        if "actionability_min" in std and act < std["actionability_min"]:
            return ("CLOSE_AS_NOISE",
                    f"type={t} but actionability={act} < {std['actionability_min']}")
        return ("STRATEGIST_DISPATCH", f"type={t} actionability={act} → 策略長")

    # 4. auto_safe_exec — whitelist types (小主管 authority per CLAUDE.md §14)
    ase = rules.get("auto_safe_exec") or {}
    ase_types = ase.get("types") or []
    if t in ase_types:
        if "confidence_min" in ase and conf < ase["confidence_min"]:
            pass  # fall through
        elif "reversibility_max" in ase and not _rev_le(rev, ase["reversibility_max"]):
            return ("BOSS_ESCALATE",
                    f"type={t} but reversibility={rev} > {ase['reversibility_max']}")
        else:
            return ("AUTO_SAFE_EXEC",
                    f"type={t} confidence={conf} reversibility={rev}")

    # 5. auto_schedule
    asc = rules.get("auto_schedule") or {}
    if t in (asc.get("types") or []):
        return ("AUTO_SCHEDULE", f"type={t} routed to scheduler")

    # 6. subagent_dispatch
    sd = rules.get("subagent_dispatch") or {}
    if t in (sd.get("types") or []):
        if "actionability_min" in sd and act < sd["actionability_min"]:
            return ("CLOSE_AS_NOISE",
                    f"type={t} but actionability={act} < {sd['actionability_min']}")
        return ("SUBAGENT_DISPATCH", f"type={t} actionability={act}")

    # fallthrough — unknown type or unmatched: send to boss
    return ("BOSS_ESCALATE", f"no rule matched type={t!r}")


def run_pass(dry_run: bool = False) -> dict:
    rules = load_rules()
    conn = get_connection()
    try:
        cur = conn.execute(
            """SELECT lead_id, type, target, confidence, actionability,
                      reversibility, auto_safe
                 FROM kb_leads
                WHERE state = 'pending'
                ORDER BY emitted_at"""
        )
        cols = [d[0] for d in cur.description]
        leads = [dict(zip(cols, r)) for r in cur.fetchall()]

        if not leads:
            log("no pending leads")
            return {"total": 0}

        counts = {
            "AUTO_SAFE_EXEC": 0, "AUTO_SCHEDULE": 0, "SUBAGENT_DISPATCH": 0,
            "STRATEGIST_DISPATCH": 0, "BOSS_ESCALATE": 0, "CLOSE_AS_NOISE": 0,
        }
        warnings = 0
        ts = now_iso()
        for lead in leads:
            lane, reason = classify(lead, rules)
            counts[lane] = counts.get(lane, 0) + 1
            if dry_run:
                log(f"DRY {lane}: {lead['lead_id']} ({lead['type']}) — {reason}")
                continue
            try:
                conn.execute(
                    """UPDATE kb_leads
                          SET state='triaged',
                              triage_lane=?,
                              triaged_at=?
                        WHERE lead_id=?""",
                    (lane, ts, lead["lead_id"]),
                )
                conn.commit()
            except Exception as e:
                log(f"FAIL update {lead['lead_id']}: {type(e).__name__}: {e}")
                continue
            if lane in ("BOSS_ESCALATE", "CLOSE_AS_NOISE", "STRATEGIST_DISPATCH"):
                if lane == "CLOSE_AS_NOISE":
                    conn.execute(
                        """UPDATE kb_leads
                              SET state='resolved_closed',
                                  resolution=?,
                                  resolution_at=?
                            WHERE lead_id=?""",
                        (f"auto_close_low_signal: {reason}", ts, lead["lead_id"]),
                    )
                    conn.commit()
                elif lane == "STRATEGIST_DISPATCH":
                    # route to 策略長 — state stays 'triaged' (executor picks up)
                    conn.commit()
                else:
                    # BOSS_ESCALATE — set state='escalated'
                    conn.execute(
                        "UPDATE kb_leads SET state='escalated' WHERE lead_id=?",
                        (lead["lead_id"],),
                    )
                    conn.commit()
                    warnings += 1
            log(f"{lane}: {lead['lead_id']} ({lead['type']}) — {reason}")

        # log aggregate metric to system_history
        if not dry_run and sum(counts.values()) > 0:
            try:
                from processors.history_log import log_event
                log_event(
                    actor="cron_lead_triage",
                    kind="metric",
                    scope="lead_pipeline",
                    title=f"triage pass: {counts['AUTO_SAFE_EXEC']}exec / "
                          f"{counts['SUBAGENT_DISPATCH']}sub / "
                          f"{counts['STRATEGIST_DISPATCH']}strat / "
                          f"{counts['AUTO_SCHEDULE']}sched / "
                          f"{counts['BOSS_ESCALATE']}esc / "
                          f"{counts['CLOSE_AS_NOISE']}noise",
                    body=f"counts={counts}",
                )
                if warnings > 0:
                    log_event(
                        actor="cron_lead_triage",
                        kind="warning",
                        scope="lead_pipeline",
                        title=f"{warnings} leads → BOSS_ESCALATE this pass",
                        body=f"check `py scripts/leads.py escalated` to review",
                    )
            except Exception as e:
                log(f"history_log fail: {type(e).__name__}: {e}")
            if warnings > 0:
                try:
                    QUEUE_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "briefs" / "queue"
                    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
                    ts = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
                    q = QUEUE_DIR / f"pending_{ts}_lead_escalate_fyi.md"
                    strat = counts['STRATEGIST_DISPATCH']
                    q.write_text(
                        f"[小主管 FYI] Lead 分流完成（{warnings} 條需你看）\n\n"
                        f"• 自動執行 {counts['AUTO_SAFE_EXEC']} 條、排程 {counts['AUTO_SCHEDULE']} 條"
                        f" → 小主管自主處理\n"
                        + (f"• 策略長處理 {strat} 條 → agent_strategy_change 已路由給策略長，不需你介入\n"
                           if strat else "")
                        + f"• 上呈 {counts['BOSS_ESCALATE']} 條 → 超出策略長授權範圍，需你決策"
                        f"（`py scripts/leads.py escalated`）\n"
                        f"• 噪音關閉 {counts['CLOSE_AS_NOISE']} 條 → 無商業價值，已關閉\n\n"
                        f"小主管已自主處理非上呈部分，不需行動。",
                        encoding="utf-8",
                    )
                except Exception as e:
                    log(f"brief_queue write fail: {type(e).__name__}: {e}")

        counts["total"] = sum(v for k, v in counts.items() if k != "total")
        log(f"pass: {counts}")
        return counts
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run_pass(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
