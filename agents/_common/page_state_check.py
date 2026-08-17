"""Page-state checks for browser-backed Field Agents.

Phase 2 starts with deterministic checks and evidence artifacts. Later phases
can add local vision / GPT on top of the same schema without changing callers.
"""

from __future__ import annotations

import re
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=7))

# TODO: set UI markers for your instance's language — add the target language's
# words for "login / sign up / account" (login_page) and "not found / none"
# (empty_feed) to the alternations below; the English markers stay as fallback.
BLOCKER_PATTERNS = {
    "captcha": re.compile(r"captcha|verify you are human|human verification|recaptcha|hcaptcha", re.I),
    "login_page": re.compile(r"log in|login|sign in|login \(localized\)|sign up \(localized\)|account \(localized\)", re.I),
    "rate_limited": re.compile(r"too many requests|try again later|temporarily blocked|floodwait|rate limit", re.I),
    "suspicious_login": re.compile(r"suspicious login|unusual activity|verify it.?s you|checkpoint", re.I),
    "empty_feed": re.compile(r"no posts|no results|nothing to show|not found \(localized\)|none \(localized\)", re.I),
}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _short(value: object, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def classify_page_state(
    *,
    url: str,
    title: str,
    body_text: str,
    logged_in: bool | None,
    matched_marker: str | None,
) -> tuple[str, float, list[str]]:
    haystack = "\n".join([url or "", title or "", body_text or ""])
    body_compact = " ".join((body_text or "").split())
    signals: list[str] = []

    for verdict, pattern in BLOCKER_PATTERNS.items():
        if pattern.search(haystack):
            signals.append(verdict)

    if logged_in is True and matched_marker:
        signals.append(f"matched_marker={matched_marker}")
        return "logged_in", 0.9, signals
    if "captcha" in signals:
        return "captcha", 0.9, signals
    if "suspicious_login" in signals:
        return "human_action_required", 0.85, signals
    if "rate_limited" in signals:
        return "rate_limited", 0.75, signals
    if "login_page" in signals:
        return "login_page", 0.7, signals
    if "empty_feed" in signals:
        return "empty_feed", 0.65, signals
    if logged_in is True and matched_marker:
        signals.append(f"matched_marker={matched_marker}")
        return "logged_in", 0.8, signals
    if len(body_compact) < 40:
        signals.append("empty_body")
        return "empty_feed", 0.6, signals
    if logged_in is True:
        signals.append(f"matched_marker={matched_marker}")
        return "logged_in", 0.8, signals
    if logged_in is False:
        return "wrong_page_or_logged_out", 0.55, signals
    return "unknown", 0.3, signals


async def capture_page_state(
    *,
    page,
    aid: str,
    persona: str,
    platform: str,
    stage: str,
    logged_in: bool | None = None,
    matched_marker: str | None = None,
    screenshot_path: Path | None = None,
) -> dict:
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        body_text = await page.locator("body").text_content(timeout=2_000) or ""
    except Exception:
        body_text = ""

    verdict, confidence, signals = classify_page_state(
        url=url,
        title=title,
        body_text=body_text,
        logged_in=logged_in,
        matched_marker=matched_marker,
    )
    return {
        "event": "page_state_check",
        "agent_id": aid,
        "persona": persona,
        "platform": platform,
        "stage": stage,
        "verdict": verdict,
        "confidence": confidence,
        "signals": signals,
        "logged_in": logged_in,
        "matched_marker": matched_marker,
        "url": url,
        "title": _short(title),
        "body_excerpt": _short(body_text, 500),
        "screenshot": screenshot_path.name if screenshot_path else None,
        "checked_at": now_iso(),
        "method": "deterministic_dom_text_v1",
    }


async def save_page_state_screenshot(page, screenshot_dir: Path, prefix: str) -> Path | None:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix).strip("_")[:80] or "page_state"
    path = screenshot_dir / f"{safe_prefix}_{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}.png"
    try:
        await page.screenshot(path=str(path), full_page=False)
        return path
    except Exception:
        return None


def write_page_state_jsonl(raw_dir: Path, record: dict) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    full = dict(record)
    full.setdefault("ts", full.get("checked_at") or now_iso())
    out_path = raw_dir / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(full, ensure_ascii=False) + "\n")
