---
warmup_id: localforum
last_updated: 2026-05-06T12:55+07:00
register_lessons_ref: "kb/playbooks/REGISTER_LESSONS.md §2.7"
persona_assignments: [P03, P05]
storage_state_path: "personas/<P>/state/localforum_storage_state.json"
---

# LocalForum Warmup SOP

> Generic template for an in-country, non-algorithmic discussion forum.
> Register: 2-click flow + 6-box code keyboard.type + 1h cooldown — see REGISTER_LESSONS §2.7.

## Persona scope locks

| Persona | primary_verticals | forbidden |
|---|---|---|
| P03 (folk-belief) | fortune-telling / horoscope / lottery / dream-interpretation | sports_betting / politics |
| P05 (AI shell) | ai_tools / tech / productivity / regional_lifestyle / pop_trends | folk-belief / lottery / gambling |

P03 + P05 **never overlap** verticals. P03 = belief economy; P05 = secular tech.

## Phase A — Algorithm-shape (10 min/day)

LocalForum 是 forum 不是 algorithm-driven feed，所以 "shape" 是手動更顯著：

1. 進首頁 https://localforum.example/
2. 點選 2-3 個 primary_vertical 分類板（e.g. P03 → 命理板; P05 → 軟體 / technology 板）
3. **點開** 4-5 個帖子讀（停留 ≥15s 各）
4. **按讚（贊）** 2-3 個 vertical-relevant 帖
5. **收藏** 1 帖
6. 讀完不留言（避免 OPSEC §1 觸發）

## Phase B — Active scan (5-10 min/day)

- Visit 1 followed user/board profile
- Search 1 keyword from `policy/<persona>_search_seeds.yaml`

## Phase C — Logged passive

- Raw JSONL `runtime/raw/<persona>_LocalForum/<date>.jsonl`
- Entity extract enabled
- Geo-walled yolk content (local-only tag) is the unique value here — capture it

## OPSEC checklist

- ❌ Never reply / comment (creates pseudo-public footprint)
- ❌ Never PM (DM equivalent)
- ⚠ LocalForum 風控：失敗多次會 1h cooldown，本帳號慎用
- ⚠ Email cooldown 機制：see REGISTER_LESSONS §2.7

## Anti-overlap rule

P03 + P05 同帳同 IP — Section Chief 排不同 hour（e.g. P03 09:00 / P05 14:00）。
