"""processors/section_chief_eval.py — Tier 2 小主管 daily KPI evaluator.

Cron daily 17:00 GMT+7 via blacksite_daemon (2h before 19:00 brief).

For each Field Agent in instances/<active>/policy/agent_kpi_baseline.yaml:
  1. Compute 24h yield from messages SQL or runtime/raw JSONL
  2. Compute signal-to-noise via rule-based sample (Pass 1 only at v1)
  3. Count ToS-violation events (system_history kind=warning scope=<platform>)
  4. Sample tier_hint accuracy (lightweight, deferred to v2 LLM-assisted)
  5. Update runtime/agent_kpi/<agent_id>.yaml current_kpi + status + notes
  6. If status transitions yellow → red (or other incident triggers per
     SECTION_CHIEF.md §15.1) → open incident at runtime/agent_incidents/

Per CLAUDE.md §15 + boss 5/2 Q5 lock: this evaluator NEVER auto-pauses
or auto-burns agents. Only updates KPI yaml + opens incidents.

🆕 Boss 5/3 §15.Z multi-chief support: iterates over every chief whose
memory file exists at runtime/agent_memory/SECTION_CHIEF*.md. Each chief's
managed Field Agents are filtered by `managed_by:` field in
agent_kpi/<agent_id>.yaml. Default behavior preserved: 1 chief
(SECTION_CHIEF) manages all 25 agents.

Per CLAUDE.md §6.4: timestamps ISO 8601 with +07:00.
Per CLAUDE.md §13.6: log_event 'metric' per pass; 'warning' per incident open.
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

from db.connection import get_connection  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
RUNTIME_DIR = INSTANCE_DIR / "runtime"
KPI_DIR = RUNTIME_DIR / "agent_kpi"
INCIDENTS_DIR = RUNTIME_DIR / "agent_incidents"
LOG_DIR = RUNTIME_DIR / "logs"
RAW_DIR = RUNTIME_DIR / "raw"
MEMORY_DIR = RUNTIME_DIR / "agent_memory"
DIGEST_DIR = RUNTIME_DIR / "strategist_digest"
BRIEF_QUEUE_DIR = RUNTIME_DIR / "briefs" / "queue"

KPI_DIR.mkdir(parents=True, exist_ok=True)
INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
DIGEST_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CHIEF = "SECTION_CHIEF"


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [section_chief_eval] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"section_chief_eval_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _hist(kind: str, title: str, body: str | None = None,
          refs: list | None = None, parent_id: int | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(
            actor="cron_section_chief_eval", kind=kind, scope="section_chief",
            title=title[:118], body=body, refs=refs, parent_id=parent_id,
        )
    except Exception as e:
        log(f"history_log fail: {type(e).__name__}: {e}")
        return -1


# ---------------------------------------------------------------------------
# Agent → platform mapping (for SQL filtering)
# ---------------------------------------------------------------------------

def _platform_from_agent_id(agent_id: str) -> str:
    """Best-effort platform extraction. Examples:
      P01_TG → telegram
      P03_Bigo → bigo
      P04_TikTok_sports → tiktok
      oneD_anon → oned
      bigo_lobby_anon → bigo
    """
    aid = agent_id.lower()
    mapping = {
        "_tg": "telegram", "_fb": "facebook", "_ig": "instagram",
        "_tiktok": "tiktok", "_youtube": "youtube", "_bigo": "bigo",
        "_nimo": "nimo", "_pantip": "pantip", "_reddit": "reddit",
        "_discord": "discord", "_lemon8": "lemon8", "_x": "x",
        "oned": "oned", "ch3plus": "ch3plus", "aisplay": "aisplay",
        "trueid": "trueid", "sanook": "sanook", "noice": "noice",
        "fb_page": "facebook",
    }
    for k, v in mapping.items():
        if k in aid:
            return v
    return aid.split("_")[0]


def _persona_from_agent_id(agent_id: str) -> str | None:
    """Extract persona id (P01-P05) from agent_id; None if anonymous_web."""
    m = re.match(r"^(P\d{2})_", agent_id)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

_NOISE_RE_DIGITS = re.compile(r"^[\d\s,.]+$")
_NOISE_RE_REPEAT = re.compile(r"(.)\1{6,}")  # >= 7 same chars


def _is_noise(text: str) -> bool:
    """Rule-based noise detector. Conservative; favors marking content
    as 'signal' unless clearly spam."""
    if not text:
        return True
    t = text.strip()
    if len(t) < 8:
        return True
    if _NOISE_RE_DIGITS.match(t):
        return True
    if _NOISE_RE_REPEAT.search(t):
        return True
    return False


def _compute_yield_24h(conn, agent_id: str, sub_class: str) -> int:
    """24h message yield. Persona-driven uses messages.persona; anonymous
    uses messages.platform with no persona OR raw JSONL line count."""
    cutoff = (datetime.now(TZ) - timedelta(hours=24)).isoformat(timespec="seconds")
    persona = _persona_from_agent_id(agent_id)
    platform = _platform_from_agent_id(agent_id)
    try:
        if sub_class == "persona_driven" and persona:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE persona=? AND platform=? AND ts >= ?",
                (persona, platform, cutoff),
            ).fetchone()
            n = row[0] if row else 0
            if n > 0:
                return n
            # Fallback: persona only (some agents may not have platform tagged)
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE persona=? AND ts >= ?",
                (persona, cutoff),
            ).fetchone()
            n = row[0] if row else 0
            if n > 0:
                return n
            # Final fallback: agent collects without persona embedding (e.g. YouTube/TikTok
            # anonymous scrapers attributed to a P0x agent). Count platform-level NULL persona.
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE platform=? "
                "AND (persona IS NULL OR persona='') AND ts >= ?",
                (platform, cutoff),
            ).fetchone()
            return row[0] if row else 0
        else:
            # anonymous_web — count platform messages with NULL persona
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE platform=? "
                "AND (persona IS NULL OR persona='') AND ts >= ?",
                (platform, cutoff),
            ).fetchone()
            return row[0] if row else 0
    except Exception as e:
        log(f"yield query fail for {agent_id}: {type(e).__name__}: {e}")
        return 0


def _compute_signal_noise(conn, agent_id: str, sub_class: str,
                          sample_size: int = 20) -> float | None:
    """Rule-based S/N estimate from random sample of past 24h text."""
    cutoff = (datetime.now(TZ) - timedelta(hours=24)).isoformat(timespec="seconds")
    persona = _persona_from_agent_id(agent_id)
    platform = _platform_from_agent_id(agent_id)
    try:
        if sub_class == "persona_driven" and persona:
            rows = conn.execute(
                "SELECT text FROM messages WHERE persona=? AND ts >= ? "
                "AND text IS NOT NULL ORDER BY RANDOM() LIMIT ?",
                (persona, cutoff, sample_size),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT text FROM messages WHERE platform=? "
                "AND (persona IS NULL OR persona='') AND ts >= ? "
                "AND text IS NOT NULL ORDER BY RANDOM() LIMIT ?",
                (platform, cutoff, sample_size),
            ).fetchall()
    except Exception as e:
        log(f"sn query fail for {agent_id}: {type(e).__name__}: {e}")
        return None
    if not rows:
        return None
    signals = sum(1 for r in rows if not _is_noise(r[0]))
    return round(signals / len(rows), 3)


def _count_tos_violations(conn, agent_id: str) -> int:
    """Count system_history warnings tagged for this agent's platform/persona
    in past 24h."""
    cutoff = (datetime.now(TZ) - timedelta(hours=24)).isoformat(timespec="seconds")
    platform = _platform_from_agent_id(agent_id)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM system_history "
            "WHERE kind='warning' AND scope=? AND ts >= ? "
            "AND (actor=? OR title LIKE ? OR body LIKE ?)",
            (platform, cutoff, agent_id, f"%{agent_id}%", f"%ToS%"),
        ).fetchone()
        return row[0] if row else 0
    except Exception as e:
        log(f"tos query fail for {agent_id}: {type(e).__name__}: {e}")
        return 0


def _live_health_issue(prev: dict) -> str | None:
    """Return an explicit live-session blocker carried by the agent KPI yaml."""
    live_status = prev.get("live_status") or {}
    if live_status.get("human_action_required"):
        return str(live_status.get("human_action_required"))
    if live_status.get("fail_reason"):
        reason = str(live_status.get("fail_reason")).strip()
        if reason.lower() not in {"resolved", "ok", "pass", "none", "false"}:
            return reason
    return None


# ---------------------------------------------------------------------------
# Status assignment + write-back
# ---------------------------------------------------------------------------

def _assign_status(current: dict, target: dict, sub_class: str,
                   prev_status: str = "green",
                   prev_yellow_streak: int = 0) -> tuple[str, int]:
    """Per SECTION_CHIEF.md §13.2 status rules. Returns (status, new_streak).

    Mode-aware (5/7 fix per strategist directive 2026-05-07): an agent in
    verify_only mode produces 0 raw yield by design (smoke pass IS the health
    metric — engagement is suppressed). Skip msg_yield_below_baseline check
    when agent is verify_only or scan_pending. Active-mode agent uses normal baseline.
    """
    breaches = []
    yld = current.get("msg_yield_24h", 0)
    if current.get("live_health_issue"):
        breaches.append("live_health_issue")

    yield_check_suspended = bool(
        target.get("is_verify_only", False)
        or target.get("yield_check_suspended", False)
    )
    if not yield_check_suspended:
        if yld < 0.5 * (target.get("msg_yield_baseline_24h", 50)):
            breaches.append("msg_yield_below_50pct")

    sn = current.get("signal_noise")
    if sn is not None and sn < target.get("signal_noise_min", 0.3):
        breaches.append("signal_noise_below_target")

    tos = current.get("tos_violations", 0)
    if tos > target.get("tos_violation_max", 0):
        return ("red", 0)

    tha = current.get("tier_hint_accuracy")
    if tha is not None and tha < target.get("tier_hint_accuracy_min", 0.6):
        breaches.append("tier_hint_accuracy_below_target")

    if sub_class == "anonymous_web":
        spr = current.get("selector_pass_rate")
        if spr is not None and spr < target.get("selector_pass_rate_min", 0.85):
            breaches.append("selector_pass_rate_below_target")
        gbr = current.get("geo_block_resilience")
        if gbr is not None and gbr < target.get("geo_block_resilience_min", 0.75):
            breaches.append("geo_block_resilience_below_target")

    if sub_class == "persona_driven":
        if current.get("identity_axis_isolation") is False:
            return ("red", 0)
        if current.get("warmup_compliance") is False:
            return ("red", 0)

    if not breaches:
        return ("green", 0)

    new_streak = prev_yellow_streak + 1
    if new_streak >= 3:
        return ("red", new_streak)
    return ("yellow", new_streak)


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


def _expire_directives(directives: list[dict]) -> list[dict]:
    """Drop expired entries from recent_directives."""
    now = now_iso()
    return [d for d in (directives or []) if not (d.get("expires_at") and d["expires_at"] < now)]


# ---------------------------------------------------------------------------
# Incident open
# ---------------------------------------------------------------------------

def _next_incident_id(date_str: str) -> str:
    """Sequence within day."""
    pat = f"INC-{date_str}-"
    existing = [p.stem for p in INCIDENTS_DIR.glob(f"{pat}*.md")]
    if not existing:
        return f"{pat}001"
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


def _open_incident(agent_id: str, kind: str, severity: str,
                   evidence_lines: list[str], hypothesis: str) -> str:
    """Create runtime/agent_incidents/<INC-id>.md and return incident_id.
    Logs warning to history."""
    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    inc_id = _next_incident_id(date_str)
    path = INCIDENTS_DIR / f"{inc_id}.md"
    body = (
        f"---\n"
        f"incident_id: {inc_id}\n"
        f"opened_at: \"{now_iso()}\"\n"
        f"opened_by: SECTION_CHIEF\n"
        f"agent_id: {agent_id}\n"
        f"state: open\n"
        f"violation_kind: {kind}\n"
        f"severity: {severity}\n"
        f"parent_incident: null\n"
        f"---\n\n"
        f"# {inc_id} — {agent_id} {kind}\n\n"
        f"## What happened\n"
        + "\n".join(f"- {ln}" for ln in evidence_lines) + "\n\n"
        f"## Hypothesis\n{hypothesis}\n\n"
        f"## Action so far\n- Opened this incident (state=open) by section_chief_eval daily 17:00 cron\n\n"
        f"## Next\n- 小主管 review: confirm hypothesis, issue corrective directive via KPI yaml\n"
        f"- If unresolved in 7d: auto-escalate to 策略長 via processors/agent_incidents.py escalate-aged\n"
    )
    path.write_text(body, encoding="utf-8")
    _hist(
        "warning",
        f"incident opened {inc_id}: {agent_id} {kind}",
        body=f"severity={severity}\nhypothesis={hypothesis[:300]}",
        refs=[f"agent:{agent_id}",
              path.relative_to(ROOT).as_posix()],
    )
    return inc_id


# ---------------------------------------------------------------------------
# Per-agent eval pass
# ---------------------------------------------------------------------------

def evaluate_agent(conn, agent_id: str, baseline_entry: dict,
                   dry_run: bool = False,
                   chief_id: str = DEFAULT_CHIEF) -> dict:
    """Compute KPI for one agent + write back yaml + open incident if needed.
    Returns summary dict.

    chief_id: which Section Chief is doing the evaluation. Recorded as
    `last_evaluated_by` in the agent's KPI yaml. Default = SECTION_CHIEF
    (singleton compat)."""
    sub_class = baseline_entry.get("sub_class", "persona_driven")
    target = {k: v for k, v in baseline_entry.items()
              if k not in ("sub_class", "notes")}

    yaml_path = KPI_DIR / f"{agent_id}.yaml"
    prev = _read_yaml(yaml_path)
    prev_status = prev.get("status", "green")
    prev_yellow_streak = int(prev.get("yellow_streak", 0))
    scan_pending = bool((prev.get("live_status") or {}).get("scan_pending", False))
    live_health_issue = _live_health_issue(prev)
    status_target = dict(target)
    if scan_pending:
        status_target["yield_check_suspended"] = True

    yld = _compute_yield_24h(conn, agent_id, sub_class)
    sn = _compute_signal_noise(conn, agent_id, sub_class)
    tos = _count_tos_violations(conn, agent_id)

    # Tier-hint accuracy: deferred to v2 LLM-assisted; preserve previous if any.
    tha = (prev.get("current_kpi") or {}).get("tier_hint_accuracy")

    if sub_class == "persona_driven":
        current = {
            "msg_yield_24h": yld,
            "signal_noise": sn,
            "tos_violations": tos,
            "tier_hint_accuracy": tha,
            "warmup_compliance": (prev.get("current_kpi") or {}).get("warmup_compliance", True),
            "persona_consistency": (prev.get("current_kpi") or {}).get("persona_consistency", True),
            "identity_axis_isolation": (prev.get("current_kpi") or {}).get("identity_axis_isolation", True),
            "live_health_issue": live_health_issue,
        }
    else:
        # anonymous_web: selector + geo derived from prev (set by agent if instrumented);
        # default to None so status unaffected when not yet measured.
        current = {
            "msg_yield_24h": yld,
            "selector_pass_rate": (prev.get("current_kpi") or {}).get("selector_pass_rate"),
            "geo_block_resilience": (prev.get("current_kpi") or {}).get("geo_block_resilience"),
            "content_rate": (prev.get("current_kpi") or {}).get("content_rate"),
            "tier_hint_accuracy": tha,
            "live_health_issue": live_health_issue,
        }

    status, new_streak = _assign_status(
        current, status_target, sub_class,
        prev_status=prev_status,
        prev_yellow_streak=prev_yellow_streak if prev_status == "yellow" else 0,
    )

    target_for_write = dict(prev.get("target_kpi") or {})
    target_for_write.update(target)

    new_yaml = dict(prev) if prev else {}
    new_yaml.update({
        "agent_id": agent_id,
        "sub_class": sub_class,
        "last_evaluated_at": now_iso(),
        "last_evaluated_by": chief_id,
        "current_kpi": current,
        "target_kpi": target_for_write,
        "status": status,
        "yellow_streak": new_streak,
        "notes": _build_notes(current, status_target, status, sub_class),
        "recent_directives": _expire_directives(prev.get("recent_directives") or []),
        "incident_history": prev.get("incident_history") or [],
        "target_kpi_history": prev.get("target_kpi_history") or [],
        "managed_by": prev.get("managed_by") or DEFAULT_CHIEF,
    })

    incident_id = None
    transitioned_to_red = (prev_status != "red" and status == "red")
    if transitioned_to_red and not dry_run:
        # Determine violation_kind for incident
        kinds = []
        if tos > 0:
            kinds.append("tos_violation")
        if current.get("live_health_issue"):
            kinds.append("live_health_issue")
        yield_check_suspended = bool(
            status_target.get("is_verify_only", False)
            or status_target.get("yield_check_suspended", False)
        )
        if not yield_check_suspended and yld < 0.5 * (target.get("msg_yield_baseline_24h", 50)):
            kinds.append("msg_yield_below_baseline")
        if sub_class == "persona_driven" and current.get("identity_axis_isolation") is False:
            kinds.append("identity_axis_collision")
        if not kinds:
            kinds.append("kpi_red_status")
        violation_kind = "+".join(kinds)
        evidence = [
            f"yield_24h={yld} (target {target.get('msg_yield_baseline_24h')})",
            f"signal_noise={sn} (target {target.get('signal_noise_min')})",
            f"tos_violations={tos}",
            f"live_health_issue={current.get('live_health_issue')}",
            f"prev_status={prev_status} → red (yellow_streak={new_streak})",
        ]
        hypothesis = f"Field Agent {agent_id} crossed red threshold; chain review required (boss 5/2 Q5: no auto-pause)."
        incident_id = _open_incident(agent_id, violation_kind, "red",
                                     evidence, hypothesis)
        # Append to incident_history
        new_yaml["incident_history"].append({
            "inc_id": incident_id,
            "opened_at": now_iso(),
            "state": "open",
        })

    if not dry_run:
        _write_yaml(yaml_path, new_yaml)

    return {
        "agent_id": agent_id, "sub_class": sub_class,
        "yield_24h": yld, "signal_noise": sn, "tos": tos,
        "status": status, "prev_status": prev_status,
        "incident_opened": incident_id,
    }


def _build_notes(current: dict, target: dict, status: str, sub_class: str) -> str:
    parts = [f"status={status}"]
    yld = current.get("msg_yield_24h")
    yld_t = target.get("msg_yield_baseline_24h")
    if yld_t:
        parts.append(f"yield={yld}/{yld_t}")
    if target.get("yield_check_suspended"):
        parts.append("yield_check=suspended(scan_pending)")
    sn = current.get("signal_noise")
    if sn is not None:
        parts.append(f"sn={sn}")
    if current.get("tos_violations"):
        parts.append(f"tos={current['tos_violations']}!")
    if current.get("live_health_issue"):
        parts.append(f"health={current['live_health_issue']}")
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------

def list_chiefs() -> list[str]:
    """Discover all Section Chief ids by globbing memory dir.
    Returns sorted list. Default singleton case returns ['SECTION_CHIEF']."""
    if not MEMORY_DIR.exists():
        return [DEFAULT_CHIEF]
    chiefs = []
    for p in sorted(MEMORY_DIR.glob("SECTION_CHIEF*.md")):
        chiefs.append(p.stem)
    return chiefs or [DEFAULT_CHIEF]


def _agents_managed_by(chief_id: str, all_agents: dict,
                       default_chief: str = DEFAULT_CHIEF) -> dict:
    """Filter agents whose `managed_by` matches chief_id. Falls back to
    KPI yaml read; if no yaml or no managed_by field, agent is implicitly
    managed by default_chief."""
    out = {}
    for aid, cfg in all_agents.items():
        kpi_path = KPI_DIR / f"{aid}.yaml"
        managed = default_chief
        if kpi_path.exists():
            try:
                data = yaml.safe_load(kpi_path.read_text(encoding="utf-8")) or {}
                managed = data.get("managed_by") or default_chief
            except yaml.YAMLError:
                pass
        if managed == chief_id:
            out[aid] = cfg
    return out


def _write_digest_stub(chief_id: str, week_iso: str, summary: dict) -> Path:
    """Create or update the per-chief digest file with the run summary
    (one-line per agent + counts). Section Chief LLM later writes full
    digest content; this stub seeds the file so downstream readers see
    a fresh run record even without LLM compose."""
    path = DIGEST_DIR / f"{chief_id}_{week_iso}.md"
    body = (
        f"---\n"
        f"chief_id: {chief_id}\n"
        f"digest_week: {week_iso}\n"
        f"last_eval_at: \"{now_iso()}\"\n"
        f"---\n\n"
        f"# {chief_id} digest — {week_iso}\n\n"
        f"## Latest eval pass ({now_iso()})\n"
        f"- green={summary.get('green',0)} yellow={summary.get('yellow',0)} red={summary.get('red',0)}\n"
        f"- managed_agent_count={summary.get('total',0)}\n"
        f"- incidents_opened={len(summary.get('incidents',[]))}\n\n"
        f"_(LLM-composed full digest follows on Sunday cron; this stub is the cron trace.)_\n"
    )
    # Append-style: replace existing file (idempotent latest-run)
    path.write_text(body, encoding="utf-8")
    return path


def run_pass_for_chief(conn, chief_id: str, agents: dict,
                       dry_run: bool = False) -> dict:
    """Run eval for one chief over their managed agents. Helper for
    multi-chief iteration."""
    counts = {"green": 0, "yellow": 0, "red": 0}
    incidents = []
    if not agents:
        log(f"chief={chief_id} has no managed agents")
        return {**counts, "total": 0, "incidents": incidents}
    for agent_id, cfg in agents.items():
        try:
            summary = evaluate_agent(conn, agent_id, cfg,
                                     dry_run=dry_run, chief_id=chief_id)
        except Exception as e:
            log(f"FAIL eval {agent_id} (chief={chief_id}): {type(e).__name__}: {e}")
            continue
        counts[summary["status"]] = counts.get(summary["status"], 0) + 1
        if summary["incident_opened"]:
            incidents.append((agent_id, summary["incident_opened"]))
        log(f"  [{chief_id}] {summary['status']:>6} {agent_id:<22} "
            f"yield={summary['yield_24h']:>5} "
            f"sn={summary['signal_noise']} tos={summary['tos']}")
        # Phase B+ (5/5): selective auto-append agent learning on meaningful
        # events only (status_change / incident / tos / yellow_streak ≥ 3).
        # Routine green-passes-green = no append → memory stays useful instead
        # of becoming a daily timestamp dump.
        if not dry_run:
            yaml_after = _read_yaml(KPI_DIR / f"{agent_id}.yaml")
            ystreak = int(yaml_after.get("yellow_streak", 0))
            target_after = yaml_after.get("target_kpi") or {}
            current_after = yaml_after.get("current_kpi") or {}
            _maybe_append_eval_learning(summary, current_after, target_after,
                                        ystreak, chief_id)
    if not dry_run:
        _append_chief_meeting_learning(
            chief_id,
            {"green": counts["green"], "yellow": counts["yellow"],
             "red": counts["red"], "incidents": incidents},
            len(agents),
        )
    return {**counts, "total": sum(counts.values()), "incidents": incidents}


# ----------------------------------------------------------------------
# Phase B+ (5/5) — agent_memory.append_learning auto-trigger from chief
# ----------------------------------------------------------------------
# Selective triggers: only append a learning when the eval surfaces a
# *meaningful* event, not on every routine green-passes-green day. Agent
# memory token budget is finite (Tier 1 = 6000) so noise = bad.
#
# Triggers (in priority order — at most ONE learning emitted per agent per
# eval pass to keep volume bounded):
#   1. status_changed         → "yellow→red" / "green→yellow" / "red→green"
#   2. incident_opened        → ref incident id
#   3. tos_violations > 0     → ToS friction warning
#   4. yellow_streak ≥ 3      → "持續 yellow 3+ days" (escalation hint)
#   else                      → no append (routine operation)

def _maybe_append_eval_learning(summary: dict, current: dict, target: dict,
                                  yellow_streak: int, chief_id: str) -> None:
    """Best-effort: emit at most one learning per agent per eval. Failures
    swallowed (history_log already non-fatal; same posture here)."""
    try:
        from agents._common.agent_memory import append_learning
    except Exception:
        return

    agent_id = summary["agent_id"]
    status = summary["status"]
    prev_status = summary["prev_status"]
    yld = summary["yield_24h"]
    sn = summary["signal_noise"]
    tos = summary["tos"]
    incident_id = summary.get("incident_opened")
    target_yld = target.get("msg_yield_baseline_24h", "?")

    line = None
    category = "eval"

    # Priority 1 — status change
    if status != prev_status:
        line = (f"status {prev_status}→{status} · yield_24h={yld}/{target_yld} "
                f"sn={sn} tos={tos} · evaluator={chief_id}")
        category = "status_change"

    # Priority 2 — incident opened (only if we didn't already emit a status_change line; status→red implies both, prefer the richer status line)
    elif incident_id:
        line = f"incident opened: {incident_id} · yield={yld} sn={sn} tos={tos}"
        category = "incident"

    # Priority 3 — ToS friction (independent — log even if status unchanged)
    elif tos and tos > 0:
        line = f"⚠ ToS friction: {tos} violations this 24h window · evaluator={chief_id}"
        category = "tos_warning"

    # Priority 4 — yellow streak escalation hint
    elif status == "yellow" and yellow_streak >= 3:
        line = (f"yellow streak day {yellow_streak} · yield={yld}/{target_yld} sn={sn} "
                f"· hint: chief 應 review keyword/scope")
        category = "streak_warning"

    if line is None:
        return  # routine — don't pollute memory

    try:
        append_learning(agent_id, line, category=category, boss_curated=False)
    except Exception:
        # append_learning already logs on failure; don't bring eval down
        pass


def _append_chief_meeting_learning(chief_id: str, res: dict, managed: int) -> None:
    """Append a one-line digest to the Section Chief's own memory after the
    eval pass completes. Surfaces 「本週評了哪些、哪些 yellow/red」 for the chief
    to recall in future passes."""
    try:
        from agents._common.agent_memory import append_learning
    except Exception:
        return
    inc_n = len(res.get("incidents") or [])
    line = (f"eval pass: managed={managed} "
            f"{res.get('green',0)}g/{res.get('yellow',0)}y/{res.get('red',0)}r"
            + (f" · +{inc_n} incidents" if inc_n else ""))
    try:
        append_learning(chief_id, line, category="eval_pass", boss_curated=False)
    except Exception:
        pass


def run_pass(dry_run: bool = False) -> dict:
    """Multi-chief eval pass. For each Section Chief, filter their managed
    Field Agents and evaluate. Default singleton (SECTION_CHIEF) manages
    every agent — backward compat preserved."""
    base_path = INSTANCE_DIR / "policy" / "agent_kpi_baseline.yaml"
    if not base_path.exists():
        log(f"baseline missing: {base_path}")
        return {"error": "baseline_missing"}

    baseline = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    all_agents = (baseline.get("field_agent") or {})
    if not all_agents:
        log("no field_agent baselines found")
        return {"total": 0}

    chiefs = list_chiefs()
    log(f"multi-chief eval: chiefs={chiefs} · agents_total={len(all_agents)}")

    overall = {"green": 0, "yellow": 0, "red": 0, "total": 0, "incidents": [],
               "per_chief": {}}

    # ISO week for digest stub
    iy, iw, _ = datetime.now(TZ).isocalendar()
    week_iso = f"{iy:04d}-W{iw:02d}"

    conn = get_connection()
    try:
        for chief_id in chiefs:
            agents = _agents_managed_by(chief_id, all_agents)
            log(f"== chief {chief_id} ==  managed={len(agents)}")
            res = run_pass_for_chief(conn, chief_id, agents, dry_run=dry_run)
            overall["green"] += res["green"]
            overall["yellow"] += res["yellow"]
            overall["red"] += res["red"]
            overall["total"] += res["total"]
            overall["incidents"].extend(res["incidents"])
            overall["per_chief"][chief_id] = res
            if not dry_run:
                try:
                    _write_digest_stub(chief_id, week_iso, res)
                except Exception as e:
                    log(f"digest stub write fail for {chief_id}: {type(e).__name__}: {e}")
                # Phase B (5/5): organization audit trail. Each chief's eval
                # pass = one "meeting" with their managed agents. Surfaces in
                # daily brief 「🏛️ 組織狀態」 + scripts/org.py meetings.
                try:
                    from processors.history_log import log_event
                    log_event(
                        actor=chief_id,
                        kind="meeting",
                        scope="section_chief",
                        title=f"{chief_id} eval pass: managed={len(agents)} "
                              f"{res['green']}g/{res['yellow']}y/{res['red']}r"
                              + (f" · +{len(res['incidents'])} inc" if res['incidents'] else ""),
                        body=f"chief={chief_id}\nmanaged_agents={len(agents)}\n"
                             f"green={res['green']}\nyellow={res['yellow']}\n"
                             f"red={res['red']}\nincidents_opened={len(res['incidents'])}",
                        refs=[i[1] for i in res['incidents']] or None,
                    )
                except Exception as e:
                    log(f"meeting log_event fail (non-fatal): {type(e).__name__}: {e}")
    finally:
        conn.close()

    log(f"pass: green={overall['green']} yellow={overall['yellow']} "
        f"red={overall['red']} incidents_opened={len(overall['incidents'])}")
    if not dry_run:
        _hist("metric",
              f"section chief eval ({len(chiefs)} chief): "
              f"{overall['green']}g / {overall['yellow']}y / {overall['red']}r"
              + (f" · {len(overall['incidents'])} incidents" if overall['incidents'] else ""),
              body=f"per_chief={overall['per_chief']}\nchiefs={chiefs}",
              refs=[i[1] for i in overall['incidents']])
        if overall['incidents']:
            try:
                from datetime import datetime as _dt
                BRIEF_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
                ts = _dt.now(TZ).strftime("%Y%m%dT%H%M%S")
                q = BRIEF_QUEUE_DIR / f"pending_{ts}_section_chief_eval_incident.md"
                inc_lines = "\n".join(f"• {i[0]} → 小主管已開立 incident 調查" for i in overall['incidents'][:5])
                q.write_text(
                    f"[小主管 FYI] 情報員異常（{len(overall['incidents'])} 件）\n\n"
                    f"{inc_lines}\n\n"
                    f"艦隊：{overall['green']} 正常 / {overall['yellow']} 警告 / {overall['red']} 異常。"
                    f"小主管自主處理，不需行動。",
                    encoding="utf-8",
                )
            except Exception as e:
                log(f"brief_queue incident notify fail (non-fatal): {type(e).__name__}: {e}")
        try:
            from processors.org_task_audit_refresh import refresh_org_task_audit
            from processors.section_chief_work_audit import refresh_work_audit
            refresh_work_audit("section_chief_eval")
            refresh_org_task_audit("section_chief_eval")
        except Exception:
            pass
    return overall


def consult(question: str, agent_id: str | None = None,
            chief_id: str | None = None) -> int:
    """Ad-hoc Section Chief consultation — boss 5/8 directive (parallel
    to chief_strategist.consult). Spawn LLM with SECTION_CHIEF.md skill
    + chief memory + relevant agent KPI/memory context, 1-shot Q&A.

    Usage:
      py processors/section_chief_eval.py --consult "P03 Bigo 連 3 天 yield<5，要不要改 KPI?" --agent P03_Bigo
      py processors/section_chief_eval.py --consult "全 fleet 哪個情報員最弱?"

    Output ≤500 字, plain text, no memo / directive write.
    For Commander relay path: commander 收到 boss 問題 → 判斷是 Section-Chief 級
    → spawn this → relay stdout 給 boss with [小主管] prefix.
    """
    skill_path = ROOT / "personas" / "skills" / "SECTION_CHIEF.md"
    # NOT inline — SECTION_CHIEF.md is 44KB, exceeds Windows CreateProcess
    # cmdline limit (8K-32K) when passed via --append-system-prompt. Instead,
    # tell spawn'd claude to Read the file as first action (claude_run already
    # adds the repo root via --add-dir so Read can resolve relative paths).

    # Resolve which chief to consult
    if not chief_id:
        if agent_id:
            kpi_path = KPI_DIR / f"{agent_id}.yaml"
            if kpi_path.exists():
                kpi_data = _read_yaml(kpi_path)
                chief_id = kpi_data.get("managed_by") or kpi_data.get(
                    "last_evaluated_by") or DEFAULT_CHIEF
            else:
                chief_id = DEFAULT_CHIEF
        else:
            chief_id = DEFAULT_CHIEF

    # Build context block (agent KPI + agent memory tail if applicable)
    context_lines: list[str] = [f"## 你是 chief = {chief_id}"]
    if agent_id:
        kpi_path = KPI_DIR / f"{agent_id}.yaml"
        if kpi_path.exists():
            context_lines.append(f"\n## 被諮詢情報員 {agent_id} 當前 KPI yaml\n")
            context_lines.append("```yaml")
            context_lines.append(kpi_path.read_text(encoding="utf-8")[:3000])
            context_lines.append("```")
        agent_mem_path = MEMORY_DIR / f"{agent_id}.md"
        if agent_mem_path.exists():
            mem_text = agent_mem_path.read_text(encoding="utf-8")
            tail = mem_text[-3000:] if len(mem_text) > 3000 else mem_text
            context_lines.append(f"\n## {agent_id} 記憶 (tail 3K chars)\n")
            context_lines.append(tail)
    else:
        # Global question — list managed agent ids + status summary
        context_lines.append("\n## Fleet 概覽 (各情報員 status snapshot)")
        context_lines.append("執行 `py scripts/agents.py ls` 拿即時狀態")

    log(f"consult chief={chief_id} agent={agent_id} q={question[:80]!r}")

    prompt = (
        f"# Ad-hoc 小主管 Consultation (no KPI write, no incident open)\n\n"
        + "## 你的 skill (FIRST ACTION: Read this)\n"
        + f"`{skill_path}` — 你的角色定義跟 SOP 都在這。Read 一次。\n\n"
        + "\n".join(context_lines)
        + f"\n\nBoss 透過 commander 問:\n\n\"\"\"\n{question}\n\"\"\"\n\n"
        + "## Chief lens\n"
        + "- You are an intelligence section chief, not a KPI clerk.\n"
        + "- Judge coverage balance, collection objectivity, evidence-chain quality,\n"
        + "  KB quality, collection blockers, and agent retasking before quoting KPI.\n"
        + "- If a surface is green but operationally blind (login failure, selector\n"
        + "  drift, thin evidence, queue backlog), say it is unhealthy and name the fix.\n\n"
        + "## Constraints\n"
        + "- 繁體中文\n"
        + "- ≤500 字（boss 在 TG 看，螢幕小）\n"
        + "- 無 markdown 表格、無 code block\n"
        + "- 直接給判斷 + 建議行動；不客套；不結語\n"
        + "- 必要時用 Read/Bash/Grep 工具讀 runtime/agent_kpi/ runtime/agent_memory/ "
        + "runtime/agent_incidents/ runtime/raw/ jsonl 或 SQL 查 messages 印證\n"
        + "- 資訊不足就老實說「不夠斷，建議跑下班 17:00 daily eval」\n"
        + "- 引用具體數字 / agent_id / incident_id / 訊息範例 where possible\n\n"
        + "## Output\n"
        + "純答案，無 metadata，無 markdown header。\n"
    )

    sys.path.insert(0, str(ROOT))
    from processors._llm_synth import claude_run, MODEL_FOR_PER_SIGNAL
    ok, stdout = claude_run(
        prompt,
        skill_prefix=False,
        extra_system="",  # skill loaded via Read in prompt (cmdline-size workaround)
        allowed_tools="Read,Bash,Grep,Glob",
        permission_mode="default",
        model=MODEL_FOR_PER_SIGNAL,
        pass_model_flag=True,
        timeout_s=300.0,
        agent_memory_id=chief_id,  # §15.Y memory injection
    )
    if not ok:
        log("consult FAILED")
        print("⚠ 小主管 接線員 timeout / 失敗，建議切回主 session 或等 17:00 daily eval")
        return 1
    print(stdout.strip())
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--consult", default=None, metavar="QUESTION",
                   help="Ad-hoc 1-shot Section Chief consultation (no KPI write); "
                        "for commander→chief relay")
    p.add_argument("--agent", default=None, metavar="AGENT_ID",
                   help="With --consult: focus the question on a specific Field "
                        "Agent (e.g. P03_Bigo). Auto-resolves managed-by chief.")
    p.add_argument("--chief", default=None, metavar="CHIEF_ID",
                   help="Override which chief answers (default: agent's "
                        "managed_by or SECTION_CHIEF)")
    args = p.parse_args()
    if args.consult:
        rc = consult(args.consult, agent_id=args.agent, chief_id=args.chief)
        raise SystemExit(rc)
    run_pass(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
