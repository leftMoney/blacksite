"""
Vision-grounded state verification for L2 agent surfaces.

Per CLAUDE.md §1.2 constitutional rule (boss 2026-05-21): when DOM-based
reasoning may be lying about page state, screenshot the page and ask the
vision pipeline what's actually there. DOM presence of nav chrome / data-e2e
markers does NOT prove logged-in (markers render for guests too on many
platforms). Vision sees real ground truth.

Public API:

    verify_state(screenshot_path, platform, question_kind, custom_question=None,
                 model="sonnet") -> VisionVerdict

VisionVerdict fields:
    ok: bool             - vision call succeeded
    logged_in: bool|None - vision's read of login state (None if not asked)
    modal_present: bool|None
    modal_kind: str|None - "interest_selector" / "signup_nag" / "captcha" /
                           "geo_block" / "age_gate" / "cookie_banner" / None
    target_visible: bool|None  - for write actions, was the target element
                                  visible & unobstructed?
    geo_hint: str|None   - any geo string vision spotted ("Taiwan", "UK", etc.)
    notes: str           - vision's full plain-text reply
    raw_reply: str       - unprocessed claude.exe stdout

Cost: ~10-15s wall time per call, OAuth no API billing.

Use:
    from agents._common.vision_verify import verify_state
    v = verify_state(shot_path, "tiktok", "logged_in_and_modals")
    if not v.logged_in or v.modal_present:
        log("vision disagrees with DOM; aborting write action")
        return
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from processors._llm_synth import claude_run  # noqa: E402


# Canonical question templates per use case. Add new kinds as platforms surface
# distinct failure modes. Each prompt asks for a structured 4-line tail so
# the parser can recover signals without LLM hallucination.
QUESTION_TEMPLATES = {
    "logged_in_and_modals": (
        "Look at the screenshot at path: {image_path}\n\n"
        "This is a {platform} page viewed in a Camoufox headless browser as "
        "persona {persona}. The session was thought to be logged in via stored "
        "cookies but may have decayed. Diagnose ground truth.\n\n"
        "Describe in ≤120 words (numbered 1-4):\n"
        "1. Is the user logged in? Cite visible evidence (avatar in nav vs "
        "'Log in' / 'Sign up' prompts; persona display name visible anywhere; "
        "etc.).\n"
        "2. Modals / interstitials / banners blocking the page? Name them "
        "concretely (e.g. 'Interest Panel', 'Sign up to follow', 'Continue "
        "with...').\n"
        "3. Geo hints visible (footer 'account located in X', cookie banner "
        "language, currency, app store CTA)?\n"
        "4. Any captcha, robot check, age gate, phone-verify prompt, or "
        "selfie liveness check visible?\n\n"
        "Then on FOUR final lines, output structured tags (one per line, "
        "no other text, exact format):\n"
        "LOGGED_IN: yes|no|ambiguous\n"
        "MODAL: <one of: none|interest_selector|signup_nag|login_modal|"
        "captcha|geo_block|age_gate|cookie_banner|other>\n"
        "GEO: <country name or 'unknown'>\n"
        "HUMAN_GATE: yes|no\n"
    ),
    "write_action_clear": (
        "Look at the screenshot at path: {image_path}\n\n"
        "This is {platform} page viewed as persona {persona}. We are about to "
        "perform a WRITE action: {action_description}. Before clicking, "
        "verify the target is actually visible and unobstructed.\n\n"
        "Describe in ≤100 words (numbered 1-3):\n"
        "1. Is the target element visible? Describe its position and label.\n"
        "2. Is anything (modal / banner / overlay) covering or near the "
        "target?\n"
        "3. Is the session logged in (avatar present, no 'Log in' CTA "
        "dominating)?\n\n"
        "Then on THREE final lines, output structured tags (one per line, "
        "exact format):\n"
        "TARGET_VISIBLE: yes|no|partial\n"
        "OBSTRUCTION: <none|modal|banner|overlay|other>\n"
        "LOGGED_IN: yes|no|ambiguous\n"
    ),
    "custom": "{custom_question}",
}


@dataclass
class VisionVerdict:
    ok: bool = False
    logged_in: bool | None = None
    modal_present: bool | None = None
    modal_kind: str | None = None
    target_visible: bool | None = None
    obstruction: str | None = None
    geo_hint: str | None = None
    human_gate: bool | None = None
    notes: str = ""
    raw_reply: str = ""
    error: str | None = None


_TAG_PATTERNS = {
    "LOGGED_IN":      re.compile(r"^\s*LOGGED_IN\s*:\s*(\S+)", re.MULTILINE),
    "MODAL":          re.compile(r"^\s*MODAL\s*:\s*(\S+)", re.MULTILINE),
    "GEO":            re.compile(r"^\s*GEO\s*:\s*(.+?)\s*$", re.MULTILINE),
    "HUMAN_GATE":     re.compile(r"^\s*HUMAN_GATE\s*:\s*(\S+)", re.MULTILINE),
    "TARGET_VISIBLE": re.compile(r"^\s*TARGET_VISIBLE\s*:\s*(\S+)", re.MULTILINE),
    "OBSTRUCTION":    re.compile(r"^\s*OBSTRUCTION\s*:\s*(\S+)", re.MULTILINE),
}


def _parse_yes_no_ambig(s: str) -> bool | None:
    s = s.strip().lower().rstrip(".,;:")
    if s in {"yes", "y", "true"}:
        return True
    if s in {"no", "n", "false"}:
        return False
    return None  # ambiguous / partial / other


def verify_state(
    screenshot_path: Path | str,
    platform: str,
    question_kind: Literal["logged_in_and_modals", "write_action_clear", "custom"] = "logged_in_and_modals",
    *,
    persona: str = "P03",
    action_description: str | None = None,
    custom_question: str | None = None,
    model: str = "sonnet",
    timeout_s: float = 120.0,
) -> VisionVerdict:
    """Send the screenshot to claude.exe + parse structured tags."""
    shot = Path(screenshot_path)
    if not shot.exists():
        return VisionVerdict(ok=False, error=f"screenshot not found: {shot}")

    tmpl = QUESTION_TEMPLATES.get(question_kind)
    if tmpl is None:
        return VisionVerdict(ok=False, error=f"unknown question_kind={question_kind!r}")

    prompt = tmpl.format(
        image_path=str(shot).replace("\\", "/"),
        platform=platform,
        persona=persona,
        action_description=action_description or "(unspecified write action)",
        custom_question=custom_question or "",
    )

    try:
        ok, raw = claude_run(
            task=prompt,
            skill_prefix=False,
            allowed_tools="Read",
            permission_mode="default",
            timeout_s=timeout_s,
            max_retries=1,
            model=model,
            pass_model_flag=True,
        )
    except Exception as e:
        return VisionVerdict(ok=False, error=f"claude_run exception: {e}")

    if not ok or not raw:
        return VisionVerdict(ok=False, error="claude_run returned empty/error",
                             raw_reply=raw or "")

    v = VisionVerdict(ok=True, notes=raw.strip(), raw_reply=raw)

    # Tag parsing — defensive against models that paraphrase.
    li = _TAG_PATTERNS["LOGGED_IN"].search(raw)
    if li:
        v.logged_in = _parse_yes_no_ambig(li.group(1))

    md = _TAG_PATTERNS["MODAL"].search(raw)
    if md:
        kind = md.group(1).strip().lower().rstrip(".,;:")
        v.modal_kind = kind if kind != "none" else None
        v.modal_present = (kind != "none")

    geo = _TAG_PATTERNS["GEO"].search(raw)
    if geo:
        val = geo.group(1).strip().rstrip(".,;:")
        v.geo_hint = val if val.lower() not in {"unknown", "none", "-"} else None

    hg = _TAG_PATTERNS["HUMAN_GATE"].search(raw)
    if hg:
        v.human_gate = _parse_yes_no_ambig(hg.group(1))

    tv = _TAG_PATTERNS["TARGET_VISIBLE"].search(raw)
    if tv:
        v.target_visible = _parse_yes_no_ambig(tv.group(1))

    ob = _TAG_PATTERNS["OBSTRUCTION"].search(raw)
    if ob:
        kind = ob.group(1).strip().lower().rstrip(".,;:")
        v.obstruction = kind if kind != "none" else None

    return v


# CLI for boss debugging: `py agents/_common/vision_verify.py <path> tiktok`
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("screenshot")
    parser.add_argument("--platform", default="tiktok")
    parser.add_argument("--persona", default="P03")
    parser.add_argument("--kind", default="logged_in_and_modals",
                        choices=list(QUESTION_TEMPLATES.keys()))
    parser.add_argument("--action", default=None,
                        help="for kind=write_action_clear")
    args = parser.parse_args()
    v = verify_state(
        args.screenshot, args.platform, args.kind,
        persona=args.persona,
        action_description=args.action,
    )
    import json
    print(json.dumps({
        "ok": v.ok,
        "logged_in": v.logged_in,
        "modal_present": v.modal_present,
        "modal_kind": v.modal_kind,
        "target_visible": v.target_visible,
        "obstruction": v.obstruction,
        "geo_hint": v.geo_hint,
        "human_gate": v.human_gate,
        "error": v.error,
    }, ensure_ascii=False, indent=2))
    print()
    print("--- raw reply ---")
    print(v.notes)
