---
instance: _TEMPLATE                 # rename to your instance, e.g. MR-MKT
country: XX                         # ISO country code of your target
domain: <one-line domain>          # e.g. "budget commerce + grey resale ecosystem"
tz_offset: "+00:00"                # 🔴 REQUIRED (§6.4). Your instance's locked GMT offset, e.g. "+05:00"
currency: XXX                      # 🔴 REQUIRED (§7). Primary currency code + symbol, e.g. "MRD (₥)"
status: onboarding
created: YYYY-MM-DD
---

# <INSTANCE> — <Country> × <Domain> Instance Config

> Copy this directory to `instances/<NAME>/`, then set `ACTIVE_INSTANCE=<NAME>` in `.env`.
> See `GETTING_STARTED.md` (steps 3–5) and the fictional example at
> `docs/EXAMPLE_INSTANCE_WALKTHROUGH.md`. Framework contract: `CLAUDE.md` / `AGENTS.md`.

---

## 0. Commercial objective (the §1 north star, made concrete)

> ONE sentence: what commercial advantage is this instance meant to create for the client?
> This is what the Chief Strategist optimizes for. Example:
> "Give <client> 1–2 weeks' lead time on shifts in <yolk> so they can counter-position
> before a competitor captures the segment."

- Client / incumbent brand: `<the client brand>`
- Advantage statement: `<fill in>`

## 1. Domain Scoping (the egg model — see CLAUDE.md §9)

**yolk (蛋黃 — core P0):** the thing you most want to understand; deepest infiltration.
- `<core target #1>`
- `<core target #2>`

**white (蛋白 — adjacent ecosystem):** what feeds or surrounds the yolk.
- `<adjacent #1 — e.g. influencer/KOL ecosystem>`
- `<adjacent #2 — e.g. payment/logistics behavior>`
- `<regulatory weather for this domain>`

**shell (蛋殼 — periphery / context):** cheap cultural/contextual lurking.
- `<pop-culture / lifestyle context>`
- `<seasonal events / national mood>`

## 2. Vocabulary Policy (CRITICAL — see CLAUDE.md §11)

- **Internal Blacksite outputs** (KB cards, agent reports, dashboards, search seeds):
  use precise market terminology + native source terms verbatim. Mirror reality.
- **Client external surfaces** (PR, marketing, regulator reports): use the client's
  sanctioned public framing only. Never internal market terms there. (Export boundary.)

## 3. Persona Roster (distinct vertical archetypes)

> Master roster: `personas/PERSONAS.md`. Per-persona profiles: `personas/<id>/profile.yaml`.
> Live creds: `.env` keyed `PERSONA_<id>_*`. One persona = one coherent identity across
> platforms; cross-persona axis sharing forbidden (§9.1a).

| ID | Tier | Archetype | Vertical | Platforms (planned) | Status |
|----|------|-----------|----------|---------------------|--------|
| P01 | yolk  | `<archetype>` | `<vertical>` | `<platforms>` | onboarding |
| P02 | white | `<archetype>` | `<vertical>` | `<platforms>` | onboarding |
| P03 | shell | `<archetype>` | `<vertical>` | `<platforms>` | onboarding |

Presentation rules:
- Each persona is synthetic, NOT a real person, NOT impersonating anyone.
- One coherent vertical interest per persona (don't mix personalities).
- Burn-and-replace is valid for non-anchor personas (new email + phone + handle).

## 4. Platform Scope (current vs blocked)

| Platform | Status | Notes |
|----------|--------|-------|
| Telegram  | `<active / blocked>` | Primary grey-intel surface (read-only by default) |
| TikTok    | `<...>` | FYP shaping may need in-country residential IP |
| YouTube   | `<...>` | Subscription-shaped FYP |
| Reddit    | `<...>` | |
| Discord   | `<...>` | |
| X/Twitter | `<...>` | |
| Facebook  | `<...>` | Meta family: read-only lurker by default (§9 rule 3) |
| Instagram | `<...>` | Meta family: read-only lurker by default |
| `<local portal>` | `<...>` | Anonymous public-read |

## 5. Proxy / IP Architecture

- v1: `<shared endpoint OR per-persona residential proxy>` — see `.env` `*_PROXY` keys.
- Serialize requests per target with jitter if personas share an IP.

## 6. Onboarding Sequence (check off as you go)

```
[ ] Step 0 — persona inventory + .env loaded
[ ] Step 1 — first platform login (anchor personas)
[ ] Step 2 — warm-up per personas/warmup/<platform>.md
[ ] Step 3 — policy/*.yaml populated with real targets (market language)
[ ] Step 4 — processors/rules/*.yaml swapped to market vocabulary
[ ] Step 5 — daemon live (scripts/run_daemon.bat), session_status green
[ ] Step 6 — first daily brief reviewed
```

Each step's live status is tracked in this instance's `CHECKPOINT.md`.
