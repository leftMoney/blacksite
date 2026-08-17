"""
Generic Field Agent warmup session entrypoint.

Modes:
  verify_only: load storage_state, navigate home, confirm logged-in marker,
      screenshot, emit raw JSONL. No engagement actions.
  active: verify_only plus future Phase A/B engagement scaffolding.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME / "logs"
RAW_DIR = RUNTIME / "raw"
RUNNING_DIR = RUNTIME / "agent_running"
SCREENSHOT_DIR = RUNTIME / "screenshots"
BRIEF_QUEUE = RUNTIME / "briefs" / "queue"
ALERT_STATE_PATH = RUNTIME / "session_recovery_alerts.json"

for path in (LOG_DIR, RUNNING_DIR, SCREENSHOT_DIR, BRIEF_QUEUE):
    path.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))
LOGIN_ALERT_THROTTLE_HOURS = int(os.environ.get("LOGIN_ALERT_THROTTLE_HOURS", "4"))

from agents._common.browser_viewport import mobile_viewport  # noqa: E402
from agents._common.page_state_check import capture_page_state  # noqa: E402


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(aid: str, msg: str) -> None:
    line = f"[{now_iso()}] [warmup_session] [{aid}] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"warmup_session_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def hist(
    actor: str,
    kind: str,
    title: str,
    body: str = "",
    scope: str = "persona",
    refs: list | None = None,
) -> int:
    try:
        from processors.history_log import log_event

        return log_event(
            actor=actor,
            kind=kind,
            scope=scope,
            title=title,
            body=body,
            refs=refs,
        )
    except Exception as e:
        log(actor, f"history_log failed: {e}")
        return -1


async def acquire_lock(aid: str) -> bool:
    lock = RUNNING_DIR / f"{aid}.lock"
    if lock.exists():
        try:
            content = lock.read_text(encoding="utf-8").strip()
            log(aid, f"lock exists: {content}; abort to avoid overlap")
            return False
        except Exception:
            return False
    lock.write_text(f"pid={os.getpid()} ts={now_iso()}", encoding="utf-8")
    return True


def release_lock(aid: str) -> None:
    lock = RUNNING_DIR / f"{aid}.lock"
    try:
        if lock.exists():
            lock.unlink()
    except Exception as e:
        log(aid, f"lock release failed: {e}")


def emit_raw_jsonl(aid: str, persona: str, platform: str, event: dict) -> None:
    out_dir = RAW_DIR / aid
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"
    full = {"ts": now_iso(), "persona": persona, "platform": platform, **event}
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(full, ensure_ascii=False) + "\n")


def _load_alert_state() -> dict[str, dict]:
    if not ALERT_STATE_PATH.exists():
        return {}
    try:
        return json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_alert_state(state: dict[str, dict]) -> None:
    ALERT_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def queue_manual_login_alert(
    *,
    aid: str,
    persona: str,
    platform: str,
    reason: str,
    action_hint: str,
    screenshot_name: str | None,
) -> bool:
    state = _load_alert_state()
    prev = state.get(aid, {})
    prev_ts = prev.get("sent_at")
    if prev_ts:
        try:
            prev_dt = datetime.fromisoformat(prev_ts)
            age_h = (datetime.now(TZ) - prev_dt).total_seconds() / 3600
            if age_h < LOGIN_ALERT_THROTTLE_HOURS:
                prev["last_suppressed_at"] = now_iso()
                prev["last_suppressed_reason"] = reason
                prev["last_suppressed_platform"] = platform
                state[aid] = prev
                _save_alert_state(state)
                return False
        except Exception:
            pass

    ts_slug = datetime.now(TZ).strftime("%Y-%m-%dT%H-%M-%S")
    out_path = BRIEF_QUEUE / f"pending_{ts_slug}_login_alert_{aid}.md"
    lines = [
        f"[LOGIN_ALERT] `{aid}` \u9700\u8981\u4f60\u4eba\u624b\u8655\u7406",
        "",
        f"\u7cfb\u7d71\u5df2\u5148\u81ea\u52d5\u5617\u8a66\u4e00\u6b21 `{platform}` session recovery\uff0c\u4f46\u9084\u662f\u5361\u5728 `{reason}`\u3002",
        f"\u4e0b\u4e00\u6b65\uff1a{action_hint}\u3002",
        "\u5b8c\u6210\u5f8c\u4e0d\u7528\u53e6\u5916\u6539\u898f\u5247\uff1b\u4e0b\u4e00\u8f2a warmup \u6703\u81ea\u52d5\u91cd\u5403\u65b0\u7684 storage_state\u3002",
    ]
    if screenshot_name:
        lines.append(
            f"\u8b49\u64da\uff1a`instances/{ACTIVE_INSTANCE}/runtime/screenshots/{screenshot_name}`"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    state[aid] = {
        "sent_at": now_iso(),
        "reason": reason,
        "persona": persona,
        "platform": platform,
    }
    _save_alert_state(state)
    hist(
        aid,
        "warning",
        f"{aid} queued boss login alert",
        body=f"platform={platform} reason={reason} action_hint={action_hint}",
        scope="persona",
        refs=[str(out_path.relative_to(ROOT)).replace("\\", "/")],
    )
    return True


async def _check_logged_in(page, logged_in_markers: list[str]) -> tuple[bool, str | None]:
    for marker in logged_in_markers:
        try:
            if await page.locator(marker).first.is_visible(timeout=4_000):
                return True, marker
        except Exception:
            continue
    return False, None


async def _check_logged_in_with_lazy_retry(
    page, aid: str, logged_in_markers: list[str]
) -> tuple[bool, str | None]:
    """First-pass marker probe + lazy-render retry. SPA platforms (TikTok mobile
    foryou) sometimes don't render nav chrome on first paint, especially under
    headless. Without retry the headless cron mis-flags logged-in sessions as
    logged-out and queues spurious manual-login alerts (boss 2026-05-18 P03_TikTok)."""
    ok, marker = await _check_logged_in(page, logged_in_markers)
    if ok:
        return ok, marker
    log(aid, "marker probe missed first pass; waiting for lazy render before retry")
    try:
        await page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass
    await page.wait_for_timeout(3_000)
    ok, marker = await _check_logged_in(page, logged_in_markers)
    if ok:
        log(aid, f"marker probe succeeded after lazy-render retry via {marker!r}")
    return ok, marker


async def _set_stable_viewport(page, aid: str, persona: str) -> None:
    viewport = mobile_viewport()
    try:
        await page.set_viewport_size(viewport)
        log(aid, f"stable viewport set: {viewport['width']}x{viewport['height']}")
    except Exception as e:
        log(aid, f"stable viewport set failed: {e}")


async def _save_session_screenshot(page, aid: str, label: str = "") -> Path | None:
    suffix = f"_{label}" if label else ""
    screenshot_path = SCREENSHOT_DIR / f"{aid}_{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}{suffix}.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=False)
        log(aid, f"screenshot saved: {screenshot_path.name}")
        return screenshot_path
    except Exception as e:
        log(aid, f"screenshot failed: {e}")
        return None


async def _hold_for_manual_login(
    *,
    aid: str,
    persona: str,
    platform: str,
    home_url: str,
    page,
    context,
    state_path: Path,
    logged_in_markers: list[str],
) -> int:
    timeout_min = int(os.environ.get("MANUAL_RELOGIN_TIMEOUT_MIN", "45"))
    deadline = datetime.now(TZ) + timedelta(minutes=timeout_min)
    log(aid, f"manual relogin handoff active; keeping browser open for {timeout_min} min")
    handoff_path = await _save_session_screenshot(page, aid, "handoff")
    emit_raw_jsonl(aid, persona, platform, {
        "event": "manual_relogin_handoff",
        "url": page.url,
        "timeout_min": timeout_min,
        "screenshot": handoff_path.name if handoff_path else None,
    })

    while datetime.now(TZ) < deadline:
        await page.wait_for_timeout(5_000)
        logged_in, matched_marker = await _check_logged_in(page, logged_in_markers)
        if not logged_in:
            continue

        try:
            await page.goto(home_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(4_000)
            logged_in2, matched_marker2 = await _check_logged_in(page, logged_in_markers)
            if logged_in2:
                matched_marker = matched_marker2 or matched_marker
        except Exception:
            pass

        try:
            await context.storage_state(path=str(state_path))
            log(aid, f"manual relogin storage_state saved -> {state_path.name}")
        except Exception as e:
            log(aid, f"manual relogin storage_state save failed: {e}")

        emit_raw_jsonl(aid, persona, platform, {
            "event": "manual_relogin_completed",
            "url": page.url,
            "logged_in": True,
            "matched_marker": matched_marker,
        })
        hist(
            aid,
            "milestone",
            f"{aid} manual relogin completed",
            body=f"platform={platform} marker={matched_marker} storage_state={state_path}",
            scope="persona",
            refs=[str(state_path.relative_to(ROOT)).replace("\\", "/")],
        )
        return 0

    log(aid, "manual relogin handoff timed out without logged-in marker")
    emit_raw_jsonl(aid, persona, platform, {
        "event": "manual_relogin_timeout",
        "url": page.url,
        "timeout_min": timeout_min,
    })
    return 3


async def run_warmup_session(
    *,
    platform: str,
    home_url: str,
    logged_in_markers: list[str],
    persona: str,
    mode: str = "verify_only",
    headless: bool = True,
    hold_on_human: bool = False,
) -> int:
    aid = f"{persona}_{_canonical_aid(platform)}"
    log(aid, f"start mode={mode} platform={platform} home={home_url}")

    if not await acquire_lock(aid):
        return 1

    try:
        from agents._common.camoufox_session import launch_persona, storage_state_path
        from agents._common.session_recovery import attempt_session_recovery
    except Exception as e:
        log(aid, f"import camoufox/session_recovery failed: {e}")
        release_lock(aid)
        return 2

    state_path = storage_state_path(persona, platform)
    if not state_path.exists():
        log(aid, f"no storage_state at {state_path}; abort")
        release_lock(aid)
        hist(
            aid,
            "warning",
            f"{aid} warmup abort no storage_state",
            body=f"persona={persona} platform={platform} expected={state_path}",
            scope="persona",
        )
        return 2

    try:
        async with launch_persona(
            persona,
            platform,
            headless=headless,
            use_storage_state=True,
        ) as (browser, context, page):
            await _set_stable_viewport(page, aid, persona)
            log(aid, f"camoufox launched, navigating to {home_url}")
            try:
                await page.goto(home_url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                log(aid, f"goto failed: {e}")
                emit_raw_jsonl(aid, persona, platform, {
                    "event": "navigation_fail",
                    "error": str(e),
                })
                return 4

            await asyncio.sleep(6)

            title = await page.title()
            url = page.url
            log(aid, f"loaded title={title!r} url={url}")

            logged_in, matched_marker = await _check_logged_in_with_lazy_retry(
                page, aid, logged_in_markers
            )

            screenshot_path = await _save_session_screenshot(page, aid)
            page_state = await capture_page_state(
                page=page,
                aid=aid,
                persona=persona,
                platform=platform,
                stage="post_login_check",
                logged_in=logged_in,
                matched_marker=matched_marker,
                screenshot_path=screenshot_path,
            )
            emit_raw_jsonl(aid, persona, platform, page_state)

            verify_event = {
                "event": "verify_session",
                "mode": mode,
                "title": title,
                "url": url,
                "logged_in": logged_in,
                "matched_marker": matched_marker,
                "screenshot": screenshot_path.name if screenshot_path else None,
            }
            emit_raw_jsonl(aid, persona, platform, verify_event)

            if not logged_in:
                log(aid, "NOT logged in; starting credential-based recovery attempt")
                recovery = await attempt_session_recovery(
                    page=page,
                    persona=persona,
                    platform=platform,
                    home_url=home_url,
                    logged_in_markers=logged_in_markers,
                )
                emit_raw_jsonl(aid, persona, platform, {
                    "event": "session_recovery",
                    "mode": mode,
                    **recovery,
                    "screenshot": screenshot_path.name if screenshot_path else None,
                })

                if recovery.get("recovery_success"):
                    matched_marker = recovery.get("matched_marker")
                    verify_event["logged_in"] = True
                    verify_event["matched_marker"] = matched_marker
                    verify_event["recovered_from_not_logged_in"] = True
                    verify_event["url"] = page.url
                    emit_raw_jsonl(aid, persona, platform, verify_event)
                    log(aid, f"recovery succeeded via marker {matched_marker!r}")
                    recovery_ok_screenshot = await _save_session_screenshot(page, aid, "post_recovery")
                    emit_raw_jsonl(
                        aid,
                        persona,
                        platform,
                        await capture_page_state(
                            page=page,
                            aid=aid,
                            persona=persona,
                            platform=platform,
                            stage="post_recovery_check",
                            logged_in=True,
                            matched_marker=matched_marker,
                            screenshot_path=recovery_ok_screenshot,
                        ),
                    )
                    hist(
                        aid,
                        "milestone",
                        f"{aid} session recovered",
                        body=f"platform={platform} mode={mode} marker={matched_marker}",
                        scope="persona",
                        refs=[str(screenshot_path.relative_to(ROOT))] if screenshot_path else None,
                    )
                    if mode == "verify_only":
                        log(aid, "verify_only mode complete after recovery")
                        return 0
                else:
                    reason = str(recovery.get("reason") or "manual_relogin_needed")
                    action_hint = str(recovery.get("action_hint") or "boss manual relogin required")
                    recovery_screenshot_path = await _save_session_screenshot(page, aid, "recovery")
                    emit_raw_jsonl(
                        aid,
                        persona,
                        platform,
                        await capture_page_state(
                            page=page,
                            aid=aid,
                            persona=persona,
                            platform=platform,
                            stage="human_gate",
                            logged_in=False,
                            matched_marker=None,
                            screenshot_path=recovery_screenshot_path,
                        ),
                    )
                    emit_raw_jsonl(aid, persona, platform, {
                        "event": "session_recovery_visual_check",
                        "mode": mode,
                        "reason": reason,
                        "url": page.url,
                        "screenshot": recovery_screenshot_path.name if recovery_screenshot_path else None,
                    })
                    log(aid, f"recovery failed; human action required reason={reason}")
                    hist(
                        aid,
                        "warning",
                        f"{aid} verify FAIL -> human action required",
                        body=(
                            f"title={title} url={url} markers_tried={logged_in_markers}\n"
                            f"recovery={json.dumps(recovery, ensure_ascii=False)}"
                        ),
                        scope="persona",
                        refs=[
                            str(p.relative_to(ROOT)).replace("\\", "/")
                            for p in (screenshot_path, recovery_screenshot_path)
                            if p
                        ],
                    )
                    if recovery.get("human_action_required"):
                        queue_manual_login_alert(
                            aid=aid,
                            persona=persona,
                            platform=platform,
                            reason=reason,
                            action_hint=action_hint,
                            screenshot_name=(
                                recovery_screenshot_path.name
                                if recovery_screenshot_path
                                else (screenshot_path.name if screenshot_path else None)
                            ),
                        )
                        if hold_on_human and not headless:
                            return await _hold_for_manual_login(
                                aid=aid,
                                persona=persona,
                                platform=platform,
                                home_url=home_url,
                                page=page,
                                context=context,
                                state_path=state_path,
                                logged_in_markers=logged_in_markers,
                            )
                    return 3

            log(aid, f"verified logged in via marker {matched_marker!r}")
            hist(
                aid,
                "milestone",
                f"{aid} warmup verify logged in",
                body=(
                    f"mode={mode} url={page.url} marker={matched_marker} "
                    f"screenshot={screenshot_path.name if screenshot_path else 'none'}"
                ),
                scope="persona",
            )

            if mode == "verify_only":
                emit_raw_jsonl(
                    aid,
                    persona,
                    platform,
                    await capture_page_state(
                        page=page,
                        aid=aid,
                        persona=persona,
                        platform=platform,
                        stage="task_end",
                        logged_in=True,
                        matched_marker=matched_marker,
                        screenshot_path=screenshot_path,
                    ),
                )
                log(aid, "verify_only mode complete; no engagement actions")
                return 0

            log(aid, "active mode requested; scaffold only")
            log(aid, "PHASE_A_TODO: scroll feed 30 items + like 5-7 vertical-relevant + save 1-2")
            log(aid, "PHASE_B_TODO: visit one followed KOL profile + search 1 keyword")
            emit_raw_jsonl(aid, persona, platform, {
                "event": "active_mode_scaffold_only",
                "phase_a_implemented": False,
                "phase_b_implemented": False,
            })
            emit_raw_jsonl(
                aid,
                persona,
                platform,
                await capture_page_state(
                    page=page,
                    aid=aid,
                    persona=persona,
                    platform=platform,
                    stage="task_end",
                    logged_in=True,
                    matched_marker=matched_marker,
                    screenshot_path=screenshot_path,
                ),
            )
            return 0
    except Exception as e:
        log(aid, f"unexpected error: {type(e).__name__}: {e}")
        hist(
            aid,
            "warning",
            f"{aid} warmup unexpected error",
            body=f"{type(e).__name__}: {e}",
            scope="persona",
        )
        return 4
    finally:
        release_lock(aid)


def _canonical_aid(platform: str) -> str:
    mapping = {
        "facebook": "FB",
        "instagram": "IG",
        "tiktok": "TikTok",
        "pantip": "Pantip",
        "discord": "Discord",
        "reddit": "Reddit",
        "twitter_x": "X",
        "youtube": "YouTube_sports",
    }
    return mapping.get(platform, platform.title())


def parse_args_and_run(*, platform: str, home_url: str, logged_in_markers: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True, choices=["P03", "P04", "P05"])
    parser.add_argument("--mode", default="verify_only", choices=["verify_only", "active"])
    parser.add_argument("--no-headless", action="store_true", help="Visible browser for debugging")
    parser.add_argument("--hold-on-human", action="store_true", help="Keep visible browser open after a human login gate")
    args = parser.parse_args()
    return asyncio.run(
        run_warmup_session(
            platform=platform,
            home_url=home_url,
            logged_in_markers=logged_in_markers,
            persona=args.persona,
            mode=args.mode,
            headless=not args.no_headless,
            hold_on_human=args.hold_on_human,
        )
    )
