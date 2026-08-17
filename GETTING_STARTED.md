# Getting Started — deploy your own Blacksite instance

This guide takes you from an empty clone to a running intelligence fleet. It is written so
that **you can hand the whole task to Claude Code or Codex**: open this folder, paste a
step's heading, and ask the AI to do it with you. Each step says what to do, which files
to touch, and how to verify.

> Read [`CLAUDE.md`](CLAUDE.md) (Claude Code) or [`AGENTS.md`](AGENTS.md) (Codex) first —
> it is the engine's operating contract. This guide is the deployment runbook on top of it.

---

## 0. Prerequisites

| Need | Why | Note |
|---|---|---|
| **Python 3.12+** | All framework code is Python | `pip install -r requirements.txt` |
| **A local GPU + Ollama** (optional but recommended) | Stage 1 noise filter (e.g. `qwen2.5vl:7b`) runs locally and free | ~14–15 GB VRAM for the 7B vision model; CPU fallback is slow |
| **A cloud LLM account** | Stage 2/3 structured + strategic judgment | Claude (default) or GPT; selectable in `config/llm_providers.yaml` |
| **Your own accounts/personas** | L2 collection | You create these — see Step 6 |
| **Chrome + deep-research Pro accounts** (optional) | §8 external research | Driven via Claude/Codex in Chrome |
| **A residential IP / proxy per persona** (recommended) | OPSEC axis isolation (§9) | Datacenter IPs get flagged fast |

Windows is the reference platform (paths/scripts assume it), but the Python is portable.

## 1. Install

```bash
# from the repo root
python -m venv .venv && .venv\Scripts\activate      # Windows; use source .venv/bin/activate on *nix
pip install -r requirements.txt

copy .env.example .env                                # then edit .env (see Step 6 & 8)
```

`.env` is gitignored. Never commit it. It holds every secret: persona credentials, LLM
auth, proxy creds.

Verify the toolchain:

```bash
py scripts/session_status.py        # structured health check; on an empty clone it will
                                    # report "no active instance / daemon" — that's expected
```

## 2. Choose your target — `(country × domain)`

Decide the **one** country and **one** professional domain you want intelligence on. This
is the single most important decision; everything downstream is shaped by it.

Run a recon pass to map which platforms actually matter in that country for that domain.
Ask your AI to run the recon module:

```
Recon <country> <domain>
```

It follows `personas/skills/RECON_CROSS_SOURCE.md` — cross-referencing audience-panel
data, a traffic tool (e.g. SimilarWeb), and in-country knowledge to produce a platform
priority map. Save the output under your instance's docs.

## 3. Scaffold an instance

Copy the template and point the engine at it:

```bash
xcopy /E /I instances\_TEMPLATE instances\MY-INSTANCE      # Windows
# cp -r instances/_TEMPLATE instances/MY-INSTANCE          # *nix
```

Set the active instance in `.env`:

```
ACTIVE_INSTANCE=MY-INSTANCE
```

(Code reads `os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")`, and `CLAUDE.md` §5 records
the default.)

## 4. Define domain scoping in `INSTANCE.md`

Edit `instances/MY-INSTANCE/INSTANCE.md`. The frontmatter pins your **timezone offset**
(§6.4) and **currency** (§7) — both are constitutional and must be set. Then scope your
domain into three concentric rings (the "egg" model, `CLAUDE.md` §9):

- **yolk (蛋黃)** — the core target you most want to understand. Deepest infiltration.
- **white (蛋白)** — adjacent ecosystem that feeds or surrounds the yolk.
- **shell (蛋殼)** — the broad cultural/contextual periphery (low-cost lurking).

State, in one line each, **what commercial advantage this instance is meant to create**
(the §1 north star, made concrete for your target). This is what the Chief Strategist
optimizes for.

## 5. Set platform scope + policy files

`instances/MY-INSTANCE/policy/*.yaml` is where all instance-specific behavior lives — no
code changes for routine tuning (`CLAUDE.md` §14). The template ships example files; for
each platform you're using:

- list your **target channels / pages / hashtags / search seeds** (in the target market's
  language),
- set scan cadence + jitter,
- set lead-triage rules.

