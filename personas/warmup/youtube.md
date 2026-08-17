---
warmup_id: youtube
last_updated: 2026-05-06T12:55+07:00
register_lessons_ref: "kb/playbooks/REGISTER_LESSONS.md §2.8 (Google login)"
persona_assignments: [P04]
storage_state_path: "personas/P04/state/youtube_storage_state.json"
---

# YouTube Warmup SOP

> No new register — uses existing `PERSONA_P04_GMAIL` Google account (Q5 2026-05-05 22:17 hotel CC).
> Login via Google flow — see REGISTER_LESSONS §2.8.

## Persona scope lock

| Persona | primary_verticals | forbidden |
|---|---|---|
| P04 (Sports) | local football highlights / local_combat_sport full-fights / sports KOL channels (ExampleSportsChannel / ExampleAthlete / ExampleAthlete2) / local esports | folk-belief / lottery / non-sports content |

YouTube 是 P04 algorithm-shape 最重要的 funnel — 一旦 trained，FYP 推 sports-vertical 內容大幅密集。

## Phase A — Algorithm-shape (15-20 min/day, longest of P04 platforms)

1. Open home (signed in) → scroll recommendations ~20 videos
2. **Watch** 3-5 videos full-length (>3 min each, algorithm-strongest signal)
3. **Like** 2-3 videos
4. **Subscribe** 1 channel/week (cumulative; never >50 total to avoid bot pattern)
5. **Save to playlist** 1-2 videos (stronger than like)
6. Skip / dislike forbidden verticals within 5 sec

## Phase B — Active scan (10 min/day)

- Visit 2-3 subscribed channel pages, view latest 5 video metadata
- Search 1 keyword from `policy/P04_yt_search_seeds.yaml` (策略長 weekly priority)
- Browse Trending (target country) > Sports tab

## Phase C — Logged passive

- Raw JSONL `runtime/raw/P04_YouTube/<date>.jsonl`
- Capture: video title / channel / duration / view count / upload date / top 5 comments
- Captions auto-download (if available) → ASR backup queue
- Entity extract: target-country sports KOL / sports brands / sportsbook mentions

## OPSEC checklist

- ❌ Never comment
- ❌ Never upload video
- ❌ Never live-stream join in chat
- ⚠ Don't bulk-subscribe (>3 channels/day looks bot-pattern)
- ⚠ Watch-time fingerprint matters; don't skip every video instantly (Phase A's full-length watch carries plausibility)

## Anti-overlap rule

P04 only persona on YouTube. Anti-overlap with other P04 platforms — Section Chief schedules different hour window per `runtime/agent_kpi/P04_YouTube_sports.yaml`.

## Known existing infra

- `agents/youtube/yt_search.py` cron `:30 every 6h` — hashtag search (anonymous, doesn't need login)
- `agents/youtube/yt_channel_monitor.py` cron `:45 every 2h` — channel content monitor
- This warmup adds **logged-in FYP shaping** layer (different signal — algorithm-personalized FYP, not search)
