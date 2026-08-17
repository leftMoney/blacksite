"""Smoke tests for Blacksite LLM provider switch points.

Default mode is non-invasive: it checks routing, Codex login status, and prints
the exact switch-point tests. Add --live to consume subscription quota.

The live path adapts to BLACKSITE_LLM_PROVIDER:
  - codex / auto:  run via `codex exec` (consumes ChatGPT subscription)
  - claude:        run via call_fast/call_strategic (Haiku OAuth + Sonnet
                   claude.exe host path)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from processors import llm_profiles  # noqa: E402
from processors.llm_router import (  # noqa: E402
    codex_login_status,
    json_schema_file,
    run_codex,
    selected_provider,
)


STAGE2_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kb_admit": {"type": "boolean"},
        "kb_value_class": {"type": "string", "enum": ["high", "medium", "low", "noise"]},
        "kb_value_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "decision_tags": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["kb_admit", "kb_value_class", "kb_value_score", "decision_tags", "rationale"],
}

AUDIT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "your_verdict": {"type": "string", "enum": ["signal", "noise"]},
        "your_kb_admit": {"type": "boolean"},
        "your_kb_value_class": {"type": "string", "enum": ["high", "medium", "low", "noise"]},
        "your_kb_value_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "qwen_correct": {"type": "boolean"},
        "haiku_correct": {"type": ["boolean", "null"]},
        "failure_mode": {"type": "string"},
        "comment": {"type": "string"},
    },
    "required": [
        "your_verdict", "your_kb_admit", "your_kb_value_class",
        "your_kb_value_score", "qwen_correct", "haiku_correct",
        "failure_mode", "comment",
    ],
}


def _print_result(name: str, ok: bool, detail: str) -> None:
    status = "PASS" if ok else "SKIP" if "not logged in" in detail.lower() else "FAIL"
    print(f"[{status}] {name}: {detail}")


def live_codex(name: str, tier: str, prompt: str, schema: dict | None = None) -> bool:
    schema_path = json_schema_file(name, schema) if schema else None
    res = run_codex(prompt, tier=tier, output_schema=schema_path, timeout_s=180)
    if not res.ok:
        _print_result(name, False, res.error or "empty")
        return False
    if schema:
        try:
            json.loads(res.text)
        except Exception as e:
            _print_result(name, False, f"json parse failed: {e}; text={res.text[:160]!r}")
            return False
    _print_result(name, True, f"provider={res.provider} model={res.model} {res.duration_ms}ms")
    return True


def live_claude(name: str, tier: str, prompt: str, schema: dict | None = None) -> bool:
    """Drive the claude path. Tier→endpoint split is structural (not
    model-specific): `fast`/`stage2`/`audit` go through the OAuth Bearer API
    (which is hard-gated to Haiku); the rest go through claude.exe host OAuth.
    Model names are resolved from `config/llm_providers.yaml` — never hardcode.
    """
    from scripts.llm_call import call_fast, call_sonnet
    t0 = time.time()
    haiku_tiers = {"fast", "stage2", "audit"}
    schema_prompt = prompt
    if schema:
        schema_prompt = (
            prompt
            + "\n\nReturn ONLY one JSON object matching this schema (no prose, no fences):\n"
            + json.dumps(schema, ensure_ascii=False)
        )
    try:
        if tier in haiku_tiers:
            text = call_fast(schema_prompt, image_path=None,
                             max_tokens=1024, timeout_s=180)
            # Display model: the OAuth Bearer endpoint always routes to the
            # `claude.fast` (Haiku) profile regardless of which tier we
            # nominally requested.
            model_used = llm_profiles.tier_model("claude", "fast")
        else:
            text = call_sonnet(schema_prompt, image_path=None,
                               max_tokens=1024, timeout_s=240)
            # Resolve from the actual tier (strategic / bridge / coherence …)
            # so the smoke output reflects whatever YAML currently says.
            display_tier = tier if tier in llm_profiles.all_tiers("claude") else "strategic"
            model_used = llm_profiles.tier_model("claude", display_tier)
    except Exception as e:
        _print_result(name, False, f"{type(e).__name__}: {str(e)[:200]}")
        return False
    duration_ms = int((time.time() - t0) * 1000)
    if not text:
        _print_result(name, False, "empty reply")
        return False
    if schema:
        text_for_parse = text.strip()
        if text_for_parse.startswith("```"):
            text_for_parse = text_for_parse.strip("`")
            if text_for_parse.startswith("json"):
                text_for_parse = text_for_parse[4:]
        try:
            json.loads(text_for_parse)
        except Exception:
            start = text.find("{")
            end = text.rfind("}")
            try:
                json.loads(text[start:end + 1])
            except Exception as e:
                _print_result(name, False, f"json parse failed: {e}; text={text[:200]!r}")
                return False
    _print_result(name, True, f"provider=claude model={model_used} {duration_ms}ms "
                              f"reply_chars={len(text)}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="run tiny subscription calls to verify each tier")
    args = parser.parse_args()

    provider = selected_provider()
    print(f"BLACKSITE_LLM_PROVIDER={provider}")
    status = codex_login_status()
    _print_result("codex_login", status.ok, status.text or status.error or "")

    if provider == "claude":
        oauth = os.environ.get("ANTHROPIC_OAUTH_TOKEN", "")
        _print_result(
            "anthropic_oauth_token",
            bool(oauth),
            f"{'present' if oauth else 'missing'} (len={len(oauth)})",
        )

    checks = [
        ("stage2_structured", "fast",
         "Return a JSON object judging this synthetic OCR: 'examplebet bonus 100% instant-pay'.",
         STAGE2_SCHEMA),
        ("stage3_strategy", "strategic",
         "Return two labeled sections: COMMERCIAL_ACTION and CROSS_CASE_PATTERN for this signal: local operator bonus ad with instant-pay rail.",
         None),
        ("audit_judge", "audit",
         "Return audit JSON. Lower tiers called an operator bonus ad signal/admit; assume they are correct.",
         AUDIT_SCHEMA),
        ("bridge_short", "bridge",
         "Reply with exactly: OK",
         None),
    ]

    if not args.live:
        for name, tier, _prompt, schema in checks:
            schema_note = " schema" if schema else " text"
            _print_result(name, True, f"configured tier={tier}{schema_note}; add --live to execute")
        return 0

    # Live mode — pick driver per provider
    ok_all = True
    if provider == "codex":
        if not status.ok:
            print("Codex CLI is not logged in; live smoke tests skipped.")
            return 2
        for name, tier, prompt, schema in checks:
            ok_all = live_codex(name, tier, prompt, schema) and ok_all
    elif provider == "claude":
        for name, tier, prompt, schema in checks:
            ok_all = live_claude(name, tier, prompt, schema) and ok_all
    else:  # auto — try codex first then claude on failure
        for name, tier, prompt, schema in checks:
            res = False
            if status.ok:
                res = live_codex(name, tier, prompt, schema)
            if not res:
                res = live_claude(name, tier, prompt, schema)
            ok_all = res and ok_all

    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
