"""Improvement — auto-proposal generator triggered by audit alerts
(CLAUDE.md §2.1 audit-loop).

Decision chain (boss directive 2026-05-17):
  warning  → section_chief auto-applies fix via section_chief_eval consult;
             logs config_change; brief queue [小主管 FYI] digest (boss informed, no action needed).
  critical → 策略長 auto-applies fix via chief_strategist.py --consult;
             logs config_change; brief queue [策略長 決策] DM boss (FYI, no approval needed).
             Escalates to boss ONLY via escalated_boss state if 策略長 exhausted.

Boss is always notified of 策略長 decisions (FYI). Boss does NOT need to approve.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
PROPOSAL_DIR = RUNTIME_DIR / "improvement_proposals"
QUEUE_DIR = RUNTIME_DIR / "briefs" / "queue"
ALERT_DIR = RUNTIME_DIR / "briefs" / "alerts"
PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_DIR.mkdir(parents=True, exist_ok=True)
ALERT_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# Failure-mode → suggested fix mapping (advisory; boss reviews)
# ----------------------------------------------------------------------

FAILURE_FIX_HINTS = {
    "qwen_under_filter": (
        "Qwen calling true signals as noise. Likely cause: Stage 1 prompt too "
        "narrow on signal vocabulary, or aggressive bias toward noise. "
        "**Suggested fix**: broaden signal-leaning tag list in PROMPT_V1 of "
        "stage1_qwen_filter.py; add domain-specific terms (e.g. the target "
        "country's lottery / folk-belief keywords, operator brand names found "
        "in failed samples). Or swap to qwen2.5vl:32b for harder cases."
    ),
    "qwen_over_admit": (
        "Qwen passing too much through to Haiku (waste). Likely cause: Stage 1 "
        "prompt too generous on 'signal' verdict. **Suggested fix**: tighten "
        "Stage 1 prompt language to require concrete operator/lottery/payment "
        "evidence; raise confidence threshold for signal verdict."
    ),
    "haiku_over_admit": (
        "Haiku admitting content of no value to the client brand. **Suggested fix**: tighten "
        "ADMISSION HEURISTICS in stage2 PROMPT_V1; explicitly add reject "
        "categories that match the failure samples; raise kb_value_score "
        "threshold for admit (currently 40)."
    ),
    "haiku_under_admit": (
        "Haiku rejecting valuable signal. **Suggested fix**: review failure "
        "samples in proposal; broaden ADMIT examples in stage2 prompt to "
        "include the missed pattern; consider lowering kb_value_score "
        "admission threshold."
    ),
    "haiku_score_too_high": (
        "Score inflation — Stage 3 is over-loaded with low-value cases. "
        "**Suggested fix**: recalibrate stage2 'HIGH value (>=70) reserved for' "
        "guidance; require explicit cross-case-pattern signal for high score."
    ),
    "haiku_score_too_low": (
        "Score deflation — Stage 3 starves of high-value cases. "
        "**Suggested fix**: ease the high-value criterion; add domain examples "
        "of high-score signal to stage2 prompt."
    ),
    "tag_mismatch": (
        "Tags often wrong while admit/score are OK. **Suggested fix**: clarify "
        "tag vocabulary definitions in stage2 prompt; consider per-tag "
        "few-shot examples."
    ),
    "prompt_ambiguity": (
        "Both tiers reasonably disagreed; prompts under-specified. "
        "**Suggested fix**: review failure samples; add concrete examples for "
        "the ambiguous case to the relevant stage prompt."
    ),
}


def render_proposal(audit_row_id: int, alert_level: str, agg: dict,
                    samples: list) -> str:
    qwen_acc = agg.get("qwen_acc")
    haiku_acc = agg.get("haiku_acc")
    failure_modes = agg.get("failure_modes") or {}

    fm_lines = []
    for fm, count in sorted(failure_modes.items(), key=lambda x: -x[1]):
        hint = FAILURE_FIX_HINTS.get(fm, "(no canned fix — manual review)")
        fm_lines.append(f"### {fm} (×{count})\n\n{hint}\n")
    fm_block = "\n".join(fm_lines) if fm_lines else "_(no specific failure modes catalogued)_"

    sample_summary = (f"sample_size={len(samples)} buckets="
                      f"{ {s['bucket']: 1 for s in samples} }")

    route = ("section_chief auto-apply" if alert_level == "warning"
             else "策略長 auto-apply (boss directive 2026-05-17)")

    body = f"""# Pipeline improvement proposal — {alert_level.upper()}

