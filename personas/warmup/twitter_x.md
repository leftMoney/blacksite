---
warmup_id: twitter_x
last_updated: 2026-05-06T12:55+07:00
register_lessons_ref: "kb/playbooks/REGISTER_LESSONS.md (no §2 entry; manual register 5/5)"
persona_assignments: [P04]
storage_state_path: "personas/P04/state/twitter_x_storage_state.json"
---

# X (Twitter) Warmup SOP

> X may require manual registration (no automated register script). Once created, set the
> persona's username in `.env` as `PERSONA_<id>_X_USERNAME`.

## Persona scope lock

| Persona | primary_verticals | forbidden |
|---|---|---|
| P04 (Sports) | sports_betting_meta / local football match analysis / local_combat_sport stats / odds discussion / local esports | folk-belief / lottery / non-sports content |

X 是 sports betting yolk-adjacent surface（不直接賭，但聊賠率 + 賽前分析 + KOL 是賽事書 / sportsbook 的高價值情報）。

## Phase A — Algorithm-shape (10 min/day)

1. Scroll For You feed ~30 tweets
2. **Like** 5-7 sports-vertical tweets
3. **Bookmark** 1-2 (algo signal stronger than like)
4. **Repost (RT)** 0 — silent presence preserved
5. Tap into 2-3 threads, read replies (depth signal)

## Phase B — Active scan (10 min/day)

- Visit 18 sport-meta handles in `policy/x_targets.yaml` rotating (3-5/day)
- Search 1 keyword from `policy/P04_search_seeds.yaml` (策略長 weekly priority)
- View Trending (target country) section (lower-priority but quick scan)

## Phase C — Logged passive

- Raw JSONL `runtime/raw/P04_X/<date>.jsonl`
- Capture: tweet text / author handle / engagement metrics / mentions / hashtags / linked images (OCR queue)
- Entity extract: examplebet / examplebrand / target-country sports KOL handles

## OPSEC checklist

- ❌ Never reply / quote-tweet / DM
- ❌ Never post original tweet
- ❌ Never follow back unsolicited follower (auto-ignore)
- ⚠ X may shadow-ban inactive accounts — Phase A likes carry minimum engagement signal
- ⚠ X strict on TOS for sports betting promotion content — read-only is the safe lane

## Anti-overlap rule

P04 only persona on X. Anti-overlap with P04_FB / P04_IG / P04_TikTok / P04_YouTube_sports — Section Chief schedules different hour windows per `runtime/agent_kpi/P04_X.yaml`.
