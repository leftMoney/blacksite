# Field Agent Intelligence Gates

Status: boss-approved framework spec  
Approved at: 2026-05-15T13:20:00+07:00  
Scope: all Blacksite instances, all Field Agents, all platforms including Telegram

## Principle

Field Agents stay cheap, deterministic, and replaceable. Intelligence is inserted at
control gates, not by turning every crawler into a free-running LLM browser driver.

Programmatic crawlers provide hands and memory. AI vision and LLM checks provide eyes,
judgment, repair diagnosis, and escalation. Section Chief owns mission health; Chief
Strategist owns commercial relevance.

## Health Model

Every Field Agent must expose four independent health tracks.

| Track | Meaning | Green example | Red example |
|---|---|---|---|
| `account_health` | login/session/access state | logged in, no checkpoint | captcha, phone verify, suspicious login |
| `mission_health` | whether the assigned job produced mission output | target feed collected | login-only, empty output, scanner missing |
| `quality_health` | whether output matches the task | relevant samples, low duplicate rate | wrong target, low relevance, spam-only yield |
| `risk_health` | platform and persona risk | read-only, rate safe | FloodWait, checkpoint, repeated failed action |

Login success never implies mission success. An agent may be
`account_health=green` and `mission_health=red`.

## Intelligence Insertion Points

| Gate | Trigger | AI role | Output |
|---|---|---|---|
| Page-state gate | task start, post-login, zero yield, before interaction, task end | vision classifies page state | `logged_in`, `login_page`, `captcha`, `empty_feed`, `wrong_page`, `rate_limited`, `human_action_required` |
| Mission QA gate | after each run and daily Section Chief review | sample relevance judge | `relevant`, `off_target`, `duplicate_noise`, `commercial_signal`, `needs_retask` |
| Repair gate | zero yield, selector drift, repeated bad samples, crawler exception | LLM diagnoses screenshot + DOM + log + samples | `crawler_repair_task` with suspected cause and smoke test |
| Interaction gate | before follow, save, like, join, request, comment, DM | policy + vision + optional LLM | allow, deny, or require human approval |
| Media value gate | new image/video/audio batch | local extraction + sampled GPT audit | promote, hold, or reject packet |

Normal collection should not call an LLM. LLM usage is event-triggered by risk,
quality failure, new mission launch, repair, or high-value synthesis.

## Action Tiers

All platform actions are classified before automation.

| Tier | Action class | Default policy |
|---|---|---|
| L0 | read-only view, scroll, search, collect public metadata | automatic if account policy allows |
| L1 | follow, subscribe, save, shape recommendation feed | gated by platform policy and frequency cap |
| L2 | like, reaction, low-signal engagement | restricted; mission purpose required |
| L3 | join group, request access, accept invite | Section Chief rule or human approval |
| L4 | comment, post, reply, DM | disabled by default; boss approval required |
| L5 | transaction, deposit, withdrawal, identity submission | prohibited |

Meta-family accounts remain read-only unless the instance policy is explicitly revised.

## Section Chief Duties

Each Section Chief review must answer:

1. Is the account alive?
2. Did the agent do mission work, not only login maintenance?
3. Was the collected sample relevant to the assigned intelligence objective?
4. Is there evidence of selector drift, wrong query, wrong audience, or empty feed?
5. Is repair needed, and who owns it?
6. Is any interaction action queued, and does it exceed the agent policy tier?
7. Did the agent produce evidence usable by the strategist?

If these cannot be answered, the review result is incomplete.

## Telegram Inclusion

Telegram is not exempt because it is stable. It uses the same health and gate model.

| TG surface | Gate requirement |
|---|---|
| joined-dialog message crawl | mission QA samples must check topical relevance and source diversity |
| entity graph extraction | lead queue must distinguish duplicate handles, spam funnels, and viable discovery leads |
| invite discovery | LLM triage before join decision |
| group join | L3 action; low-frequency auto-join only under explicit Section Chief rule |
| FloodWait | set `risk_health` yellow/red; do not force continued crawling |
| voice/video/media | enter media pipeline with transcript, keyframes, OCR, and source timecode |

Telegram lead flow:

```text
t.me lead discovered
-> lead queue
-> dedupe and risk classification
-> LLM relevance/value triage
-> Section Chief rule or human approval for join
-> 24h read-only observation
-> post-join QA: keep, hold, or exit-candidate
```

## Media Intelligence Pipeline

Raw media is archived, but raw media is not the LLM payload.

```text
media URL/file/hash/metadata
-> local ASR for audio/video
-> keyframe extraction
-> OCR, logo, QR, LINE ID, price, brand, handle detection
-> local signal filter
-> low-tier GPT sample audit
-> high-value packet to strategic LLM
-> evidence-linked intelligence card
```

Every media-derived insight must retain:

| Field | Requirement |
|---|---|
| source | URL, platform, account/group/channel |
| time reference | observed timestamp and media timecode when applicable |
| evidence | transcript excerpt, OCR text, keyframe path, raw hash |
| model trace | local filter result, GPT audit result, final synthesis model |
| business meaning | commercial implication and next action |

Retention policy:

| Value | Keep |
|---|---|
| high | raw media, keyframes, transcript, OCR, insight packet |
| medium | URL/hash, keyframes, transcript/OCR, rejection or hold reason |
| low | metadata/hash/rejection reason only |

## Model Routing

| Task | Preferred route |
|---|---|
| login/captcha/empty page detection | local vision first; GPT audit on uncertainty |
| OCR, QR, LINE ID, price, handle extraction | local OCR/vision first |
| ASR | local Whisper-class model |
| relevance sampling | low-tier GPT |
| crawler repair diagnosis | low/mid-tier GPT |
| local slang, euphemism, hidden funnel wording | mid-tier GPT or higher |
| commercial strategy | high-tier GPT/strategic model |
| human approval brief | high-tier model only when stakes justify |

Local models carry volume. Low-tier GPT audits quality and classifies failure.
High-tier models handle strategy, ambiguous language, and repair plans with broad
commercial consequence.

## Required Runtime Artifacts

| Artifact | Purpose |
|---|---|
| `mission_status` | per-agent health tracks and latest Section Chief verdict |
| `mission_qa_sample` | sampled output with relevance verdict and reason |
| `page_state_check` | screenshot-based state result |
| `crawler_repair_task` | owner, suspected cause, evidence, smoke test, resolution |
| `interaction_request` | requested action tier, purpose, cap, approval state |
| `media_intel_packet` | compressed evidence payload for LLM and KB admission |

## Rollout Phases

| Phase | Goal |
|---|---|
| 1 | Add four health tracks to all Field Agent reports and boss dashboard |
| 2 | Add screenshot page-state checks to task start, zero-yield, and task end |
| 3 | Add LLM mission QA sampling and repair-task generation |
| 4 | Add interaction tier enforcement, including Telegram lead triage |
| 5 | Add full media packet pipeline and retention policy |