There is one policy file per platform/concern (e.g. `tiktok_hashtags.yaml`,
`facebook_pages.yaml`, `reddit_subs.yaml`, `lead_triage_rules.yaml`,
`persona_warmup_schedule.yaml`). The L4 classification rules in `processors/rules/*.yaml`
also ship as generic English examples — replace them with your market's vocabulary.

> Anything market-specific belongs **here in the instance**, not in framework code
> (`CLAUDE.md` §1 anti-pattern). If you find a hardcoded keyword in `agents/` or
> `processors/`, lift it into a policy file.

## 6. Create personas (synthetic accounts)

🔴 Read `CLAUDE.md` §9 in full first — persona OPSEC is the riskiest part of the system.

For each persona:

1. Copy `personas/_TEMPLATE/` to `personas/<id>/` (e.g. `personas/P01/`).
2. Fill `profile.yaml` — one **coherent** identity (display name, bio, interest vertical,
   DOB for register forms, residence). Don't mix personalities; one persona = one vertical.
3. Mint its **isolated identity axes** (the hard rule — never shared across personas):
   `email + phone + residential IP + browser profile + username pattern`.
4. Put the live credentials in `.env`, keyed `PERSONA_<id>_*` (NOT in `profile.yaml`,
   which is plain config). See `.env.example` for the key shape.
5. Record the roster in `personas/PERSONAS.md` (use the template's axis-isolation matrix to
   verify no axis is shared between personas).

Tier each persona yolk / white / shell. yolk personas need weeks of warm-up and human
supervision; shell personas are cheap lurkers.

> The framework ships **zero** accounts. You create real synthetic accounts yourself, in
> compliance with the platforms' terms and your local law (§11 hard lines: no real-person
> impersonation, no identity fraud, no financial transactions under a persona).

## 7. Warm up personas

A cold account is a burned account (`CLAUDE.md` §9 rule 6). Before any persona touches a
target, run its per-platform warm-up sequence:

```
personas/warmup/<platform>.md
```

These define organic content-consumption cadences that shape the platform algorithm
toward the persona's vertical and build account age/trust. Don't skip this.

## 8. Configure the LLM stack

Edit [`config/llm_providers.yaml`](config/llm_providers.yaml) — the single source of truth
for which model serves each tier (`fast` / `strategic` / `audit` / `coherence` /
`bridge`). Then:

```bash
py scripts/switch_llm_provider.py claude     # or: codex   (writes the tier→model map into .env)
py scripts/switch_llm_provider.py --list     # show the current registry
```

- **Stage 1 (local)**: pull your vision model in Ollama (e.g. `ollama pull qwen2.5vl:7b`).
- **Stage 2/3 (cloud)**: Stage 3 uses the host CLI's OAuth session (e.g.
  `claude.exe --print --model sonnet`). If you run headless, keep the OAuth session warm
  with `processors/oauth_keepalive.py` (see `CLAUDE.md` §2.1).

## 9. Start collecting

Manual single-agent test first, then the daemon:

```bash
py scripts/agents.py --help               # discover agent/chief lifecycle commands
# run one agent / one scan to smoke-test, then:
scripts\run_daemon.bat                      # launches scripts/blacksite_daemon.py (detached pythonw)
```

For 24/7 operation, wire `run_daemon.bat` into a logon-startup shortcut (`CLAUDE.md` §14).
Do **not** pop a `cmd` window on auto-launch — use `pythonw` / hidden PowerShell.

Verify it's alive:

```bash
py scripts/session_status.py              # daemon PID alive? raw JSONL fresh? listener healthy?
```

## 10. Set up the Commander TG bridge (your report + remote-control channel)

This is your **commander console**: a Telegram bot that (a) DMs you the daily briefs and the
Chief Strategist's `[STRATEGY]` memos, and (b) lets you drive the whole engine by DMing it
back (ask status, trigger the strategist, approve/reject leads, request a re-scan). It is how
the fleet **reports to you** and how you command it while away from the main session.
Internally this is the "Commander" bridge — `agents/telegram/tg_bridge.py` +
`agents/telegram/brief_send.py`.

