"""
Blacksite — Facebook + Instagram register flow (Route A, boss-in-loop).

Walks boss through FB register form, then IG SSO bind, then exports the
storage_state for both surfaces so daemon-driven Route B can take over.

Boss-in-loop responsibilities (per fb_ig_strategy.md §1 register sequence):
  - Receive SMS OTP on +44 retail SIM (P0X), type into the form when prompted
  - Solve CAPTCHA if Meta presents it
  - Approve photo verification if Meta requests it (using personas/P0X/avatar.jpg
    + 0X.png cover photo)

Engine responsibilities:
  - Open Camoufox with persona's per-platform user_data_dir
  - Auto-fill identity fields from personas/P0X/profile.yaml + .env
  - Watch for register success (URL transition + cookie presence)
  - Save storage_state on success
  - Drive IG SSO bind ("Sign up with Facebook")
  - Mark lifecycle event in personas/P0X/state/meta_lifecycle.json

Usage:
  py agents/facebook/register.py --persona P03
  py agents/facebook/register.py --persona P03 --skip-fb         # IG only (rare)
  py agents/facebook/register.py --persona P03 --skip-ig         # FB only

Headless=False is hardcoded — boss watches the browser to handle SMS/CAPTCHA.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agents._common.camoufox_session import (
    launch_persona,
    load_persona_profile,
    storage_state_path,
)
from agents._common import meta_lifecycle

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(persona_id: str, msg: str) -> None:
    line = f"[{now_iso()}] [register] [{persona_id}] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"meta_register_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_persona_creds(persona_id: str) -> dict[str, str]:
    """Pull email + phone + name + DOB + avatar path from .env + profile.yaml."""
    profile = load_persona_profile(persona_id)
    ident = profile["identity"]
    res = ident["residence"]
    edu = ident["education"]

    email = os.environ.get(f"PERSONA_{persona_id}_GMAIL")
    pwd = os.environ.get(f"PERSONA_{persona_id}_GMAIL_PWD")
    phone = os.environ.get(f"PERSONA_{persona_id}_TG_PHONE")

    if not (email and pwd and phone):
        raise SystemExit(
            f"Missing PERSONA_{persona_id}_GMAIL / _GMAIL_PWD / _TG_PHONE in .env"
        )

    dob = ident["date_of_birth"]
    if hasattr(dob, "isoformat"):
        dob_str = dob.isoformat()
    else:
        dob_str = str(dob)
    return {
        "first_name": ident["real_name"]["first"],
        "last_name": ident["real_name"]["last"],
        "email": email,
        "password": pwd,
        "phone": phone,
        "dob_iso": dob_str,            # YYYY-MM-DD
        "gender_register": ident.get("gender_register",
                                     "male" if ident.get("gender_presentation") == "male" else "female"),
        "city": res["city"],
        "hometown": res.get("hometown", res["city"]),
        "country": res["country"],
        "school": edu.get("school", ""),
        "occupation": ident.get("occupation", ""),
        "marital_status": ident.get("marital_status", "Single"),
        "bio_short": ident.get("bio_short", "").strip(),
        "bio_long": ident.get("bio_long", "").strip(),
        "display_name": ident.get("display_name", ""),
        "handle_pool": ident.get("handle_pool", []),
        "avatar_path": str(ROOT / "personas" / persona_id / "avatar.jpg"),
        "cover_path": str(next((ROOT / "personas" / persona_id).glob("0?.png"), Path(""))),
    }


# -------------------------------------------------------------------------
# Boss-in-loop pause helpers
# -------------------------------------------------------------------------

async def boss_pause(persona_id: str, prompt: str, timeout_s: int = 600) -> str:
    """Pause execution for boss action. Returns whatever boss types (or '' on enter).

    The browser stays open while we wait. Boss types into stdin when ready.
    """
    log(persona_id, f"BOSS-IN-LOOP wait: {prompt}")
    print(f"\n{'=' * 60}")
    print(f"  BOSS ACTION REQUIRED ({persona_id}):")
    print(f"  {prompt}")
    print(f"  Type your response then Enter (or just Enter to continue):")
    print(f"{'=' * 60}\n")
    loop = asyncio.get_event_loop()
    try:
        answer = await asyncio.wait_for(
            loop.run_in_executor(None, sys.stdin.readline), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        log(persona_id, f"BOSS-IN-LOOP TIMED OUT after {timeout_s}s on: {prompt}")
        raise SystemExit(2)
    return answer.strip()


# -------------------------------------------------------------------------
# Facebook register
# -------------------------------------------------------------------------

FB_REGISTER_URL = "https://www.facebook.com/r.php"


async def register_facebook(persona_id: str) -> bool:
    """Returns True if FB register succeeded (storage_state has c_user cookie)."""
    creds = get_persona_creds(persona_id)
    log(persona_id, f"FB register start as {creds['first_name']} {creds['last_name']}")

    async with launch_persona(
        persona_id, "facebook", headless=False, use_storage_state=False,
    ) as (browser, context, page):

        await page.goto(FB_REGISTER_URL, wait_until="domcontentloaded")
        log(persona_id, f"loaded {FB_REGISTER_URL}")
        await page.wait_for_timeout(2500)

        # Best-effort form fill. Selectors are generic-ish; if Meta has
        # changed the form, boss fills the rest by hand. Engine just
        # primes the textboxes it can confidently locate.
        try:
            # First name
            await _try_fill(page, ['input[name="firstname"]', 'input[aria-label*="First"]'],
                            creds["first_name"])
            # Last name
            await _try_fill(page, ['input[name="lastname"]', 'input[aria-label*="Last"]'],
                            creds["last_name"])
            # Email or phone
            await _try_fill(page, ['input[name="reg_email__"]', 'input[aria-label*="email"]'],
                            creds["email"])
            # Password
            await _try_fill(page, ['input[name="reg_passwd__"]', 'input[type="password"]'],
                            creds["password"])
            # DOB selects (YYYY-MM-DD)
            try:
                yyyy, mm, dd = creds["dob_iso"].split("-")
                await page.select_option('select[name="birthday_year"]', yyyy)
                await page.select_option('select[name="birthday_month"]', str(int(mm)))
                await page.select_option('select[name="birthday_day"]', str(int(dd)))
            except Exception as e:
                log(persona_id, f"DOB select fill skipped: {e} (boss fills manually)")
            # Gender radio
            try:
                gender_value = "1" if creds["gender_register"].lower() == "female" else "2"
                await page.click(f'input[name="sex"][value="{gender_value}"]')
            except Exception as e:
                log(persona_id, f"Gender radio skipped: {e} (boss fills manually)")
        except Exception as e:
            log(persona_id, f"Form prefill encountered exception: {e} — boss fills the rest")

        log(persona_id, "form pre-filled where possible")

        await boss_pause(
            persona_id,
            "Review the form, click 'Sign Up' yourself. Then we wait for SMS OTP.",
            timeout_s=600,
        )

        await boss_pause(
            persona_id,
            f"After clicking Sign Up: receive SMS OTP on phone {creds['phone']} "
            "and type it into the form, click Confirm. Then press Enter here when "
            "you've reached your FB feed (logged in).",
            timeout_s=900,
        )

        # Verify register success
        cookies = await context.cookies()
        c_user = next((c for c in cookies if c["name"] == "c_user"), None)
        if not c_user:
            log(persona_id, "FB register FAILED — no c_user cookie present")
            return False
        log(persona_id, f"FB register OK (c_user={c_user['value'][:6]}...)")

        # Persist storage_state explicitly (also done in launch_persona finalize)
        sp = storage_state_path(persona_id, "facebook")
        await context.storage_state(path=str(sp))
        log(persona_id, f"FB storage_state -> {sp}")

        # Optional profile fill (avatar / cover / bio) — boss-driven via prompt
        await boss_pause(
            persona_id,
            f"Now fill profile: upload avatar from {creds['avatar_path']}, "
            f"upload cover from {creds['cover_path']}, set bio = '{creds['bio_short']}', "
            f"city = {creds['city']}, hometown = {creds['hometown']}, "
            f"school = {creds['school']}, work = {creds['occupation']}. "
            "Press Enter when done.",
            timeout_s=900,
        )

        # Final storage_state save with profile data populated
        await context.storage_state(path=str(sp))
        meta_lifecycle.mark_register_event(persona_id, "facebook")
        log(persona_id, "FB lifecycle event recorded")
        return True


async def _try_fill(page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            await page.fill(sel, value, timeout=2500)
            return True
        except Exception:
            continue
    return False


# -------------------------------------------------------------------------
# Instagram register (via FB SSO bind)
# -------------------------------------------------------------------------

IG_SIGNUP_URL = "https://www.instagram.com/accounts/emailsignup/"


async def register_instagram(persona_id: str) -> bool:
    """IG via FB SSO. Loads FB-state Camoufox, then opens IG signup with FB option.

    Returns True if IG register succeeded (sessionid cookie + handle confirmed).
    """
    creds = get_persona_creds(persona_id)
    log(persona_id, "IG register start (via FB SSO)")

    # Use facebook user_data_dir for the SSO link to be auto-recognized
    async with launch_persona(
        persona_id, "facebook", headless=False, use_storage_state=True,
    ) as (browser, context, page):

        await page.goto(IG_SIGNUP_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        log(persona_id, "loaded IG signup page (looking for 'Log in with Facebook')")

        await boss_pause(
            persona_id,
            "Click 'Log in with Facebook' (or 'Continue as <FB name>'). "
            f"Use handle from pool: {' / '.join(creds['handle_pool'][:3])}. "
            "Walk through any IG prompts (DOB, gender, find friends — skip find friends!). "
            "When you reach the IG home feed, press Enter.",
            timeout_s=900,
        )

        cookies = await context.cookies()
        # IG cookies live on .instagram.com domain
        sessionid = next((c for c in cookies
                          if c["name"] == "sessionid" and "instagram" in c["domain"]),
                         None)
        if not sessionid:
            log(persona_id, "IG register FAILED — no instagram sessionid cookie")
            return False
        log(persona_id, f"IG register OK (sessionid={sessionid['value'][:8]}...)")

        # Save IG storage_state separately
        sp_ig = storage_state_path(persona_id, "instagram")
        # We dump full context state (includes IG cookies). The FB state is also
        # in there; that's fine because both surfaces share Meta auth.
        await context.storage_state(path=str(sp_ig))
        log(persona_id, f"IG storage_state -> {sp_ig}")

        await boss_pause(
            persona_id,
            f"Optionally fill IG bio (e.g. '{creds['bio_short']}'), upload avatar "
            f"from {creds['avatar_path']}. Then go IG Settings -> 'Show on Facebook' = ON. "
            "Press Enter when done.",
            timeout_s=600,
        )

        await context.storage_state(path=str(sp_ig))
        meta_lifecycle.mark_register_event(persona_id, "instagram")
        log(persona_id, "IG lifecycle event recorded")
        return True


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

async def amain() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True, choices=["P03", "P04", "P05"])
    parser.add_argument("--skip-fb", action="store_true", help="Skip FB step (IG only)")
    parser.add_argument("--skip-ig", action="store_true", help="Skip IG step (FB only)")
    args = parser.parse_args()

    log(args.persona, f"register run starting (skip_fb={args.skip_fb} skip_ig={args.skip_ig})")
    fb_ok = True
    ig_ok = True

    if not args.skip_fb:
        fb_ok = await register_facebook(args.persona)
        if not fb_ok:
            log(args.persona, "FB register failed — aborting before IG")
            return 1

    if not args.skip_ig:
        ig_ok = await register_instagram(args.persona)
        if not ig_ok:
            log(args.persona, "IG register failed (FB succeeded — IG can be re-attempted)")

    if fb_ok and ig_ok:
        log(args.persona, "FB+IG register full success — Route B daemon can take over")
        # Try to advance lifecycle (register -> limited)
        advanced, stage = meta_lifecycle.maybe_advance_stage(args.persona)
        log(args.persona, f"lifecycle advanced={advanced} now stage={stage}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
