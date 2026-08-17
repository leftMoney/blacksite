"""Pantip P05 recovery: try alias login once, then register if missing.

Flow:
  1. Try login with the configured alias and existing Pantip password.
  2. If Pantip says the account does not exist, run the 2-click register flow.
  3. Poll Gmail IMAP for Pantip OTP, set password, and persist storage_state.

This is intentionally a one-clean-run helper because Pantip applies cooldowns
after failed or half-completed attempts.
"""

from __future__ import annotations

import argparse
import asyncio
import imaplib
import os
import re
import sys
from email import message_from_bytes
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from agents._common.camoufox_session import launch_persona, storage_state_path  # noqa: E402
from processors.history_log import log_event  # noqa: E402

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
SCREENSHOT_DIR = RUNTIME / "screenshots"
LOG_DIR = RUNTIME / "logs"
RAW_DIR = RUNTIME / "raw" / "P05_Pantip"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))
PLATFORM = "pantip"
HOME_URL = "https://pantip.com/"
LOGIN_URL = "https://pantip.com/login"
REGISTER_URL = "https://pantip.com/register"
LOGGED_IN_MARKERS = [
    'a[href*="/profile"]',
    '[class*="userZone" i]',
    'a[href*="/logout"]',
    'a[href*="/notification"]',
    'img[class*="avatar" i]',
]
# TODO: set UI markers for your instance's language — add the target language's
# "account not found" / "sign up for an account" page wording.
MISSING_ACCOUNT_TOKENS = [
    "account not found (localized)",
    "sign up for an account (localized)",
    "does not match any account",
    "no account",
]


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [pantip_recover] {msg}"
    print(line, flush=True)
    with (LOG_DIR / f"pantip_recover_{datetime.now(TZ).strftime('%Y-%m-%d')}.log").open(
        "a", encoding="utf-8"
    ) as f:
        f.write(line + "\n")


def env_required(persona: str, key: str) -> str:
    name = f"PERSONA_{persona}_{key}"
    val = os.environ.get(name, "").strip()
    if not val or val.startswith("__"):
        raise RuntimeError(f"{name} is missing")
    return val


def load_profile(persona: str) -> dict[str, Any]:
    path = ROOT / "personas" / persona / "profile.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def emit_raw(event: dict[str, Any]) -> None:
    path = RAW_DIR / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"
    import json

    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_iso(), **event}, ensure_ascii=False) + "\n")


def decode_header_text(value: str) -> str:
    out = ""
    for part, enc in decode_header(value or ""):
        if isinstance(part, bytes):
            out += part.decode(enc or "utf-8", errors="replace")
        else:
            out += part
    return out


def html_to_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value)


def poll_pantip_otp(persona: str, ref: str | None, after_ts: datetime, timeout_s: int = 180) -> str | None:
    import time

    email = env_required(persona, "GMAIL")
    app_pwd = env_required(persona, "GMAIL_APP_PWD")
    deadline = time.monotonic() + timeout_s
    ref = (ref or "").strip().upper()
    fallback: tuple[datetime | None, str] | None = None

    while time.monotonic() < deadline:
        try:
            mbox = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            mbox.login(email, app_pwd)
            mbox.select("INBOX")
            typ, data = mbox.search(None, 'FROM "Pantip.com"')
            if typ != "OK" or not data or not data[0]:
                time.sleep(6)
                continue
            ids = data[0].split()[-12:]
            for raw_id in reversed(ids):
                typ, msg_data = mbox.fetch(raw_id, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = message_from_bytes(msg_data[0][1])
                try:
                    msg_dt = parsedate_to_datetime(msg.get("Date", ""))
                except Exception:
                    msg_dt = None
                if msg_dt and msg_dt < after_ts:
                    continue
                subject = decode_header_text(msg.get("Subject", ""))
                parts: list[str] = []
                if msg.is_multipart():
                    for part in msg.walk():
                        payload = part.get_payload(decode=True)
                        if not payload:
                            continue
                        charset = part.get_content_charset() or "utf-8"
                        parts.append(payload.decode(charset, errors="replace"))
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
                text = html_to_text(subject + " " + " ".join(parts))
                codes = re.findall(r"\b(\d{6})\b", text)
                refs = [x.upper() for x in re.findall(r"(?:Ref[.\s:]*|Ref\.)([A-Z0-9]{5,8})", text, re.I)]
                if not codes:
                    continue
                newest_code = codes[-1]
                if ref and ref in refs:
                    log(f"OTP matched Pantip Ref.{ref}")
                    return newest_code
                if not fallback:
                    fallback = (msg_dt, newest_code)
            try:
                mbox.logout()
            except Exception:
                pass
        except Exception as e:
            log(f"OTP poll error: {type(e).__name__}: {e}")
        time.sleep(6)

    if fallback and not ref:
        log("OTP fallback: latest Pantip code without ref match")
        return fallback[1]
    return None


async def screenshot(page, persona: str, label: str) -> Path | None:
    path = SCREENSHOT_DIR / f"{persona}_Pantip_{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}_{label}.png"
    try:
        await page.screenshot(path=str(path), full_page=False)
        log(f"screenshot {label}: {path.name}")
        return path
    except Exception as e:
        log(f"screenshot {label} failed: {type(e).__name__}: {e}")
        return None


async def body_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=3_000)
    except Exception:
        return ""


