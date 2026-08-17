"""
Blacksite — persona readiness check.

Verifies all prerequisites for a P03/P04/P05 register flow:
  1. .env has PERSONA_<id>_GMAIL + GMAIL_PWD + GMAIL_APP_PWD + TG_PHONE + TG_2FA
  2. App password is REAL (not the __GENERATE_BOSS_TASK__ placeholder)
  3. personas/<id>/profile.yaml exists and parses
  4. personas/<id>/avatar.jpg exists (size > 1 KB)
  5. personas/<id>/browser/ + state/ directories exist
  6. IMAP login to the Gmail succeeds (validates app password actually works)

Usage:
  py scripts/check_persona_ready.py P03
  py scripts/check_persona_ready.py --all
"""

from __future__ import annotations

import argparse
import imaplib
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def green(s): return f"\033[32m{s}\033[0m"
def red(s):   return f"\033[31m{s}\033[0m"
def yellow(s): return f"\033[33m{s}\033[0m"


def check_persona(persona_id: str) -> dict:
    """Return dict of {check_name: (ok_bool, detail)}."""
    results = {}

    # Read profile to determine which platforms this persona uses
    profile_path = ROOT / "personas" / persona_id / "profile.yaml"
    persona_platforms: set[str] = set()
    if profile_path.exists():
        try:
            with profile_path.open("r", encoding="utf-8") as f:
                pdata = yaml.safe_load(f) or {}
            persona_platforms = set((pdata.get("platforms") or {}).keys())
        except Exception:
            pass
    uses_tg = "telegram" in persona_platforms

    # Always-required env keys
    always_required = [
        f"PERSONA_{persona_id}_GMAIL",
        f"PERSONA_{persona_id}_GMAIL_APP_PWD",
        f"PERSONA_{persona_id}_TG_PHONE",  # phone is always needed (SMS receive)
    ]
    # Conditionally required (only if persona uses TG)
    conditional = []
    if uses_tg:
        conditional.append(f"PERSONA_{persona_id}_TG_2FA")
    # Optional but tracked
    optional = [
        f"PERSONA_{persona_id}_GMAIL_PWD",  # raw pwd is rarely used after TOTP+APP_PWD setup
        f"PERSONA_{persona_id}_TG_2FA" if not uses_tg else None,
    ]
    optional = [k for k in optional if k]

    for k in always_required + conditional:
        v = os.environ.get(k, "")
        if not v:
            results[f"env:{k}"] = (False, "missing")
        elif v.startswith("__"):
            results[f"env:{k}"] = (False, f"placeholder ({v}) — boss task pending")
        else:
            results[f"env:{k}"] = (True, f"set ({len(v)} chars)")
    for k in optional:
        v = os.environ.get(k, "")
        if not v or v.startswith("__"):
            results[f"env:{k}"] = (True, "N/A (persona doesn't use TG)" if "TG_2FA" in k else "(optional, not set)")
        else:
            results[f"env:{k}"] = (True, f"set ({len(v)} chars)")

    # profile.yaml
    profile_path = ROOT / "personas" / persona_id / "profile.yaml"
    if not profile_path.exists():
        results["profile.yaml"] = (False, f"missing at {profile_path.relative_to(ROOT)}")
    else:
        try:
            with profile_path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            display = data.get("identity", {}).get("display_name", "?")
            results["profile.yaml"] = (True, f"display_name={display!r}")
        except Exception as e:
            results["profile.yaml"] = (False, f"parse error: {e}")

    # avatar
    avatar_path = ROOT / "personas" / persona_id / "avatar.jpg"
    if not avatar_path.exists():
        results["avatar.jpg"] = (False, f"missing at {avatar_path.relative_to(ROOT)}")
    else:
        size = avatar_path.stat().st_size
        if size < 1024:
            results["avatar.jpg"] = (False, f"too small ({size}B; expected > 1KB)")
        else:
            results["avatar.jpg"] = (True, f"{size:,} bytes")

    # dirs
    for sub in ("browser", "state"):
        d = ROOT / "personas" / persona_id / sub
        if d.exists():
            results[f"dir:{sub}/"] = (True, "exists")
        else:
            results[f"dir:{sub}/"] = (False, f"missing — run: mkdir -p {d.relative_to(ROOT)}")

    # 6: IMAP live login
    gmail = os.environ.get(f"PERSONA_{persona_id}_GMAIL", "")
    app_pwd = os.environ.get(f"PERSONA_{persona_id}_GMAIL_APP_PWD", "")
    if gmail and app_pwd and not app_pwd.startswith("__"):
        try:
            M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            M.login(gmail, app_pwd)
            M.select("INBOX")
            typ, data = M.search(None, "ALL")
            count = len(data[0].split()) if data and data[0] else 0
            M.logout()
            results["imap_login"] = (True, f"OK — INBOX has {count} messages")
        except imaplib.IMAP4.error as e:
            results["imap_login"] = (False, f"auth failed: {e}")
        except Exception as e:
            results["imap_login"] = (False, f"{type(e).__name__}: {e}")
    else:
        results["imap_login"] = (False, "skipped — missing GMAIL or APP_PWD")

    return results


def print_results(persona_id: str, results: dict) -> bool:
    all_ok = True
    print(f"\n=== {persona_id} readiness ===")
    for name, (ok, detail) in results.items():
        prefix = green("✅") if ok else red("❌")
        print(f"  {prefix}  {name:<32} {detail}")
        if not ok:
            all_ok = False
    if all_ok:
        print(green(f"\n  {persona_id} READY — register flow can fire."))
    else:
        print(red(f"\n  {persona_id} NOT READY — fix above before register."))
    return all_ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("persona", nargs="?", default="P03")
    parser.add_argument("--all", action="store_true",
                        help="check P03, P04, P05")
    args = parser.parse_args()

    targets = ["P03", "P04", "P05"] if args.all else [args.persona]
    overall_ok = True
    for pid in targets:
        ok = print_results(pid, check_persona(pid))
        overall_ok = overall_ok and ok
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
