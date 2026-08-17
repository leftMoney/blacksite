"""scripts/gen_agent_memory_stubs.py — generate agent_memory stubs for all
known agents (Tier 1 Field Agents from agent_kpi_baseline.yaml + Tier 2
SECTION_CHIEF + Tier 3 CHIEF_STRATEGIST).

Idempotent: skips existing files unless --overwrite. New agents added to
baseline yaml later need to re-run this with the new id.

Per CLAUDE.md §15 + boss 5/3 directive.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents._common.agent_memory import write_stub, list_memory_files  # noqa: E402

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
BASELINE = INSTANCE_DIR / "policy" / "agent_kpi_baseline.yaml"


# Identity hints per agent — short, taken from PERSONAS.md / FIELD_AGENT.md /
# baseline notes. Engine uses these as starter context; agents append learnings
# as they accumulate experience.
PERSONA_IDENTITY = {
    "P01": "Anglo-Asian internet citizen, target-country curiosity. Sacred TG persona, never expand beyond TG.",
    "P02": "Anglo-Asian internet citizen, target-country curiosity. Sacred TG persona, never expand beyond TG.",
    "P03": "Example yolk persona — folk-belief / lottery vertical.",
    "P04": "Example white persona — sports vertical.",
    "P05": "AI Helper — shell persona, tech / lifestyle generalist.",
}

PLATFORM_JOB = {
    "TG": "Telegram grey-channel infiltration; raw JSONL → 小主管 daily ingest.",
    "Bigo": "Bigo Live room comments + virtual-gift signals; gift-laundering surface.",
    "FB": "Facebook Pages logged-in lurker (mbasic dead 2026-04-30); read-only per §9.3.",
    "IG": "Instagram read-only lurker per §9.3; no posting/reacting/DM.",
    "TikTok": "TikTok feed scraping; v1 hold (no in-country residential IP).",
    "TikTok_sports": "Sports KOL ecosystem; high monthly reach on sports hashtag (example metric).",
    "LocalForum": "In-country discussion forum read-heavy (VPN required).",
    "Livestream": "Livestream gift platform + tip mechanic.",
    "YouTube_sports": "YouTube algorithm-shape via subscriptions.",
    "X": "X / Twitter sports + grey-market cross-pollination.",
    "Discord": "Gambling/gaming overlap servers.",
    "Reddit": "r/<target-country-subreddit> + grey-adjacent subs (praw).",
    "LocalSocial": "In-country lifestyle social catch-all.",
}

ANON_IDENTITY = {
    "ottA_anon": "Local OTT scanner — ottA, low-priority.",
    "ottB_anon": "Local OTT scanner — ottB, selector tuning v1.6.",
    "streamA_anon": "Local OTT scanner — streamA.",
    "streamB_anon": "Local OTT scanner — streamB, top-priority platform (visits/mo — example metric).",
    "newsportalA_anon": "Local portal scanner — newsportalA (newsportalB replacement v1.7).",
    "newsportalB_anon": "Local portal scanner — newsportalB; replaced by newsportalA; backfill only.",
    "bigo_lobby_anon": "Bigo lobby anonymous scan; gift-laundering surface.",
    "livestream_lobby_anon": "Livestream lobby scanner — skeleton-loader from datacenter IP.",
    "fb_page_anon": "FB Pages anonymous — DEAD 2026-04-30 (Meta closed mbasic loophole).",
}


def _identity_for_persona_agent(agent_id: str) -> tuple[str, str]:
    """Return (identity, job) for persona_driven agent."""
    persona, _, platform = agent_id.partition("_")
    persona_intro = PERSONA_IDENTITY.get(persona, f"Persona {persona}")
    job = PLATFORM_JOB.get(platform, f"Platform {platform} collection.")
    return persona_intro, job


def _identity_for_anon(agent_id: str) -> tuple[str, str]:
    intro = ANON_IDENTITY.get(agent_id, f"Anonymous web scanner — {agent_id}.")
    return intro, intro


def _identity_for_chief() -> tuple[str, str]:
    return (
        "I am Tier 2 Section Chief (小主管 / 情報課長) — daily intel synthesizer.",
        "Synthesize 24h Field Agent raw → KB cards + leads; evaluate Field Agent KPIs; "
        "open incidents; weekly digest to 策略長.",
    )


def _identity_for_strategist() -> tuple[str, str]:
    return (
        "I am Tier 3 Chief Strategist (策略長 / Director of Intelligence) — single executive synthesizer.",
        "Weekly cross-day strategic synthesis for the client brand commercial decisions; "
        "issue directives to Section Chief; org-adjustment authority (boss 5/3 directive).",
    )


FIELD_CAPABILITIES = (
    "- 看自己 KPI: `py scripts/agents.py show <id>`\n"
    "- 看自己 memory: `py scripts/agents.py memory <id>`\n"
    "- Append learning: 透過 agents._common.agent_memory.append_learning\n"
    "- Read KPI yaml: `from agents._common.kpi_loader import load_kpi`\n"
    "- raw JSONL output: `runtime/raw/<persona_or_anon>/<platform>_<date>.jsonl`\n"
    "- ToS friction warning: `from processors.history_log import log_event` "
    "kind=warning scope=<platform>"
)

CHIEF_CAPABILITIES = (
    "- KB query: `py kb/query.py search|cards|entity|leads|memo|funnel|state`\n"
    "- Field Agent KPI eval: `processors/section_chief_eval.py`\n"
    "- Open incident: `py processors/agent_incidents.py open <agent_id> <kind> --hypothesis ...`\n"
    "- Modify Field Agent KPI: `py scripts/agents.py kpi <agent_id> --target K=V`\n"
    "- Write digest: `runtime/strategist_digest/<chief_id>_<YYYY-WW>.md`\n"
    "- Memory: `py scripts/agents.py memory SECTION_CHIEF [--compact]`"
)

STRATEGIST_CAPABILITIES = (
    "- KB query: `py kb/query.py search|cards|entity|leads|memo|funnel|state`\n"
    "- Read past memos: `runtime/strategy_memos/`\n"
    "- Read digests: `runtime/strategist_digest/`\n"
    "- Write strategy memo: `runtime/strategy_memos/<YYYY-WW>.md`\n"
    "- Write directive yaml (7 kinds): `runtime/strategy_directives/<YYYY-MM-DD>.yaml`\n"
    "- Org adjustment: chief_create / chief_dissolve / agent_reassign / metric_redefine / "
    "monitoring_track_open / org_meta_review / agent_kpi_adjust\n"
    "- Memory: `py scripts/agents.py memory CHIEF_STRATEGIST [--compact]`"
)


def gen_field_kpi_summary(cfg: dict) -> str:
    sub = cfg.get("sub_class", "persona_driven")
    parts = [f"sub_class: {sub}"]
    for k in ("msg_yield_baseline_24h", "signal_noise_min", "tos_violation_max",
              "tier_hint_accuracy_min", "selector_pass_rate_min",
              "geo_block_resilience_min", "content_rate_min"):
        if k in cfg:
            parts.append(f"{k}: {cfg[k]}")
    return "\n".join(f"- {p}" for p in parts)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--default-chief", default="SECTION_CHIEF",
                   help="default chief id all field agents are managed by")
    args = p.parse_args()

    if not BASELINE.exists():
        print(f"baseline missing: {BASELINE}")
        return 1
    base = yaml.safe_load(BASELINE.read_text(encoding="utf-8")) or {}
    field_agents = (base.get("field_agent") or {})

    created = []
    skipped = []

    # Tier 1 — Field Agents
    for agent_id, cfg in sorted(field_agents.items()):
        sub_class = cfg.get("sub_class", "persona_driven")
        if sub_class == "persona_driven":
            identity, job = _identity_for_persona_agent(agent_id)
        else:
            identity, job = _identity_for_anon(agent_id)
        # Append baseline notes to job if present
        notes = cfg.get("notes", "")
        if notes:
            job = f"{job} ({notes})"
        kpi_summary = gen_field_kpi_summary(cfg)
        ok = write_stub(
            agent_id,
            tier=1,
            sub_class=sub_class,
            identity=identity,
            job=job,
            kpi_summary=kpi_summary,
            capabilities=FIELD_CAPABILITIES,
            managed_by=args.default_chief,
            overwrite=args.overwrite,
        )
        (created if ok else skipped).append(agent_id)

    # Tier 2 — default Section Chief (singleton on bootstrap; multi-chief
    # added later via agents.py chief create)
    chief_identity, chief_job = _identity_for_chief()
    ok = write_stub(
        args.default_chief,
        tier=2,
        sub_class=None,
        identity=chief_identity,
        job=chief_job,
        kpi_summary=(
            "- brief library admission count (cards/day)\n"
            "- actionable lead ratio (escalated / total)\n"
            "- false-signal rate (resolved_closed_as_noise / total)\n"
            "- cross-platform corroboration rate\n"
            "- boss adoption rate of brief escalate section"
        ),
        capabilities=CHIEF_CAPABILITIES,
        managed_by=None,
        scope_tags=[],
        overwrite=args.overwrite,
    )
    (created if ok else skipped).append(args.default_chief)

    # Tier 3 — Chief Strategist
    strat_identity, strat_job = _identity_for_strategist()
    ok = write_stub(
        "CHIEF_STRATEGIST",
        tier=3,
        sub_class=None,
        identity=strat_identity,
        job=strat_job,
        kpi_summary=(
            "- boss adoption rate of memo directives\n"
            "- predictive lead time vs public-news baseline (target ≥ 14 days)\n"
            "- directive RoI (new monitoring tracks → actionable signal yield)\n"
            "- net new insight per memo (target ≥ 60% net new vs daily briefs)"
        ),
        capabilities=STRATEGIST_CAPABILITIES,
        managed_by=None,
        scope_tags=[],
        overwrite=args.overwrite,
    )
    (created if ok else skipped).append("CHIEF_STRATEGIST")

    print(f"created: {len(created)}")
    for c in created:
        print(f"  + {c}")
    print(f"skipped (already exist): {len(skipped)}")
    for s in skipped:
        print(f"  · {s}")
    print(f"\ntotal memory files: {len(list_memory_files())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
