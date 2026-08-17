# Persona template

Copy this directory to `personas/<id>/` (e.g. `personas/P01/`) to mint a new persona.

## What goes where

| File / dir | Tracked? | Contents |
|---|---|---|
| `profile.yaml` | ✅ git-tracked | Plain config: bio, archetype, interests, identity fields for register forms. **No secrets.** |
| `avatar.jpg` | ✅ (you add) | The persona's face/avatar image. Use AI-generated or licensed stock — never a real person's photo. |
| `browser/` | ❌ gitignored | Per-platform Playwright `user_data_dir` (live cookies + fingerprint). Created at runtime. |
| `state/` | ❌ gitignored | Saved `storage_state` per platform. Created at runtime. |
| `BURN_HANDOFF.md` | optional | If this persona burns, instructions to spin its replacement. |

## Credentials live in `.env`, NOT here

Add to `.env` (gitignored), keyed by persona id:

```
PERSONA_P01_EMAIL='...'
PERSONA_P01_PASSWORD='REPLACE'
PERSONA_P01_PHONE='+...'            # this persona's own isolated number
PERSONA_P01_TOTP_SECRET='...'       # if you set up 2FA via an authenticator app
PERSONA_P01_PROXY='http://user:pass@residential-endpoint:port'
```

## The hard rules (CLAUDE.md §9)

- Synthetic only — never a real person's identity, never stolen IDs.
- One persona = one coherent identity across platforms. Don't mix personalities.
- Isolated axes per persona (email/phone/IP/browser/username). Never share an axis.
- Cold accounts are burned — complete `personas/warmup/<platform>.md` before targeting.
- Meta family (FB/IG) is read-only lurker by default.
- Log every action. No financial transactions under a persona.
