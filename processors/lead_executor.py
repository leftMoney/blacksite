"""processors/lead_executor.py — execute triaged kb_leads (P2 + P3).

Cron */30 min via blacksite_daemon. Reads kb_leads WHERE state='triaged' and
dispatches by triage_lane:

  AUTO_SAFE_EXEC  → run handler matching lead.type, write evidence, state→executed
  AUTO_SCHEDULE   → write entry to runtime/lead_cron_schedule.jsonl, state→executed
  SUBAGENT_DISPATCH → write task file to runtime/lead_subagent_queue/<lead_id>.task,
                      state→executed (boss / next session picks up from queue)
  BOSS_ESCALATE   → already 'escalated' from triage; skip
  CLOSE_AS_NOISE  → already 'resolved_closed' from triage; skip

P2 handlers:
  sql_sample        — parse target ('chat_username=X' / 'entity:X'), query messages,
                      capture rows to evidence JSON
  whois_lookup      — parse target ('domain:X'), subprocess whois, capture output
  tier_upgrade      — parse target ('entity:X'), update entities.tier, log prev_tier
  code_fix_regex    — allowlist-only (LINE ID format / Bigo numeric filter); else escalate
  card_builder_check — verify card_builder cron last_run_at + last_card

Per-handler timeout 60s; 1 concurrent execution per type.
Per CLAUDE.md §10 destructive ops gate: tier_upgrade reversible (logs prev_tier);
code_fix_regex allowlist-only refuses unknown patterns.
Per CLAUDE.md §6.4: timestamps ISO 8601 with +07:00.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
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
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SUBAGENT_QUEUE_DIR = RUNTIME_DIR / "lead_subagent_queue"
SUBAGENT_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
LEAD_CRON_SCHEDULE_PATH = RUNTIME_DIR / "lead_cron_schedule.jsonl"

PER_HANDLER_TIMEOUT_S = 60
MAX_PER_TYPE_PER_PASS = 5  # bound work per pass; rest picked up next cron

# Allowlist for code_fix_regex (boss-safe specific patterns only).
# Boss must add new keys here explicitly — handler refuses anything unknown.
CODE_FIX_REGEX_ALLOWLIST = {
    "lineid_extractor",   # add ^[a-zA-Z0-9._-]{4,20}$ filter, exclude http/https/t.me
    "bigo_numeric_filter",  # bigo numeric ID min seen_count threshold
}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [lead_exec] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"lead_executor_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _hist(kind: str, title: str, body: str | None = None,
          refs: list | None = None, parent_id: int | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(
            actor="cron_lead_executor", kind=kind, scope="lead_pipeline",
            title=title[:118], body=body, refs=refs, parent_id=parent_id,
        )
    except Exception as e:
        log(f"history_log fail: {type(e).__name__}: {e}")
        return -1


# --------------------------------------------------------------------
# Target parsing
# --------------------------------------------------------------------

def parse_target(target: str | None) -> tuple[str, str]:
    """Parse 'kind:value' or 'key=value' targets. Returns (kind, value).
    Examples:
      'chat_username=Tinapoipet07' → ('chat_username', 'Tinapoipet07')
      'entity:examplebet'              → ('entity', 'examplebet')
      'domain:examplebrand.me'             → ('domain', 'examplebrand.me')
      'agent:bigo'                 → ('agent', 'bigo')
      'regex:lineid_extractor'     → ('regex', 'lineid_extractor')
      'cron:card_builder'          → ('cron', 'card_builder')
    """
    if not target:
        return ("", "")
    if "=" in target:
        k, _, v = target.partition("=")
        return (k.strip(), v.strip())
    if ":" in target:
        k, _, v = target.partition(":")
        return (k.strip(), v.strip())
    return ("", target.strip())


# --------------------------------------------------------------------
# AUTO_SAFE_EXEC handlers
# --------------------------------------------------------------------

def handle_sql_sample(lead: dict, conn) -> dict:
    """Read-only sample query against messages table. Always SELECT, always LIMIT.
    target forms: 'chat_username=X' / 'entity:X' / 'sender_username=X'.
    Returns evidence dict: {rows, query, count}."""
    kind, value = parse_target(lead.get("target"))
    if not value:
        return {"error": "empty target"}
    # Build read-only query — never execute analyst's `suggested_action` verbatim
    # (security: SQL injection / boss-side audit).
    if kind in ("chat_username", "entity"):
        q = ("SELECT ts, chat_username, sender_username, text "
             "FROM messages WHERE chat_username=? "
             "ORDER BY ts DESC LIMIT 5")
        params = (value,)
    elif kind == "sender_username":
        q = ("SELECT ts, chat_username, sender_username, text "
             "FROM messages WHERE sender_username=? "
             "ORDER BY ts DESC LIMIT 5")
        params = (value,)
    else:
        return {"error": f"unsupported target kind {kind!r} for sql_sample"}
    try:
        rows = conn.execute(q, params).fetchall()
        out = {
            "query": q,
            "params": list(params),
            "row_count": len(rows),
            "rows": [
                {"ts": r["ts"], "chat_username": r["chat_username"],
                 "sender_username": r["sender_username"],
                 "text": (r["text"] or "")[:500]}
                for r in rows
            ],
        }
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9.\-]{3,253}$")


def handle_whois_lookup(lead: dict, conn) -> dict:
    """Run system whois on target domain. Parses common fields.
    target form: 'domain:X'."""
    kind, value = parse_target(lead.get("target"))
    if kind != "domain" or not value:
        return {"error": f"expected target=domain:<X> got kind={kind!r} value={value!r}"}
    if not _DOMAIN_RE.match(value):
        return {"error": f"domain {value!r} format invalid"}
    if shutil.which("whois") is None:
        return {"error": "whois binary not in PATH; install via package manager"}
    # Suppress console window pop: whois.exe is console subsystem.
    no_window_kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = subprocess.run(
            ["whois", value],
            capture_output=True, text=True, timeout=PER_HANDLER_TIMEOUT_S,
            encoding="utf-8", errors="replace",
            **no_window_kw,
        )
        raw = proc.stdout or ""
        # Best-effort field extraction
        def find(pat):
            m = re.search(pat, raw, re.IGNORECASE)
            return m.group(1).strip() if m else None
        return {
            "domain": value,
            "rc": proc.returncode,
            "registrar": find(r"Registrar:\s*(.+)"),
            "creation_date": find(r"Creation Date:\s*(.+)"),
            "registry_expiry": find(r"Registry Expiry Date:\s*(.+)"),
            "name_servers": re.findall(r"Name Server:\s*(.+)", raw, re.IGNORECASE)[:5],
            "raw_head": raw[:1500],
        }
    except subprocess.TimeoutExpired:
        return {"error": f"whois timed out after {PER_HANDLER_TIMEOUT_S}s"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def handle_tier_upgrade(lead: dict, conn) -> dict:
    """Update entities.tier. Logs prev_tier for reversibility audit.
    target: 'entity:X' (matches entities.name; if multiple kinds exist, all updated).
    suggested_action parsed for new_tier (yolk/white/shell)."""
    kind, value = parse_target(lead.get("target"))
    if kind != "entity" or not value:
        return {"error": f"expected target=entity:<X> got kind={kind!r} value={value!r}"}
    action = (lead.get("suggested_action") or "").lower()
    # Reject if marked destructive in lead
    if (lead.get("reversibility") or "").lower() == "destructive":
        return {"error": "tier_upgrade with reversibility=destructive must boss_escalate; refusing auto-exec"}
    new_tier = None
    if "yolk" in action or "蛋黃" in action:
        new_tier = "yolk"
    elif "white" in action or "蛋白" in action:
        new_tier = "white"
    elif "shell" in action or "蛋殼" in action:
        new_tier = "shell"
    else:
        return {"error": f"could not parse new tier from suggested_action: {action[:120]!r}"}

    rows = conn.execute(
        "SELECT row_id, kind, name, tier FROM entities WHERE name = ?",
        (value,),
    ).fetchall()
    if not rows:
        return {"error": f"no entity found with name={value!r}"}
    updates = []
    for r in rows:
        prev = r["tier"]
        conn.execute(
            "UPDATE entities SET tier=? WHERE row_id=?", (new_tier, r["row_id"]),
        )
        updates.append({
            "row_id": r["row_id"], "kind": r["kind"], "name": r["name"],
            "prev_tier": prev, "new_tier": new_tier,
        })
    conn.commit()
    return {
        "name": value, "new_tier": new_tier, "updated_count": len(updates),
        "updates": updates,
        "reversibility_audit": "to revert: UPDATE entities SET tier=<prev_tier> WHERE row_id=<id>",
    }


def handle_code_fix_regex(lead: dict, conn) -> dict:
    """Strict allowlist. If lead's regex name not in allowlist → refuse.
    This handler does NOT modify code; it writes a structured spec to the
    subagent_queue dir for boss / next session to apply."""
    kind, value = parse_target(lead.get("target"))
    if kind != "regex" or not value:
        return {"error": f"expected target=regex:<name> got kind={kind!r} value={value!r}"}
    if value not in CODE_FIX_REGEX_ALLOWLIST:
        return {
            "refused": True,
            "reason": f"regex name {value!r} not in CODE_FIX_REGEX_ALLOWLIST {sorted(CODE_FIX_REGEX_ALLOWLIST)!r}",
            "remediation": "boss must add to allowlist in processors/lead_executor.py before this lead can auto-exec",
        }
    # Write spec to subagent queue (apply via separate session)
    spec_path = SUBAGENT_QUEUE_DIR / f"{lead['lead_id']}.task"
    spec_path.write_text(
        json.dumps({
            "lead_id": lead["lead_id"],
            "type": "code_fix_regex",
            "regex_name": value,
            "suggested_action": lead.get("suggested_action"),
            "queued_at": now_iso(),
            "note": "code_fix_regex queued for boss / next session pickup",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "queued": True, "spec_path": spec_path.relative_to(ROOT).as_posix(),
        "regex_name": value,
    }


def handle_card_builder_check(lead: dict, conn) -> dict:
    """Verify card_builder cron is running: check most recent card last_built_at."""
    try:
        row = conn.execute(
            """SELECT MAX(last_built_at) as last_built,
                      COUNT(*) as total
                 FROM cards WHERE state='active'"""
        ).fetchone()
        if not row or not row["last_built"]:
            return {"healthy": False, "reason": "no active cards in DB"}
        last_built = row["last_built"]
        # Stale if > 8h since last build
        try:
            last_dt = datetime.fromisoformat(last_built)
            age_h = (datetime.now(TZ) - last_dt).total_seconds() / 3600
        except Exception:
            age_h = -1
        return {
            "healthy": age_h < 8.0 and age_h >= 0,
            "last_built_at": last_built,
            "age_hours": round(age_h, 2),
            "total_active_cards": row["total"],
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


HANDLERS = {
    "sql_sample": handle_sql_sample,
    "whois_lookup": handle_whois_lookup,
    "tier_upgrade": handle_tier_upgrade,
    "code_fix_regex": handle_code_fix_regex,
    "card_builder_check": handle_card_builder_check,
}


# --------------------------------------------------------------------
# Lane dispatchers
# --------------------------------------------------------------------

def exec_auto_safe(lead: dict, conn) -> tuple[str, dict]:
    """Returns (next_state, evidence_dict)."""
    handler = HANDLERS.get(lead["type"])
    if not handler:
        # type-rule mismatch — defer to boss
        return ("escalated", {"error": f"no handler for type={lead['type']!r}"})
    try:
        evidence = handler(lead, conn)
    except Exception as e:
        evidence = {"error": f"handler crash: {type(e).__name__}: {e}"}
    if evidence.get("error"):
        # Handler-side rejection → re-route to boss
        return ("escalated", evidence)
    if evidence.get("refused"):
        return ("escalated", evidence)
    return ("executed", evidence)


def exec_auto_schedule(lead: dict, conn) -> tuple[str, dict]:
    """Append a record to runtime/lead_cron_schedule.jsonl. Daemon picks up
    on next restart; boss can also `cat` the file to see queue."""
    rec = {
        "lead_id": lead["lead_id"],
        "type": lead["type"],
        "target": lead.get("target"),
        "suggested_action": lead.get("suggested_action"),
        "queued_at": now_iso(),
    }
    try:
        with LEAD_CRON_SCHEDULE_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return ("executed", {"queued_to": LEAD_CRON_SCHEDULE_PATH.relative_to(ROOT).as_posix(),
                              "rec": rec, "note": "daemon picks up on next restart"})
    except Exception as e:
        return ("escalated", {"error": f"write schedule fail: {type(e).__name__}: {e}"})


def exec_subagent_dispatch(lead: dict, conn) -> tuple[str, dict]:
    """Write a stub task file to subagent_queue. Real subagent spawn requires
    Claude Code session context — boss / next session handles."""
    spec = {
        "lead_id": lead["lead_id"],
        "type": lead["type"],
        "target": lead.get("target"),
        "suggested_action": lead.get("suggested_action"),
        "queued_at": now_iso(),
        "note": "SUBAGENT_DISPATCH — boss / next Claude Code session should pick up",
    }
    spec_path = SUBAGENT_QUEUE_DIR / f"{lead['lead_id']}.task"
    try:
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        # signal a warning so brief / dashboard surfaces this
        _hist("warning",
              f"subagent task queued: {lead['lead_id']} ({lead['type']})",
              body=f"task spec at {spec_path.relative_to(ROOT).as_posix()}",
              refs=[spec_path.relative_to(ROOT).as_posix(), f"lead:{lead['lead_id']}"])
        return ("executed", {"spec_path": spec_path.relative_to(ROOT).as_posix(),
                              "note": "queued for boss/subagent pickup"})
    except Exception as e:
        return ("escalated", {"error": f"queue write fail: {type(e).__name__}: {e}"})


# --------------------------------------------------------------------
# Main pass
# --------------------------------------------------------------------

def run_pass(dry_run: bool = False) -> dict:
    conn = get_connection()
    counts_by_type: dict[str, int] = {}
    try:
        cur = conn.execute(
            """SELECT lead_id, type, target, suggested_action, confidence,
                      actionability, reversibility, auto_safe, triage_lane
                 FROM kb_leads
                WHERE state='triaged'
                  AND triage_lane IN ('AUTO_SAFE_EXEC','AUTO_SCHEDULE','SUBAGENT_DISPATCH')
                ORDER BY emitted_at"""
        )
        cols = [d[0] for d in cur.description]
        leads = [dict(zip(cols, r)) for r in cur.fetchall()]

        if not leads:
            log("no triaged leads to execute")
            return {"total": 0}

        executed = 0
        escalated = 0
        ts = now_iso()
        for lead in leads:
            t = lead["type"] or ""
            if counts_by_type.get(t, 0) >= MAX_PER_TYPE_PER_PASS:
                log(f"  cap hit for type={t}; deferring {lead['lead_id']} to next pass")
                continue
            counts_by_type[t] = counts_by_type.get(t, 0) + 1

            lane = lead["triage_lane"]
            if dry_run:
                log(f"DRY {lane} {lead['lead_id']} ({t}) target={lead['target']}")
                continue

            # state='executing' marker (for crash recovery)
            try:
                conn.execute(
                    "UPDATE kb_leads SET state='executing' WHERE lead_id=?",
                    (lead["lead_id"],),
                )
                conn.commit()
            except Exception as e:
                log(f"FAIL set executing on {lead['lead_id']}: {e}")
                continue

            try:
                if lane == "AUTO_SAFE_EXEC":
                    next_state, evidence = exec_auto_safe(lead, conn)
                elif lane == "AUTO_SCHEDULE":
                    next_state, evidence = exec_auto_schedule(lead, conn)
                elif lane == "SUBAGENT_DISPATCH":
                    next_state, evidence = exec_subagent_dispatch(lead, conn)
                else:
                    next_state, evidence = ("escalated", {"error": f"unknown lane {lane!r}"})
            except Exception as e:
                next_state = "escalated"
                evidence = {"error": f"executor crash: {type(e).__name__}: {e}"}

            try:
                conn.execute(
                    """UPDATE kb_leads
                          SET state=?, evidence=?
                        WHERE lead_id=?""",
                    (next_state, json.dumps(evidence, ensure_ascii=False), lead["lead_id"]),
                )
                conn.commit()
            except Exception as e:
                log(f"FAIL persist evidence on {lead['lead_id']}: {e}")

            if next_state == "executed":
                executed += 1
                _hist("milestone",
                      f"executed lead {lead['lead_id']} ({t})",
                      body=f"lane={lane}\nevidence_head={json.dumps(evidence, ensure_ascii=False)[:400]}",
                      refs=[f"lead:{lead['lead_id']}"])
            else:
                escalated += 1
                _hist("warning",
                      f"escalated lead {lead['lead_id']} ({t}) — {evidence.get('error','refused')[:60]}",
                      body=f"lane={lane}\nevidence={json.dumps(evidence, ensure_ascii=False)[:600]}",
                      refs=[f"lead:{lead['lead_id']}"])
            log(f"{next_state}: {lead['lead_id']} ({t}) lane={lane}")

        log(f"pass: executed={executed} escalated={escalated} total={executed+escalated}")
        return {"executed": executed, "escalated": escalated,
                "total": executed + escalated}
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    run_pass(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
