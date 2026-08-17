"""
Blacksite — TikTok KOL follow action (one-shot, daily-pace-capped).

Realises 策略長 5/21 directive H5: follow P0/P1 KOLs from
policy/persona_follow_targets/<persona>.yaml to inject explicit follow-graph
signal into TikTok's algo. Original 5/11 boss directive allows 'low-risk
saves/follows only'.

Pace cap: per persona_warmup_schedule.yaml `kol_follow_pace`:
  per_persona_per_day: 2
  per_persona_per_week: 10

OPSEC: this is the ONLY engagement action engine executes on TikTok. No
like / save / comment / DM / duet / stitch / share / upload — those remain
forbidden per personas/warmup/tiktok.md §3.

Usage:
  py agents/tiktok/kol_follow.py --persona P03 --handle example_handle \\
      --kol-name "ExampleAthlete" --priority-score 95
  py agents/tiktok/kol_follow.py --persona P03 --handle example_handle \\
      --dry-run   # navigate + verify follow button visible, don't click
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
RAW_DIR = RUNTIME / "raw"
LOG_DIR = RUNTIME / "logs"
SCREENSHOT_DIR = RUNTIME / "screenshots"
FOLLOW_LOG_PATH = RUNTIME / "kol_follow_log.jsonl"
for d in (LOG_DIR, SCREENSHOT_DIR):
    d.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

PERSONA_AGENT_ID = {
    "P03": "P03_TikTok",
    "P04": "P04_TikTok_sports",
}

# Daily / weekly pace cap (mirror of persona_warmup_schedule.yaml kol_follow_pace)
PACE_PER_DAY = 2
PACE_PER_WEEK = 10

# Selectors: TikTok follow button on profile page. data-e2e="follow-button"
# is the documented accessibility attribute. CRITICAL: profile page contains
# MULTIPLE follow-button elements (one for the target user + 4-5 for the
# "Suggested accounts" / "People you may know" row). Using .first picks
# whichever DOM-order is first which is often a suggestion. Always target
# via aria-label substring match on the target's display name. See
# resolve_follow_selector() below.
FOLLOW_SELECTOR_BASE = '[data-e2e="follow-button"]'

# Display-name fragments to expect in the aria-label for known handles.
# Maintained per-handle because TikTok aria-label is "Follow <nickname>"
# and nickname contains local script + parenthetical brand suffix.
# TODO: set search seeds for your instance (see instances/_TEMPLATE/policy/persona_follow_targets/<persona>.yaml)
HANDLE_TO_ARIA_FRAGMENT = {
    "example_handle": "ExampleAthlete",
    "example_handle_2": "ExampleKOL2",  # placeholder — resolve at runtime
    "example_handle_3": "ExampleKOL3",  # placeholder — resolve at runtime
}

# State markers — after click, button should change to "Following" / active.
# CRITICAL: do NOT match [data-e2e*="following"] generically — that catches
# the left-nav tab data-e2e="nav-following" and produces false positives.
# Markers must be scoped to the profile's follow button area.
# TODO: set UI markers for your instance's language — replace the
# "Following (localized)" has-text() entries with the target language's
# "Following" button label.
FOLLOWED_SELECTORS = [
    '[data-e2e="follow-icon-active"]',                          # explicit active state
    '[data-e2e="profile-follow-button"][aria-pressed="true"]',  # ARIA toggle on
    'button[data-e2e="follow-button"]:has-text("Following")',
    'button[data-e2e="follow-button"]:has-text("Following (localized)")', # local "Following"
    'button[data-e2e="profile-follow-button"]:has-text("Following")',
    'button[data-e2e="profile-follow-button"]:has-text("Following (localized)")',
]

LOGGED_IN_MARKERS = [
    '[data-e2e*="nav-profile" i]',
    '[data-e2e*="profile" i]',
    'img[class*="avatar" i]',
]


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [kol_follow] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"kol_follow_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def hist(actor: str, kind: str, title: str, body: str = "",
         scope: str = "persona", refs: list | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(actor=actor, kind=kind, scope=scope,
                         title=title, body=body, refs=refs)
    except Exception as e:
        log(f"history_log failed: {e}")
        return -1


def _load_follow_log() -> list[dict]:
    if not FOLLOW_LOG_PATH.exists():
        return []
    out = []
    for line in FOLLOW_LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _append_follow_log(record: dict) -> None:
    FOLLOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FOLLOW_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def check_pace_caps(persona: str) -> tuple[bool, str]:
    """Returns (allowed, reason). Allowed=False if cap hit."""
    now = datetime.now(TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=now.weekday())  # Monday start

    daily = 0
    weekly = 0
    for r in _load_follow_log():
        if r.get("persona") != persona:
            continue
        if not r.get("clicked"):
            continue
        try:
            ts = datetime.fromisoformat(r.get("ts", ""))
        except Exception:
            continue
        if ts >= day_start:
            daily += 1
        if ts >= week_start:
            weekly += 1

    if daily >= PACE_PER_DAY:
        return False, f"daily_cap ({daily}/{PACE_PER_DAY})"
    if weekly >= PACE_PER_WEEK:
        return False, f"weekly_cap ({weekly}/{PACE_PER_WEEK})"
    return True, f"daily={daily}/{PACE_PER_DAY} weekly={weekly}/{PACE_PER_WEEK}"


def emit_raw(agent_id: str, persona: str, event: dict) -> None:
    d = RAW_DIR / agent_id
    d.mkdir(parents=True, exist_ok=True)
    out_path = d / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"
    rec = {"ts": now_iso(), "persona": persona, "platform": "tiktok",
           "agent_id": agent_id, **event}
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def _try_visible(page, selectors: list[str], timeout_ms: int = 3_000) -> str | None:
    for sel in selectors:
        try:
            if await page.locator(sel).first.is_visible(timeout=timeout_ms):
                return sel
        except Exception:
            continue
    return None


async def _dismiss_overlays(page) -> int:
    """TikTok shows interstitial modals on profile page (interest survey,
    login nag, cookie banner, etc.) that intercept pointer events. Try
    in order: (1) click close buttons, (2) Escape key, (3) JS-force-remove
    overlay DOM nodes. Returns number of dismissal attempts that fired."""
    dismissed = 0
    # 1) Click close buttons in close-affordance order
    close_selectors = [
        'button[aria-label*="close" i]',
        '[data-e2e="modal-close-inner-button"]',
        '[data-e2e*="close" i][role="button"]',
        '[data-floating-ui-portal] button[aria-label*="close" i]',
        '.tux-modal button[aria-label*="close" i]',
        '[data-floating-ui-portal] svg[class*="close" i]',
    ]
    for sel in close_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1_500):
                await loc.click(timeout=2_500)
                dismissed += 1
                await page.wait_for_timeout(600)
        except Exception:
            continue

    # 2) Escape
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        await page.keyboard.press("Escape")
    except Exception:
        pass

    # 3) JS-force-remove pointer-event-intercepting overlays. Last resort
    # for TikTok's floating-ui-portal modals that don't expose a working
    # close button (interest panel, canary modal).
    try:
        removed = await page.evaluate("""() => {
            let n = 0;
            const selectors = [
                '[data-floating-ui-portal]',
                '[class*="DivInterestPanelContainer"]',
                '[class*="tux-modal__overlay"]',
                '[id*="modal-overlay"]',
            ];
            for (const sel of selectors) {
                for (const el of document.querySelectorAll(sel)) {
                    el.remove();
                    n++;
                }
            }
            return n;
        }""")
        if removed:
            dismissed += removed
    except Exception:
        pass

    return dismissed


async def execute_follow(
    persona: str,
    handle: str,
    kol_name: str | None,
    priority_score: int | None,
    dry_run: bool,
) -> int:
    agent_id = PERSONA_AGENT_ID.get(persona)
    if not agent_id:
        print(f"unknown persona {persona!r}", file=sys.stderr)
        return 2

    log(f"start persona={persona} handle={handle} kol={kol_name!r} dry_run={dry_run}")

    allowed, pace_msg = check_pace_caps(persona)
    if not allowed and not dry_run:
        log(f"pace cap hit: {pace_msg} — abort follow")
        hist(agent_id, kind="warning", scope="persona",
             title=f"{agent_id} kol_follow pace cap hit",
             body=f"persona={persona} handle={handle} reason={pace_msg}")
        return 3
    log(f"pace check: {pace_msg}")

    from agents._common.camoufox_session import launch_persona, storage_state_path

    state_path = storage_state_path(persona, "tiktok")
    if not state_path.exists():
        log(f"no storage_state at {state_path}; abort")
        return 2

    url = f"https://www.tiktok.com/@{handle}"

    async with launch_persona(
        persona, "tiktok", headless=True, use_storage_state=True,
    ) as (browser, context, page):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            log(f"goto failed: {e}")
            emit_raw(agent_id, persona, {
                "event": "kol_follow_abort", "reason": "goto_failed",
                "handle": handle, "detail": str(e),
            })
            return 4

        await page.wait_for_timeout(4_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        await page.wait_for_timeout(2_000)

        # Dismiss any interstitial modals (interest panel, login nag, cookie
        # banner) that block follow button clicks. Run twice — sometimes a
        # second modal pops after first is dismissed.
        for _ in range(2):
            n = await _dismiss_overlays(page)
            if n == 0:
                break
            log(f"dismissed {n} overlay(s)")
            await page.wait_for_timeout(800)

        # Logged-in confirmation (TikTok shows different UI for guests)
        marker = await _try_visible(page, LOGGED_IN_MARKERS, timeout_ms=4_000)
        if not marker:
            log("not logged in — abort")
            emit_raw(agent_id, persona, {
                "event": "kol_follow_abort", "reason": "not_logged_in",
                "handle": handle,
            })
            return 4

        # Already followed?
        already_followed = await _try_visible(page, FOLLOWED_SELECTORS, timeout_ms=2_000)
        if already_followed:
            log(f"already following @{handle} (marker={already_followed!r})")
            emit_raw(agent_id, persona, {
                "event": "kol_follow_already",
                "handle": handle, "kol_name": kol_name,
            })
            _append_follow_log({
                "ts": now_iso(), "persona": persona, "handle": handle,
                "kol_name": kol_name, "priority_score": priority_score,
                "clicked": False, "outcome": "already_following",
            })
            return 0

        # Locate the SPECIFIC follow button for the target handle by
        # aria-label. Profile page may have 4-6 follow buttons (target +
        # suggestions row) so .first picks the wrong one.
        aria_fragment = HANDLE_TO_ARIA_FRAGMENT.get(handle)
        follow_locator = None
        follow_sel = None
        if aria_fragment:
            sel = f'button[data-e2e="follow-button"][aria-label*="{aria_fragment}"]'
            try:
                if await page.locator(sel).first.is_visible(timeout=4_000):
                    follow_locator = page.locator(sel).first
                    follow_sel = sel
            except Exception:
                pass

        if follow_locator is None:
            # Fallback: pick the follow button whose aria-label literally
            # contains the @handle (TikTok sometimes uses handle in label).
            try:
                sel = f'button[data-e2e="follow-button"][aria-label*="{handle}"]'
                if await page.locator(sel).first.is_visible(timeout=2_000):
                    follow_locator = page.locator(sel).first
                    follow_sel = sel
            except Exception:
                pass

        if follow_locator is None:
            log(f"target follow button not found by aria-label fragment "
                f"{aria_fragment!r} or handle {handle!r} — likely DOM drift, "
                f"page redirect, or suggestions-only render")
            screenshot = SCREENSHOT_DIR / f"{agent_id}_kol_follow_nobtn_{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}.png"
            try:
                await page.screenshot(path=str(screenshot), full_page=False)
            except Exception:
                screenshot = None
            emit_raw(agent_id, persona, {
                "event": "kol_follow_abort", "reason": "follow_button_missing",
                "handle": handle, "kol_name": kol_name,
                "screenshot": screenshot.name if screenshot else None,
            })
            return 5

        if dry_run:
            log(f"DRY-RUN: would click {follow_sel!r} on @{handle}")
            emit_raw(agent_id, persona, {
                "event": "kol_follow_dry_run",
                "handle": handle, "kol_name": kol_name, "selector": follow_sel,
            })
            return 0

        log(f"clicking follow button (selector={follow_sel!r})")
        try:
            await follow_locator.click(timeout=6_000)
        except Exception as e:
            log(f"click failed: {e}")
            emit_raw(agent_id, persona, {
                "event": "kol_follow_abort", "reason": "click_failed",
                "handle": handle, "detail": str(e),
            })
            return 6

        # Verify state change: re-check the SAME button's aria-label /
        # text. After successful follow, TikTok flips the button to
        # "Following" + aria-label "Following <name>" or removes the
        # button entirely (replaced by Message/Following pair).
        await page.wait_for_timeout(2_500)

        post_marker = None
        try:
            new_text = await follow_locator.inner_text(timeout=2_000)
            new_aria = await follow_locator.get_attribute("aria-label", timeout=2_000)
        except Exception:
            new_text = ""
            new_aria = None

        # TODO: set UI markers for your instance's language — FOLLOWING_LOCALIZED
        # should be the target language's "Following" button label.
        FOLLOWING_LOCALIZED = "Following (localized)"
        if new_text and ("following" in new_text.lower()
                         or FOLLOWING_LOCALIZED in new_text):
            post_marker = f"button_text={new_text!r}"
        elif new_aria and ("following" in new_aria.lower()
                           or FOLLOWING_LOCALIZED in new_aria):
            post_marker = f"aria_label={new_aria!r}"
        else:
            # Fallback: scan all follow buttons matching the aria fragment
            # — TikTok may re-render the button with new ID
            post_marker = await _try_visible(page, FOLLOWED_SELECTORS, timeout_ms=2_500)

        if not post_marker:
            log(f"WARN: clicked but no 'Following' marker; "
                f"button text={new_text!r} aria={new_aria!r}")
            # Take post-click screenshot to diagnose. TikTok may have shown
            # a sign-up modal or silently dropped the request.
            try:
                shot = SCREENSHOT_DIR / (
                    f"{agent_id}_followfail_{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}.png"
                )
                await page.screenshot(path=str(shot), full_page=False)
                log(f"post-click screenshot: {shot.name}")
            except Exception:
                pass
            # Dump any visible modal / login prompt
            try:
                post_state = await page.evaluate(r"""() => {
                    const out = {};
                    out.title = document.title;
                    out.url = location.href;
                    out.signup_modal = !!document.querySelector(
                        '[data-e2e*="signup" i], [data-e2e*="login" i][data-e2e*="modal" i]'
                    );
                    // Visible buttons in post-click DOM
                    const visBtns = [];
                    for (const b of document.querySelectorAll('button[data-e2e]')) {
                        const r = b.getBoundingClientRect();
                        if (r.width > 20 && r.height > 20 && r.top < window.innerHeight) {
                            visBtns.push({
                                e2e: b.getAttribute('data-e2e'),
                                text: (b.innerText || '').trim().slice(0,40),
                            });
                            if (visBtns.length >= 10) break;
                        }
                    }
                    out.visible_buttons = visBtns;
                    return out;
                }""")
                log(f"post-click DOM: {json.dumps(post_state, ensure_ascii=False)[:400]}")
            except Exception:
                pass
        else:
            log(f"verified follow state via {post_marker}")

        # Persist storage_state (follow may set a new cookie)
        try:
            await context.storage_state(path=str(state_path))
        except Exception as e:
            log(f"storage_state save failed (non-fatal): {e}")

        emit_raw(agent_id, persona, {
            "event": "kol_follow",
            "handle": handle,
            "kol_name": kol_name,
            "priority_score": priority_score,
            "follow_button_selector": follow_sel,
            "verified_marker": post_marker,
        })
        _append_follow_log({
            "ts": now_iso(), "persona": persona, "handle": handle,
            "kol_name": kol_name, "priority_score": priority_score,
            "clicked": True, "verified": bool(post_marker),
            "outcome": "followed_verified" if post_marker else "followed_unverified",
        })

        hist(agent_id, kind="milestone", scope="persona",
             title=f"{agent_id} followed @{handle} ({kol_name or 'unnamed'})",
             body=f"persona={persona} handle={handle} kol={kol_name} "
                  f"priority={priority_score} verified={bool(post_marker)} "
                  f"pace_post={pace_msg}",
             refs=["instances/_TEMPLATE/policy/persona_follow_targets/" +
                   f"{persona}.yaml"])

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True, choices=["P03", "P04"])
    parser.add_argument("--handle", required=True,
                        help="TikTok @handle without leading @")
    parser.add_argument("--kol-name", default=None,
                        help="Human-readable KOL name for audit log")
    parser.add_argument("--priority-score", type=int, default=None,
                        help="From policy/persona_follow_targets/<persona>.yaml")
    parser.add_argument("--dry-run", action="store_true",
                        help="Navigate + verify follow button visible, don't click")
    args = parser.parse_args()
    return asyncio.run(execute_follow(
        persona=args.persona,
        handle=args.handle.lstrip("@"),
        kol_name=args.kol_name,
        priority_score=args.priority_score,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    sys.exit(main())
