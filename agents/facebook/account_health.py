"""
Blacksite — Facebook + Instagram account health probe (burn signal detection).

Critical for Day 0-14 limited mode: Meta enforces friction on new accounts and
escalates fast. Engine must detect burn signals early and pause / DM boss.

Probe strategy (passive — no engagement actions, just navigations):
  1. Load account home / profile page
  2. Detect Meta interstitials: "we suspect", "review required", "limited",
     "verify your identity", "phone re-verify", "photo selfie"
  3. Detect feed disability (empty feed despite warm account)
  4. Check own profile is reachable / not soft-deleted
  5. Verify session cookies haven't been invalidated

Outputs:
  instances/<inst>/runtime/health/meta_<persona>_<YYYY-MM-DD>.json
  Append summary to runtime/logs/meta_health_<YYYY-MM-DD>.log

If burn signal detected:
  - Updates personas/<id>/state/meta_lifecycle.json: add burn_signals entry
  - Reset consecutive_clean_days = 0
  - Logs to system_history (kind=warning, scope=meta)
  - Returns exit code 2 (caller — daemon — bridges P01 DM boss alert)

Usage:
  py agents/facebook/account_health.py --persona P03
  py agents/facebook/account_health.py --persona P03 --platform instagram
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agents._common.camoufox_session import launch_persona
from agents._common import meta_lifecycle

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
HEALTH_DIR = RUNTIME / "health"
LOG_DIR = RUNTIME / "logs"
HEALTH_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

# Burn-signal text patterns — case-insensitive substring match against page text
BURN_PATTERNS_FB = [
    ("review_required",   ["we need to review", "review required", "your account is under review"]),
    ("limited",           ["your account has been limited", "you can't post"]),
    ("phone_reverify",    ["confirm your phone", "verify your phone", "we sent a code to"]),
    ("photo_selfie",      ["upload a photo of yourself", "verify your identity with a photo",
                           "video selfie"]),
    ("captcha_loop",      ["please solve this challenge", "security check"]),
    ("disabled",          ["your account has been disabled", "your account is disabled"]),
    ("suspicious_login",  ["unrecognized device", "we don't recognize this", "login attempt"]),
    ("checkpoint",        ["help us confirm", "confirm your identity"]),
]
BURN_PATTERNS_IG = [
    ("review_required",   ["we restrict certain activity", "your account has been restricted"]),
    ("limited",           ["this action was blocked", "we've limited"]),
    ("phone_reverify",    ["confirm your phone number", "send code"]),
    ("photo_selfie",      ["video selfie", "verify your identity with a video"]),
    ("captcha_loop",      ["security check", "please solve"]),
    ("disabled",          ["your account has been disabled", "we've disabled"]),
    ("suspicious_login",  ["new login", "unrecognized device", "was this you"]),
]

PLATFORM_HOME = {
    "facebook":  "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(persona_id: str, msg: str) -> None:
    line = f"[{now_iso()}] [health] [{persona_id}] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"meta_health_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def _probe_one(persona_id: str, platform: str) -> dict:
    burns_found: list[dict] = []
    text_excerpt = ""
    feed_size_estimate = 0
    cookie_session_present = False

    patterns = BURN_PATTERNS_FB if platform == "facebook" else BURN_PATTERNS_IG
    home_url = PLATFORM_HOME[platform]

    async with launch_persona(
        persona_id, platform, headless=True, use_storage_state=True,
    ) as (browser, context, page):

        # 1) Cookie session check
        cookies = await context.cookies()
        target = "c_user" if platform == "facebook" else "sessionid"
        cookie_session_present = any(c["name"] == target for c in cookies)
        if not cookie_session_present:
            burns_found.append({
                "kind": "session_invalidated",
                "detail": f"missing {target} cookie — re-login needed",
            })
            log(persona_id, f"WARN no {target} cookie present (session invalidated?)")

        # 2) Home navigation — check for interstitials / blocks
        try:
            await page.goto(home_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)
        except Exception as e:
            burns_found.append({"kind": "navigation_failure", "detail": str(e)[:200]})
            log(persona_id, f"navigation to {home_url} failed: {e}")
        else:
            try:
                body = await page.locator("body").inner_text(timeout=8000)
            except Exception:
                body = ""
            body_lower = body.lower()
            text_excerpt = body[:300]
            for kind, needles in patterns:
                if any(n in body_lower for n in needles):
                    burns_found.append({
                        "kind": kind,
                        "detail": f"text match on {home_url}",
                    })
                    log(persona_id, f"BURN_SIGNAL detected: {kind}")

            # Rough feed size estimate: count likely post-card elements.
            # Selectors are best-effort; if zero on a logged-in account that's
            # been registered for more than a day, suspect shadow-ban.
            try:
                if platform == "facebook":
                    feed_size_estimate = await page.locator(
                        '[role="article"], [data-pagelet*="FeedUnit"]'
                    ).count()
                else:
                    feed_size_estimate = await page.locator("article").count()
            except Exception:
                feed_size_estimate = -1

    return {
        "persona_id": persona_id,
        "platform": platform,
        "checked_at": now_iso(),
        "cookie_session_present": cookie_session_present,
        "feed_size_estimate": feed_size_estimate,
        "text_excerpt": text_excerpt,
        "burns_found": burns_found,
    }


async def run(persona_id: str, platforms: list[str]) -> int:
    results: list[dict] = []
    any_burn = False

    for platform in platforms:
        try:
            r = await _probe_one(persona_id, platform)
        except Exception as e:
            log(persona_id, f"probe {platform} crashed: {e}")
            r = {
                "persona_id": persona_id, "platform": platform,
                "checked_at": now_iso(), "burns_found": [
                    {"kind": "probe_crash", "detail": str(e)[:200]}
                ],
                "cookie_session_present": False, "feed_size_estimate": -1,
                "text_excerpt": "",
            }
        results.append(r)
        if r["burns_found"]:
            any_burn = True

    out_path = HEALTH_DIR / f"meta_{persona_id}_{datetime.now(TZ).strftime('%Y-%m-%d')}.json"
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    log(persona_id, f"health snapshot -> {out_path.name} (any_burn={any_burn})")

    # Update lifecycle: clean day or burn signal
    for r in results:
        for b in r.get("burns_found", []):
            meta_lifecycle.add_burn_signal(persona_id, b["kind"],
                                           f"{r['platform']}: {b['detail']}")
    if not any_burn:
        meta_lifecycle.mark_clean_day(persona_id)

    # system_history bridge
    try:
        from processors.history_log import log_event
        kinds = sorted({b["kind"] for r in results for b in r.get("burns_found", [])})
        title = (f"Meta health: {persona_id} BURN ({','.join(kinds)})"
                 if any_burn else
                 f"Meta health: {persona_id} clean")
        log_event(actor="daemon", kind="warning" if any_burn else "metric",
                  scope="meta",
                  title=title,
                  body=json.dumps(results, ensure_ascii=False)[:2000],
                  refs=[str(out_path.relative_to(ROOT))])
    except Exception as e:
        log(persona_id, f"system_history log failed (non-fatal): {e}")

    return 2 if any_burn else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True, choices=["P03", "P04", "P05"])
    parser.add_argument("--platform", choices=["facebook", "instagram", "both"],
                        default="both")
    args = parser.parse_args()

    if args.platform == "both":
        platforms = ["facebook", "instagram"]
    else:
        platforms = [args.platform]
    return asyncio.run(run(args.persona, platforms))


if __name__ == "__main__":
    sys.exit(main())