async def is_logged_in(page) -> tuple[bool, str | None]:
    for marker in LOGGED_IN_MARKERS:
        try:
            if await page.locator(marker).first.is_visible(timeout=2_000):
                return True, marker
        except Exception:
            continue
    return False, None


async def click_first(page, selectors: list[str], timeout_ms: int = 3_000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() < 1:
                continue
            await loc.click(timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


async def fill_first(page, selectors: list[str], value: str, timeout_ms: int = 3_000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() < 1:
                continue
            await loc.fill(value, timeout=timeout_ms)
            return True
        except Exception:
            continue
    return False


async def fill_input_near_password(page, identifier: str) -> bool:
    return bool(await page.evaluate(
        """({ identifier }) => {
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== "hidden"
                    && style.display !== "none"
                    && rect.width > 80
                    && rect.height > 20;
            };
            const pass = Array.from(document.querySelectorAll('input[type="password"]')).find(visible);
            if (!pass) return false;
            const passBox = pass.getBoundingClientRect();
            const candidates = Array.from(document.querySelectorAll("input")).filter((el) => {
                const type = (el.getAttribute("type") || "text").toLowerCase();
                if (["password", "hidden", "search", "submit", "button"].includes(type)) return false;
                if (!visible(el)) return false;
                const box = el.getBoundingClientRect();
                return Math.abs(box.left - passBox.left) < 180
                    && box.top < passBox.top
                    && box.top > passBox.top - 140;
            });
            const target = candidates[0];
            if (!target) return false;
            target.focus();
            target.value = identifier;
            target.dispatchEvent(new Event("input", { bubbles: true }));
            target.dispatchEvent(new Event("change", { bubbles: true }));
            return true;
        }""",
        {"identifier": identifier},
    ))


async def fill_login_identifier(page, identifier: str) -> bool:
    # TODO: set UI markers for your instance's language — the first two
    # placeholders should be the target language's "username" / "email" labels.
    return await fill_first(page, [
        'input[placeholder*="username (localized)"]',
        'input[placeholder*="email (localized)"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="username" i]',
        'input[name*="email" i]',
        'input[name*="user" i]',
        'input[type="email"]',
    ], identifier) or await fill_input_near_password(page, identifier)


async def accept_popup(page) -> None:
    # TODO: set UI markers for your instance's language — "Accept (localized)"
    # should be the target language's cookie/consent "Accept" button label.
    await click_first(page, [
        'button:has-text("Accept (localized)")',
        'button:has-text("Accept")',
        '[role="button"]:has-text("Accept (localized)")',
        '[role="button"]:has-text("Accept")',
    ], timeout_ms=1_500)


async def try_alias_login(page, persona: str, alias: str, password: str) -> str:
    log(f"try alias login alias={alias}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(4_000)
    await accept_popup(page)
    filled = await fill_login_identifier(page, alias)
    await fill_first(page, ['input[type="password"]'], password)
    await screenshot(page, persona, "alias_login_filled")
    if not filled:
        log("alias login: could not find account input")
        emit_raw({"persona": persona, "platform": PLATFORM, "event": "alias_login", "result": "input_not_found"})
        return "input_not_found"
    # TODO: set UI markers for your instance's language — "Login (localized)"
    # should be the target language's "Login" button label.
    await click_first(page, [
        'button:has-text("Login (localized)")',
        'button:has-text("Login")',
        'button:has-text("Log in")',
        'input[type="submit"]',
    ])
    await page.wait_for_timeout(8_000)
    await screenshot(page, persona, "alias_login_result")
    ok, marker = await is_logged_in(page)
    if ok:
        log(f"alias login success marker={marker}")
        emit_raw({"persona": persona, "platform": PLATFORM, "event": "alias_login", "result": "success", "marker": marker})
        return "success"
    text = await body_text(page)
    if any(token.lower() in text.lower() for token in MISSING_ACCOUNT_TOKENS):
        log("alias login result: account_missing")
        emit_raw({"persona": persona, "platform": PLATFORM, "event": "alias_login", "result": "account_missing"})
        return "account_missing"
    if re.search(r"captcha|robot|verify|security", text, re.I):
        log("alias login result: human_gate")
        emit_raw({"persona": persona, "platform": PLATFORM, "event": "alias_login", "result": "human_gate"})
        return "human_gate"
    log("alias login result: failed_unknown")
    emit_raw({"persona": persona, "platform": PLATFORM, "event": "alias_login", "result": "failed_unknown"})
    return "failed_unknown"


async def fill_register_email(page, email: str) -> bool:
    # TODO: set UI markers for your instance's language — "email (localized)"
    # should be the target language's "email" placeholder label.
    filled = await fill_first(page, [
        'input[type="email"]',
        'input[name*="email" i]',
        'input[placeholder*="email (localized)"]',
        'input[placeholder*="email" i]',
    ], email)
    if filled:
        return True
    return bool(await page.evaluate(
        """({ email }) => {
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.visibility !== "hidden"
                    && style.display !== "none"
                    && rect.width > 100
                    && rect.height > 20;
            };
            const inputs = Array.from(document.querySelectorAll("input")).filter((el) => {
                const type = (el.getAttribute("type") || "text").toLowerCase();
                return !["hidden", "search", "submit", "button", "password"].includes(type) && visible(el);
            });
            const target = inputs[0];
            if (!target) return false;
            target.focus();
            target.value = email;
            target.dispatchEvent(new Event("input", { bubbles: true }));
            target.dispatchEvent(new Event("change", { bubbles: true }));
            return true;
        }""",
        {"email": email},
    ))


async def click_signup(page) -> bool:
    # TODO: set UI markers for your instance's language — "Sign up (localized)"
    # should be the target language's "Sign up" button label.
    return await click_first(page, [
        'button:has-text("Sign up (localized)")',
        '[role="button"]:has-text("Sign up (localized)")',
        'button:has-text("Sign up")',
        'button:has-text("Register")',
        'input[type="submit"]',
    ], timeout_ms=4_000)


async def close_popup(page) -> None:
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(1_000)
    # TODO: set UI markers for your instance's language — "Close (localized)"
    # should be the target language's "Close" button label.
    await click_first(page, [
        'button[aria-label="Close"]',
        '[aria-label="Close"]',
        'button:has-text("×")',
        'button:has-text("Close (localized)")',
    ], timeout_ms=1_500)


async def fill_otp(page, code: str) -> bool:
    boxes = page.locator('input[maxlength="1"]')
    try:
        if await boxes.count() >= 4:
            await boxes.first.click(timeout=3_000)
            await page.keyboard.type(code, delay=80)
            return True
    except Exception:
        pass
    try:
        visible_count = await page.evaluate(
            """() => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.visibility !== "hidden"
                        && style.display !== "none"
                        && rect.width >= 30
                        && rect.height >= 30;
                };
                return Array.from(document.querySelectorAll("input")).filter((el) => {
                    const type = (el.getAttribute("type") || "text").toLowerCase();
                    return !["hidden", "search", "submit", "button", "password", "email"].includes(type)
                        && visible(el);
                }).length;
            }"""
        )
        if int(visible_count or 0) >= 4:
            clicked = await page.evaluate(
                """() => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== "hidden"
                            && style.display !== "none"
                            && rect.width >= 30
                            && rect.height >= 30;
                    };
                    const inputs = Array.from(document.querySelectorAll("input")).filter((el) => {
                        const type = (el.getAttribute("type") || "text").toLowerCase();
                        return !["hidden", "search", "submit", "button", "password", "email"].includes(type)
                            && visible(el);
                    });
                    const first = inputs[0];
                    if (!first) return false;
                    first.focus();
                    first.click();
                    return true;
                }"""
            )
            if clicked:
                await page.keyboard.type(code, delay=80)
                return True
    except Exception:
        pass
    if await fill_first(page, ['input[type="tel"]', 'input[name*="otp" i]', 'input[name*="code" i]'], code):
        return True
    return False


async def current_pantip_ref(page) -> str | None:
    text = await body_text(page)
    match = re.search(r"Ref[.\s:]*([A-Z0-9]{5,8})", text, re.I)
    if match:
        return match.group(1).upper()
    return None


async def fill_passwords(page, password: str) -> bool:
    inputs = page.locator('input[type="password"]')
    try:
        count = await inputs.count()
        if count < 1:
            return False
        for i in range(min(count, 2)):
            await inputs.nth(i).fill(password, timeout=3_000)
        return True
    except Exception:
        return False


async def maybe_fill_profile(page, alias: str, profile: dict[str, Any]) -> None:
    display = profile.get("identity", {}).get("display_name") or alias
    # TODO: set UI markers for your instance's language — the localized
    # placeholders should be the target language's "name" / "alias" labels.
    await fill_first(page, [
        'input[name*="display" i]',
        'input[name*="name" i]',
        'input[placeholder*="name (localized)"]',
    ], display, timeout_ms=1_500)
    await fill_first(page, [
        'input[name*="alias" i]',
        'input[name*="username" i]',
        'input[placeholder*="alias (localized)"]',
        'input[placeholder*="username" i]',
    ], alias, timeout_ms=1_500)


async def register_pantip(page, context, persona: str, alias: str, email: str, password: str) -> str:
    log(f"register start email={email} alias={alias}")
    profile = load_profile(persona)
    marker_ts = datetime.now(timezone.utc)
    await page.goto(REGISTER_URL, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(4_000)
    await accept_popup(page)
    await screenshot(page, persona, "register_start")
    if not await fill_register_email(page, email):
        log("register: email input not found")
        emit_raw({"persona": persona, "platform": PLATFORM, "event": "register", "result": "email_input_not_found"})
        return "email_input_not_found"
    await screenshot(page, persona, "register_email_filled")
    if not await click_signup(page):
        log("register: signup button not found")
        emit_raw({"persona": persona, "platform": PLATFORM, "event": "register", "result": "signup_button_not_found"})
        return "signup_button_not_found"
    await page.wait_for_timeout(5_000)
    await screenshot(page, persona, "register_after_first_click")
    await close_popup(page)
    await click_signup(page)
    await page.wait_for_timeout(6_000)
    await screenshot(page, persona, "register_after_second_click")

    text = await body_text(page)
    # TODO: set UI markers for your instance's language — add the target
    # language's "already in use" / "account exists" page wording.
    if any(token.lower() in text.lower() for token in ("already", "in use (localized)", "already registered (localized)", "account (localized)")):
        log("register page reports account/email condition; continuing to inspect OTP fields")

    ref = await current_pantip_ref(page)
    if ref:
        log(f"register: page Ref.{ref}")
    code = poll_pantip_otp(persona, ref=ref, after_ts=marker_ts, timeout_s=180)
    if not code:
        await screenshot(page, persona, "register_otp_timeout")
        emit_raw({"persona": persona, "platform": PLATFORM, "event": "register", "result": "otp_timeout"})
        return "otp_timeout"
    log("register: got OTP from Gmail")
    if not await fill_otp(page, code):
        await screenshot(page, persona, "register_otp_input_not_found")
        emit_raw({"persona": persona, "platform": PLATFORM, "event": "register", "result": "otp_input_not_found"})
        return "otp_input_not_found"
    await screenshot(page, persona, "register_otp_filled")
    # TODO: set UI markers for your instance's language — "Confirm (localized)"
    # should be the target language's "Confirm" button label.
    await click_first(page, [
        'button:has-text("Confirm (localized)")',
        'button:has-text("Confirm")',
        'button:has-text("Next")',
        'input[type="submit"]',
    ], timeout_ms=4_000)
    await page.wait_for_timeout(6_000)
    await screenshot(page, persona, "register_after_otp")

    await maybe_fill_profile(page, alias, profile)
    if await fill_passwords(page, password):
        await screenshot(page, persona, "register_password_filled")
        # TODO: set UI markers for your instance's language — the localized
        # has-text() entries should be the target language's "Sign up" / "Confirm" labels.
        await click_first(page, [
            'button:has-text("Sign up (localized)")',
            'button:has-text("Confirm (localized)")',
            'button:has-text("Confirm")',
            'button:has-text("Done")',
            'input[type="submit"]',
        ], timeout_ms=4_000)
        await page.wait_for_timeout(8_000)
    else:
        log("register: password inputs not visible yet")

    await maybe_fill_profile(page, alias, profile)
    # TODO: set UI markers for your instance's language — the localized
    # has-text() entries should be the target language's "Save" / "Confirm" / "Done" labels.
    await click_first(page, [
        'button:has-text("Save (localized)")',
        'button:has-text("Save")',
        'button:has-text("Confirm (localized)")',
        'button:has-text("Confirm")',
        'button:has-text("Done (localized)")',
    ], timeout_ms=2_000)
    await page.wait_for_timeout(5_000)
    await screenshot(page, persona, "register_final")

    try:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(5_000)
    except Exception:
        pass
    ok, marker = await is_logged_in(page)
    await screenshot(page, persona, "register_home_verify")
    if ok:
        sp = storage_state_path(persona, PLATFORM)
        await context.storage_state(path=str(sp))
        log(f"register success marker={marker}; storage_state saved -> {sp.name}")
        emit_raw({"persona": persona, "platform": PLATFORM, "event": "register", "result": "success", "marker": marker})
        log_event(
            actor=f"{persona}_Pantip",
            kind="milestone",
            scope="persona",
            title=f"{persona}_Pantip registered and verified",
            body=f"alias={alias} marker={marker} storage_state={sp}",
            refs=[str(sp.relative_to(ROOT)).replace("\\", "/")],
        )
        return "success"

    log("register incomplete: logged-in marker not found")
    emit_raw({"persona": persona, "platform": PLATFORM, "event": "register", "result": "incomplete"})
    return "incomplete"


async def run(persona: str, alias: str, headless: bool, register_if_missing: bool) -> int:
    email = env_required(persona, "GMAIL")
    password = env_required(persona, "PANTIP_PWD")
    log(f"start persona={persona} email={email} alias={alias} headless={headless}")
    async with launch_persona(persona, PLATFORM, headless=headless, use_storage_state=True) as (
        browser,
        context,
        page,
    ):
        login_result = await try_alias_login(page, persona, alias, password)
        if login_result == "success":
            sp = storage_state_path(persona, PLATFORM)
            await context.storage_state(path=str(sp))
            log(f"alias login saved storage_state -> {sp.name}")
            return 0
        if login_result == "account_missing" and register_if_missing:
            result = await register_pantip(page, context, persona, alias, email, password)
            return 0 if result == "success" else 2
        log(f"stop after alias login result={login_result}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", default="P05", choices=["P03", "P04", "P05"])
    parser.add_argument("--alias", default="example_alias")
    parser.add_argument("--headless", action="store_true", help="run without visible browser")
    parser.add_argument("--register-if-missing", action="store_true")
    args = parser.parse_args()
    return asyncio.run(
        run(
            persona=args.persona,
            alias=args.alias,
            headless=args.headless,
            register_if_missing=args.register_if_missing,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
