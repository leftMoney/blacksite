# Worked example — a fictional Blacksite instance, end to end

> **Everything below is invented.** "Marisol", "MarketHub", "GreyPort", the brands, the
> handles, and the numbers are fictional placeholders chosen to show *how the pieces fit
> together*. Copy this shape for your own real `(country × domain)`. Nothing here is a real
> market, company, or person.

This walks through deploying one instance from scratch, mirroring
[`GETTING_STARTED.md`](../GETTING_STARTED.md). Read it as "what a filled-in instance looks
like."

---

## The brief (what we were asked to build)

> *"I'm advising **MarketHub**, the licensed online-marketplace operator in the Republic of
> **Marisol**. A grey 'parallel-import / resale' economy competes with us for the same
> budget-shopper audience. I want continuous intelligence on that grey ecosystem, the
> influencers steering shoppers to it, and the regulatory weather — so MarketHub can make
> better commercial moves."*

- **Country:** Marisol (fictional) — `MR`
- **Domain:** budget-shopper commerce + grey parallel-import/resale ecosystem
- **Client / incumbent brand:** MarketHub (the legal operator we advise)
- **Instance name:** `MR-MKT`
- **Timezone:** GMT+5 (Marisol has no DST) → `TZ = timezone(timedelta(hours=5))`
- **Currency:** Marisol Dinar (₥)

## Step 2 — recon output (abridged)

`Recon Marisol "grey parallel-import commerce"` returned a platform priority map:

| Platform | Why it matters in Marisol | Access |
|---|---|---|
| Telegram | Primary channel for grey-resale deal drops + invite funnels | persona, read-only |
| TikTok | Where budget-shopper influencers push "where to buy cheaper" | persona FYP-shaping + anon hashtag |
| Instagram/Facebook | Resale storefronts + influencer reach | read-only lurker (Meta family) |
| A local marketplace portal | Price/listing ground truth | anonymous web scan |
| YouTube | "How I import for less" long-form | persona-subscription shaping |

## Step 4 — domain scoping (`instances/MR-MKT/INSTANCE.md`)

```
---
instance: MR-MKT
country: MR
domain: budget commerce + grey parallel-import/resale ecosystem
tz_offset: "+05:00"
currency: MRD (₥)
created: 2026-06-10
---
```

**yolk (core):** the grey parallel-import operators themselves — Telegram deal-drop
channels, their funnel bots, the resale storefronts. Example fictional brands:
`examplebet`-style placeholders → here `cheapio`, `greyport`, `parallelmart`.

**white (adjacent):** the budget-shopper influencer ecosystem (the KOLs who route
audience to the grey operators), payment/logistics behavior, and regulatory weather (the
Marisol customs/commerce regulator).

**shell (periphery):** general Marisol youth-shopping culture, viral product trends,
seasonal sale events — cheap context that frames the rest.

**Commercial advantage this instance creates (the §1 north star, concrete):**
> "Give MarketHub 1–2 weeks' lead time on where grey demand is shifting and which
> influencers are scaling, so MarketHub can counter-position price/assortment before the
> grey channel captures the segment."

## Step 5 — policy files (`instances/MR-MKT/policy/`)

