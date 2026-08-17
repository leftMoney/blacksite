# Bigo Live — persona warmup SOP

> Run for **3 days** before this persona enters target gambling rooms or
> KOL-following actions. Goal: convince Bigo's anti-fraud system that the
> account is a normal new user, not a bot, before the algorithm starts
> pushing gambling-adjacent rooms via the recommendation feed.

## Day 1 (post-register, ~30 min total split into 3 sessions)

| Session | Action | Duration | Notes |
|---|---|---|---|
| Morning | Open Bigo, scroll lobby, watch 2 random non-gambling streams ~3 min each | 10 min | Pick from `/show/` feed (variety / lifestyle / chat) |
| Afternoon | Browse 1 game category, watch 1 RoV/PUBG stream ~5 min | 10 min | Avoid clicking on any "VIP" / "lucky" / "win" rooms |
| Evening | Open Bigo home, scroll, follow 2 mainstream streamers (singer/dancer types) | 10 min | Following = signal, but stay non-gambling |

**Forbidden Day 1**:
- ❌ Sending gifts / chat messages
- ❌ Joining any "VIP" / "private" rooms
- ❌ Clicking on any link in a streamer's bio
- ❌ Searching for gambling-coded keywords (local-language "register", "jackpot", "lottery", etc.)

## Day 2

- Same pattern + start watching 1 chat-category stream ~10 min
- Follow 1 more mainstream streamer
- Read but DO NOT type in chat
- Total ~30 min split sessions

## Day 3 (end of warmup)

- Browse 1 local-tagged stream
- Watch ~10 min, observe chat patterns
- If no captcha / "verify your account" friction appears → READY
- If friction appears → pause 24h, reassess

## Post-warmup → intel collection mode

After Day 3 clean, start the room-monitor agent (separate file —
`agents/bigo/room_monitor.py`, to be built).

Room monitor logic:
- Enter rooms from the policy/bigo_rooms.yaml watchlist (populated post-warmup
  via gambling-keyword search + manual KOL handles)
- Capture comment stream (every message with timestamp + sender)
- Capture viewer-count time-series
- Capture virtual-gift events (the laundering signal per Q6)
- Stay 5-15 min per room, then move on
- Log everything to runtime/raw/bigo/<persona>_<date>.jsonl

## OPSEC universal

- Never gift, never tip, never chat
- Random session times (don't be a 24/7 bot)
- Random session durations (5-15 min, not exactly 10)
- Random rooms within a tier (don't always enter the same room)
- If Bigo asks for "verify your account" / phone re-bind → BURN signal,
  pause + reassess persona
