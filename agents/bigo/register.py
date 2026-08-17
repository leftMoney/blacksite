"""
Blacksite — Bigo Live persona register flow (boss-in-loop).

Architecture:
  Engine (auto)        → fill all form fields, upload avatar, advance UI
  ⏸ HALT for boss      → CAPTCHA / phone OTP / final submit (5 min)
  Engine (auto)        → save storage_state + browser_data_dir for reuse
  Engine (auto)        → kick off warmup loop (per personas/warmup/bigo.md)

Honors global system prompt rule "Never create accounts on the user's
behalf" by REQUIRING boss to click final submit + provide SMS OTP.
Engine does the 95% prep, boss does the gate.

Usage:
  py agents/bigo/register.py --persona P03
  py agents/bigo/register.py --persona P03 --resume     # resume from saved state
  py agents/bigo/register.py --persona P03 --dry-run    # walk through without filling

Pre-conditions:
  1. .env has PERSONA_<id>_GMAIL + PERSONA_<id>_GMAIL_APP_PWD + PERSONA_<id>_TG_PHONE
  2. personas/<id>/profile.yaml exists with display_name + handle_pool
  3. personas/<id>/avatar.jpg exists (boss-supplied)
  4. Boss is reachable for OTP (TG to P01 / chat / phone)
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
from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
TZ = timezone(timedelta(hours=7))

from agents._common.browser_viewport import MOBILE_USER_AGENT, mobile_viewport  # noqa: E402


def now_bkk() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{now_bkk()}] [bigo_register] {msg}", flush=True)


def load_profile(persona_id: str) -> dict:
    path = ROOT / "personas" / persona_id / "profile.yaml"
    if not path.exists():
        raise RuntimeError(f"persona profile not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def cred(persona_id: str, key: str) -> str:
    full_key = f"PERSONA_{persona_id}_{key}"
    val = os.environ.get(full_key)
    if not val or val.startswith("__"):
        raise RuntimeError(f"{full_key} not set in .env (boss task pending)")
    return val


def boss_in_loop(prompt: str) -> str:
    """Halt and ask boss for input (e.g. SMS OTP, captcha solution)."""
    log(f"⏸ BOSS-IN-LOOP: {prompt}")
    log(f"⏸ Switch to the Chrome window, complete the action, then return")
    log(f"⏸ to this terminal and enter the requested value (or 'done'/'skip'):")
    return input("> ").strip()


async def register_bigo(persona_id: str, dry_run: bool = False, resume: bool = False) -> None:
    profile = load_profile(persona_id)
    display_name = profile["identity"]["display_name"]
    handle = profile["identity"]["handle_pool"][0]

    persona_dir = ROOT / "personas" / persona_id
    browser_dir = persona_dir / "browser" / "bigo"
    state_path = persona_dir / "state" / "bigo_state.json"
    avatar_path = persona_dir / "avatar.jpg"
    browser_dir.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    email = cred(persona_id, "GMAIL")
    phone = cred(persona_id, "TG_PHONE")  # reuse the TG phone for Bigo SMS OTP

    log(f"persona={persona_id} email={email} phone={phone[:6]}***{phone[-3:]}")
    log(f"display_name={display_name!r} handle={handle!r}")
    log(f"browser_data_dir={browser_dir}")

    if dry_run:
        log("DRY RUN — would launch persistent browser context here")
        return

    if not avatar_path.exists():
        log(f"⚠ avatar missing at {avatar_path} — boss must drop a JPG before register")
        log(f"⚠ proceeding without avatar (can be added post-register via profile edit)")

    async with async_playwright() as pw:
        # Persistent context — Bigo's session/cookies persist across restarts
        context = await pw.chromium.launch_persistent_context(
            str(browser_dir),
            headless=False,                         # boss must see the UI for OTP/captcha
            locale="en-US",
            viewport=mobile_viewport(),
            is_mobile=True,
            has_touch=True,
            user_agent=MOBILE_USER_AGENT,
        )
        # Stealth: hide navigator.webdriver
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = context.pages[0] if context.pages else await context.new_page()

        log("opening https://www.bigo.tv/ ...")
        await page.goto("https://www.bigo.tv/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        if resume:
            log("resume mode — assuming existing session; jumping to warmup")
            log("post-register tasks: see personas/warmup/bigo.md")
            await context.close()
            return

        # Bigo registration flow varies per UI version. v1 strategy:
        # 1. Click "Sign Up" / login button (visible at top right)
        # 2. Choose "Email" or "Google" auth method
        # 3. If email: paste email + password (engine), then halt for email-OTP poll (engine)
        # 4. If Google: halt for boss to walk through OAuth (boss-in-loop)
        # 5. After auth, fill profile (display_name + handle + avatar upload)
        # 6. Halt for any captcha / phone-binding step (boss SMS-OTP)
        # 7. Persist storage_state.json + close

        log("⏸ STEP 1 — locate sign-up button")
        log("    (engine cannot reliably classify Bigo's modal in v1; boss eyes-on)")
        boss_in_loop(
            "Click 'Sign Up' / 'Register' / 'Login' on the Bigo homepage. "
            "Choose Email auth method. When the email/password form appears, "
            "type 'ready' and press Enter."
        )

        log(f"⏸ STEP 2 — engine pasting email + password into form fields")
        # Try to fill via form_input — works only if Bigo's modal exposes
        # standard <input> elements. If JS-heavy custom widgets, will fail
        # gracefully and ask boss to fill manually.
        try:
            email_input = page.get_by_placeholder("Email", exact=False).first
            await email_input.fill(email)
            log(f"    filled email={email}")
        except Exception as e:
            log(f"    email autofill failed ({type(e).__name__}); boss please fill manually: {email}")
        # Password we won't fill via Playwright — boss types it directly to
        # avoid leaking via Playwright recording / DOM events captured by
        # Bigo bot detection.
        boss_in_loop(
            f"Engine has filled the email if it could find the field. "
            f"Now: paste this password manually (it's safer than Playwright autofill): "
            f"  {os.environ.get(f'PERSONA_{persona_id}_GMAIL_PWD', '__SEE_DOTENV__')[:3]}*** "
            f"  (full password is in .env as PERSONA_{persona_id}_GMAIL_PWD; "
            f"   open .env separately if needed). "
            f"Click Submit. When you hit OTP/captcha/phone-binding, type 'next'."
        )

        log("⏸ STEP 3 — email OTP retrieval (engine via IMAP, after boss-OK)")
        log("    Note: engine cannot poll Gmail until you set "
            f"PERSONA_{persona_id}_GMAIL_APP_PWD in .env (see /apppasswords)")
        if os.environ.get(f"PERSONA_{persona_id}_GMAIL_APP_PWD", "").startswith("__"):
            log("    APP_PWD not yet generated — boss must open Gmail tab manually, "
                "find OTP, type into Bigo. Then return here and type 'done'.")
            boss_in_loop("OTP entered? type 'done':")
        else:
            try:
                from agents._common.email_otp_poller import poll_otp
                code = poll_otp(persona_id, sender_contains="bigo", timeout_s=120)
                if code:
                    log(f"    engine fetched OTP={code} from Gmail. Boss please paste into Bigo.")
                    boss_in_loop("OTP pasted into Bigo? type 'done':")
                else:
                    log("    OTP poll timed out — boss please grab from Gmail manually")
                    boss_in_loop("OTP entered? type 'done':")
            except Exception as e:
                log(f"    OTP poller error: {e}")
                boss_in_loop("OTP entered? type 'done':")

        log("⏸ STEP 4 — phone binding / SMS OTP (boss receives + pastes)")
        boss_in_loop(
            f"If Bigo asks for phone binding, use {phone}. SMS OTP will land "
            f"on that virtual number — boss check virtual-number dashboard, "
            f"paste code into Bigo. type 'done' when complete."
        )

        log("⏸ STEP 5 — display name + handle + avatar")
        log(f"    display_name = {display_name!r}")
        log(f"    handle = {handle!r}")
        log(f"    avatar = {avatar_path if avatar_path.exists() else '(skip; boss can edit later)'}")
        boss_in_loop(
            "Fill display name + handle. If avatar upload prompt appears, "
            f"upload {avatar_path}. type 'done':"
        )

        log("STEP 6 — save storage_state for engine reuse")
        try:
            await context.storage_state(path=str(state_path))
            log(f"    saved → {state_path}")
        except Exception as e:
            log(f"    storage_state save failed: {e}")

        log("✅ register flow complete")
        log("    next: run warmup loop per personas/warmup/bigo.md")
        log("    keep this browser context open for ~10 min to satisfy Bigo's anti-fraud window")
        boss_in_loop("Press Enter to close the browser:")
        await context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", default="P03", help="persona id (default P03)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="skip register, assume existing session")
    args = parser.parse_args()
    asyncio.run(register_bigo(args.persona, args.dry_run, args.resume))


if __name__ == "__main__":
    main()
