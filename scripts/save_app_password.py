"""
Boss helper — paste a freshly-generated Gmail app password and have it
written into .env at the right key. Avoids manual .env editing + risk of
accidentally clobbering other lines.

Usage:
  py scripts/save_app_password.py P03
  py scripts/save_app_password.py P04
  py scripts/save_app_password.py P05

Prompts for the 16-char app password (Google generates them as 4 groups
of 4 chars separated by spaces — paste as-is, the script normalizes).
Validates length, atomically rewrites .env.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("P03", "P04", "P05"):
        print("usage: py scripts/save_app_password.py P03|P04|P05")
        sys.exit(2)
    persona_id = sys.argv[1]
    key = f"PERSONA_{persona_id}_GMAIL_APP_PWD"

    print(f"Persona: {persona_id}")
    print(f"Target .env key: {key}")
    print()
    print("Paste the app password (Google shows it as 4 groups of 4 chars,")
    print("e.g. 'abcd efgh ijkl mnop'). Spaces will be stripped.")
    raw = input("> app password: ").strip()
    cleaned = re.sub(r"\s+", "", raw)
    if len(cleaned) != 16 or not cleaned.isalnum():
        print(f"❌ expected 16 alphanumeric chars; got {len(cleaned)} chars: {cleaned[:4]}...{cleaned[-2:]}")
        sys.exit(1)

    if not ENV_PATH.exists():
        print(f"❌ .env not found at {ENV_PATH}")
        sys.exit(1)

    # Atomic rewrite: read all lines, replace the matching key line
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=False)
    new_lines = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={cleaned}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        # Append if key wasn't present
        new_lines.append(f"{key}={cleaned}")
    new_text = "\n".join(new_lines) + "\n"
    # Write to temp + rename for atomicity
    tmp = ENV_PATH.with_suffix(".env.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(ENV_PATH)
    print(f"✅ wrote {key} (16 chars) to .env — last 4 chars: ***{cleaned[-4:]}")
    print()
    print(f"Verify with: py scripts/check_persona_ready.py {persona_id}")


if __name__ == "__main__":
    main()
