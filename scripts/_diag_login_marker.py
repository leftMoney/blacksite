"""Generic logged-in DOM probe — load persona's storage_state, navigate home,
dump cookies + nav text + a wide net of selector counts to identify working
logged-in marker for a (persona, platform) pair.
"""
import sys, asyncio
if hasattr(sys.stdout,"reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents._common.camoufox_session import launch_persona

PROBE_TARGETS = {
    "pantip":   "https://pantip.com/",
    "instagram":"https://www.instagram.com/",
    "tiktok":   "https://www.tiktok.com/foryou",
    "twitter_x":"https://x.com/home",
}

PROBE_SELECTORS = [
    'a[href*="profile"]', 'a[href*="account"]', 'a[href*="logout"]',
    'a[href*="settings"]', 'a[href*="dashboard"]',
    'img[class*="avatar" i]', 'img[alt*="avatar" i]',
    'button[aria-label*="account" i]', 'button[aria-label*="profile" i]',
    'button[aria-label*="user" i]', 'button[aria-label*="menu" i]',
    '[data-testid*="profile" i]', '[data-testid*="account" i]',
    '[data-testid*="user" i]',
    '[data-e2e*="profile" i]', '[data-e2e*="user" i]', '[data-e2e*="nav-profile" i]',
    'a[href*="/notification"]', 'a[href*="/inbox"]', 'a[href*="/messages"]',
    'a[href*="/direct/"]',
    'svg[aria-label="Home"]', 'svg[aria-label*="Home" i]',
    'a[role="link"][aria-label*="Profile" i]',
    'a[data-testid="AppTabBar_Profile_Link"]',
    'a[data-testid="SideNav_AccountSwitcher_Button"]',
    'a[href="/compose/post"]',
    'a:has-text("Log In"), button:has-text("Log In")',  # logged-OUT signal
    'a:has-text("Sign in"), button:has-text("Sign in")',
    'input[name="username"], input[type="email"]',  # login form = NOT logged in
]

async def probe(persona, platform):
    home = PROBE_TARGETS[platform]
    print(f"\n========================== {persona} / {platform} ==========================")
    async with launch_persona(persona, platform, headless=True,
                              use_storage_state=True) as (browser, context, page):
        await page.goto(home, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        title = await page.title()
        url = page.url
        print(f"title={title!r}  url={url}")
        cookies = await context.cookies()
        print(f"cookies({len(cookies)}): {sorted(c['name'] for c in cookies)[:10]}{'...' if len(cookies)>10 else ''}")
        for sel in PROBE_SELECTORS:
            try:
                count = await page.locator(sel).count()
                vis = False
                if count:
                    try:
                        vis = await page.locator(sel).first.is_visible(timeout=300)
                    except Exception: pass
                if count > 0:
                    print(f"  [{count:>3}] vis={vis}  {sel}")
            except Exception as e:
                pass

async def main():
    targets = [
        ("P04", "tiktok"),
        ("P04", "twitter_x"),
    ]
    for p, plat in targets:
        try:
            await probe(p, plat)
        except Exception as e:
            print(f"\n{p}/{plat} EXC: {type(e).__name__}: {e}")

asyncio.run(main())
