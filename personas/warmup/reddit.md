---
warmup_id: reddit
last_updated: 2026-05-06T12:55+07:00
register_lessons_ref: "kb/playbooks/REGISTER_LESSONS.md §2.6"
persona_assignments: [P05]
storage_state_path: "personas/P05/state/reddit_storage_state.json"
---

# Reddit Warmup SOP

> Register: email → 6-digit verify → username/pwd. shadow DOM web-component — see REGISTER_LESSONS §2.6.

## Persona scope lock

| Persona | primary_verticals | forbidden |
|---|---|---|
| P05 (AI shell) | ai_tools / r/the target country / r/SoutheastAsia / r/MachineLearning / r/buildapc / r/programming | folk-belief / lottery / sports_betting |

## Phase A — Subscribe + algorithm-shape (10 min/day)

1. **Subscribe** 5-10 P05-vertical subreddits (one-time setup; persist in `policy/P05_subreddits.yaml`)
2. Daily home-feed scroll 30 posts
3. **Upvote** 5-7 vertical-relevant posts
4. **Save** 1-2 posts (bookmark)
5. **Click into** 3-5 threads, read first 5 comments each (engagement depth signal)

## Phase B — Active scan (5-10 min/day)

- Visit r/<target-country-subreddit> `/new` (target-country conversation pulse — yolk-adjacent for boss intel)
- Search 1 keyword from `policy/P05_search_seeds.yaml` (策略長 weekly priority)
- Read 1 trending post in primary_vertical

## Phase C — Logged passive

- Raw JSONL `runtime/raw/P05_Reddit/<date>.jsonl`
- Capture: subreddit / post title / author (entity_id) / score / top 5 comments / link
- Entity extract: AI tool brands / target-country community names / event mentions

## OPSEC checklist

- ❌ Never comment / reply
- ❌ Never post submission
- ❌ Never DM
- ❌ Never join private subreddit (avoids captcha + admin scrutiny)
- ⚠ Reddit shadow-bans accounts that don't engage at all — Phase A's upvotes carry minimum activity signal

## V6 Reddit Script-App backlog

Reddit 有 PRAW API 可程式化 (more efficient than browser scraping). Boss procurement #5 待 `reddit.com/prefs/apps` 5-min setup → 之後 P05_Reddit 可 dual-mode (browser warmup + PRAW bulk read).
