"""processors/strategy_directive_apply.py — apply strategist org-adjustment directives.

Per CLAUDE.md §15.W (boss 5/3 directive): the Chief Strategist may issue
directives that adjust the multi-agent organization in-flight. This applier
runs daily 16:30 GMT+7 (before section_chief_eval at 17:00) and processes
any unapplied yaml under runtime/strategy_directives/ from the past 7 days.

Supported directive kinds:
  - chief_create          spawn new SECTION_CHIEF_<id> (with scope_tags + initial managed)
  - chief_dissolve        dismiss chief; reassign managed agents to fallback (boss-approve only)
  - agent_reassign        move single agent between chiefs
  - metric_redefine       adjust KPI baseline rules for an agent (or sub_class) — writes to baseline yaml
  - monitoring_track_open open new observation cron via lead_cron_schedule.jsonl (existing AUTO_SCHEDULE lane)
  - org_meta_review       strategist-flagged need for boss-level org review (no auto-action; surfaces in boss inbox)
  - agent_kpi_adjust      legacy/existing — KPI adjust on a single agent

Each handler audits change to:
  runtime/strategy_directive_audit.jsonl
  + history.log_event(kind=config_change|warning|directive)

Mark each directive applied via timestamp on the yaml; re-run is idempotent
(skips already-applied entries).

Per CLAUDE.md §6.4: timestamps ISO 8601 with +07:00.
Per CLAUDE.md §10: chief_dissolve requires explicit `boss_approved: true`
in the directive body — refuses to dissolve otherwise (default refuses).

CLI:
  py processors/strategy_directive_apply.py            # daily cron entry
  py processors/strategy_directive_apply.py --file <path>   # apply specific file
  py processors/strategy_directive_apply.py --dry-run       # preview without writing
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
RUNTIME_DIR = INSTANCE_DIR / "runtime"
DIRECTIVE_DIR = RUNTIME_DIR / "strategy_directives"
KPI_DIR = RUNTIME_DIR / "agent_kpi"
MEMORY_DIR = RUNTIME_DIR / "agent_memory"
LOG_DIR = RUNTIME_DIR / "logs"
AUDIT_PATH = RUNTIME_DIR / "strategy_directive_audit.jsonl"
BASELINE_PATH = INSTANCE_DIR / "policy" / "agent_kpi_baseline.yaml"
CRON_SCHEDULE_PATH = RUNTIME_DIR / "lead_cron_schedule.jsonl"
SUBAGENT_QUEUE_DIR = RUNTIME_DIR / "lead_subagent_queue"

BRIEF_QUEUE_DIR = RUNTIME_DIR / "briefs" / "queue"

DIRECTIVE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
SUBAGENT_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
BRIEF_QUEUE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CHIEF = "SECTION_CHIEF"

SUPPORTED_KINDS = {
    "chief_create",
    "chief_dissolve",
    "agent_reassign",
    "metric_redefine",
    "monitoring_track_open",
    "org_meta_review",
    "agent_kpi_adjust",
    "focus_topic",
    "agent_directive",
    "open_incident",
    "investigation_request",
}

# Kinds the applier executes. Other kinds are read by Section Chief at
# daily-run time (per SECTION_CHIEF.md §17), not by this applier.
APPLIER_KINDS = {
    "chief_create",
    "chief_dissolve",
    "agent_reassign",
    "metric_redefine",
    "monitoring_track_open",
    "org_meta_review",
    "agent_kpi_adjust",
    "focus_topic",
    "agent_directive",
    "open_incident",
    "investigation_request",
}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [strategy_directive_apply] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"strategy_directive_apply_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _hist(kind: str, title: str, body: str | None = None,
          refs: list | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(
            actor="cron_strategy_directive_apply", kind=kind, scope="org",
            title=title[:118], body=body, refs=refs,
        )
    except Exception as e:
        log(f"history_log fail: {type(e).__name__}: {e}")
        return -1


def _audit(record: dict) -> None:
    record = {"ts": now_iso(), **record}
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _slug(value: str, fallback: str = "item", max_len: int = 48) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", value or "").strip("_").lower()
    return (s or fallback)[:max_len]


def _append_learning(agent_id: str, text: str, category: str = "strategist_directive") -> None:
    try:
        from agents._common.agent_memory import append_learning
        append_learning(agent_id, text, category=category, boss_curated=False)
    except Exception as e:
        log(f"append_learning fail agent={agent_id}: {type(e).__name__}: {e}")


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.fromisoformat(str(expires_at))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=TZ)
        return exp < datetime.now(TZ)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Directive handlers
# ---------------------------------------------------------------------------

def _handle_chief_create(d: dict, dry_run: bool) -> dict:
    chief_id = d.get("chief_id") or d.get("id")
    if not chief_id:
        return {"ok": False, "reason": "missing chief_id"}
    if not chief_id.startswith("SECTION_CHIEF"):
        chief_id = f"SECTION_CHIEF_{chief_id}"
    scope_tags = d.get("scope_tags") or []
    manages = d.get("manages") or []
    rationale = d.get("rationale", "")

    if dry_run:
        return {"ok": True, "preview": {"chief_id": chief_id,
                                        "scope_tags": scope_tags,
                                        "manages": manages}}

    from agents._common.agent_memory import write_stub, _path_for
    if _path_for(chief_id).exists():
        return {"ok": False, "reason": f"chief {chief_id} already exists"}

    write_stub(
        chief_id, tier=2, sub_class=None,
        identity=f"Section Chief {chief_id} (created via strategist directive)",
        job=f"Manage {len(manages)} agents in scope {scope_tags}. Rationale: {rationale[:200]}",
        kpi_summary="(strategist-created; defaults apply)",
        capabilities="(see SECTION_CHIEF.md skill spec)",
        managed_by=None, scope_tags=scope_tags,
    )
    moved = []
    for aid in manages:
        kpi_path = KPI_DIR / f"{aid}.yaml"
        if kpi_path.exists():
            data = yaml.safe_load(kpi_path.read_text(encoding="utf-8")) or {}
            data["managed_by"] = chief_id
            kpi_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
                encoding="utf-8")
            moved.append(aid)
    _hist("config_change", f"chief_create {chief_id} (strategist directive)",
          body=f"scope_tags={scope_tags}\nmanages={moved}\nrationale={rationale}",
          refs=[f"chief:{chief_id}"])
    _audit({"directive": "chief_create", "chief_id": chief_id,
            "scope_tags": scope_tags, "managed": moved})
    return {"ok": True, "chief_id": chief_id, "managed_count": len(moved)}


def _handle_chief_dissolve(d: dict, dry_run: bool) -> dict:
    chief_id = d.get("chief_id") or d.get("id")
    if not chief_id:
        return {"ok": False, "reason": "missing chief_id"}
    if not chief_id.startswith("SECTION_CHIEF"):
        chief_id = f"SECTION_CHIEF_{chief_id}"
    if chief_id == DEFAULT_CHIEF:
        return {"ok": False, "reason": "refusing to dissolve default SECTION_CHIEF"}
    if not d.get("boss_approved"):
        # Per CLAUDE.md §10: destructive op requires explicit boss approval
        return {"ok": False,
                "reason": "chief_dissolve requires boss_approved: true (CLAUDE.md §10 destructive)"}
    target_chief = d.get("reassign_to") or DEFAULT_CHIEF
    if dry_run:
        return {"ok": True, "preview": {"dissolve": chief_id, "to": target_chief}}

    from agents._common.agent_memory import _path_for
    src = _path_for(chief_id)
    if not src.exists():
        return {"ok": False, "reason": f"chief {chief_id} memory not found"}

    moved = []
    for p in sorted(KPI_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        if data.get("managed_by") == chief_id:
            data["managed_by"] = target_chief
            p.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
                encoding="utf-8")
            moved.append(p.stem)

    archive_dir = RUNTIME_DIR / "agent_memory_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{chief_id}_dissolved_{now_iso().replace(':','-')}.md"
    archive_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    src.unlink()

    _hist("config_change",
          f"chief_dissolve {chief_id} → {target_chief} (strategist directive, boss-approved)",
          body=f"reassigned={moved}\narchive={archive_path}",
          refs=[f"chief:{chief_id}"])
    _audit({"directive": "chief_dissolve", "chief_id": chief_id,
            "reassigned_to": target_chief, "moved": moved,
            "archive": str(archive_path)})
    return {"ok": True, "dissolved": chief_id, "to": target_chief, "moved": moved}


def _handle_agent_reassign(d: dict, dry_run: bool) -> dict:
    aid = d.get("agent_id")
    chief_id = d.get("to_chief") or d.get("chief_id")
    if not aid or not chief_id:
        return {"ok": False, "reason": "missing agent_id or to_chief"}
    if not chief_id.startswith("SECTION_CHIEF"):
        chief_id = f"SECTION_CHIEF_{chief_id}"
    if dry_run:
        return {"ok": True, "preview": {"agent": aid, "to": chief_id}}

    from agents._common.agent_memory import _path_for
    if not _path_for(chief_id).exists():
        return {"ok": False, "reason": f"target chief {chief_id} memory not found"}
    kpi_path = KPI_DIR / f"{aid}.yaml"
    if not kpi_path.exists():
        return {"ok": False, "reason": f"agent {aid} KPI yaml not found"}
    data = yaml.safe_load(kpi_path.read_text(encoding="utf-8")) or {}
    prev = data.get("managed_by")
    data["managed_by"] = chief_id
    kpi_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8")
    _hist("config_change",
          f"agent_reassign {aid}: {prev} → {chief_id} (strategist directive)",
          refs=[f"agent:{aid}", f"chief:{chief_id}"])
    _audit({"directive": "agent_reassign", "agent_id": aid,
            "from": prev, "to": chief_id})
    return {"ok": True, "agent": aid, "from": prev, "to": chief_id}


def _handle_metric_redefine(d: dict, dry_run: bool) -> dict:
    """Adjust KPI baseline at instance level — modifies
    instances/<active>/policy/agent_kpi_baseline.yaml. Either targets a
    specific agent_id or a sub_class default."""
    target = d.get("agent_id") or d.get("sub_class")
    if not target:
        return {"ok": False, "reason": "missing agent_id or sub_class"}
    field = d.get("field")
    new_value = d.get("new_value")
    if field is None or new_value is None:
        return {"ok": False, "reason": "missing field or new_value"}
    if dry_run:
        return {"ok": True, "preview": {"target": target, "field": field, "new_value": new_value}}

    if not BASELINE_PATH.exists():
        return {"ok": False, "reason": "baseline yaml missing"}
    base = yaml.safe_load(BASELINE_PATH.read_text(encoding="utf-8")) or {}

    if d.get("agent_id"):
        agents = base.setdefault("field_agent", {})
        entry = agents.setdefault(d["agent_id"], {})
        prev = entry.get(field)
        entry[field] = new_value
    else:
        defaults = base.setdefault("defaults", {}).setdefault(d["sub_class"], {})
        prev = defaults.get(field)
        defaults[field] = new_value
    BASELINE_PATH.write_text(
        yaml.safe_dump(base, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8")
    _hist("config_change",
          f"metric_redefine {target} {field}={new_value} (strategist directive)",
          body=f"prev={prev} new={new_value}\nrationale={d.get('rationale','')}",
          refs=[f"target:{target}", "policy/agent_kpi_baseline.yaml"])
    _audit({"directive": "metric_redefine", "target": target,
            "field": field, "from": prev, "to": new_value})
    return {"ok": True, "target": target, "field": field, "from": prev, "to": new_value}


def _handle_monitoring_track_open(d: dict, dry_run: bool) -> dict:
    """Open new observation cron — uses lead_cron_schedule.jsonl (existing
    AUTO_SCHEDULE lane). Each entry is a one-off insert of a query / probe
    that the lead pipeline picks up."""
    target = d.get("target")
    sql = d.get("query") or d.get("suggested_action")
    freq = d.get("freq", "daily")
    rationale = d.get("rationale", "")
    if not target or not sql:
        return {"ok": False, "reason": "missing target or query/suggested_action"}
    if dry_run:
        return {"ok": True, "preview": {"target": target, "freq": freq}}

    entry = {
        "scheduled_at": now_iso(),
        "scheduled_by": "CHIEF_STRATEGIST",
        "kind": "observation_cron",
        "target": target,
        "query": sql,
        "freq": freq,
        "rationale": rationale,
    }
    with CRON_SCHEDULE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _hist("directive", f"monitoring_track_open: {target} ({freq})",
          body=f"query={sql[:200]}\nrationale={rationale}",
          refs=[f"target:{target}", "lead_cron_schedule.jsonl"])
    _audit({"directive": "monitoring_track_open", "target": target, "freq": freq})
    return {"ok": True, "target": target, "freq": freq}


def _handle_org_meta_review(d: dict, dry_run: bool) -> dict:
    """Strategist self-flags need for boss-level review of org structure.
    No auto-action — surfaces in boss inbox via brief queue + history."""
    rationale = d.get("rationale", "")
    suggestion = d.get("suggestion", "")
    if dry_run:
        return {"ok": True, "preview": {"meta_review_flagged": True}}

    flag_path = RUNTIME_DIR / "org_meta_review_pending.jsonl"
    entry = {
        "ts": now_iso(),
        "issued_by": "CHIEF_STRATEGIST",
        "rationale": rationale,
        "suggestion": suggestion,
        "state": "pending_boss_review",
    }
    with flag_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _hist("warning", "org_meta_review flagged by CHIEF_STRATEGIST",
          body=f"rationale={rationale[:300]}\nsuggestion={suggestion[:300]}",
          refs=["org_meta_review_pending.jsonl"])
    _audit({"directive": "org_meta_review", "state": "pending_boss_review"})
    return {"ok": True, "flagged": True}


def _handle_agent_kpi_adjust(d: dict, dry_run: bool) -> dict:
    """Existing kind — adjust a single agent's target_kpi field. Mirrors
    section_chief feedback (§14) but invoked via strategist directive."""
    aid = d.get("agent_id")
    field = d.get("field")
    new_value = d.get("new_value")
    if not aid or field is None or new_value is None:
        return {"ok": False, "reason": "missing agent_id / field / new_value"}
    if dry_run:
        return {"ok": True, "preview": {"agent": aid, "field": field, "new_value": new_value}}

    kpi_path = KPI_DIR / f"{aid}.yaml"
    if not kpi_path.exists():
        return {"ok": False, "reason": f"agent {aid} KPI yaml not found"}
    data = yaml.safe_load(kpi_path.read_text(encoding="utf-8")) or {}
    target_kpi = data.setdefault("target_kpi", {})
    prev = target_kpi.get(field)
    target_kpi[field] = new_value
    history = data.setdefault("target_kpi_history", [])
    history.append({
        "changed_at": now_iso(),
        "changed_by": "CHIEF_STRATEGIST",
        "field": field, "from": prev, "to": new_value,
        "reason": d.get("rationale", "(strategist directive)"),
    })
    kpi_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8")
    _hist("config_change",
          f"agent_kpi_adjust {aid} {field}={new_value} (strategist directive)",
          body=f"prev={prev} new={new_value}",
          refs=[f"agent:{aid}", str(kpi_path.relative_to(ROOT).as_posix())])
    _audit({"directive": "agent_kpi_adjust", "agent_id": aid,
            "field": field, "from": prev, "to": new_value})
    return {"ok": True, "agent": aid, "field": field, "from": prev, "to": new_value}


def _handle_focus_topic(d: dict, dry_run: bool) -> dict:
    topic = d.get("topic") or d.get("target")
    action = d.get("action_for_chief") or d.get("instruction")
    if not topic or not action:
        return {"ok": False, "reason": "missing topic or action_for_chief"}
    chief_id = d.get("issued_for") or DEFAULT_CHIEF
    if dry_run:
        return {"ok": True, "preview": {"chief": chief_id, "topic": topic}}

    line = (
        f"focus_topic {topic}: {action} | success={d.get('success_criterion', '')}"
    )
    _append_learning(chief_id, line)
    _hist("directive", f"focus_topic assigned to {chief_id}: {topic}",
          body=line,
          refs=[d.get("parent_memo", ""), d.get("_directive_file", "")])
    _audit({"directive": "focus_topic", "chief_id": chief_id, "topic": topic})
    return {"ok": True, "chief": chief_id, "topic": topic}


def _handle_agent_directive(d: dict, dry_run: bool) -> dict:
    aid = d.get("agent_id")
    instruction = d.get("instruction") or d.get("action_for_agent") or d.get("action_for_chief")
    if not aid or not instruction:
        return {"ok": False, "reason": "missing agent_id or instruction"}
    if dry_run:
        return {"ok": True, "preview": {"agent": aid, "instruction": instruction[:120]}}

    kpi_path = KPI_DIR / f"{aid}.yaml"
    if not kpi_path.exists():
        return {"ok": False, "reason": f"agent {aid} KPI yaml not found"}
    data = yaml.safe_load(kpi_path.read_text(encoding="utf-8")) or {}
    recent = data.setdefault("recent_directives", [])
    directive_id = (
        f"{Path(d.get('_directive_file', 'directive')).stem}:agent_directive:"
        f"{aid}:{_slug(instruction, 'instruction', 32)}"
    )
    entry = {
        "directive_id": directive_id,
        "kind": "agent_directive",
        "issued_at": d.get("issued_at") or now_iso(),
        "expires_at": d.get("expires_at"),
        "instruction": instruction,
        "rationale": d.get("rationale"),
        "success_criterion": d.get("success_criterion"),
        "source": d.get("parent_memo") or d.get("_directive_file"),
    }
    if not any(x.get("directive_id") == directive_id for x in recent if isinstance(x, dict)):
        recent.append(entry)
    kpi_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8")
    _append_learning(
        aid,
        f"agent_directive {directive_id}: {instruction} | success={d.get('success_criterion', '')}",
    )
    _hist("directive", f"agent_directive assigned to {aid}",
          body=json.dumps(entry, ensure_ascii=False, indent=2),
          refs=[f"agent:{aid}", str(kpi_path.relative_to(ROOT).as_posix())])
    _audit({"directive": "agent_directive", "agent_id": aid,
            "directive_id": directive_id})
    return {"ok": True, "agent": aid, "directive_id": directive_id}


def _handle_investigation_request(d: dict, dry_run: bool) -> dict:
    target = d.get("target")
    if not target:
        return {"ok": False, "reason": "missing target"}
    task_id = (
        f"STRAT-{datetime.now(TZ).strftime('%Y-%m-%d')}-"
        f"{_slug(target, 'investigation')}.task"
    )
    task_path = SUBAGENT_QUEUE_DIR / task_id
    if dry_run:
        return {"ok": True, "preview": {"task": task_id, "target": target}}
    entry = {
        "task_id": task_id,
        "type": d.get("depth") or "investigation_request",
        "target": target,
        "source": "strategy_directive",
        "source_file": d.get("_directive_file"),
        "parent_memo": d.get("parent_memo"),
        "deadline": d.get("deadline"),
        "queued_at": now_iso(),
        "rationale": d.get("rationale"),
        "success_criterion": d.get("success_criterion"),
        "suggested_action": d.get("action_for_chief") or d.get("instruction") or f"Investigate {target}",
        "note": "Picked up by Section Chief / next field-agent execution pass.",
    }
    task_path.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_learning(
        DEFAULT_CHIEF,
        f"investigation_request queued {task_id}: {target} | deadline={d.get('deadline')}",
    )
    _hist("directive", f"investigation_request queued: {target}",
          body=json.dumps(entry, ensure_ascii=False, indent=2),
          refs=[str(task_path.relative_to(ROOT).as_posix())])
    _audit({"directive": "investigation_request", "target": target, "task": task_id})
    return {"ok": True, "task": task_id, "target": target}


def _handle_open_incident(d: dict, dry_run: bool) -> dict:
    template = d.get("template") or "strategist_open_incident"
    apply_to = d.get("apply_to") or []
    if isinstance(apply_to, str):
        apply_to = [apply_to]
    agent_id = d.get("agent_id") or d.get("agent") or "orchestration"
    rationale = d.get("rationale") or ""
    if not rationale:
        return {"ok": False, "reason": "missing rationale"}
    if dry_run:
        return {"ok": True, "preview": {"agent": agent_id, "kind": template}}
    from processors.agent_incidents import open_incident
    hypothesis = (
        f"{rationale}\n\nSuccess criterion: {d.get('success_criterion', '')}"
    )
    inc_id = open_incident(
        agent_id=agent_id,
        kind=template,
        hypothesis=hypothesis,
        evidence=[f"apply_to: {x}" for x in apply_to] or [f"source: {d.get('_directive_file')}"],
        severity=d.get("severity", "yellow"),
    )
    _hist("directive", f"open_incident directive materialized: {inc_id}",
          body=f"agent_id={agent_id}\ntemplate={template}\nrationale={rationale}",
          refs=[f"incident:{inc_id}", d.get("_directive_file", "")])
    _audit({"directive": "open_incident", "incident_id": inc_id,
            "agent_id": agent_id, "template": template})
    return {"ok": True, "incident_id": inc_id, "agent": agent_id, "incident_kind": template}


HANDLERS = {
    "chief_create": _handle_chief_create,
    "chief_dissolve": _handle_chief_dissolve,
    "agent_reassign": _handle_agent_reassign,
    "metric_redefine": _handle_metric_redefine,
    "monitoring_track_open": _handle_monitoring_track_open,
    "org_meta_review": _handle_org_meta_review,
    "agent_kpi_adjust": _handle_agent_kpi_adjust,
    "focus_topic": _handle_focus_topic,
    "agent_directive": _handle_agent_directive,
    "open_incident": _handle_open_incident,
    "investigation_request": _handle_investigation_request,
}


# ---------------------------------------------------------------------------
# File-level apply
# ---------------------------------------------------------------------------

def _load_directive_yaml(yaml_path: Path) -> tuple[dict, dict]:
    """Parse the directive yaml. Existing files use 2-document layout:
    `---\\nfrontmatter\\n---\\ndirectives: [...]`. Returns (frontmatter, body).
    Single-doc layout also supported (everything in one mapping)."""
    text = yaml_path.read_text(encoding="utf-8")
    docs = list(yaml.safe_load_all(text))
    docs = [d for d in docs if d is not None]
    if len(docs) == 0:
        return {}, {}
    if len(docs) == 1:
        d = docs[0] or {}
        return d, d
    fm = docs[0] or {}
    body = docs[1] or {}
    return fm, body


def _save_directive_yaml(yaml_path: Path, frontmatter: dict, body: dict) -> None:
    """Round-trip write back as 2-doc yaml when frontmatter≠body, else single."""
    if frontmatter is body:
        yaml_path.write_text(
            yaml.safe_dump(body, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8")
        return
    fm_str = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False)
    body_str = yaml.safe_dump(body, allow_unicode=True, sort_keys=False, default_flow_style=False)
    yaml_path.write_text(f"---\n{fm_str}---\n{body_str}", encoding="utf-8")


def apply_file(yaml_path: Path, dry_run: bool = False) -> dict:
    """Apply all applier-handled directives in a single yaml file. Idempotent:
    the file's frontmatter `applied_at` field marks fully-processed runs.
    Per-directive applied_at also tracked for partial-run resume."""
    if not yaml_path.exists():
        return {"ok": False, "reason": f"file not found: {yaml_path}"}
    try:
        frontmatter, body = _load_directive_yaml(yaml_path)
    except yaml.YAMLError as e:
        return {"ok": False, "reason": f"yaml parse fail: {e}"}
    raw = body
    directives = raw.get("directives") or []
    if _expired(frontmatter.get("expires_at")):
        return {"ok": True, "skipped": "expired", "file": str(yaml_path),
                "expires_at": frontmatter.get("expires_at")}
    if frontmatter.get("applied_at") and not dry_run:
        unapplied = [
            d for d in directives
            if d.get("kind") in APPLIER_KINDS and not d.get("applied_at")
        ]
        if not unapplied:
            return {"ok": True, "skipped": "already_applied", "file": str(yaml_path)}
        log(f"file has applied_at but {len(unapplied)} directive(s) still need apply")
    results = []
    for i, d in enumerate(directives):
        kind = d.get("kind")
        if not kind or kind not in APPLIER_KINDS:
            results.append({"index": i, "kind": kind, "skipped": "not_applier_kind"})
            continue
        if d.get("applied_at"):
            results.append({"index": i, "kind": kind, "skipped": "already_applied"})
            continue
        handler = HANDLERS.get(kind)
        if not handler:
            results.append({"index": i, "kind": kind, "skipped": "no_handler"})
            continue
        handler_input = {**d}
        for key in ("issued_at", "issued_for", "expires_at", "parent_memo", "issued_by"):
            if key not in handler_input and key in frontmatter:
                handler_input[key] = frontmatter.get(key)
        handler_input["_directive_file"] = yaml_path.name
        try:
            res = handler(handler_input, dry_run=dry_run)
        except Exception as e:
            log(f"  HANDLER FAIL kind={kind}: {type(e).__name__}: {e}")
            res = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        results.append({"index": i, "kind": kind, **res})
        if res.get("ok") and not dry_run:
            d["applied_at"] = now_iso()
        if not res.get("ok"):
            log(f"  fail [{i}] {kind}: {res.get('reason')}")
        else:
            log(f"  ok   [{i}] {kind}: {res}")

    # Mark file-level applied if every applier-kind directive applied OK
    if not dry_run:
        applier_kind_dirs = [d for d in directives if d.get("kind") in APPLIER_KINDS]
        applier_done = bool(applier_kind_dirs) and all(
            d.get("applied_at") for d in applier_kind_dirs
        )
        if applier_done:
            frontmatter["applied_at"] = now_iso()
        # rewrite with applied_at markers
        _save_directive_yaml(yaml_path, frontmatter, raw)
    return {"ok": True, "file": str(yaml_path), "results": results}


def discover_files(window_days: int = 7) -> list[Path]:
    """Find directive yaml files in the past N days that may need application."""
    if not DIRECTIVE_DIR.exists():
        return []
    cutoff = datetime.now(TZ) - timedelta(days=window_days)
    out = []
    for p in sorted(DIRECTIVE_DIR.glob("*.yaml")):
        try:
            mtime = datetime.fromtimestamp(p.stat().st_mtime, TZ)
        except Exception:
            continue
        if mtime >= cutoff:
            out.append(p)
    return out


def run_pass(dry_run: bool = False, window_days: int = 7) -> dict:
    files = discover_files(window_days)
    log(f"discovered {len(files)} directive yaml(s) in past {window_days}d")
    summary = {"files": [], "applied": 0, "skipped": 0}
    for f in files:
        res = apply_file(f, dry_run=dry_run)
        summary["files"].append({"file": f.name, "result": res})
        if res.get("skipped") == "already_applied":
            summary["skipped"] += 1
        elif res.get("ok"):
            applied_count = sum(1 for r in (res.get("results") or [])
                                if r.get("ok") and r.get("kind") in APPLIER_KINDS)
            summary["applied"] += applied_count
    if not dry_run:
        _hist("milestone",
              f"strategy_directive_apply: {summary['applied']} applied, {summary['skipped']} skipped",
              body=f"files={[f.name for f in files]}")
        if summary["applied"] > 0:
            try:
                from processors.org_task_audit_refresh import refresh_org_task_audit
                refresh_org_task_audit("strategy_directive_apply")
            except Exception:
                pass
            # Boss FYI — 策略長 directive applied (no approval needed)
            try:
                from datetime import datetime, timedelta, timezone
                TZ_LOCAL = timezone(timedelta(hours=7))
                ts = datetime.now(TZ_LOCAL).strftime("%Y-%m-%dT%H-%M-%S")
                applied_files = [r["file"] for r in summary["files"]
                                 if not r["result"].get("skipped")]
                kinds_applied = []
                for r in summary["files"]:
                    for sub in (r["result"].get("results") or []):
                        if sub.get("ok") and sub.get("kind"):
                            kinds_applied.append(sub["kind"])
                kinds_str = ", ".join(sorted(set(kinds_applied))) or "see files"
                q = BRIEF_QUEUE_DIR / f"pending_{ts}_strategist_directive_fyi.md"
                files_summary = ", ".join(applied_files[:3])
                if len(applied_files) > 3:
                    files_summary += f" 等 {len(applied_files)} 個"
                q.write_text(
                    f"[策略長 決策] 策略指令執行完成\n\n"
                    f"• 套用 {summary['applied']} 條 directive（{kinds_str}）"
                    f" → 情報員任務/KPI/監控範圍已依策略長意圖調整\n"
                    f"• 影響：{files_summary}\n\n"
                    f"策略長自主執行，不需行動。",
                    encoding="utf-8",
                )
            except Exception as e:
                log(f"boss fyi write fail: {type(e).__name__}: {e}")
    log(f"summary: applied={summary['applied']} skipped={summary['skipped']}")
    return summary


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--file", default=None,
                   help="apply specific file (skip discovery)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--window-days", type=int, default=7)
    args = p.parse_args()

    if args.file:
        res = apply_file(Path(args.file), dry_run=args.dry_run)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        if res.get("ok") and not args.dry_run:
            try:
                from processors.org_task_audit_refresh import refresh_org_task_audit
                refresh_org_task_audit("strategy_directive_apply:file")
            except Exception:
                pass
        return 0 if res.get("ok") else 1

    summary = run_pass(dry_run=args.dry_run, window_days=args.window_days)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
