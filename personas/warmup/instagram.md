# Instagram — persona warmup SOP

> Per `instances/_TEMPLATE/policy/fb_ig_strategy.md`. **IG is bound to FB via SSO**
> at register time, so most lifecycle gates are FB-driven. This file covers
> IG-specific behaviour.

## Register

IG register has no separate flow — bound to FB via "Log in with Facebook" SSO
in `agents/facebook/register.py` (continues into IG after FB success). Boss
runs ONE command per persona; FB and IG both register in the same session.

After register the engine binds IG ↔ FB visibility:
- IG Settings → Privacy & Security → "Show on Facebook" = ON
- This reduces Meta's downstream "are these the same person?" doubt later

## Day 0-14 — limited mode (IG = same gate as FB)

Engine fires only `agents/instagram/feed_harvest.py` for passive scroll +
JSONL capture. NO reactions, follows, posting.

## Day 14-30 — calibration

Same micro-engagement budget as FB (per `meta_lifecycle.calibration_budget`).
Note: IG follow ramp is more conservative than FB because IG is stricter on
new-account follow velocity (~50/day cap for new accounts; we stay way under).

## Day 30-60 — ramp-up

Engine adds:
- Follow mass-market IG accounts per persona vertical (P03 folk-belief IG accounts,
  P04 sports clubs IG, P05 tech accounts)
- IG Story view sweep (`agents/instagram/story_sweep.py`)
- Reels feed deep dive (engine to be built post-register based on observed DOM)

## Day 60+ — mission

Full IG intel collection:
- Targeted account Page scan (handles to be added to `policy/facebook_pages.yaml`
  IG-section once defined)
- IG Story sweep 3× daily (≤24h decay window — see fb_ig_strategy.md §5.4
  reactive trigger: Story shows promo code/QR → urgent KB ingest)
- Reels feed harvest (deep-research noted Reels are a heavy gambling-promo surface in the target market)
- Explore tab harvest for algo-recommended accounts

## IG-specific quirks

- **Story tray DOM rotates often** — `story_sweep.py` uses defensive selector
  fallbacks; if zero accounts match the tray locator, log + skip (DOM drift
  needs manual investigation)
- **"Suggested for you" carousel** = high-intel surface; algo's read on which
  accounts P03 most resembles. Engine should screenshot+capture text from this
  carousel weekly (TODO post-register)
- **DM tab** = OFF LIMITS. Even reading DMs has a "seen" receipt that signals to
  Meta the account is human-active in DMs; we don't want this footprint
- **Notification tab** = read-only OK (low signal); harvest weekly for who's
  reacting to our own Stories (post Day 60)

## Burn-signal protocol

Same as FB. IG and FB lifecycle are tied — if either fires a burn signal,
the **persona** is at risk, not just one surface. Engine pauses both surfaces
when either reports burn.

## Avatar / handle reuse

IG handle comes from each persona's `profile.yaml` `identity.handle_pool` (first choice):
- P01 → primary `<handle_pool[0]>`
- P02 → primary `<handle_pool[0]>`
- P03 → primary `<handle_pool[0]>`

If primary is taken at register time, fall to next in pool. Once chosen, locked
for the persona's life — never change handle (Meta tracks handle history;
changing looks suspicious).
