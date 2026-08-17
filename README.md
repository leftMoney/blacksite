# Blacksite

**A reusable framework for standing up a real-time digital-intelligence network — an
agent fleet that collects, AI-processes, and strategically interprets open-source signal
from a country's social/video/messaging platforms, for one `(country × domain)` target at
a time.**

This repository ships **empty**. It contains the framework, the agent code, the
processing pipeline, the knowledge-base design, and the deployment scaffolding — but **no
accounts, no collected data, and no configured target**. You bring those. Think of it as
the chassis and the engine; you choose the destination and fit the wheels.

---

## ⭐ The fastest way to use this repo: ask your AI

This project is built to be operated by an AI coding agent — **Claude Code** or **Codex**.
The whole framework is documented *for the AI to read*. So the intended first move is
literally to open this folder in Claude Code or Codex and ask, in plain language:

- **"What is this project?"** → It will read [`CLAUDE.md`](CLAUDE.md) (or
  [`AGENTS.md`](AGENTS.md) for Codex) and explain the architecture, the layers, and the
  agent organization.
- **"How do I deploy my own instance?"** → It will walk you through
  [`GETTING_STARTED.md`](GETTING_STARTED.md): pick a target, scaffold an instance, wire
  your accounts, warm up personas, start collecting.
- **"How would you help me build this out?"** → It will propose a concrete plan: which
  platforms to target first, which personas to create, what to put in your policy files,
  and what to run.

You do **not** need a human to hand this over to you. Everything a new operator needs is
written down in the files below. Start by asking.

> **Engine prompt:** Claude Code auto-loads `CLAUDE.md`; Codex auto-loads `AGENTS.md`.
> They are the same contract with host/tool names swapped. Read whichever matches your
> tool first — it is the source of truth for how the system behaves.

---

## What it actually does

Given two parameters — a **country** and a **professional domain** (e.g. "the lottery /
belief economy", "competitive e-commerce", "sports-betting KOLs") — Blacksite:

1. **Collects** (Layer 2–3): synthetic "sock-puppet" personas and anonymous public-read
   scanners monitor the platforms that matter in that country (Telegram, TikTok, YouTube,
   Reddit, Discord, X, Facebook, Instagram, livestream/gift platforms, local portals…).
2. **Processes** (Layer 4): a 3-stage hybrid AI pipeline turns raw images/video/text into
   scored, structured insight — a cheap **local** vision model filters noise, a **fast
   cloud** model does structured judgment, and a **strategic cloud** model writes the
   commercial interpretation. A daily/weekly audit loop keeps it honest.
3. **Stores** (Layer 5): a knowledge base of insight "cards" and entities, each
   reverse-linked to the raw material it came from, with time-decay pruning.
4. **Organizes** (the multi-agent org): a 3-tier intelligence hierarchy — **Field Agents**
   collect, **Section Chiefs** synthesize daily and run the fleet, a **Chief Strategist**
   produces the cross-day commercial strategy. You receive only what the strategist
   escalates.
5. **Surfaces** (Layer 6): a real-time dashboard of insights + agent activity.

The guiding principle (the "north star" in `CLAUDE.md` §1): **use abundant compute to
build advantage-creating commercial strategy** — always prefer real LLM intelligence over
heuristic shortcuts, and commercial-advantage signal over generic coverage.

## Architecture at a glance

| Layer | Role | Where in the repo |
|---|---|---|
| L1 Scheduler | Dispatch fleet, cron, prune stale intel | `scheduler/`, `scripts/blacksite_daemon.py` |
| L2 Agents | Per-platform persona / anonymous collectors | `agents/<platform>/` |
| L3 Collection | Multimedia ingest + blob store | `collectors/` |
| L4 AI processing | 3-stage hybrid pipeline + audit | `processors/`, `processors/pipeline/` |
| L5 Knowledge base | Insight cards + entities + decay | `kb/`, `db/` |
| L6 Dashboard | Real-time feeds | `dashboard/` |

Full detail (the 6 layers, the 3-tier agent org, the constitutional rules on timezone,
currency, persona OPSEC, checkpointing, autonomy) is in [`CLAUDE.md`](CLAUDE.md).

## Repo layout

See `CLAUDE.md` §3 for the annotated tree. The two things you create per deployment:

- **`instances/<NAME>/`** — your target's config (copy `instances/_TEMPLATE/`).
- **`personas/<id>/`** — your synthetic accounts (copy `personas/_TEMPLATE/`).

Everything else is framework you can run as-is once configured.

## What you must provide (it is NOT in this repo)

- **Accounts / personas.** Email + phone + (ideally) residential IP per persona. The repo
  ships zero accounts; you create your own. See `personas/_TEMPLATE/` and
  [`GETTING_STARTED.md`](GETTING_STARTED.md).
- **Secrets.** Copy `.env.example` → `.env` and fill in your credentials, API keys, and
  LLM auth. `.env` is gitignored and must never be committed.
- **An LLM.** A local GPU model for Stage 1 (e.g. Qwen2.5-VL via Ollama) and a cloud
  account for Stage 2/3 (Claude or GPT — selectable in `config/llm_providers.yaml`).
- **A target.** The country × domain you actually want intelligence on, defined in your
  `instances/<NAME>/INSTANCE.md`.

## Worked example

[`docs/EXAMPLE_INSTANCE_WALKTHROUGH.md`](docs/EXAMPLE_INSTANCE_WALKTHROUGH.md) is a fully
**fictional** end-to-end example (a made-up country × domain) showing exactly how the
pieces fit: scoping yolk/white/shell, choosing platforms, minting personas, populating
policy files, and reading the first brief. Use it as a template for your own.

## ⚠️ Responsible use

This framework operates synthetic accounts and collects open-source intelligence, which
brushes platform Terms of Service. The built-in rules (`CLAUDE.md` §9, §11) are strict and
non-negotiable: **no real-person impersonation, no identity fraud, no financial
transactions under a persona, exhaustive audit logging, read-only in elevated-risk
venues.** Use it for legitimate competitive/market intelligence and your own commercial
decisions, in compliance with the laws of your jurisdiction and the platforms you touch.
You are responsible for how you deploy it.

## License / provenance

This is a sanitized framework template, handed off with no live data. Configure it for
your own use. There is no warranty.

---

**TL;DR:** open this folder in Claude Code or Codex and send one message —
*"Read CLAUDE.md and help me set this up."* (Codex reads `AGENTS.md` instead.)

It will enter **onboarding mode**: it asks you **(1) which country** and **(2) what
market / domain** you're targeting (plus what accounts, LLM, and proxy you already have),
then hands you a concrete deployment plan — scoping, platform priorities, a persona
roster, and a checklist of exactly which accounts/resources you need to provide — and
walks you through [`GETTING_STARTED.md`](GETTING_STARTED.md) from zero. The AI won't start
until you send that first message, and won't collect anything until you've supplied real
accounts.
