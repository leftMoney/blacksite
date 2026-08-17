"""
Blacksite — Discord listener (skeleton).

Status: SKELETON. Activated when:
  - Discord account registered for P01 / P02 (email-based)
  - User-token captured (DISCORD_TOKEN_PNN in .env)
  - policy/discord_servers.yaml populated with target server invite codes

Note: Discord ToS forbids selfbots / automation of user accounts. Operate as
read-only listener with caution — passive only, never auto-react/reply.
Bot account (developer-registered) is the safer path long-term.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

print("[discord_listen] not yet activated — see module docstring", flush=True)
