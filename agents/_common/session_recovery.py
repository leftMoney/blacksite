from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

if sys.platform == "win32":
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


async def attempt_session_recovery(
    *,
    page,
    persona: str,
    platform: str,
    home_url: str,
    logged_in_markers: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "attempted": False,
        "recovery_success": False,
        "human_action_required": False,
        "reason": "",
        "detail": "",
        "action_hint": "",
        "matched_marker": None,
        "platform": platform,
    }

    cfg = _platform_creds(persona, platform)
    if not cfg:
        result.update({
            "human_action_required": True,
            "reason": "no_recovery_plan",
            "detail": f"platform={platform}",
            "action_hint": _action_hint(platform),
        })
        return result

    result["attempted"] = True

    if platform == "facebook":
        await _recover_facebook(page, cfg)
    elif platform == "instagram":
        await _recover_instagram(page, cfg)
    elif platform == "twitter_x":
        await _recover_twitter_x(page, cfg)
    elif platform == "pantip":
        await _recover_pantip(page, cfg)
    else:
        result.update({
            "human_action_required": True,
            "reason": "unsupported_platform",
            "detail": f"platform={platform}",
            "action_hint": _action_hint(platform),
        })
        return result

    ok, marker = await _logged_in_marker(page, logged_in_markers)
    if ok:
        result.update({
            "recovery_success": True,
            "matched_marker": marker,
            "reason": "recovered",
            "detail": f"url={page.url}",
        })
        try:
            await page.goto(home_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(4_000)
            ok2, marker2 = await _logged_in_marker(page, logged_in_markers)
            if ok2:
                result["matched_marker"] = marker2 or marker
        except Exception:
            pass
        return result

    gate = await _detect_human_gate(page, platform)
    if gate:
        result.update({
            "human_action_required": True,
            "reason": gate["reason"],
            "detail": gate["detail"],
            "action_hint": _action_hint(platform),
        })
        return result

    result.update({
        "human_action_required": True,
        "reason": "manual_relogin_needed",
        "detail": f"url={page.url}",
        "action_hint": _action_hint(platform),
    })
    return result


def _platform_creds(persona: str, platform: str) -> dict[str, str] | None:
    gmail = os.environ.get(f"PERSONA_{persona}_GMAIL", "").strip()
    gmail_pwd = os.environ.get(f"PERSONA_{persona}_GMAIL_PWD", "").strip()
    fb_pwd = os.environ.get(f"PERSONA_{persona}_FB_PWD", "").strip() or gmail_pwd
    x_pwd = os.environ.get(f"PERSONA_{persona}_X_PWD", "").strip()
    x_username = os.environ.get(f"PERSONA_{persona}_X_USERNAME", "").strip().lstrip("@")
    pantip_pwd = os.environ.get(f"PERSONA_{persona}_PANTIP_PWD", "").strip()

    if platform == "facebook":
        return {"email": gmail, "password": fb_pwd} if gmail and fb_pwd else None
    if platform == "instagram":
        return {"email": gmail, "password": fb_pwd} if gmail and fb_pwd else None
    if platform == "twitter_x":
        password = x_pwd or gmail_pwd
        if gmail and password:
            return {
                "email": gmail,
                "password": password,
                "username": x_username or gmail,
            }
        return None
    if platform == "pantip":
        return {"email": gmail, "password": pantip_pwd} if gmail and pantip_pwd else None
    return None


def _action_hint(platform: str) -> str:
    hints = {
        "facebook": "boss manual Meta relogin/checkpoint required",
        "instagram": "boss manual Meta relogin/checkpoint required",
        "twitter_x": "boss manual X relogin/verify required",
        "pantip": "boss manual Pantip relogin required",
    }
    return hints.get(platform, "boss manual relogin required")


async def _logged_in_marker(page, markers: list[str]) -> tuple[bool, str | None]:
    for marker in markers:
        try:
            if await page.locator(marker).first.is_visible(timeout=2_000):
                return True, marker
        except Exception:
            continue
    return False, None


async def _fill_first(page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() < 1:
                continue
            await loc.fill(value, timeout=3_000)
            return True
        except Exception:
            continue
    return False


async def _click_first(page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() < 1:
                continue
            await loc.click(timeout=3_000)
            return True
        except Exception:
            continue
    return False


async def _visible(page, selectors: list[str], timeout_ms: int = 1_500) -> bool:
    for sel in selectors:
        try:
            if await page.locator(sel).first.is_visible(timeout=timeout_ms):
                return True
        except Exception:
            continue
    return False


async def _visible_box(page, selectors: list[str], timeout_ms: int = 1_500) -> dict[str, Any] | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not await loc.is_visible(timeout=timeout_ms):
                continue
            box = await loc.bounding_box(timeout=timeout_ms)
            if not box:
                continue
            if box.get("width", 0) < 80 or box.get("height", 0) < 40:
                continue
            return {"selector": sel, "box": box}
        except Exception:
            continue
    return None


async def _body_text(page) -> str:
    try:
        text = await page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return ""
    return " ".join(text.lower().split())


async def _detect_human_gate(page, platform: str) -> dict[str, str] | None:
    url = (page.url or "").lower()
    body = await _body_text(page)

    captcha_box = await _visible_box(page, [
        'iframe[title*="captcha" i]',
        'iframe[src*="captcha" i]',
        'iframe[src*="hcaptcha" i]',
        'iframe[src*="recaptcha" i]',
    ])
    captcha_context = [
        "captcha",
        "not a robot",
        "robot",
        "security check",
        "verify",
        "human",
        "cloudflare",
    ]
    if captcha_box and any(token in body for token in captcha_context):
        return {
            "reason": "captcha_gate",
            "detail": f"url={url} selector={captcha_box['selector']} box={captcha_box['box']}",
        }

    if platform == "twitter_x" and (
        "suspicious login prevented" in body
        or "you'll need to wait before trying to log in again" in body
        or "we blocked an attempt to access your account" in body
    ):
        return {"reason": "x_suspicious_login_cooldown", "detail": f"url={url}"}

    common_checks = [
        "verify it's you",
        "verify your identity",
        "confirm your identity",
        "security check",
        "unusual login",
        "suspicious login",
        "review required",
        "phone number",
        "code sent",
        "enter the code",
    ]
    if any(token in body for token in common_checks):
        return {"reason": "verification_gate", "detail": f"url={url}"}

    if platform in {"facebook", "instagram"} and ("/checkpoint/" in url or "/challenge/" in url):
        return {"reason": "meta_checkpoint", "detail": f"url={url}"}
    if platform == "twitter_x" and "/i/flow/login" in url and ("confirm" in body or "verify" in body):
        return {"reason": "x_verify_gate", "detail": f"url={url}"}
    return None


async def _recover_facebook(page, creds: dict[str, str]) -> None:
    await page.goto("https://www.facebook.com/login.php", wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(3_500)
    await _fill_first(page, ['input[name="email"]', 'input[type="text"]'], creds["email"])
    await _fill_first(page, ['input[name="pass"]', 'input[type="password"]'], creds["password"])
    await _click_first(page, ['button[name="login"]', 'button:has-text("Log in")', 'input[name="login"]'])
    await page.wait_for_timeout(7_000)


async def _recover_instagram(page, creds: dict[str, str]) -> None:
    await page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(4_000)
    await _click_first(page, [
        'button:has-text("Only allow essential cookies")',
        'button:has-text("Allow all cookies")',
    ])
    if await _click_first(page, [
        'button:has-text("Continue")',
        'div[role="button"]:has-text("Continue")',
    ]):
        await page.wait_for_timeout(3_500)
        if await _visible(page, ['input[name="password"]', 'input[type="password"]'], timeout_ms=5_000):
            await _fill_first(page, ['input[name="password"]', 'input[type="password"]'], creds["password"])
            await _click_first(page, [
                'button[type="submit"]',
                'button:has-text("Log in")',
                'div[role="button"]:has-text("Log in")',
            ])
            await page.wait_for_timeout(7_000)
            return
    if await _click_first(page, [
        'button:has-text("Log in with Facebook")',
        'div[role="button"]:has-text("Log in with Facebook")',
    ]):
        await page.wait_for_timeout(3_500)
        if await _visible(page, ['input[name="email"]', 'input[name="pass"]'], timeout_ms=5_000):
            await _fill_first(page, ['input[name="email"]', 'input[type="text"]'], creds["email"])
            await _fill_first(page, ['input[name="pass"]', 'input[type="password"]'], creds["password"])
            await _click_first(page, ['button[name="login"]', 'button:has-text("Log in")', 'input[name="login"]'])
            await page.wait_for_timeout(7_000)
            return

    await _fill_first(page, ['input[name="username"]', 'input[aria-label="Phone number, username, or email"]'], creds["email"])
    await _fill_first(page, ['input[name="password"]', 'input[type="password"]'], creds["password"])
    await _click_first(page, ['button[type="submit"]', 'div[role="button"]:has-text("Log in")', 'button:has-text("Log in")'])
    await page.wait_for_timeout(7_000)


async def _recover_twitter_x(page, creds: dict[str, str]) -> None:
    await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(4_000)
    await _fill_first(page, ['input[autocomplete="username"]', 'input[name="text"]'], creds["email"])
    await _click_first(page, ['div[role="button"]:has-text("Next")', 'button:has-text("Next")'])
    await page.wait_for_timeout(3_000)

    if not await _visible(page, ['input[name="password"]', 'input[type="password"]'], timeout_ms=2_000):
        await _fill_first(page, ['input[name="text"]', 'input[autocomplete="username"]'], creds["username"])
        await _click_first(page, ['div[role="button"]:has-text("Next")', 'button:has-text("Next")'])
        await page.wait_for_timeout(3_000)

    await _fill_first(page, ['input[name="password"]', 'input[type="password"]'], creds["password"])
    await _click_first(page, [
        'div[data-testid="LoginForm_Login_Button"]',
        'div[role="button"]:has-text("Log in")',
        'button:has-text("Log in")',
    ])
    await page.wait_for_timeout(7_000)


async def _recover_pantip(page, creds: dict[str, str]) -> None:
    await page.goto("https://pantip.com/login", wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(4_000)
    await _click_first(page, ['button:has-text("Accept")'])
    # TODO: set UI markers for your instance's language — the first two
    # placeholders should be the target language's "username" / "email" labels.
    filled_email = await _fill_first(page, [
        'input[placeholder*="username (localized)"]',
        'input[placeholder*="email (localized)"]',
        'input[placeholder*="email" i]',
        'input[placeholder*="username" i]',
        'input[name*="email" i]',
        'input[name*="user" i]',
        'input[type="email"]',
    ], creds["email"])
    if not filled_email:
        await page.evaluate(
            """({ email }) => {
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
                    return Math.abs(box.left - passBox.left) < 160
                        && box.top < passBox.top
                        && box.top > passBox.top - 120;
                });
                const target = candidates[0];
                if (!target) return false;
                target.focus();
                target.value = email;
                target.dispatchEvent(new Event("input", { bubbles: true }));
                target.dispatchEvent(new Event("change", { bubbles: true }));
                return true;
            }""",
            {"email": creds["email"]},
        )
    await _fill_first(page, ['input[type="password"]'], creds["password"])
    # TODO: set UI markers for your instance's language — "Login (localized)"
    # should be the target language's "Login" button label.
    await _click_first(page, [
        'button:has-text("Login")',
        'button:has-text("Log in")',
        'button:has-text("Login (localized)")',
        'input[type="submit"]',
    ])
    await page.wait_for_timeout(7_000)
