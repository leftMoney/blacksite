---
skill_id: business_analyst
status: alias
canonical: personas/skills/SECTION_CHIEF.md
renamed_at: 2026-05-02T22:35:00+07:00
reason: "CLAUDE.md §15 3-tier reorg — Tier 2 small chief is now SECTION_CHIEF; this file kept as alias for any lingering imports/refs"
---

# BUSINESS_ANALYST — alias for SECTION_CHIEF (renamed 5/2 §15 reorg)

This file is an alias redirect. The canonical Tier 2 小主管 / 情報課長 skill is
now [`personas/skills/SECTION_CHIEF.md`](SECTION_CHIEF.md).

If you are an LLM and reached this file via a hardcoded path, IMMEDIATELY
read the canonical at `personas/skills/SECTION_CHIEF.md` and use that as
your skill prefix. Do not synthesize from this redirect file alone.

`processors/_llm_synth.py` SKILL_PATH was updated 2026-05-02 to point at
SECTION_CHIEF.md, so all daily_brief / card_builder / Manager Pack flows
now load the canonical. This redirect remains for:
- Any external script that still hardcodes BUSINESS_ANALYST.md path
- Documentation references in older code comments
- git history reference

The skill content itself is unchanged from the 5/2 PM version PLUS the
new Tier 2 sections (§13 KPI Evaluator / §14 Field Agent Feedback /
§15 Incident Authoring / §16 Strategist Digest / §17 Strategy Directive
Reading) — see SECTION_CHIEF.md for full text.
