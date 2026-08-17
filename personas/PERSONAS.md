# <INSTANCE> — Persona Master Roster (TEMPLATE)

> Single source of truth for persona allocation. **No passwords here** — those live in
> `.env` keyed `PERSONA_<id>_*`. Each persona's profile metadata (display name / avatar /
> bio / interests / DOB / residence) lives in `personas/<id>/profile.yaml`, which is
> authoritative across parallel sessions. Mint new personas from `personas/_TEMPLATE/`.

## Allocation rules (CLAUDE.md §9.1a)

1. **1 persona = 1 email + 1 phone + 1 display name + 1 avatar**, spanning multiple
   platforms with consistent identity. NOT "1 email = 1 platform".
2. **Cross-persona axis sharing forbidden.** An OSINT correlation tool (Sherlock /
   Maigret / EpieOS) finding one shared axis collapses multiple personas into one.
3. **Anchor personas are sacred** — whichever personas hold deepest-cost intel (e.g. weeks
   of earned channel/group access) should NOT be expanded to new platforms; protect them.
4. **One coherent vertical interest per persona** — warm-up + consumption shapes the
   platform algorithm to feed THAT vertical. Don't mix personalities.
5. **Burn-and-replace is valid** for non-anchor personas — if one burns, spin a successor
   with a new email + phone + handle (see `personas/<id>/BURN_HANDOFF.md`).

## Active roster

> Fill this in as you create personas. Show email PREFIX and phone LAST-4 only here;
> full values stay in `.env`.

| Persona | Tier | Archetype / vertical | Email (prefix) | Phone (last-4) | Platforms (planned) | Status |
|---|---|---|---|---|---|---|
| P01 | yolk  | `<vertical>` | `<prefix>` | `…0000` | `<platforms>` | onboarding |
| P02 | white | `<vertical>` | `<prefix>` | `…0000` | `<platforms>` | onboarding |
| P03 | shell | `<vertical>` | `<prefix>` | `…0000` | `<platforms>` | onboarding |

## Persona-axis-isolation matrix (§9.1a compliance check)

> Verify NO row has a repeated value across persona columns. If any axis is shared, you
> have collapsed two personas into one in an adversary's view — fix before going live.

| Axis | P01 | P02 | P03 |
|---|---|---|---|
| Phone (last-4) | … | … | … |
| Phone carrier | … | … | … |
| Email | … | … | … |
| Display name | … | … | … |
| Avatar | … | … | … |
| Browser profile dir | personas/P01/browser/ | personas/P02/browser/ | personas/P03/browser/ |
| Residential IP / proxy | … | … | … |

**Verification:** ✅ each axis unique per persona (no sharing).

> ⚠️ Watch carrier-level / billing-account linkage too: if several persona SIMs sit under
> one billing account, the telco's internal systems see the cross-persona link even if the
> numbers differ. Spread procurement across carriers where it matters.

## What you do vs what the engine does

| Action | Who |
|---|---|
| Provide email + phone + proxy per persona | You |
| Provide display-name + avatar image | You |
| Receive SMS OTP during register (paste into chat) | You (boss-in-loop) |
| Email OTP from inbox (after app password supplied) | Engine (IMAP poller) |
| CAPTCHA / human-verify | You (rare) |
| Form fill / submit / warm-up / collection | Engine (automated) |
