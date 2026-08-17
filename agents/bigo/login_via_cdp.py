"""
Blacksite — capture Bigo session via CDP attach to a manually-launched Chrome.

Why this exists:
  - Playwright-launched Chrome trips Google's "automation browser" block on OAuth
  - Copying Chrome user_data_dir cross-path breaks Chrome 127+ App-Bound
    Encryption (v20 cookies)
  - Solution: boss runs real Chrome with --remote-debugging-port=9222, engine
    attaches via CDP and reads cookies through Playwright's API, which returns
    them already decrypted (Chrome does the work natively)

Workflow:
  1. boss closes all Chrome
  2. boss runs scripts\\launch_chrome_debug.bat  (opens Chrome on bigo.tv with
     Profile 3, debug port 9222)
  3. boss confirms Bigo is logged in (avatar visible top-right)
  4. py agents/bigo/login_via_cdp.py --persona P03
  5. engine dumps all bigo.tv cookies + localStorage origins → storage_state JSON
  6. boss closes Chrome — engine no longer needs it; future agents reuse state

Usage:
  py agents/bigo/login_via_cdp.py --persona P03
  py agents/bigo/login_via_cdp.py --persona P03 --cdp-url http://localhost:9222
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
TZ = timezone(timedelta(hours=7))


def log(msg: str) -> None:
    print(f"[{datetime.now(TZ).isoformat(timespec='seconds')}] [bigo_cdp] {msg}", flush=True)


async def run(persona_id: str, cdp_url: str) -> None:
    persona_dir = ROOT / "personas" / persona_id
    state_path = persona_dir / "state" / "bigo_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    log(f"connecting to CDP at {cdp_url}")
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url, timeout=10000)
        except Exception as e:
            log(f"CONNECT FAILED: {e}")
            log("  ensure Chrome is running with --remote-debugging-port=9222")
            log("  (run scripts\\launch_chrome_debug.bat first)")
            sys.exit(1)

        contexts = browser.contexts
        log(f"connected. contexts={len(contexts)}")
        if not contexts:
            log("no contexts available — Chrome must have at least one window open")
            sys.exit(2)

        ctx = contexts[0]
        pages = ctx.pages
        log(f"context[0] pages={len(pages)}")
        for i, p in enumerate(pages[:10]):
            log(f"  page[{i}]: {p.url}")

        bigo_page = None
        for p in pages:
            if "bigo.tv" in p.url:
                bigo_page = p
                break
        if bigo_page is None:
            log("no bigo.tv page found — opening one")
            bigo_page = await ctx.new_page()
            await bigo_page.goto("https://www.bigo.tv/th/", wait_until="domcontentloaded")
            await bigo_page.wait_for_timeout(3000)
        else:
            log(f"using existing bigo page: {bigo_page.url}")

        nickname = None
        try:
            nick_el = bigo_page.locator(".user-profile-userinfo__nickname").first
            if await nick_el.is_visible(timeout=3000):
                nickname = (await nick_el.text_content() or "").strip()
        except Exception:
            pass
        log(f"detected nickname: {nickname!r}")

        all_cookies = await ctx.cookies()
        bigo_cookies = [c for c in all_cookies if "bigo" in c["domain"]]
        log(f"total cookies: {len(all_cookies)}  bigo: {len(bigo_cookies)}")
        auth_keys = {"tid", "yyuid", "uniqid", "user_name", "deviceId", "nick_name"}
        present_auth = [c["name"] for c in bigo_cookies if c["name"] in auth_keys]
        log(f"auth cookies present: {sorted(present_auth)}")

        ls_origins = []
        for origin in ["https://www.bigo.tv"]:
            try:
                ls = await bigo_page.evaluate("""
                    () => Object.keys(localStorage).map(k => ({name: k, value: localStorage.getItem(k)}))
                """)
                ls_origins.append({"origin": origin, "localStorage": ls})
                log(f"localStorage[{origin}]: {len(ls)} keys")
            except Exception as e:
                log(f"localStorage read failed: {e}")

        state = {
            "cookies": [
                {
                    "name": c["name"], "value": c["value"], "domain": c["domain"],
                    "path": c["path"], "expires": c.get("expires", -1),
                    "httpOnly": c.get("httpOnly", False), "secure": c.get("secure", False),
                    "sameSite": c.get("sameSite", "Lax"),
                }
                for c in all_cookies
            ],
            "origins": ls_origins,
        }
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"saved storage_state → {state_path} ({state_path.stat().st_size} bytes)")

        if nickname and present_auth:
            log(f"SUCCESS — logged in as {nickname!r} with auth cookies {sorted(present_auth)}")
        elif present_auth:
            log(f"PARTIAL — auth cookies present but no nickname element (page may need reload)")
        else:
            log(f"FAILED — no auth cookies. Check Chrome is on bigo.tv and logged in.")

        await browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default="P03")
    ap.add_argument("--cdp-url", default="http://localhost:9222")
    args = ap.parse_args()
    asyncio.run(run(args.persona, args.cdp_url))


if __name__ == "__main__":
    main()
