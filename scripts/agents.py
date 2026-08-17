"""scripts/agents.py — boss/main-session Field Agent inspection CLI.

Per CLAUDE.md §15 3-tier organization. Boss + main session inspect Field
Agent fleet status, KPI yamls, recent directives, incident history.

Subcommands:
  ls                                     list all Field Agents with status
  show <agent_id>                        full KPI yaml + recent directives + incident history
  kpi <agent_id> --target K=V [...]      manual target_kpi override (writes audit trail)
  incidents [--state X]                  alias for processors/agent_incidents.py ls
  hierarchy                              print 3-tier org chart with current populations
  memory <agent_id> [--compact]          cat agent memory file (or compact)
  chief create <id> [--scope-tags S]     create new SECTION_CHIEF_<id> (multi-chief)
                  [--manages A,B,C]
  chief dissolve <id> [--reassign-to X]  archive chief, reassign managed agents
  chief reassign <agent> --to <chief>    move an agent to another chief

Boss invocation patterns:
  py scripts/agents.py ls
  py scripts/agents.py show P03_Bigo
  py scripts/agents.py kpi oneD_anon --target msg_yield_baseline_24h=80
  py scripts/agents.py incidents --state in_review
  py scripts/agents.py hierarchy
  py scripts/agents.py memory P03_Bigo
  py scripts/agents.py chief create grey_gambling_chief --scope-tags tg,bigo --manages P01_TG,P02_TG
  py scripts/agents.py chief reassign P03_Bigo --to grey_gambling_chief

Per CLAUDE.md §6.4: timestamps ISO 8601 with +07:00.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
KPI_DIR = RUNTIME_DIR / "agent_kpi"
INCIDENTS_DIR = RUNTIME_DIR / "agent_incidents"
MEMORY_DIR = RUNTIME_DIR / "agent_memory"
DIGEST_DIR = RUNTIME_DIR / "strategist_digest"
ARCHIVE_DIR = RUNTIME_DIR / "agent_memory_archive"
BASELINE_PATH = ROOT / "instances" / ACTIVE_INSTANCE / "policy" / "agent_kpi_baseline.yaml"
DEFAULT_CHIEF = "SECTION_CHIEF"

_TIER_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _read_yaml(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _write_yaml(p: Path, data: dict) -> None:
    p.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _load_baseline() -> dict:
    return _read_yaml(BASELINE_PATH)


# ---------------------------------------------------------------------------
# ls — list all field agents with current status
# ---------------------------------------------------------------------------

def cmd_ls(args) -> int:
    base = _load_baseline()
    agents = (base.get("field_agent") or {})
    if not agents:
        print(f"(no agents in {BASELINE_PATH})")
        return 0
    print(f"{'agent_id':<24} {'sub_class':<16} {'status':<8} {'yield_24h':<12} {'last_eval':<22} {'notes'}")
    print("-" * 110)
    for agent_id in sorted(agents.keys()):
        kpi_path = KPI_DIR / f"{agent_id}.yaml"
        kpi = _read_yaml(kpi_path)
        sub_class = kpi.get("sub_class") or agents[agent_id].get("sub_class", "persona_driven")
        status = kpi.get("status") or "(none)"
        emoji = _TIER_EMOJI.get(status, "⬜")
        yld = (kpi.get("current_kpi") or {}).get("msg_yield_24h", "-")
        target_yld = (kpi.get("target_kpi") or agents[agent_id]).get("msg_yield_baseline_24h", "-")
        last = (kpi.get("last_evaluated_at") or "-")[:19]
        notes = (kpi.get("notes") or agents[agent_id].get("notes") or "")[:40]
        print(f"{agent_id:<24} {sub_class:<16} {emoji} {status:<6} "
              f"{str(yld) + '/' + str(target_yld):<12} {last:<22} {notes}")
    return 0


# ---------------------------------------------------------------------------
# show — full body of one agent
# ---------------------------------------------------------------------------

def cmd_show(args) -> int:
    agent_id = args.agent_id
    kpi_path = KPI_DIR / f"{agent_id}.yaml"
    base = _load_baseline()
    base_entry = (base.get("field_agent") or {}).get(agent_id)
    if not kpi_path.exists() and not base_entry:
        print(f"agent_id {agent_id!r} not found in baseline or runtime KPI")
        return 1
    kpi = _read_yaml(kpi_path)
    if not kpi and base_entry:
        kpi = {"agent_id": agent_id, "sub_class": base_entry.get("sub_class", "persona_driven"),
               "status": "(no eval yet)", "target_kpi": base_entry, "current_kpi": {},
               "recent_directives": [], "incident_history": []}
    print(yaml.safe_dump(kpi, allow_unicode=True, sort_keys=False, default_flow_style=False))

    # Append related incidents (from runtime/agent_incidents/)
    related = []
    if INCIDENTS_DIR.exists():
        for inc_path in sorted(INCIDENTS_DIR.glob("INC-*.md")):
            text = inc_path.read_text(encoding="utf-8", errors="replace")
            if f"agent_id: {agent_id}" in text:
                # Pull state + opened_at from frontmatter quickly
                state_line = next((l for l in text.splitlines() if l.startswith("state:")), "state: ?")
                opened_line = next((l for l in text.splitlines() if l.startswith("opened_at:")), "opened_at: ?")
                related.append(f"{inc_path.stem:<22} {state_line.strip()}  {opened_line.strip()}")
    if related:
        print("--- related incidents ---")
        for r in related:
            print(r)
    else:
        print("--- no related incidents ---")
    return 0


# ---------------------------------------------------------------------------
# kpi — manual target override (writes audit trail to target_kpi_history)
# ---------------------------------------------------------------------------

def cmd_kpi(args) -> int:
    agent_id = args.agent_id
    kpi_path = KPI_DIR / f"{agent_id}.yaml"
    if not kpi_path.exists():
        # Bootstrap from baseline
        base = _load_baseline()
        base_entry = (base.get("field_agent") or {}).get(agent_id)
        if not base_entry:
            print(f"agent_id {agent_id!r} not in baseline; cannot bootstrap KPI yaml")
            return 1
        kpi = {
            "agent_id": agent_id,
            "sub_class": base_entry.get("sub_class", "persona_driven"),
            "last_evaluated_at": None, "last_evaluated_by": None,
            "current_kpi": {}, "target_kpi": dict(base_entry),
            "status": "green", "notes": "(bootstrapped via scripts/agents.py kpi)",
            "recent_directives": [], "incident_history": [], "target_kpi_history": [],
        }
    else:
        kpi = _read_yaml(kpi_path)

    if not args.target:
        print("no --target K=V specified")
        return 1

    target_kpi = kpi.setdefault("target_kpi", {})
    history = kpi.setdefault("target_kpi_history", [])
    changes = []
    for kv in args.target:
        if "=" not in kv:
            print(f"bad --target {kv!r}; expected K=V")
            return 1
        k, _, v = kv.partition("=")
        k, v = k.strip(), v.strip()
        # type-coerce: int / float / bool / str
        try:
            v_typed: object = int(v)
        except ValueError:
            try:
                v_typed = float(v)
            except ValueError:
                if v.lower() in ("true", "false"):
                    v_typed = (v.lower() == "true")
                else:
                    v_typed = v
        prev = target_kpi.get(k)
        target_kpi[k] = v_typed
        history.append({
            "changed_at": now_iso(),
            "changed_by": args.actor or "boss_via_agents_cli",
            "field": k, "from": prev, "to": v_typed,
            "reason": args.reason or "(manual override via scripts/agents.py kpi)",
        })
        changes.append((k, prev, v_typed))

    _write_yaml(kpi_path, kpi)

    # log to system_history
    try:
        from processors.history_log import log_event
        body_lines = [f"  {k}: {prev!r} → {new!r}" for k, prev, new in changes]
        log_event(
            actor=args.actor or "boss_via_agents_cli",
            kind="config_change",
            scope="kpi",
            title=f"agent_kpi: {agent_id} target_kpi changed ({len(changes)} field(s))",
            body=("changes:\n" + "\n".join(body_lines) +
                  f"\nreason: {args.reason or '(none)'}"),
            refs=[f"agent:{agent_id}", kpi_path.relative_to(ROOT).as_posix()],
        )
    except Exception as e:
        print(f"⚠ history_log fail: {type(e).__name__}: {e}")

    print(f"OK · {agent_id} target_kpi updated:")
    for k, prev, new in changes:
        print(f"  {k}: {prev!r} → {new!r}")
    return 0


# ---------------------------------------------------------------------------
# incidents — alias for processors/agent_incidents.py ls
# ---------------------------------------------------------------------------

def cmd_incidents(args) -> int:
    try:
        from processors.agent_incidents import list_incidents
    except ImportError as e:
        print(f"agent_incidents import fail: {e}")
        return 1
    incs = list_incidents(state=args.state, agent_id=args.agent)
    if not incs:
        print("(no matching incidents)")
        return 0
    for inc in incs:
        fm = inc["frontmatter"]
        print(f"{fm.get('incident_id','-'):<22} {fm.get('opened_at','-')[:19]} "
              f"[{fm.get('state','-'):<22}] {fm.get('agent_id','-'):<24} "
              f"{fm.get('violation_kind','-')}")
    print(f"\n  ({len(incs)} rows)")
    return 0


# ---------------------------------------------------------------------------
# hierarchy — 3-tier org chart with populations
# ---------------------------------------------------------------------------

def cmd_hierarchy(args) -> int:
    base = _load_baseline()
    agents = (base.get("field_agent") or {})
    persona_driven = [a for a, c in agents.items() if c.get("sub_class") == "persona_driven"]
    anonymous_web = [a for a, c in agents.items() if c.get("sub_class") == "anonymous_web"]

    # status counts
    status_counts = {"green": 0, "yellow": 0, "red": 0, "(none)": 0}
    for a in agents:
        kpi_path = KPI_DIR / f"{a}.yaml"
        kpi = _read_yaml(kpi_path)
        st = kpi.get("status") or "(none)"
        status_counts[st] = status_counts.get(st, 0) + 1

    # incidents by state
    inc_counts = {"open": 0, "in_review": 0, "escalated_strategist": 0,
                  "escalated_boss": 0, "resolved": 0, "abandoned": 0}
    if INCIDENTS_DIR.exists():
        for p in INCIDENTS_DIR.glob("INC-*.md"):
            text = p.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                if line.startswith("state:"):
                    s = line.split(":", 1)[1].strip()
                    inc_counts[s] = inc_counts.get(s, 0) + 1
                    break

    # Multi-chief detection
    chiefs = []
    if MEMORY_DIR.exists():
        chiefs = sorted(p.stem for p in MEMORY_DIR.glob("SECTION_CHIEF*.md"))
    if not chiefs:
        chiefs = [DEFAULT_CHIEF]

    # Per-chief managed counts
    per_chief_count = {c: 0 for c in chiefs}
    for a in agents:
        kpi_path = KPI_DIR / f"{a}.yaml"
        kpi = _read_yaml(kpi_path)
        c = kpi.get("managed_by") or DEFAULT_CHIEF
        per_chief_count[c] = per_chief_count.get(c, 0) + 1

    print("Blacksite _TEMPLATE — Multi-Agent Intelligence Organization")
    print(f"({now_iso()})")
    print()
    print("┌──────────────────────────────────────────────────────────────────────┐")
    print("│ Tier 3 — 策略長 (Chief Strategist / Director of Intelligence)        │")
    print("│   1 agent · weekly Sun 21:00 GMT+7 · boss-trigger 「策略長 上工」     │")
    print("│   memory budget: 25,000 tokens                                       │")
    print("│   skill: personas/skills/CHIEF_STRATEGIST.md                         │")
    print("│   org-adjustment authority: chief_create/dissolve/reassign +         │")
    print("│                              metric_redefine + monitoring_track_open │")
    print("└────────────────────────┬─────────────────────────────────────────────┘")
    print("                         │ directives via runtime/strategy_directives/")
    print("                         │ apply: processors/strategy_directive_apply.py")
    print("                         ▼")
    print("┌──────────────────────────────────────────────────────────────────────┐")
    print(f"│ Tier 2 — 小主管 (Section Chief)  · {len(chiefs):2d} chief(s) (default 1, N supported)")
    for c in chiefs:
        print(f"│   - {c:<32} · manages {per_chief_count.get(c,0):2d} agent(s)")
    print("│   memory budget: 12,000 tokens each                                  │")
    print("│   skill: personas/skills/SECTION_CHIEF.md                            │")
    print("└────────────────────────┬─────────────────────────────────────────────┘")
    print("                         │ KPI yamls in runtime/agent_kpi/")
    print("                         │ filtered by managed_by field                ")
    print("                         ▼")
    print("┌──────────────────────────────────────────────────────────────────────┐")
    print(f"│ Tier 1 — 情報員 (Field Agents)  · {len(agents):2d} total              ")
    print(f"│   ├─ persona_driven: {len(persona_driven):2d}  ({', '.join(sorted(persona_driven)[:6])}…)")
    print(f"│   └─ anonymous_web:  {len(anonymous_web):2d}  ({', '.join(sorted(anonymous_web)[:6])}…)")
    print(f"│   Status: 🟢 {status_counts['green']:2d}  🟡 {status_counts['yellow']:2d}  🔴 {status_counts['red']:2d}  ⬜ {status_counts['(none)']:2d}")
    print("│   memory budget: 6,000 tokens each                                   │")
    print("│   skill: personas/skills/FIELD_AGENT.md (sub-classes inside)         │")
    print("└──────────────────────────────────────────────────────────────────────┘")
    print()
    print(f"Open incidents: open={inc_counts['open']} · in_review={inc_counts['in_review']} · "
          f"escalated_strategist={inc_counts['escalated_strategist']} · "
          f"escalated_boss={inc_counts['escalated_boss']}")
    print(f"Closed: resolved={inc_counts['resolved']} · abandoned={inc_counts['abandoned']}")
    print()
    print("Inspect: py scripts/agents.py ls | show <id> | kpi <id> --target K=V | incidents")
    print("         py scripts/agents.py memory <id> [--compact]")
    print("         py scripts/agents.py chief create|dissolve|reassign ...")
    return 0


# ---------------------------------------------------------------------------
# memory — cat / compact agent memory
# ---------------------------------------------------------------------------

def cmd_memory(args) -> int:
    sys.path.insert(0, str(ROOT))
    from agents._common.agent_memory import load, compact, get_budget, append_learning
    if args.compact:
        res = compact(args.agent_id, target_tokens=args.target)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.append:
        ok = append_learning(args.agent_id, args.append,
                             category=args.category or "ops",
                             boss_curated=args.boss_curated)
        print(f"appended={ok}")
        return 0
    text = load(args.agent_id)
    if not text:
        print(f"(no memory file for {args.agent_id})")
        return 1
    print(f"## budget: {get_budget(args.agent_id)} tokens (~{len(text)//2} estimated current)")
    print()
    print(text)
    return 0


# ---------------------------------------------------------------------------
# chief — multi-chief lifecycle (boss 5/3 §15.Z)
# ---------------------------------------------------------------------------

def _list_chiefs() -> list[str]:
    if not MEMORY_DIR.exists():
        return [DEFAULT_CHIEF]
    chiefs = []
    for p in sorted(MEMORY_DIR.glob("SECTION_CHIEF*.md")):
        chiefs.append(p.stem)
    return chiefs or [DEFAULT_CHIEF]


def _set_managed_by(agent_id: str, chief_id: str) -> bool:
    """Set agent's managed_by field in its KPI yaml. Returns True on success."""
    kpi_path = KPI_DIR / f"{agent_id}.yaml"
    if not kpi_path.exists():
        return False
    try:
        data = yaml.safe_load(kpi_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return False
    data["managed_by"] = chief_id
    _write_yaml(kpi_path, data)
    return True


def cmd_chief_create(args) -> int:
    chief_id = args.chief_id
    if not chief_id.startswith("SECTION_CHIEF"):
        chief_id = f"SECTION_CHIEF_{chief_id}"
    sys.path.insert(0, str(ROOT))
    from agents._common.agent_memory import write_stub, _path_for
    if _path_for(chief_id).exists():
        print(f"chief {chief_id} already exists at {_path_for(chief_id)}")
        return 1

    scope_tags = [t.strip() for t in (args.scope_tags or "").split(",") if t.strip()]
    manages = [a.strip() for a in (args.manages or "").split(",") if a.strip()]

    identity = (
        f"I am Section Chief {chief_id}. Multi-chief instance per boss 5/3 §15.Z. "
        f"Scope tags: {scope_tags or '[]'}."
    )
    job = (
        f"Daily intel synthesis + KPI eval for managed Field Agents. "
        f"Initial managed: {manages or '[]'}. Append learnings to my memory; "
        f"weekly digest to 策略長."
    )
    ok = write_stub(
        chief_id,
        tier=2,
        sub_class=None,
        identity=identity,
        job=job,
        kpi_summary=(
            "- brief library admission count (cards/day)\n"
            "- actionable lead ratio (escalated / total)\n"
            "- false-signal rate\n"
            "- cross-platform corroboration rate\n"
            "- boss adoption rate"
        ),
        capabilities=(
            f"- KB query: `py kb/query.py search|cards|entity|leads|memo|funnel|state`\n"
            f"- KPI eval: `processors/section_chief_eval.py` (filtered to my managed agents)\n"
            f"- Open incident: `py processors/agent_incidents.py open <agent_id> <kind>`\n"
            f"- Modify Field Agent KPI: `py scripts/agents.py kpi <agent_id> --target K=V`\n"
            f"- Write digest: `runtime/strategist_digest/{chief_id}_<YYYY-WW>.md`\n"
            f"- Memory: `py scripts/agents.py memory {chief_id}`"
        ),
        managed_by=None,
        scope_tags=scope_tags,
    )
    if not ok:
        print(f"failed to create chief memory")
        return 1

    # Reassign listed agents
    moved = []
    for aid in manages:
        if _set_managed_by(aid, chief_id):
            moved.append(aid)
    print(f"OK · created chief {chief_id} (scope_tags={scope_tags})")
    print(f"   reassigned {len(moved)}/{len(manages)} agents:")
    for aid in moved:
        print(f"     {aid} → managed_by={chief_id}")

    try:
        from processors.history_log import log_event
        log_event(
            actor="boss_via_agents_cli", kind="config_change", scope="org",
            title=f"chief_create {chief_id}",
            body=f"scope_tags={scope_tags}\nmanages={moved}",
            refs=[f"chief:{chief_id}"],
        )
    except Exception:
        pass
    return 0


def cmd_chief_dissolve(args) -> int:
    chief_id = args.chief_id
    if not chief_id.startswith("SECTION_CHIEF"):
        chief_id = f"SECTION_CHIEF_{chief_id}"
    if chief_id == DEFAULT_CHIEF:
        print(f"refusing to dissolve default chief {DEFAULT_CHIEF}")
        return 1
    if not args.confirm:
        print(f"chief_dissolve is destructive (boss approval required per CLAUDE.md §10).")
        print(f"re-run with --confirm to proceed.")
        return 1

    sys.path.insert(0, str(ROOT))
    from agents._common.agent_memory import _path_for
    src = _path_for(chief_id)
    if not src.exists():
        print(f"chief {chief_id} memory not found at {src}")
        return 1

    target_chief = args.reassign_to or DEFAULT_CHIEF
    # Reassign all agents currently under this chief
    moved = []
    if KPI_DIR.exists():
        for p in sorted(KPI_DIR.glob("*.yaml")):
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                continue
            if data.get("managed_by") == chief_id:
                data["managed_by"] = target_chief
                _write_yaml(p, data)
                moved.append(p.stem)

    # Archive memory
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = ARCHIVE_DIR / f"{chief_id}_dissolved_{now_iso().replace(':','-')}.md"
    archive_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    src.unlink()

    print(f"OK · dissolved {chief_id}; archive={archive_path.name}")
    print(f"   reassigned {len(moved)} agents → managed_by={target_chief}")
    for aid in moved:
        print(f"     {aid}")

    try:
        from processors.history_log import log_event
        log_event(
            actor="boss_via_agents_cli", kind="config_change", scope="org",
            title=f"chief_dissolve {chief_id} → {target_chief}",
            body=f"reassigned={moved}\narchive={archive_path}",
            refs=[f"chief:{chief_id}"],
        )
    except Exception:
        pass
    return 0


def cmd_chief_reassign(args) -> int:
    chief_id = args.to
    if not chief_id.startswith("SECTION_CHIEF"):
        chief_id = f"SECTION_CHIEF_{chief_id}"
    sys.path.insert(0, str(ROOT))
    from agents._common.agent_memory import _path_for
    if not _path_for(chief_id).exists():
        print(f"target chief {chief_id} memory does not exist")
        return 1
    if not _set_managed_by(args.agent_id, chief_id):
        print(f"agent {args.agent_id} KPI yaml not found")
        return 1
    print(f"OK · {args.agent_id} → managed_by={chief_id}")
    try:
        from processors.history_log import log_event
        log_event(
            actor="boss_via_agents_cli", kind="config_change", scope="org",
            title=f"agent_reassign {args.agent_id} → {chief_id}",
            refs=[f"agent:{args.agent_id}", f"chief:{chief_id}"],
        )
    except Exception:
        pass
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Field Agent fleet inspection (CLAUDE.md §15)")
    sub = p.add_subparsers(dest="cmd")

    p_ls = sub.add_parser("ls", help="list all Field Agents with status")
    p_ls.set_defaults(func=cmd_ls)

    p_show = sub.add_parser("show", help="show full KPI yaml + incidents for one agent")
    p_show.add_argument("agent_id")
    p_show.set_defaults(func=cmd_show)

    p_kpi = sub.add_parser("kpi", help="override target_kpi field(s) for an agent")
    p_kpi.add_argument("agent_id")
    p_kpi.add_argument("--target", action="append", required=True,
                       help="K=V (repeatable) e.g. --target msg_yield_baseline_24h=600")
    p_kpi.add_argument("--reason", default=None,
                       help="audit-trail reason for the change")
    p_kpi.add_argument("--actor", default=None,
                       help="override actor (default: boss_via_agents_cli)")
    p_kpi.set_defaults(func=cmd_kpi)

    p_inc = sub.add_parser("incidents", help="list incidents (alias for agent_incidents ls)")
    p_inc.add_argument("--state", default=None)
    p_inc.add_argument("--agent", default=None)
    p_inc.set_defaults(func=cmd_incidents)

    p_h = sub.add_parser("hierarchy", help="print 3-tier org chart with populations")
    p_h.set_defaults(func=cmd_hierarchy)

    p_mem = sub.add_parser("memory", help="cat or compact agent memory file")
    p_mem.add_argument("agent_id")
    p_mem.add_argument("--compact", action="store_true",
                       help="run LRU compaction and print summary")
    p_mem.add_argument("--target", type=int, default=None,
                       help="target token budget for compact (default: tier budget)")
    p_mem.add_argument("--append", default=None,
                       help="append a learning text to memory")
    p_mem.add_argument("--category", default=None,
                       help="learning category (ops/intel/opsec/boss_curated)")
    p_mem.add_argument("--boss-curated", action="store_true",
                       help="append to Boss curated section (never evicted)")
    p_mem.set_defaults(func=cmd_memory)

    p_chief = sub.add_parser("chief", help="multi-chief lifecycle (boss 5/3 §15.Z)")
    chief_sub = p_chief.add_subparsers(dest="chief_cmd")
    p_chief_create = chief_sub.add_parser("create", help="create new SECTION_CHIEF_<id>")
    p_chief_create.add_argument("chief_id",
                                help="suffix; auto-prefixes SECTION_CHIEF_ if not present")
    p_chief_create.add_argument("--scope-tags", default=None,
                                help="comma-separated tags e.g. tg,bigo,sportsbook")
    p_chief_create.add_argument("--manages", default=None,
                                help="comma-separated agent_ids to reassign to this chief")
    p_chief_create.set_defaults(func=cmd_chief_create)

    p_chief_diss = chief_sub.add_parser("dissolve", help="archive chief, reassign agents")
    p_chief_diss.add_argument("chief_id")
    p_chief_diss.add_argument("--reassign-to", default=None,
                              help="target chief_id (default: SECTION_CHIEF)")
    p_chief_diss.add_argument("--confirm", action="store_true",
                              help="REQUIRED: destructive op per §10")
    p_chief_diss.set_defaults(func=cmd_chief_dissolve)

    p_chief_re = chief_sub.add_parser("reassign", help="move single agent to another chief")
    p_chief_re.add_argument("agent_id")
    p_chief_re.add_argument("--to", required=True, help="target chief_id")
    p_chief_re.set_defaults(func=cmd_chief_reassign)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 0
    if args.cmd == "chief" and not getattr(args, "chief_cmd", None):
        p_chief.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
