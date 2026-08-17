"""One-shot: bring 12 persona-driven Field Agents LIVE per boss 5/6 directive.

Reads persona_warmup_schedule.yaml → for each (persona, platform), update or create
the agent KPI yaml with status=live + schedule reference + storage_state path.
"""
import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path
import yaml
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TZ = timezone(timedelta(hours=7))
NOW = datetime.now(TZ).isoformat(timespec="seconds")

KPI_DIR = ROOT / "instances" / "_TEMPLATE" / "runtime" / "agent_kpi"
SCHED = yaml.safe_load((ROOT / "instances" / "_TEMPLATE" / "policy" /
                       "persona_warmup_schedule.yaml").read_text(encoding="utf-8"))

# Map agent_id from schedule → canonical KPI yaml name
# Some schedule entries use slightly different agent_id (e.g. P04_TikTok_sports vs P04_TikTok)
AGENT_FILE_MAP = {
    "P03_Pantip": "P03_Pantip.yaml",
    "P05_Pantip": "P05_Pantip.yaml",
    "P04_YouTube_sports": "P04_YouTube_sports.yaml",
    "P03_FB": "P03_FB.yaml",
    "P04_FB": "P04_FB.yaml",  # to be created
    "P03_IG": "P03_IG.yaml",
    "P04_IG": "P04_IG.yaml",  # to be created
    "P03_TikTok": "P03_TikTok.yaml",
    "P04_TikTok_sports": "P04_TikTok_sports.yaml",
    "P04_X": "P04_X.yaml",
    "P05_Discord": "P05_Discord.yaml",
    "P05_Reddit": "P05_Reddit.yaml",
}

DEFAULT_TEMPLATE = {
    "sub_class": "persona_driven",
    "current_kpi": {
        "msg_yield_24h": 0,
        "signal_noise": None,
        "tos_violations": 0,
        "tier_hint_accuracy": None,
        "warmup_compliance": None,
        "persona_consistency": None,
        "identity_axis_isolation": True,
    },
    "target_kpi": {
        "msg_yield_baseline_24h": 100,
        "signal_noise_min": 0.3,
        "tos_violation_max": 0,
        "tier_hint_accuracy_min": 0.6,
    },
    "status": "live",
    "recent_directives": [],
    "incident_history": [],
    "target_kpi_history": [],
    "yellow_streak": 0,
    "managed_by": "SECTION_CHIEF",
}

processed = []
for window in SCHED["daily_windows"]:
    aid = window["agent_id"]
    fname = AGENT_FILE_MAP.get(aid)
    if not fname:
        print(f"WARN: schedule has agent_id {aid!r} not in AGENT_FILE_MAP — skip")
        continue
    fpath = KPI_DIR / fname
    persona, platform = window["persona"], window["platform"]
    state_file = ROOT / "personas" / persona / "state" / f"{platform.lower()}_storage_state.json"

    # Load existing or create new
    if fpath.exists():
        data = yaml.safe_load(fpath.read_text(encoding="utf-8"))
        action = "UPDATE"
    else:
        data = {**DEFAULT_TEMPLATE, "agent_id": aid}
        action = "CREATE"

    # Apply LIVE upgrade (preserve existing KPIs / incidents)
    data["status"] = "live"
    data["last_evaluated_at"] = NOW
    data["last_evaluated_by"] = "SECTION_CHIEF"
    data["live_status"] = {
        "live_since": NOW,
        "scheduled_window": window["hh"],
        "primary_focus": window["primary_focus"],
        "storage_state_path": str(state_file.relative_to(ROOT)).replace("\\", "/"),
        "warmup_sop": f"personas/warmup/{platform.lower()}.md",
        "follow_targets": f"instances/_TEMPLATE/policy/persona_follow_targets/{persona}.yaml",
        "schedule_ref": "instances/_TEMPLATE/policy/persona_warmup_schedule.yaml",
    }
    data["scope_lock"] = window.get("notes", "")
    if "managed_by" not in data:
        data["managed_by"] = "SECTION_CHIEF"

    fpath.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    processed.append((action, aid, str(fpath.relative_to(ROOT))))
    print(f"  {action:<6} {aid:<22} window={window['hh']:<13} status=live")

print()
print(f"=== {len(processed)} agents brought LIVE ===")

# log milestone
from processors.history_log import log_event
hid = log_event(
    actor="SECTION_CHIEF", kind="milestone", scope="multi_agent_org",
    title=f"Field Agents 全部上工 — {len(processed)}/12 persona-driven LIVE per 5/6 schedule",
    body=f"Boss 5/6 directive: 全部情報員上工. 12 Field Agents (P03×4 / P04×5 / P05×3) "
         f"transitioned to live status with schedule reference, storage_state path, "
         f"warmup SOP cross-link, and follow_targets list. P05_FB excluded (abandoned_opsec). "
         f"Daily windows 07:00-15:00 GMT+7 per persona_warmup_schedule.yaml. "
         f"Section Chief orchestrator (processors/section_chief_orchestrate.py) reads schedule "
         f"hourly + spawns agents at their windows. {len(processed)} yamls touched: "
         + ", ".join(f"{a}:{aid}" for a, aid, _ in processed),
    refs=["instances/_TEMPLATE/policy/persona_warmup_schedule.yaml",
          "instances/_TEMPLATE/policy/persona_follow_targets/P03.yaml",
          "instances/_TEMPLATE/policy/persona_follow_targets/P04.yaml",
          "instances/_TEMPLATE/policy/persona_follow_targets/P05.yaml",
          "kb/playbooks/REGISTER_LESSONS.md"])
print(f"history#{hid} logged")
