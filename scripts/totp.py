"""
Quick TOTP code generator for any persona.

Usage:
  py scripts/totp.py P03         # current 6-digit code
  py scripts/totp.py P03 --watch # live-refresh until ctrl+c
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import pyotp
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("persona", help="P03 / P04 / P05")
    ap.add_argument("--watch", action="store_true", help="keep refreshing")
    args = ap.parse_args()

    key = f"PERSONA_{args.persona}_TOTP_SECRET"
    secret = os.environ.get(key, "").replace(" ", "").upper()
    if not secret or secret.startswith("__"):
        print(f"[totp] {key} not set in .env"); sys.exit(1)

    totp = pyotp.TOTP(secret)
    while True:
        remaining = 30 - int(time.time()) % 30
        print(f"\r{args.persona} code: {totp.now()}  (next in {remaining:2d}s)", end="", flush=True)
        if not args.watch:
            print()
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