**You provide the credentials:**

1. In Telegram, message **@BotFather** → `/newbot` → copy the **bot token**.
2. Get **your own numeric Telegram user id** (message **@userinfobot**). This becomes the
   allow-list so ONLY you can command the bot.
3. Put both in `.env`:
   ```
   TG_BRIDGE_BOT_TOKEN='123456:ABC...'      # from @BotFather
   TG_BRIDGE_OPERATOR_ID='123456789'        # your numeric Telegram id (commander allow-list)
   ```
4. Start the bridge (the daemon launches it, or run it directly):
   ```
   py agents/telegram/tg_bridge.py
   ```
5. DM your bot `status` — it should answer. From now on, briefs + strategist memos arrive in
   that chat, and you command the engine from there.

🔴 **Security (non-negotiable):** the bot obeys ONLY the Telegram `sender_id` matching
`TG_BRIDGE_OPERATOR_ID`; anyone else messaging it is ignored. Never share the token — if it
leaks, revoke via @BotFather and rotate. The Chief Strategist escalates to you through this
channel (the `[STRATEGY]` prefix is lifted to the top of your next brief — `CLAUDE.md` §15).

## 11. Read the intelligence

- **Raw intel** lands in `instances/MY-INSTANCE/runtime/raw/<agent_id>/*.jsonl`.
- **Library cards** (curated insight) live in the KB tables; query with
  `py processors/kb_query.py --help` and `py kb/query.py`.
- **Daily brief** is composed by the Section Chief (`processors/daily_brief.py`) into
  `runtime/briefs/`.
- **Strategy memos** (cross-day) come from the Chief Strategist into
  `runtime/strategy_memos/`.
- **Event history**: `py scripts/history.py ls --since 24h`.

## 12. How the agent org runs itself (`CLAUDE.md` §15)

- **Field Agents** collect and self-tag. Evaluated daily.
- **Section Chiefs** synthesize 24h of signal into cards + leads, score Field Agents, and
  redirect them by writing `runtime/agent_kpi/<agent_id>.yaml`. Scale to N chiefs with
  `py scripts/agents.py chief create|reassign|dissolve …`.
- **The Chief Strategist** runs weekly cross-day synthesis and may restructure the fleet
  via `runtime/strategy_directives/<date>.yaml` (applied by
  `processors/strategy_directive_apply.py`).
- **You** receive only what the strategist escalates: destructive ops, new-instance
  launches, elevated-risk persona ops, and unresolved incidents.

## 13. Ongoing ops & resume protocol

- **Checkpoint** (`CLAUDE.md` §13): `instances/MY-INSTANCE/CHECKPOINT.md` is the
  resume-state snapshot, rewritten only at session boundaries. Narrative history goes to
  the `system_history` SQL log, never into CHECKPOINT.
- **Bootstrap** every session by re-reading `CLAUDE.md` → `INSTANCE.md` → `CHECKPOINT.md`
  and running `session_status.py` (§4). Any new chat/machine resumes from these without
  re-asking you.

## 14. Where to look when something breaks

| Symptom | Look at |
|---|---|
| "Is it running?" | `py scripts/session_status.py` |
| Login/scrape acting weird | `CLAUDE.md` §1.2 — screenshot + vision-verify before guessing selectors |
| Pipeline stalled / no cards | `processors/pipeline/` stages + `pipeline_audit` table; queue > 6h is a critical alert |
| Persona logged out | `CLAUDE.md` §9.6a — engine-first credential recovery, then human handoff with a screenshot |
| What happened recently | `py scripts/history.py ls --since 24h` |
| LLM calls failing | `config/llm_providers.yaml` + OAuth keepalive (`processors/oauth_keepalive.py`) |

---

**Hand-off note:** this folder was prepared so a new operator needs no human briefing. If
anything here is unclear, ask your AI to read the relevant file in `CLAUDE.md` /
`personas/skills/` / `docs/` and explain or do it. The fictional end-to-end example in
[`docs/EXAMPLE_INSTANCE_WALKTHROUGH.md`](docs/EXAMPLE_INSTANCE_WALKTHROUGH.md) shows every
step above filled in for a sample target.