`tiktok_hashtags.yaml` (excerpt — generic shape, your seeds in the market's language):
```yaml
scope: read_only_anonymous
scan: { enable: true, schedule_cron: "10,40 * * * *", per_run_max_hashtags: 6 }
hashtags:
  yolk:  ["cheapimport", "parallelbuy", "greydeal"]      # TODO: real market terms
  white: ["budgetshopping", "wheretobuy", "saletips"]
handles:
  influencer: ["example_shopkol", "budgetqueen_mr"]       # probe at runtime
output: { raw_jsonl_dir: instances/MR-MKT/runtime/raw/tiktok }
```

`lead_triage_rules.yaml`, `reddit_subs.yaml`, `facebook_pages.yaml`, etc. follow the same
pattern: targets in the market's language, cadence, jitter. The L4 classification rules in
`processors/rules/*.yaml` get swapped from the shipped English examples to Marisol-market
vocabulary.

## Step 6 — personas (`personas/` + `.env`)

Three fictional personas, each a single coherent identity with isolated axes:

| ID | Tier | Archetype / vertical | Platforms | Status |
|----|------|----------------------|-----------|--------|
| P01 | yolk | Budget-deal hunter ("always finds it cheaper") | Telegram + TikTok | warm-up |
| P02 | white | Shopping-tips influencer-follower | IG + FB + YouTube | warm-up |
| P03 | shell | General Marisol lifestyle lurker | Reddit + portal read | live (read-only) |

`personas/P01/profile.yaml` (excerpt):
```yaml
persona_id: P01
tier: yolk
archetype: budget_deal_hunter
identity:
  display_name: "Deal Hunter MR"
  age: 27
  language: bilingual_en_mr
  nationality_presentation: regional (NOT pretending to be a specific real person)
algorithm_target:
  primary_verticals: [budget_commerce, parallel_import, deal_drops]
platforms:
  telegram: { priority: P0, register_status: warmup }
  tiktok:   { priority: P1, requires_phone_otp: true, register_status: warmup }
```

Live credentials go in `.env`, never in `profile.yaml`:
```
PERSONA_P01_EMAIL='dealhunter.mr@example.com'
PERSONA_P01_PHONE='+0000000000'        # your isolated number
PERSONA_P01_PASSWORD='REPLACE'
PERSONA_P01_PROXY='http://user:pass@residential-mr-endpoint:port'
```

`personas/PERSONAS.md` records the roster + an axis-isolation matrix verifying P01/P02/P03
share **no** email/phone/IP/browser-profile/display-name. (If two personas shared a phone,
an OSINT tool would collapse them into one — forbidden, `CLAUDE.md` §9.1a.)

## Step 7 — warm-up

P01 runs `personas/warmup/telegram` + `personas/warmup/tiktok.md`: a few days of organic
budget-shopping content consumption (no target action) so TikTok's FYP learns the vertical
and the account looks aged before it touches yolk channels.

## Step 8 — LLM stack

```bash
ollama pull qwen2.5vl:7b                       # Stage 1 local noise filter
py scripts/switch_llm_provider.py claude       # Stage 2 fast + Stage 3 strategic
```

## Step 9–10 — run + read

```bash
scripts\run_daemon.bat
py scripts/session_status.py        # daemon alive, raw JSONL flowing
```

A few hours later the **Section Chief** composes the first daily brief. Fictional excerpt:

> **[For MarketHub to decide]** Grey operator `greyport` ran a 2-hour burst of deal-drop
> posts across 3 Telegram channels last night (252 mentions, single promo code), all
> echoed by influencer `budgetqueen_mr` on TikTok this morning. This is the same
> coordinated-launch pattern seen before a price war. **Recommended move:** pre-empt with a
> targeted price-match on the affected category this week.
>
> *Evidence:* P01@Telegram raw 06-09; tiktok_anon hashtag scan 06-10; corroboration_count 4.

That "what should the operator decide" headline first, evidence second, is the §1.1 house
style.

## Step 11 — the org running itself

- The **Section Chief** noticed P01's Telegram yield was high-signal and bumped its KPI to
  prioritize deal-drop channels (wrote `runtime/agent_kpi/P01@telegram.yaml`).
- The **Chief Strategist**, in the weekly memo, opened a new monitoring track on
  `greyport`'s domain cluster and flagged the influencer `budgetqueen_mr` for follower-
  overlap analysis — a directive in `runtime/strategy_directives/2026-06-15.yaml`.
- Only one thing reached the operator: a request to approve elevated-risk infiltration of
  a closed Telegram group suspected to be operator-run (escalated per `CLAUDE.md` §14).

---

## Mapping this back to your deployment

| This example | Your real instance |
|---|---|
| `MR-MKT`, Marisol, MarketHub | your `<NAME>`, your country, your client brand |
| grey parallel-import | your actual yolk domain |
| `greyport` / `budgetqueen_mr` | your real (researched) targets, in policy files |
| GMT+5 / ₥ | your locked offset + currency |
| P01/P02/P03 fictional | your real synthetic personas with isolated axes |

Everything market-specific above lives in `instances/MR-MKT/` and `personas/` — **never in
framework code**. That separation is what lets the same framework serve any target.
