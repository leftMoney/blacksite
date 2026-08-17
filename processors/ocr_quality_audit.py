"""OCR quality audit — daily Claude-vision spot-check on qwen2.5vl OCR output.

Boss directive 5/6: "OCR 機制要設計使用 CC 智商抽檢是否正常".

Mechanism:
  1. Daily cron 06:00 GMT+7 (after 03:00 OCR done, after 04:00 ASR done)
  2. Random-sample N=10 rows from yesterday's OCR output
  3. For each: spawn claude.exe with Read access → claude reads image,
     compares with stored ocr_text, returns JSON verdict
  4. Aggregate: avg_score, high_concern_count, loop_detected_count
  5. Write YAML to runtime/agent_kpi/ocr_audit/<YYYY-MM-DD>.yaml
  6. log_event(kind='metric', scope='ocr') for trend tracking
  7. If avg < 75 OR high_concern >= 3: log warning + auto-flag for boss brief

Why claude.exe and not direct Anthropic SDK:
  - OAuth token (sk-ant-oat01-) needs host CLAUDE_CODE_* env vars to refresh
  - claude.exe wraps that; raw SDK with bare OAuth token fails after refresh
  - Already proven path in _llm_synth.py for daily brief / strategist

Cost: 0 USD (Claude Pro plan, OAuth, not API key billing).
Rate limit: 10 calls/day vs Pro plan 1500/5h window — trivial.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402
from processors._llm_synth import claude_run  # noqa: E402
from processors.history_log import log_event  # noqa: E402

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
AUDIT_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "agent_kpi" / "ocr_audit"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SAMPLE_N = int(os.environ.get("OCR_AUDIT_SAMPLE_N", "10"))
ALERT_AVG_THRESHOLD = int(os.environ.get("OCR_AUDIT_ALERT_AVG", "75"))
ALERT_HIGH_CONCERN_COUNT = int(os.environ.get("OCR_AUDIT_ALERT_CONCERN", "3"))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [ocr_audit] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"ocr_audit_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------
# Sample selection
# ----------------------------------------------------------------------

def pick_samples(n: int = SAMPLE_N, since_hours: int = 30) -> list[dict]:
    """Pick N random recently-OCR'd rows, mix of length buckets so we test
    both empty / short / mid / long outputs.
    """
    cutoff = (datetime.now(TZ) - timedelta(hours=since_hours)).strftime("%Y-%m-%d")
    conn = get_connection()
    out: list[dict] = []
    # Bucket: 3 short (<20 chars or NOTEXT), 4 mid (20-300), 3 long (>300)
    buckets = [
        ("short", "(ocr_text IS NULL OR length(ocr_text) < 20)", 3),
        ("mid", "length(ocr_text) BETWEEN 20 AND 300", 4),
        ("long", "length(ocr_text) > 300", 3),
    ]
    for label, where, k in buckets:
        rows = conn.execute(
            f"""SELECT row_id, file_path, file_size, ocr_text, processed_at
                  FROM media
                 WHERE media_kind='photo'
                   AND processed_at LIKE ?
                   AND {where}
              ORDER BY random() LIMIT ?""",
            (f"{cutoff}%", k),
        ).fetchall()
        for r in rows:
            out.append({
                "row_id": r[0], "file_path": r[1], "file_size": r[2],
                "qwen_text": r[3] or "", "processed_at": r[4],
                "bucket": label,
            })
    return out


# ----------------------------------------------------------------------
# Claude invocation
# ----------------------------------------------------------------------

AUDIT_PROMPT_TEMPLATE = """You are auditing an OCR pipeline. The OCR model (Qwen2.5-VL 7B local)
processed an image and produced this text:

<qwen_ocr_output>
{qwen_text}
</qwen_ocr_output>

Read the image at this path using the Read tool: {image_path}

Then evaluate Qwen's OCR output. Output ONLY a single JSON object on the last line of
your response, no other text after it. Schema:

{{
  "visible_text_summary": "<one-sentence describe what text is visible in the image>",
  "qwen_score_0_100": <integer 0-100; 100=perfect, 80=mostly correct minor errors, 60=partial, 30=mostly wrong, 0=catastrophic/loop/empty when text exists>,
  "verdict": "<one of: PASS, MINOR_ERRORS, PARTIAL, MAJOR_MISS, HALLUCINATION, EMPTY_BUT_TEXT_EXISTS, EMPTY_CORRECT, LOOP_DETECTED>",
  "missing_critical": "<comma-list of critical missing items, or empty>",
  "hallucinated": "<text qwen wrote that is NOT in image, or empty>",
  "notes": "<any pattern observation, max 100 chars>"
}}

