"""
Blacksite — Bigo Live persona LOGIN flow (boss-in-loop, one-shot).

Use AFTER boss has registered the account elsewhere (own browser / phone).
Engine opens a persistent Chromium context, boss logs in once, engine
saves storage_state for downstream agents (room_monitor etc.) to reuse.

Subsequent runs: persistent context already has cookies → boss just
confirms logged-in status, engine refreshes storage_state.

Usage:
  py agents/bigo/login.py --persona P03
  py agents/bigo/login.py --persona P03 --probe-only  # check session, no save

Pre-conditions:
  1. Account registered on bigo.tv (any method: email+pwd / Google / phone)
  2. personas/<id>/profile.yaml exists
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyotp
import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout

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
    print(f"[{now_bkk()}] [bigo_login] {msg}", flush=True)


def load_profile(persona_id: str) -> dict:
    path = ROOT / "personas" / persona_id / "profile.yaml"
    if not path.exists():
        raise RuntimeError(f"persona profile not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def google_oauth_flow(context: BrowserContext, bigo_page: Page, persona_id: str) -> bool:
    """Drive 'Continue with Google' on Bigo all the way back to logged-in Bigo.

    Bigo opens Google OAuth in a popup. We intercept it via expect_page().
    Returns True if Google flow completed (Bigo page reload still TBD)."""
    email = os.environ.get(f"PERSONA_{persona_id}_GMAIL")
    pwd = os.environ.get(f"PERSONA_{persona_id}_GMAIL_PWD")
    totp_secret = os.environ.get(f"PERSONA_{persona_id}_TOTP_SECRET")
    if not email or not pwd:
        log("⚠ missing GMAIL or GMAIL_PWD in .env — cannot drive OAuth automatically")
        return False
    log(f"google oauth: email={email} totp={'available' if totp_secret and not totp_secret.startswith('__') else 'absent (will boss-in-loop if challenged)'}")

    log("clicking Bigo 'Sign In' button (top right)...")
    candidates = [
        "text=/^Sign In$/i",
        "text=/^Log In$/i",
        "text=/登入/",
        "[class*='login' i]",
    ]
    clicked = False
    for sel in candidates:
        try:
            loc = bigo_page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                await loc.click(timeout=3000)
                log(f"    clicked Sign In via selector: {sel}")
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        log("⚠ could not find Sign In button — boss please click it manually then type 'continue'")
        if input("> ").strip().lower() != "continue":
            return False

    await bigo_page.wait_for_timeout(2000)

    log("clicking 'Continue with Google' in login modal...")
    google_candidates = [
        "text=/Continue with Google/i",
        "text=/Sign in with Google/i",
        "text=/Google/i",
        "[class*='google' i]",
        "img[alt*='google' i]",
    ]
    google_clicked = False
    popup_promise = context.wait_for_event("page", timeout=15000)
    for sel in google_candidates:
        try:
            loc = bigo_page.locator(sel).first
            if await loc.is_visible(timeout=2000):
                await loc.click(timeout=3000)
                log(f"    clicked Google via selector: {sel}")
                google_clicked = True
                break
        except Exception:
            continue
    if not google_clicked:
        log("⚠ could not find 'Continue with Google' — boss please click then type 'continue'")
        if input("> ").strip().lower() != "continue":
            return False

    try:
        oauth_page = await popup_promise
        log(f"    Google OAuth popup captured: {oauth_page.url}")
    except PWTimeout:
        log("    no popup — checking if same-tab redirect to accounts.google.com")
        if "accounts.google.com" in bigo_page.url:
            oauth_page = bigo_page
            log("    same-tab redirect detected")
        else:
            log("⚠ no OAuth window detected after Google click")
            return False

    await oauth_page.wait_for_load_state("domcontentloaded", timeout=15000)

    try:
        log(f"  filling email: {email}")
        await oauth_page.locator("input[type='email']").first.fill(email, timeout=10000)
        await oauth_page.locator("button:has-text('Next'), button:has-text('下一步')").first.click(timeout=5000)
        await oauth_page.wait_for_timeout(2500)
    except Exception as e:
        log(f"⚠ email step failed: {type(e).__name__}: {e}")
        if input("Boss please complete email step then type 'continue': ").strip().lower() != "continue":
            return False

    try:
        log("  filling password")
        await oauth_page.locator("input[type='password']").first.fill(pwd, timeout=10000)
        await oauth_page.locator("button:has-text('Next'), button:has-text('下一步')").first.click(timeout=5000)
        await oauth_page.wait_for_timeout(3000)
    except Exception as e:
        log(f"⚠ password step failed: {type(e).__name__}: {e}")
        if input("Boss please complete password then type 'continue': ").strip().lower() != "continue":
            return False

    for _ in range(20):
        try:
            if oauth_page.is_closed():
                log("  oauth popup closed — assuming success, returning to Bigo")
                break
        except Exception:
            break
        url = oauth_page.url
        if "challenge/totp" in url or "challenge/ipp" in url:
            if totp_secret and not totp_secret.startswith("__"):
                code = pyotp.TOTP(totp_secret).now()
                log(f"  TOTP challenge: generated code {code}")
                try:
                    await oauth_page.locator("input[type='tel'], input[name='totpPin'], input[type='text']").first.fill(code, timeout=5000)
                    await oauth_page.locator("button:has-text('Next'), button:has-text('下一步')").first.click(timeout=5000)
                    await oauth_page.wait_for_timeout(3000)
                except Exception as e:
                    log(f"  TOTP fill failed: {e}")
                    if input("Boss please complete TOTP then type 'continue': ").strip().lower() != "continue":
                        return False
            else:
                log("  TOTP challenge but no TOTP_SECRET in .env — boss-in-loop")
                if input("Complete TOTP then type 'continue': ").strip().lower() != "continue":
                    return False
        elif "challenge/dp" in url or "challenge/sk" in url or "challenge/" in url:
            log(f"  Google challenge requires manual: {url}")
            print(f"\nGoogle is asking for an extra verification: {url}")
            print("Common challenges: tap notification on phone / pick number / recovery email")
            if input("Complete in browser then type 'continue': ").strip().lower() != "continue":
                return False
        elif "bigo.tv" in url or oauth_page == bigo_page:
            log("  redirected back to bigo — oauth complete")
            break
        else:
            await oauth_page.wait_for_timeout(1500)

    return True


async def detect_logged_in(page) -> tuple[bool, str]:
    """Bigo logged-in indicators verified against live DOM (TH/TW/EN locales):
       - element with class 'user-profile-userinfo__nickname' contains the nickname
       - cookie 'tid' / 'yyuid' set on .bigo.tv
       - 'Sign In' / '登入' / 'Log In' button absent
    """
    nickname = None
    try:
        nick_el = page.locator(".user-profile-userinfo__nickname").first
        if await nick_el.is_visible(timeout=2500):
            nickname = (await nick_el.text_content() or "").strip()
    except Exception:
        nickname = None

    cookies = await page.context.cookies()
    auth_cookies = {c["name"] for c in cookies if "bigo" in c["domain"] and c["name"] in {"tid", "yyuid", "uniqid", "user_name"}}

    try:
        signin_visible = await page.locator(
            "text=/^Sign\\s*In$|^Log\\s*In$|^登入$|^登錄$/i"
        ).first.is_visible(timeout=1500)
    except Exception:
        signin_visible = False

    if nickname:
        return True, f"nickname={nickname!r} auth_cookies={sorted(auth_cookies)}"
    if auth_cookies and not signin_visible:
        return True, f"auth_cookies={sorted(auth_cookies)} no signin button"
    if signin_visible:
        return False, f"signin button visible / no auth cookies"
    return False, f"no nickname / no auth cookies (cookies on bigo: {len([c for c in cookies if 'bigo' in c['domain']])})"


async def run(persona_id: str, probe_only: bool, google: bool, use_chrome: bool) -> None:
    profile = load_profile(persona_id)
    display_name = profile["identity"]["display_name"]

    persona_dir = ROOT / "personas" / persona_id
    browser_dir = persona_dir / "browser" / ("chrome" if use_chrome else "bigo")
    state_path = persona_dir / "state" / "bigo_state.json"
    browser_dir.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    email = os.environ.get(f"PERSONA_{persona_id}_GMAIL", "(unknown)")
    log(f"persona={persona_id} display={display_name!r} email={email}")
    log(f"browser_data_dir={browser_dir} (channel={'chrome' if use_chrome else 'chromium-bundled'})")
    log(f"state_path={state_path}")

    async with async_playwright() as pw:
        launch_kwargs = dict(
            headless=False,
            locale="en-US",
            viewport=mobile_viewport(),
            is_mobile=True,
            has_touch=True,
            user_agent=MOBILE_USER_AGENT,
        )
        if use_chrome:
            launch_kwargs["channel"] = "chrome"
        context = await pw.chromium.launch_persistent_context(
            str(browser_dir), **launch_kwargs
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = context.pages[0] if context.pages else await context.new_page()

        log("opening https://www.bigo.tv/th/ ...")
        await page.goto("https://www.bigo.tv/th/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)

        logged_in, evidence = await detect_logged_in(page)
        log(f"initial state: logged_in={logged_in} ({evidence})")

        if probe_only:
            log("probe-only mode — closing without changes")
            await context.close()
            return

        if not logged_in and google:
            log("attempting Google OAuth automated login...")
            ok = await google_oauth_flow(context, page, persona_id)
            if ok:
                await page.bring_to_front()
                await page.reload(wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(3000)
                logged_in, evidence = await detect_logged_in(page)
                log(f"post-oauth state: logged_in={logged_in} ({evidence})")

        if not logged_in:
            print()
            print("=" * 70)
            print("BOSS-IN-LOOP — engine cannot type the Bigo password (we do not")
            print("have it; and Bigo's bot detection penalizes Playwright autofill).")
            print()
            print("In the Chromium window now open:")
            print("  1. Click 'Sign In' (top right)")
            print("  2. Choose the SAME method you used to register")
            print("     (email + password / Continue with Google / phone OTP)")
            print("  3. Complete login. Solve any captcha / device verify if asked.")
            print("  4. Wait until you see your avatar in the top nav.")
            print()
            print("Then return to this terminal and type 'done' (or 'fail' to abort):")
            print("=" * 70)
            ans = input("> ").strip().lower()
            if ans != "done":
                log("aborted by boss; not saving state")
                await context.close()
                return
            await page.wait_for_timeout(2000)
            logged_in, evidence = await detect_logged_in(page)
            log(f"post-login state: logged_in={logged_in} ({evidence})")

        if not logged_in:
            log("⚠ login indicators NOT detected — selectors may be stale")
            log("⚠ saving storage_state anyway (cookies present even if DOM probe failed)")

        try:
            await context.storage_state(path=str(state_path))
            size = state_path.stat().st_size
            log(f"✅ storage_state saved → {state_path} ({size} bytes)")
        except Exception as e:
            log(f"⚠ storage_state save FAILED: {e}")

        cookies = await context.cookies()
        bigo_cookies = [c for c in cookies if "bigo" in c.get("domain", "")]
        log(f"cookies captured: total={len(cookies)} bigo-domain={len(bigo_cookies)}")
        if bigo_cookies:
            names = sorted({c["name"] for c in bigo_cookies})
            log(f"bigo cookie names: {', '.join(names[:20])}{'...' if len(names)>20 else ''}")

        print()
        print("Browser window kept open. Press Enter to close (state already saved):")
        input("> ")
        await context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", default="P03")
    parser.add_argument("--probe-only", action="store_true",
                        help="open browser, check session, exit without saving")
    parser.add_argument("--google", action="store_true", default=True,
                        help="attempt Google OAuth automation (default ON)")
    parser.add_argument("--no-google", dest="google", action="store_false",
                        help="skip OAuth automation, go straight to boss-in-loop")
    parser.add_argument("--use-chrome", action="store_true",
                        help="use real Chrome (channel='chrome') with browser/chrome/ "
                             "user_data_dir — pair with import_chrome_profile.py")
    args = parser.parse_args()
    asyncio.run(run(args.persona, args.probe_only, args.google, args.use_chrome))


if __name__ == "__main__":
    main()
