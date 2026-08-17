---
last_updated: YYYY-MM-DDTHH:MM:SS+00:00
active_instance: _TEMPLATE
phase: onboarding
---

# Active platforms (persona × platform status)

| Persona | Platform | Status | Notes |
|---|---|---|---|
| — | — | — | (none yet — fresh instance) |

# Live processes

PID-agnostic. Get real live state with `py scripts/session_status.py`.
Restart command: `scripts\run_daemon.bat`. Do not store PIDs here (they drift).

# Pending harvest (Chrome research awaiting collection)

- (none)

# Last completed action

Scaffolded instance from `instances/_TEMPLATE/`.

# Next intended action

Follow `GETTING_STARTED.md` step 4 onward: fill `INSTANCE.md` scoping, then create personas.

# Pending user input (BLOCKING)

- (none — but you must provide accounts/personas and a target before collection can start)

# Pending procurement (NON-BLOCKING)

- Residential IP / proxy per persona
- Persona email + phone axes
- LLM access (local Ollama model + cloud account)

# Search / seed reservoir

- (empty — populate policy/*.yaml with your market's seeds)

# Files map (top-of-mind beyond CLAUDE.md/INSTANCE.md)

- `policy/*.yaml` — per-platform targets + cadence
- `personas/PERSONAS.md` — roster + axis-isolation matrix
- `runtime/` — created at first run (gitignored)