**audit_row_id**: {audit_row_id}
**generated_at**: {now_iso()}
**alert_level**: {alert_level}
**routed_to**: {route}
**qwen_acc**: {qwen_acc if qwen_acc is not None else 'n/a'} \
(floor warning {os.environ.get('AUDIT_QWEN_WARN', '0.75')} / critical {os.environ.get('AUDIT_QWEN_CRIT', '0.50')})
**haiku_acc**: {haiku_acc if haiku_acc is not None else 'n/a'} \
(floor warning {os.environ.get('AUDIT_HAIKU_WARN', '0.85')} / critical {os.environ.get('AUDIT_HAIKU_CRIT', '0.65')})
**sample**: {sample_summary}

## Failure modes detected

{fm_block}

## Resolution path

- **warning**: section_chief evaluates and auto-applies fix(es); logs `config_change`.
  Next audit measures effect; after 3 days improved accuracy → `fix_validated`.
- **critical**: 策略長 evaluates and auto-applies fix(es); logs `config_change`.
  Boss is notified via `[策略長 決策]` FYI (no approval needed). Escalates to boss ONLY via `escalated_boss` if exhausted.

---

_Drafted by `processors/pipeline/improvement.py` (audit-loop)._
"""
    return body


def _route_to_chain(audit_row_id: int, alert_level: str,
                    proposal_path: Path, qwen_acc, haiku_acc) -> None:
    """Route pipeline audit alert through 小主管 → 策略長 chain. Never boss."""
    # Channel 1 — system_history (always)
    try:
        from processors.history_log import log_event
        kind = "critical" if alert_level == "critical" else "warning"
        log_event(
            actor="cron_audit_sonnet",
            kind=kind,
            scope="ocr_pipeline",
            title=f"Pipeline audit {alert_level} (audit #{audit_row_id}) → routed to chain",
            body=f"qwen_acc={qwen_acc} haiku_acc={haiku_acc}\n"
                 f"Proposal: {proposal_path.relative_to(ROOT)}\n"
                 f"warning→section_chief auto-apply; critical→策略長 brief",
            refs=[str(proposal_path.relative_to(ROOT))],
        )
    except Exception as e:
        print(f"[improvement] history_log fail: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)

    ts = datetime.now(TZ).strftime("%Y-%m-%dT%H-%M-%S")

    if alert_level == "warning":
        # warning → section_chief auto-applies; boss gets lightweight FYI
        _section_chief_auto_apply(audit_row_id, proposal_path, qwen_acc, haiku_acc)
        queue_path = QUEUE_DIR / f"pending_{ts}_pipeline_sc_fyi_{audit_row_id}.md"
        queue_body = (
            f"[小主管 FYI] 圖像辨識品質 警告（audit #{audit_row_id}）\n\n"
            f"• Qwen 準確率={qwen_acc}，Haiku 準確率={haiku_acc} → 低於門檻，情報收錄品質受影響\n"
            f"• 小主管已選定 fix 並套用（Prompt 調整）→ 下次 audit 追蹤效果\n\n"
            f"不需行動。"
        )
    else:
        # critical → 策略長 auto-applies (boss directive 2026-05-17: 策略長 自己挑)
        _strategist_auto_apply(audit_row_id, proposal_path, qwen_acc, haiku_acc)
        queue_path = QUEUE_DIR / f"pending_{ts}_pipeline_strategist_fyi_{audit_row_id}.md"
        queue_body = (
            f"[策略長 決策] 圖像辨識品質 嚴重（audit #{audit_row_id}）\n\n"
            f"• Qwen 準確率={qwen_acc}，Haiku 準確率={haiku_acc} → 情況嚴重，KB 情報品質大幅受損\n"
            f"• 策略長已選定 fix 並套用 → 詳見 `{proposal_path.relative_to(ROOT)}`\n\n"
            f"下次 audit 追蹤效果，3 天後若改善則標記 fix_validated。"
        )

    try:
        queue_path.write_text(queue_body, encoding="utf-8")
    except Exception as e:
        print(f"[improvement] queue write fail: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


def _section_chief_auto_apply(audit_row_id: int, proposal_path: Path,
                               qwen_acc, haiku_acc) -> None:
    """Spawn section_chief_eval consult to review and apply the warning fix."""
    import subprocess
    consult_q = (
        f"Pipeline audit #{audit_row_id} returned warning: "
        f"qwen_acc={qwen_acc} haiku_acc={haiku_acc}. "
        f"Proposal at {proposal_path}. "
        f"Review failure modes, select appropriate prompt fix(es), apply them "
        f"(edit stage1/stage2 prompt files), and log a config_change event."
    )
    try:
        result = subprocess.run(
            ["python", str(ROOT / "processors" / "section_chief_eval.py"),
             "--consult", consult_q],
            capture_output=True, text=True, timeout=120,
            cwd=str(ROOT),
        )
        from processors.history_log import log_event
        log_event(
            actor="improvement",
            kind="config_change",
            scope="ocr_pipeline",
            title=f"section_chief auto-applied pipeline fix (audit #{audit_row_id})",
            body=(result.stdout or "")[:500] + (
                f"\n[stderr excerpt]: {result.stderr[:200]}" if result.stderr else ""),
            refs=[str(proposal_path.relative_to(ROOT))],
        )
    except Exception as e:
        print(f"[improvement] section_chief_auto_apply fail: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


def _strategist_auto_apply(audit_row_id: int, proposal_path: Path,
                            qwen_acc, haiku_acc) -> None:
    """Spawn chief_strategist consult to review and apply critical pipeline fix.
    Boss directive 2026-05-17: 策略長 resolves autonomously, no boss approval needed.
    """
    import subprocess
    consult_q = (
        f"Pipeline audit #{audit_row_id} returned CRITICAL: "
        f"qwen_acc={qwen_acc} haiku_acc={haiku_acc}. "
        f"Proposal at {proposal_path}. "
        f"Review all failure modes, select and apply the most effective prompt "
        f"fix(es) across stage1/stage2 (edit files directly), log a config_change "
        f"event, and note the expected next-audit accuracy improvement. "
        f"Resolve autonomously — do not escalate to boss unless truly exhausted."
    )
    try:
        result = subprocess.run(
            ["python", str(ROOT / "processors" / "chief_strategist.py"),
             "--consult", consult_q],
            capture_output=True, text=True, timeout=180,
            cwd=str(ROOT),
        )
        from processors.history_log import log_event
        log_event(
            actor="improvement",
            kind="config_change",
            scope="ocr_pipeline",
            title=f"策略長 auto-applied critical pipeline fix (audit #{audit_row_id})",
            body=(result.stdout or "")[:500] + (
                f"\n[stderr excerpt]: {result.stderr[:200]}" if result.stderr else ""),
            refs=[str(proposal_path.relative_to(ROOT))],
        )
    except Exception as e:
        print(f"[improvement] strategist_auto_apply fail: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)


def draft_proposal(audit_row_id: int, alert_level: str,
                   agg: dict, samples: list) -> str | None:
    """Public entry called by audit_sonnet. Returns proposal path
    (relative to ROOT) or None on failure."""
    if alert_level == "none":
        return None

    date_str = datetime.now(TZ).strftime("%Y-%m-%d")
    proposal_path = (PROPOSAL_DIR /
                     f"{date_str}_{alert_level}_audit{audit_row_id}.md")
    body = render_proposal(audit_row_id, alert_level, agg, samples)
    try:
        proposal_path.write_text(body, encoding="utf-8")
    except Exception as e:
        print(f"[improvement] proposal write fail: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return None

    _route_to_chain(audit_row_id, alert_level, proposal_path,
                    agg.get("qwen_acc"), agg.get("haiku_acc"))

    return str(proposal_path.relative_to(ROOT)).replace("\\", "/")


# ----------------------------------------------------------------------
# CLI: dry-run with synthetic data, useful for smoke testing
# ----------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Improvement proposal drafter")
    parser.add_argument("--smoke", action="store_true",
                        help="generate a synthetic warning proposal for testing")
    args = parser.parse_args()
    if args.smoke:
        agg = {
            "qwen_acc": 0.65,
            "haiku_acc": 0.78,
            "qwen_disagreements": 7,
            "haiku_disagreements": 4,
            "failure_modes": {
                "qwen_under_filter": 4,
                "haiku_over_admit": 3,
                "tag_mismatch": 1,
            },
        }
        samples = [{"media_row_id": i, "bucket": b}
                   for i, b in enumerate(["noise"] * 5 + ["low"] * 5 +
                                         ["mid"] * 5 + ["high"] * 5)]
        path = draft_proposal(audit_row_id=999, alert_level="warning",
                              agg=agg, samples=samples)
        print(f"smoke proposal at: {path}")


if __name__ == "__main__":
    main()