Score guidance:
- If image truly has no readable text and qwen output is empty/<NOTEXT>: PASS, score 100
- If image has local text and qwen got 80%+ characters right: PASS or MINOR_ERRORS, score 80-95
- If qwen looped/repeated the same phrase 50+ times: LOOP_DETECTED, score 0
- If qwen wrote things not in image (hallucination): HALLUCINATION, score 0-30
- If qwen returned empty but image clearly has prominent text: EMPTY_BUT_TEXT_EXISTS, score 0-20
"""

JSON_LINE_RE = re.compile(r"\{[^{}]*\"qwen_score_0_100\"[^{}]*\}", re.DOTALL)


def parse_audit_response(raw: str) -> dict | None:
    """Extract the JSON verdict from claude's stdout (last JSON object)."""
    if not raw:
        return None
    # Try last code block
    candidates = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    if candidates:
        candidates = [candidates[-1]]
    else:
        # Fallback: find last {...} that looks like our schema
        candidates = JSON_LINE_RE.findall(raw)
        if not candidates:
            # Greedy try: find last { } block
            depth = 0
            start = -1
            blocks = []
            for i, ch in enumerate(raw):
                if ch == "{":
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and start >= 0:
                        blocks.append(raw[start:i+1])
                        start = -1
            candidates = [b for b in blocks if "qwen_score_0_100" in b]
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1])
    except Exception:
        return None


def audit_one(sample: dict, dry_run: bool = False) -> dict:
    """Send one (image, qwen_text) pair to Claude for verdict."""
    img_path = ROOT / sample["file_path"]
    if not img_path.exists():
        return {**sample, "verdict": "MISSING_FILE", "score": None, "raw": ""}

    qwen_excerpt = sample["qwen_text"][:1500] + ("...[truncated]" if len(sample["qwen_text"]) > 1500 else "")
    if not qwen_excerpt:
        qwen_excerpt = "<EMPTY>"
    prompt = AUDIT_PROMPT_TEMPLATE.format(
        qwen_text=qwen_excerpt,
        image_path=str(img_path).replace("\\", "/"),
    )
    if dry_run:
        return {**sample, "verdict": "DRY_RUN", "score": None, "raw": prompt[:200]}

    ok, raw_out = claude_run(
        task=prompt,
        skill_prefix=False,           # this is an audit, not an agent role
        allowed_tools="Read",         # only need image read, no writes
        permission_mode="default",
        timeout_s=300.0,
        max_retries=2,
    )
    if not ok:
        return {**sample, "verdict": "CLAUDE_ERROR", "score": None, "raw": raw_out[:500]}

    parsed = parse_audit_response(raw_out)
    if not parsed:
        return {**sample, "verdict": "PARSE_FAIL", "score": None, "raw": raw_out[:500]}

    return {**sample, **parsed, "score": parsed.get("qwen_score_0_100"), "raw": raw_out[:200]}


# ----------------------------------------------------------------------
# Aggregation + reporting
# ----------------------------------------------------------------------

def write_yaml_report(results: list[dict]) -> Path:
    """Write per-day audit yaml to runtime/agent_kpi/ocr_audit/."""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    p = AUDIT_DIR / f"{today}.yaml"

    # Compute aggregates
    scores = [r["score"] for r in results if isinstance(r.get("score"), (int, float))]
    avg = round(sum(scores) / len(scores), 1) if scores else None
    high_concern = [r for r in results if isinstance(r.get("score"), (int, float)) and r["score"] < 60]
    loops = [r for r in results if r.get("verdict") == "LOOP_DETECTED"]
    halluc = [r for r in results if r.get("verdict") == "HALLUCINATION"]
    empty_miss = [r for r in results if r.get("verdict") == "EMPTY_BUT_TEXT_EXISTS"]

    # Manual YAML (avoid pyyaml dep)
    lines = [
        f"date: '{today}'",
        f"audit_at: '{now_iso()}'",
        f"sample_size: {len(results)}",
        f"avg_score: {avg if avg is not None else 'null'}",
        f"high_concern_count: {len(high_concern)}",
        f"loop_count: {len(loops)}",
        f"hallucination_count: {len(halluc)}",
        f"empty_miss_count: {len(empty_miss)}",
        "samples:",
    ]
    for r in results:
        lines.append(f"  - row_id: {r['row_id']}")
        lines.append(f"    bucket: {r.get('bucket', '?')}")
        lines.append(f"    score: {r.get('score', 'null')}")
        lines.append(f"    verdict: {r.get('verdict', 'unknown')}")
        miss = (r.get('missing_critical') or '').replace("'", "")[:120]
        if miss:
            lines.append(f"    missing: '{miss}'")
        halluc = (r.get('hallucinated') or '').replace("'", "")[:120]
        if halluc:
            lines.append(f"    hallucinated: '{halluc}'")
        notes = (r.get('notes') or '').replace("'", "")[:120]
        if notes:
            lines.append(f"    notes: '{notes}'")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"audit yaml → {p}")
    return p


