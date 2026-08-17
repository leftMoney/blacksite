---
warmup_id: discord
last_updated: 2026-05-06T12:55+07:00
register_lessons_ref: "kb/playbooks/REGISTER_LESSONS.md §2.5"
persona_assignments: [P05]
storage_state_path: "personas/P05/state/discord_storage_state.json"
---

# Discord Warmup SOP

> Register: hCaptcha drag-tile boss-manual; email link verify — see REGISTER_LESSONS §2.5.

## Persona scope lock

| Persona | primary_verticals | forbidden |
|---|---|---|
| P05 (AI shell) | ai_tools_communities / sea_tech_servers / open-source / productivity | folk-belief / lottery / gambling / sports_betting |

Discord 是 community-graph platform (not algorithm-feed) — warmup focus is **join + lurk + signal**.

## Phase A — Server discovery (1-2 / week)

Daily 不 join；weekly 策略長 給 1-2 個 target server invite（從 weekly memo §6 KOL Ecosystem 對應 AI tool / target-country tech / regional pop-trends 板）。Field Agent execute join via Camoufox load storage_state.

## Phase B — Active scan (10 min/day)

1. 進已 join 的 servers，**輪流** view 各 #general / #news / #announcements channel
2. **不發言**，只讀
3. **react emoji** ≤2 次/server/day on relevant messages（low-touch presence signal）
4. visit 2-3 highest-volume server 的 latest messages

## Phase C — Logged passive

- Raw JSONL `runtime/raw/P05_Discord/<date>.jsonl`
- Capture: server name / channel / poster (anonymized as `entity_id` only) / message text / link / image OCR
- Entity extract: AI tool brands / target-country startup names / open-source projects

## OPSEC checklist

- ❌ Never DM strangers
- ❌ Never @mention or reply in public channel
- ❌ Never join voice channel
- ❌ Never accept friend request without 策略長 directive (auto-decline via UI)
- ⚠ If server admin DMs P05 about inactivity → minimal canned reply or silence
- ⚠ Discord rate-limits join speed — never join >2 servers/day

## Anti-overlap rule

P05 only persona on Discord. No same-platform overlap concern; instead anti-overlap with P05_Reddit / P05_LocalForum = different hour windows.
