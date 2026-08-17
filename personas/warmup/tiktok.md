---
warmup_id: tiktok
last_updated: 2026-05-06T12:55+07:00
register_lessons_ref: "kb/playbooks/REGISTER_LESSONS.md §2.4"
persona_assignments: [P03, P04]
storage_state_path: "personas/<P>/state/tiktok_storage_state.json"
---

# TikTok Warmup SOP

> DOM cheats / register details: see REGISTER_LESSONS §2.4. This file = warmup behaviour only.

## Persona scope locks

| Persona | primary_verticals | forbidden |
|---|---|---|
| P03 (folk-belief) | folk-belief / lottery / fortune-telling / horoscope / lucky_number / sexy_lifestyle_cover | sports_betting / politics |
| P04 (Sports) | local_combat_sport / local football / local esports / sports_match_analysis | folk-belief / lottery |

P03 + P04 must **NEVER cross verticals** — feeding both into same recommendation space pollutes Algorithm. See OPSEC §3 cross-persona contamination.

## Phase A — Algorithm-shape (10-15 min/day per persona)

1. Scroll FYP (`/foryou`) ~30 videos
2. Watch each ≥3s before swipe (algorithm signal)
3. **Like** 5-7 videos matching primary_verticals
4. **Save** 2-3 videos to bookmarks (stronger signal than like)
5. Watch 1-2 full-length (>30s) on top-vertical
6. Avoid: any video tagged `forbidden` — swipe within 1s

## Phase B — Active scan (5-10 min/day)

- Visit 2-3 followed KOL profiles, scroll their recent 10 videos
- Search 1 keyword from `policy/<persona>_search_seeds.yaml` (策略長 weekly priority)
- Enter 1 hashtag from `INSTANCE.md §1` yolk vertical

## Phase C — Logged passive (always-on while session active)

- Raw JSONL emit at `runtime/raw/<persona>_TikTok/<date>.jsonl`
- Entity extract via `processors/rules_layer.py`
- Capture FYP recommendations as algorithm-shape audit signal

## OPSEC checklist

- ❌ Never DM strangers / unsolicited messages
- ❌ Never duet / stitch with others (creates traceable graph edge)
- ❌ Never upload original video (P03/P04 are read-only intel personas)
- ⚠ If TikTok shows "verify your identity" → abandon session, log warning, escalate to chief
- ⚠ TikTok rate-limits same email after 5+ failed verify attempts; back off ≥12h before retry

## Anti-overlap rule (boss 5/6 directive 1)

Same persona must not have TikTok + IG + FB **all running same hour**. Section Chief schedules each persona's TikTok session in a discrete daily window per `runtime/agent_kpi/<P>_TikTok.yaml` `next_run_window` field.