def emit_history(results: list[dict], yaml_path: Path) -> None:
    scores = [r["score"] for r in results if isinstance(r.get("score"), (int, float))]
    avg = round(sum(scores) / len(scores), 1) if scores else None
    high_concern = [r for r in results if isinstance(r.get("score"), (int, float)) and r["score"] < 60]
    loops = sum(1 for r in results if r.get("verdict") == "LOOP_DETECTED")
    halluc = sum(1 for r in results if r.get("verdict") == "HALLUCINATION")

    body = (
        f"sample_n: {len(results)}\n"
        f"avg_score: {avg}\n"
        f"high_concern: {len(high_concern)}\n"
        f"loops: {loops}\n"
        f"hallucinations: {halluc}\n"
        f"yaml: {yaml_path.name}\n"
    )
    log_event(actor="ocr_audit", kind="metric", scope="ocr",
              title=f"OCR audit avg={avg} concern={len(high_concern)}/{len(results)}",
              body=body, refs=[str(yaml_path.relative_to(ROOT)).replace("\\", "/")])

    # Alert path
    alert = False
    reasons = []
    if avg is not None and avg < ALERT_AVG_THRESHOLD:
        alert = True
        reasons.append(f"avg {avg} < {ALERT_AVG_THRESHOLD}")
    if len(high_concern) >= ALERT_HIGH_CONCERN_COUNT:
        alert = True
        reasons.append(f"{len(high_concern)} samples score < 60 (threshold {ALERT_HIGH_CONCERN_COUNT})")
    if loops > 0 or halluc > 0:
        alert = True
        reasons.append(f"loops={loops} hallucinations={halluc}")
    if alert:
        log_event(actor="ocr_audit", kind="warning", scope="ocr",
                  title="OCR quality below threshold",
                  body=f"reasons: {'; '.join(reasons)}\nyaml: {yaml_path.name}",
                  refs=[str(yaml_path.relative_to(ROOT)).replace("\\", "/")])


# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample-n", type=int, default=SAMPLE_N)
    ap.add_argument("--dry-run", action="store_true",
                    help="pick samples + show prompts, don't call claude")
    ap.add_argument("--since-hours", type=int, default=30)
    args = ap.parse_args()

    log(f"=== OCR audit start sample_n={args.sample_n} since={args.since_hours}h ===")
    samples = pick_samples(args.sample_n, args.since_hours)
    if not samples:
        log("WARN: no samples available — abort")
        return
    log(f"picked {len(samples)} samples ({[s['bucket'] for s in samples]})")

    results: list[dict] = []
    for i, s in enumerate(samples, 1):
        log(f"[{i}/{len(samples)}] row={s['row_id']} bucket={s['bucket']} qwen_len={len(s['qwen_text'])}")
        r = audit_one(s, dry_run=args.dry_run)
        results.append(r)
        v = r.get("verdict", "?")
        sc = r.get("score", "?")
        log(f"  → verdict={v} score={sc}")

    if args.dry_run:
        log("DRY RUN — skipping yaml + history")
        return

    yaml_path = write_yaml_report(results)
    emit_history(results, yaml_path)

    scores = [r["score"] for r in results if isinstance(r.get("score"), (int, float))]
    avg = round(sum(scores) / len(scores), 1) if scores else None
    log(f"=== audit done avg={avg} samples={len(results)} ===")


if __name__ == "__main__":
    main()
