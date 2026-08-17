"""processors/agent_incidents.py — Field Agent incident lifecycle.

Per CLAUDE.md §15 incident workflow + boss 5/2 Q5 lock: KPI violations DO
NOT auto-pause / auto-burn agents. Open incident → chain review → escalate
if unresolved.

State machine (per SECTION_CHIEF.md §15.3):
  open → in_review → {resolved | abandoned | escalated_strategist}
  escalated_strategist → {resolved | escalated_boss}
  escalated_boss → {resolved | abandoned}

Auto-escalation: incidents in state=in_review > 7 days auto-transition to
escalated_strategist (cron daily 03:00).

CLI:
  py processors/agent_incidents.py ls [--state X] [--agent Y]
  py processors/agent_incidents.py show <inc_id>
  py processors/agent_incidents.py open <agent_id> <kind> --hypothesis "..." [--severity X]
  py processors/agent_incidents.py transition <inc_id> <new_state> [--note "..."]
  py processors/agent_incidents.py escalate-aged [--days 7]
  py processors/agent_incidents.py pending-review

Per CLAUDE.md §6.4: timestamps ISO 8601 with +07:00.
Per CLAUDE.md §13.6: log_event 'milestone' on transitions; 'warning' on
new incidents + boss escalations.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INCIDENTS_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "agent_incidents"
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

VALID_STATES = {
    "open", "in_review", "resolved", "abandoned",
    "escalated_strategist", "escalated_boss",
}

# Allowed transitions (from → set of valid to-states)
ALLOWED_TRANSITIONS = {
    "open": {"in_review", "abandoned", "escalated_strategist"},
    "in_review": {"resolved", "abandoned", "escalated_strategist"},
    "escalated_strategist": {"resolved", "escalated_boss", "abandoned"},
    "escalated_boss": {"resolved", "abandoned"},
    "resolved": set(),     # terminal
    "abandoned": set(),    # terminal
}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [agent_incidents] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"agent_incidents_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _hist(kind: str, title: str, body: str | None = None,
          refs: list | None = None, parent_id: int | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(
            actor="cron_agent_incidents", kind=kind, scope="incidents",
            title=title[:118], body=body, refs=refs, parent_id=parent_id,
        )
    except Exception as e:
        log(f"history_log fail: {type(e).__name__}: {e}")
        return -1


# ---------------------------------------------------------------------------
# YAML frontmatter parse / write
# ---------------------------------------------------------------------------

_FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def parse_incident(path: Path) -> dict | None:
    """Parse an incident MD into {frontmatter_dict, body_str}."""
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None
    m = _FM_RE.match(raw)
    if not m:
        return None
    import yaml
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    return {"frontmatter": fm, "body": m.group(2), "path": path}


def write_incident(path: Path, frontmatter: dict, body: str) -> None:
    import yaml
    fm_str = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False)
    path.write_text(f"---\n{fm_str}---\n{body}", encoding="utf-8")


def list_incidents(state: str | None = None, agent_id: str | None = None) -> list[dict]:
    out = []
    for p in sorted(INCIDENTS_DIR.glob("INC-*.md")):
        inc = parse_incident(p)
        if not inc:
            continue
        fm = inc["frontmatter"]
        if state and fm.get("state") != state:
            continue
        if agent_id and fm.get("agent_id") != agent_id:
            continue
        out.append(inc)
    return out


# ---------------------------------------------------------------------------
# Public API: open / transition / pending / escalate-aged
# ---------------------------------------------------------------------------

def _next_id(date_str: str) -> str:
    pat = f"INC-{date_str}-"
    existing = [p.stem for p in INCIDENTS_DIR.glob(f"{pat}*.md")]
    nums = []
    for stem in existing:
        m = re.search(r"-(\d+)$", stem)
        if m:
            try:
                nums.append(int(m.group(1)))
            except ValueError:
                pass
    nxt = (max(nums) + 1) if nums else 1
    return f"{pat}{nxt:03d}"


def open_incident(agent_id: str, kind: str, hypothesis: str,
                  evidence: list[str] | None = None,
                  severity: str = "yellow",
                  parent_incident: str | None = None) -> str:
    """Create a new incident. Returns incident_id."""
    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    inc_id = _next_id(date_str)
    path = INCIDENTS_DIR / f"{inc_id}.md"
    fm = {
        "incident_id": inc_id,
        "opened_at": now_iso(),
        "opened_by": "SECTION_CHIEF",
        "agent_id": agent_id,
        "state": "open",
        "violation_kind": kind,
        "severity": severity,
        "parent_incident": parent_incident,
        "transitions": [],
    }
    body = (
        f"# {inc_id} — {agent_id} {kind}\n\n"
        f"## What happened\n"
        + ("\n".join(f"- {e}" for e in (evidence or [])) or "- (evidence pending)") + "\n\n"
        f"## Hypothesis\n{hypothesis}\n\n"
        f"## Action so far\n- Opened by `processors/agent_incidents.py open` at {now_iso()}\n\n"
        f"## Next\n- 小主管 review: confirm hypothesis, issue corrective directive\n"
        f"- If unresolved 7d: auto-escalate to 策略長\n"
    )
    write_incident(path, fm, body)
    _hist("warning",
          f"incident opened {inc_id}: {agent_id} {kind}",
          body=f"severity={severity}\nhypothesis={hypothesis[:300]}",
          refs=[f"agent:{agent_id}", path.relative_to(ROOT).as_posix()])
    log(f"opened {inc_id} agent={agent_id} kind={kind} severity={severity}")
    return inc_id


def transition(incident_id: str, new_state: str, note: str | None = None,
               actor: str = "SECTION_CHIEF") -> bool:
    """Move incident to new_state. Validates against ALLOWED_TRANSITIONS."""
    if new_state not in VALID_STATES:
        log(f"invalid new_state {new_state!r}; valid={sorted(VALID_STATES)}")
        return False
    path = INCIDENTS_DIR / f"{incident_id}.md"
    inc = parse_incident(path)
    if not inc:
        log(f"incident {incident_id} not found at {path}")
        return False
    fm = inc["frontmatter"]
    cur = fm.get("state", "open")
    allowed = ALLOWED_TRANSITIONS.get(cur, set())
    if new_state not in allowed:
        log(f"invalid transition {cur} → {new_state} for {incident_id} (allowed: {sorted(allowed)})")
        return False
    fm["state"] = new_state
    fm.setdefault("transitions", []).append({
        "at": now_iso(), "from": cur, "to": new_state,
        "by": actor, "note": note,
    })

    # Append a transition section to body
    section = f"\n## Transition {now_iso()}\n- {cur} → **{new_state}** by {actor}\n"
    if note:
        section += f"- note: {note}\n"
    body = inc["body"] + section
    write_incident(path, fm, body)

    is_escalation = new_state in ("escalated_strategist", "escalated_boss")
    kind = "warning" if is_escalation else "milestone"
    _hist(kind,
          f"incident {incident_id} {cur} → {new_state}",
          body=f"agent_id={fm.get('agent_id')}\nactor={actor}\nnote={note}",
          refs=[f"incident:{incident_id}", path.relative_to(ROOT).as_posix()])
    log(f"transition {incident_id}: {cur} → {new_state} by {actor}")
    return True


def pending_review_for_chief() -> list[dict]:
    """List incidents in state=in_review (Section Chief queue)."""
    return list_incidents(state="in_review")


def escalate_aged(days: int = 7) -> int:
    """Auto-transition in_review incidents older than N days to escalated_strategist."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat(timespec="seconds")
    n = 0
    # Both 'open' and 'in_review' should escalate if aged (open shouldn't sit either)
    for state in ("open", "in_review"):
        for inc in list_incidents(state=state):
            opened = inc["frontmatter"].get("opened_at", "")
            if opened and opened < cutoff:
                ok = transition(
                    inc["frontmatter"]["incident_id"],
                    "escalated_strategist",
                    note=f"auto-escalated by escalate-aged (>{days}d in {state})",
                    actor="cron_escalate_aged",
                )
                if ok:
                    n += 1
    if n:
        _hist("warning",
              f"auto-escalated {n} aged incidents to strategist",
              body=f"days={days} cutoff={cutoff}")
    log(f"escalate-aged: escalated {n} incident(s) older than {days}d")
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_ls(args) -> int:
    incs = list_incidents(state=args.state, agent_id=args.agent)
    if not incs:
        print("(no matching incidents)")
        return 0
    for inc in incs:
        fm = inc["frontmatter"]
        print(
            f"{fm['incident_id']:<22}  {fm.get('opened_at','-')[:19]}  "
            f"[{fm.get('state','-'):<22}]  {fm.get('agent_id','-'):<24}  "
            f"{fm.get('violation_kind','-')}"
        )
    print(f"\n  ({len(incs)} rows)")
    return 0


