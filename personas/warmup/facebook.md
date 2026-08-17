# Facebook — persona warmup SOP

> Per `instances/_TEMPLATE/policy/fb_ig_strategy.md` §3 (lifecycle) + §4 (engagement budget).
> This SOP is the **boss-readable cheatsheet**. Engine code (`agents/facebook/`)
> follows the same rules programmatically via `agents/_common/meta_lifecycle.py`.

## Pre-register checklist (boss runs once per persona)

- [ ] Camoufox installed (`scripts/install_camoufox.bat` returned OK)
- [ ] `personas/<P0X>/profile.yaml` `identity.*` fully populated
- [ ] `personas/<P0X>/avatar.jpg` exists (1024² JPG)
- [ ] `personas/<P0X>/0X.png` cover photo exists
- [ ] `.env` has `PERSONA_<P0X>_GMAIL` + `_GMAIL_PWD` + `_TG_PHONE`
- [ ] Boss has phone in hand for SMS OTP (Three / O2 retail SIM per `reference_sms_provider_resolved.md`)
- [ ] FB+IG account doesn't already exist for this email (check before register!)

## Day 0 — register (boss-in-loop)

```
py agents/facebook/register.py --persona P03
```

| Step | Engine does | Boss does |
|---|---|---|
| 1 | Open Camoufox to `facebook.com/r.php` | watch the Camoufox window |
| 2 | Pre-fill first/last name, email, password, DOB, gender from yaml/.env | review pre-fill |
| 3 | Pause; engine asks boss to click "Sign Up" | click Sign Up |
| 4 | Pause; engine asks boss to type SMS OTP | type OTP into the FB form, click Confirm |
| 5 | Engine verifies `c_user` cookie present | acknowledge OK |
| 6 | Pause; engine asks boss to upload avatar + cover, fill bio/city/edu | upload `personas/<P0X>/avatar.jpg` and `0X.png`, paste bio_short, fill city = (persona home city per yaml), school = (per yaml) |
| 7 | Engine saves storage_state, marks `fb_register_at` in lifecycle JSON | OK |
| 8 | Engine navigates to IG signup, asks boss to click "Log in with Facebook" | click that, walk through IG prompts |
| 9 | Engine verifies IG `sessionid` present, saves IG storage_state | OK — IG is now bound |
| 10 | Engine marks `ig_register_at`, advances stage `register → limited` | done |

**Forbidden during register**:
- Don't click any "Find friends" button — leaks contacts to Meta
- Don't fill phone if FB asks "for security" beyond the OTP step (keep phone use minimal)
- Don't enable "Two-factor auth" yet (we'll add via TOTP later)
- Don't accept any friend suggestions

## Day 0-14 — limited mode (Meta-enforced, persona stays passive)

Engine runs only **passive harvest** during this window:

| Cron | Job | What it does |
|---|---|---|
| persona online window 3× daily | `feed_harvest.py` | scroll feed, scrape posts to JSONL — NO reactions, NO saves |
| 04:30 daily | `account_health.py` | probe for burn signals, mark clean/burn day in lifecycle |

**Boss observes**: if any "review required" / "limited" / "verify your phone" /
"upload selfie" notice appears (engine alerts via P01 DM), persona may need to
burn-and-respawn.

**Forbidden Day 0-14**:
- ❌ Posting (Story or grid)
- ❌ Liking / reacting / saving (Meta watches velocity hardest in first 14 days)
- ❌ Commenting
- ❌ Following any Page
- ❌ Friend-requesting anyone
- ❌ Joining any Group

**OK Day 0-14**:
- ✅ Scrolling feed (algo training via dwell time)
- ✅ Watching Reels to ≥80% (huge algo positive signal)
- ✅ Viewing Stories (passive)
- ✅ Tapping into Page profiles to read About (no Follow click)

## Day 14-30 — calibration (engine adds minimal engagement)

After `account_health` reports 14 consecutive clean days, lifecycle auto-advances
to `calibration`. Engine starts firing **small** reactions and saves per
`meta_lifecycle.calibration_budget(tier)`:

| Persona | Daily reactions | Daily saves | Daily Reels | Mins |
|---|---|---|---|---|
| P03 yolk | 5 | 2 | 10 | 45 |
| P04 white | 4 | 1 | 6 | 35 |
| P05 shell | 0-1 | 1 | 4 | 15 |

Still no follows, no Story posting, no comments.

## Day 30-60 — ramp-up (engine starts following Pages, optional Story)

Lifecycle auto-advances after 14 consecutive clean days in calibration.
Now engine can:

- Follow mass-market Pages (per `personas/PROFILES_HUMAN.md` interest seeds —
  e.g. P03 folk-belief mass: online-fortune-teller Pages, horoscope-by-birthday Pages, a mainstream lifestyle outlet)
- Save more posts
- Optionally post own Story (24h ephemeral) once a week — boss supplies image to
  `personas/<P0X>/posts_pool/story_<n>.jpg`; engine schedules upload Mondays

Engagement budget linearly interpolates calibration → mission over 30 days.

**Still forbidden in ramp-up**:
- ❌ Comments on KOL Pages
- ❌ Friend-requesting strangers
- ❌ Apply to closed Groups
- ❌ Click external CTA links in Page posts

## Day 60+ — mission active

Lifecycle auto-advances after 7 consecutive clean days in ramp-up. Full
mission stack engages:

- Follow target KOL Pages from `instances/_TEMPLATE/policy/facebook_pages.yaml`
  (P03 takes yolk Pages: lottery-influencer Pages + P0 KOLs;
   P04 takes sports_journalist Pages: e.g. a major sports-news Page;
   P05 takes none — shell stays generic)
- Daily targeted Page scan (every 30 min cron during persona online window)
- Cross-persona triangulation Friday 03:00
- Full daily/weekly engagement per fb_ig_strategy.md §4.3 cadence
- Funnel auto-review pipeline picks up new grey-brand mentions in KOL post comments

## Burn-signal response protocol

Engine detects any of:
- Account "review required" / "limited" notice
- Phone re-verify request
- Photo selfie request
- "Unrecognized device" challenge
- Disabled account

→ engine pauses persona, logs `system_history` warning, P01 DMs boss with
incident detail. Boss decides: (a) handle re-verify in 24h, or (b) burn persona
+ spawn P0Xb per `personas/<P0X>/BURN_HANDOFF.md`.

Default per `fb_ig_strategy.md §8 Q7`: photo selfie → burn immediately.

## Boss daily/weekly responsibilities (post-register)

- **Daily**: nothing — fully autonomous
- **Per `daily_brief` 19:00**: boss skims TG DM from P01 for any meta-related KPI / burn alert
- **Weekly**: boss may supply 1-2 fresh stock photos to `personas/<P0X>/posts_pool/` for
  upcoming Story/grid post cadence (P03 weekly Story, P04 weekly Story, P05 monthly Story)
- **On phone re-verify alert**: boss responds within 24h or persona burns
- **On photo selfie alert**: persona burns; boss runs `personas/<P0X>/BURN_HANDOFF.md` to spin P0Xb
