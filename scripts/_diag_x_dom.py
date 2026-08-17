"""Probe X (Twitter) with P04 cookies — what does logged-in vs logged-out look like."""
import sys, asyncio
if hasattr(sys.stdout,"reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents._common.camoufox_session import launch_persona

async def main():
    async with launch_persona("P04", "twitter_x", headless=True,
                              use_storage_state=True) as (browser, context, page):
        # Try multiple URLs
        for url in ["https://x.com/", "https://twitter.com/", "https://x.com/home"]:
            print(f"\n=== {url} ===")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(7)
                title = await page.title()
                print(f"title={title!r}  url_after={page.url}")

                # specifically X selectors
                xtest = [
                    'a[data-testid="AppTabBar_Profile_Link"]',
                    'a[data-testid="SideNav_AccountSwitcher_Button"]',
                    'a[data-testid="SideNav_NewTweet_Button"]',
                    'div[data-testid="primaryColumn"]',
                    'a[aria-label="Profile"]',
                    'a[aria-label="Home"]',
                    'div[role="main"]',
                    'input[name="text"]',  # login email input
                    'input[autocomplete="username"]',  # login form
                    'a:has-text("Log in")',
                    'a[href="/login"]',
                    'div:has-text("Don’t miss what’s happening")',  # X login modal
                ]
                for s in xtest:
                    try:
                        c = await page.locator(s).count()
                        v = False
                        if c:
                            try: v = await page.locator(s).first.is_visible(timeout=300)
                            except: pass
                        if c > 0:
                            print(f"  [{c:>2}] vis={v}  {s}")
                    except Exception as e:
                        print(f"  ERR {s}: {e}")

                # body text snippet
                try:
                    bt = await page.locator("body").inner_text(timeout=2000)
                    print(f"  body[:300]: {bt[:300]!r}")
                except Exception as e:
                    print(f"  body inner_text fail: {e}")
            except Exception as e:
                print(f"goto failed: {e}")

asyncio.run(main())