def cmd_show(args) -> int:
    path = INCIDENTS_DIR / f"{args.inc_id}.md"
    inc = parse_incident(path)
    if not inc:
        print(f"incident {args.inc_id} not found at {path}")
        return 1
    print(f"--- {args.inc_id} ---")
    fm = inc["frontmatter"]
    for k, v in fm.items():
        print(f"{k:<18}: {v}")
    print()
    print(inc["body"])
    return 0


def cmd_open(args) -> int:
    inc_id = open_incident(
        agent_id=args.agent_id,
        kind=args.kind,
        hypothesis=args.hypothesis,
        evidence=args.evidence,
        severity=args.severity,
        parent_incident=args.parent,
    )
    print(inc_id)
    return 0


def cmd_transition(args) -> int:
    ok = transition(args.inc_id, args.new_state, note=args.note,
                    actor=args.actor or "SECTION_CHIEF")
    return 0 if ok else 1


def cmd_escalate_aged(args) -> int:
    n = escalate_aged(days=args.days)
    print(f"escalated {n}")
    return 0


def cmd_pending_review(args) -> int:
    incs = pending_review_for_chief()
    if not incs:
        print("(no incidents in_review)")
        return 0
    for inc in incs:
        fm = inc["frontmatter"]
        print(f"{fm['incident_id']:<22}  {fm.get('agent_id','-'):<24}  "
              f"{fm.get('violation_kind','-')}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Field Agent incident lifecycle")
    sub = p.add_subparsers(dest="cmd")

    p_ls = sub.add_parser("ls", help="list incidents")
    p_ls.add_argument("--state", default=None)
    p_ls.add_argument("--agent", default=None)
    p_ls.set_defaults(func=cmd_ls)

    p_show = sub.add_parser("show", help="show incident body")
    p_show.add_argument("inc_id")
    p_show.set_defaults(func=cmd_show)

    p_open = sub.add_parser("open", help="open new incident")
    p_open.add_argument("agent_id")
    p_open.add_argument("kind")
    p_open.add_argument("--hypothesis", required=True)
    p_open.add_argument("--evidence", action="append", default=None)
    p_open.add_argument("--severity", default="yellow",
                        choices=["yellow", "red"])
    p_open.add_argument("--parent", default=None)
    p_open.set_defaults(func=cmd_open)

    p_t = sub.add_parser("transition", help="change incident state")
    p_t.add_argument("inc_id")
    p_t.add_argument("new_state", choices=sorted(VALID_STATES))
    p_t.add_argument("--note", default=None)
    p_t.add_argument("--actor", default=None)
    p_t.set_defaults(func=cmd_transition)

    p_ea = sub.add_parser("escalate-aged", help="auto-escalate aged in_review/open incidents")
    p_ea.add_argument("--days", type=int, default=7)
    p_ea.set_defaults(func=cmd_escalate_aged)

    p_pr = sub.add_parser("pending-review", help="list incidents awaiting chief review")
    p_pr.set_defaults(func=cmd_pending_review)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
