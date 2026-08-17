"""Audit — Sonnet daily/weekly accuracy check on Stage 1 + 2 verdicts
(CLAUDE.md §2.1).

Daily  06:00 GMT+7 — N=20 sample (5 noise / 5 stage2-low / 5 stage2-mid / 5 stage2-high)
Weekly Monday 07:00 GMT+7 — N=100 cross-7-day mix

For each sample:
  1. Sonnet (via claude.exe --model sonnet, host OAuth) re-evaluates the image
     against image bytes + Stage 1 verdict + Stage 2 verdict (if any).
  2. Records ground-truth (Sonnet's view), disagreements, failure modes.
  3. Aggregates qwen_acc / haiku_acc / failure_modes_json.
  4. Decides alert_level:
        none      qwen_acc>=0.75 AND haiku_acc>=0.85
        warning   qwen_acc>=0.50 AND haiku_acc>=0.65 (one or both failed)
        critical  below either floor
  5. If warning/critical: triggers improvement.py (P6) to draft a proposal.

Insert one row into pipeline_audit per audit run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from db.connection import get_connection
from db.schema import init_db
from processors import llm_profiles
from processors.claude_auth import claude_host_oauth_env, is_claude_auth_error
from processors._llm_synth import find_claude_exe
from processors.llm_router import (
    codex_model_for_tier,
    fallback_provider,
    json_schema_file,
    run_codex,
    selected_provider,
    should_try_codex,
    should_use_claude_fallback,
)

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))

# Model resolution: env override > config/llm_providers.yaml `claude.audit`.
MODEL_ALIAS = (
    os.environ.get("AUDIT_MODEL")
    or llm_profiles.tier_model_for_claude_exe("claude", "audit")
)
MODEL_FULL_ID = (
    os.environ.get("AUDIT_MODEL_FULL")
    or llm_profiles.tier_model("claude", "audit")
)
PER_REQ_TIMEOUT_S = int(os.environ.get("AUDIT_TIMEOUT_S", "240"))

# Threshold floors (CLAUDE.md §2.1)
QWEN_FLOOR_WARN = float(os.environ.get("AUDIT_QWEN_WARN", "0.75"))
HAIKU_FLOOR_WARN = float(os.environ.get("AUDIT_HAIKU_WARN", "0.85"))
QWEN_FLOOR_CRIT = float(os.environ.get("AUDIT_QWEN_CRIT", "0.50"))
HAIKU_FLOOR_CRIT = float(os.environ.get("AUDIT_HAIKU_CRIT", "0.65"))

PROMPT_V1 = """You are auditing two cheaper tiers of the Blacksite intel pipeline:

Stage 1 (Qwen2.5-VL 7B local) — binary signal/noise filter
Stage 2 (Haiku 4.5)           — structured kb_admit / kb_value_score / tags

# === INSTANCE DOMAIN CONTEXT (customize per instance — see instances/_TEMPLATE/INSTANCE.md) ===
CONTEXT (CLAUDE.md §1 north star):
Project: Blacksite — digital intel for the client brand. The generic domain
example is: the target market's lottery + folk-belief economy + grey-market
gambling + sports KOL ecosystem. Library admission = commercial intel
value for the client brand's strategy.

You will see the image. Audit whether the lower tiers got it right.

LOWER-TIER VERDICTS:
Stage 1 verdict       : {stage1_verdict}
Stage 1 confidence    : {stage1_confidence}
Stage 1 tags          : {stage1_tags}

Stage 2 verdict       : {stage2_present}
Stage 2 kb_admit      : {kb_admit}
Stage 2 value_class   : {kb_value_class}
Stage 2 value_score   : {kb_value_score}
Stage 2 tags          : {decision_tags}
Stage 2 rationale     : {rationale}

Stored OCR text:
<ocr>
{ocr_text}
</ocr>

Output ONE JSON object on the LAST line, no markdown fences:

{{
  "your_verdict": "signal" | "noise",
  "your_kb_admit": <true|false>,
  "your_kb_value_class": "<high|medium|low|noise>",
  "your_kb_value_score": <int 0-100>,
  "qwen_correct": <true|false>,
  "haiku_correct": <true|false|null>,    // null if Stage 2 absent
  "failure_mode": "<short label or 'none'>",
  "comment": "<<=180 chars>"
}}

Failure mode labels (pick one if disagreement, else 'none'):
  qwen_under_filter      — Qwen called noise but actually signal
  qwen_over_admit        — Qwen called signal but actually noise
  haiku_over_admit       — Haiku admitted but no real client-brand value
  haiku_under_admit      — Haiku rejected but signal IS valuable
  haiku_score_too_high   — admitted correctly but score inflated
  haiku_score_too_low    — admitted correctly but score deflated
  tag_mismatch           — admit/score OK but tags wrong
  prompt_ambiguity       — both tiers reasonable; prompt unclear
"""

PROMPT_HASH = hashlib.sha256(PROMPT_V1.encode("utf-8")).hexdigest()[:12]

JSON_RE = re.compile(r"\{[\s\S]*?\"your_verdict\"[\s\S]*?\}", re.MULTILINE)

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
        "comment": {"type": "string", "maxLength": 240},
    },
    "required": [
        "your_verdict", "your_kb_admit", "your_kb_value_class",
        "your_kb_value_score", "qwen_correct", "haiku_correct",
        "failure_mode", "comment",
    ],
}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [audit] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"audit_sonnet_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates = [fenced[-1]] if fenced else JSON_RE.findall(raw)
    if not candidates:
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
                    blocks.append(raw[start:i + 1])
                    start = -1
        candidates = [b for b in blocks if "your_verdict" in b]
    for cand in reversed(candidates):
        try:
            return json.loads(cand)
        except Exception:
            try:
                cleaned = cand.replace("True", "true").replace("False", "false")
                cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
                return json.loads(cleaned)
            except Exception:
                continue
    return None


# ----------------------------------------------------------------------
# Sample selection
# ----------------------------------------------------------------------

def sample_daily(conn, n_per_bucket: int = 5) -> list:
    """N=20 daily: 5 noise / 5 stage2-low (<40) / 5 mid (40-69) / 5 high (>=70)."""
    cutoff = (datetime.now(TZ) - timedelta(days=2)).isoformat(timespec="seconds")

    # bucket A: Stage 1 noise (no Stage 2 row exists)
    noise_rows = conn.execute(
        """SELECT s.media_row_id
             FROM media_signal_filter s
            WHERE s.verdict = 'noise'
              AND s.processed_at >= ?
         ORDER BY RANDOM() LIMIT ?""",
        (cutoff, n_per_bucket),
    ).fetchall()

    # buckets B/C/D: Stage 2 by score band
    def stage2_band(low: int, high: int) -> list:
        return conn.execute(
            """SELECT d.media_row_id
                 FROM media_kb_decision d
                WHERE d.kb_value_score >= ?
                  AND d.kb_value_score < ?
                  AND d.processed_at >= ?
             ORDER BY RANDOM() LIMIT ?""",
            (low, high, cutoff, n_per_bucket),
        ).fetchall()

    low = stage2_band(0, 40)
    mid = stage2_band(40, 70)
    high = stage2_band(70, 101)

    sample = []
    for bucket, rows in (("noise", noise_rows), ("low", low),
                         ("mid", mid), ("high", high)):
        for r in rows:
            sample.append({"media_row_id": r["media_row_id"], "bucket": bucket})
    return sample


def sample_weekly(conn, n: int = 100) -> list:
    """N=100 across 7 days, mix matching daily proportions."""
    cutoff = (datetime.now(TZ) - timedelta(days=7)).isoformat(timespec="seconds")
    n_per_bucket = n // 4

    def query(verdict_clause: str, params: tuple) -> list:
        return conn.execute(
            f"""SELECT media_row_id FROM ({verdict_clause})
            ORDER BY RANDOM() LIMIT ?""",
            params + (n_per_bucket,),
        ).fetchall()

    noise = conn.execute(
        """SELECT media_row_id FROM media_signal_filter
            WHERE verdict='noise' AND processed_at >= ?
         ORDER BY RANDOM() LIMIT ?""",
        (cutoff, n_per_bucket),
    ).fetchall()
    low = conn.execute(
        """SELECT media_row_id FROM media_kb_decision
            WHERE kb_value_score < 40 AND processed_at >= ?
         ORDER BY RANDOM() LIMIT ?""",
        (cutoff, n_per_bucket),
    ).fetchall()
    mid = conn.execute(
        """SELECT media_row_id FROM media_kb_decision
            WHERE kb_value_score >= 40 AND kb_value_score < 70 AND processed_at >= ?
         ORDER BY RANDOM() LIMIT ?""",
        (cutoff, n_per_bucket),
    ).fetchall()
    high = conn.execute(
        """SELECT media_row_id FROM media_kb_decision
            WHERE kb_value_score >= 70 AND processed_at >= ?
         ORDER BY RANDOM() LIMIT ?""",
        (cutoff, n_per_bucket),
    ).fetchall()

    sample = []
    for bucket, rows in (("noise", noise), ("low", low), ("mid", mid),
                         ("high", high)):
        for r in rows:
            sample.append({"media_row_id": r["media_row_id"], "bucket": bucket})
    random.shuffle(sample)
    return sample


def fetch_context(conn, media_row_id: int) -> dict | None:
    s1 = conn.execute(
        "SELECT verdict, confidence, qwen_tags FROM media_signal_filter WHERE media_row_id=?",
        (media_row_id,),
    ).fetchone()
    s2 = conn.execute(
        """SELECT kb_admit, kb_value_class, kb_value_score, decision_tags, rationale
             FROM media_kb_decision WHERE media_row_id=?""",
        (media_row_id,),
    ).fetchone()
    m = conn.execute(
        "SELECT row_id, file_path, ocr_text FROM media WHERE row_id=?",
        (media_row_id,),
    ).fetchone()
    if not m:
        return None
    return {
        "media_row_id": media_row_id,
        "file_path": m["file_path"],
        "ocr_text": m["ocr_text"],
        "stage1": dict(s1) if s1 else None,
        "stage2": dict(s2) if s2 else None,
    }


# ----------------------------------------------------------------------
# Sonnet audit call
# ----------------------------------------------------------------------

def build_audit_prompt(ctx: dict) -> tuple[str, Path]:
    s1 = ctx.get("stage1") or {}
    s2 = ctx.get("stage2") or {}
    abs_path = ROOT / ctx["file_path"]

    prompt = PROMPT_V1.format(
        stage1_verdict=s1.get("verdict", "(absent)"),
        stage1_confidence=s1.get("confidence", "?"),
        stage1_tags=s1.get("qwen_tags", "[]"),
        stage2_present="present" if s2 else "ABSENT (Stage 1 was noise)",
        kb_admit=s2.get("kb_admit", "n/a"),
        kb_value_class=s2.get("kb_value_class", "n/a"),
        kb_value_score=s2.get("kb_value_score", "n/a"),
        decision_tags=s2.get("decision_tags", "n/a"),
        rationale=(s2.get("rationale") or "")[:240],
        ocr_text=(ctx.get("ocr_text") or "")[:1200],
    )
    return prompt, abs_path


def call_codex_audit(ctx: dict) -> tuple[str, dict]:
    prompt, abs_path = build_audit_prompt(ctx)
    schema_path = json_schema_file("audit_judge", AUDIT_SCHEMA)
    result = run_codex(
        prompt,
        tier="audit",
        model=codex_model_for_tier("audit"),
        image_path=abs_path,
        output_schema=schema_path,
        timeout_s=PER_REQ_TIMEOUT_S,
    )
    return result.text, result.meta()


def call_claude_audit(ctx: dict) -> tuple[str, dict]:
    claude_exe = find_claude_exe()
    if not claude_exe:
        return "", {"_error": "claude.exe not found", "_duration_ms": 0}

    prompt, abs_path = build_audit_prompt(ctx)
    # Embed image path for Read tool to pick up.
    prompt = (f"Read the image at: {str(abs_path).replace(chr(92), '/')}\n\n"
              f"{prompt}")

    cmd = [
        claude_exe,
        "--print", prompt,
        "--add-dir", str(ROOT),
        "--no-session-persistence",
        "--output-format", "text",
        "--allowed-tools", "Read",
        "--permission-mode", "default",
        "--model", MODEL_ALIAS,
    ]
    spawn_env = claude_host_oauth_env(os.environ)
    no_window_kw = ({"creationflags": subprocess.CREATE_NO_WINDOW}
                    if os.name == "nt" else {})
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, env=spawn_env,
            timeout=PER_REQ_TIMEOUT_S, cwd=str(ROOT),
            stdin=subprocess.DEVNULL, **no_window_kw,
        )
    except Exception as e:
        return "", {"_error": f"{type(e).__name__}: {str(e)[:200]}",
                    "_duration_ms": int((time.time() - t0) * 1000)}

    duration_ms = int((time.time() - t0) * 1000)
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        return out, {"_error": f"rc={proc.returncode}", "_duration_ms": duration_ms}
    return out, {"_duration_ms": duration_ms}


def call_audit(ctx: dict) -> tuple[str, dict]:
    provider = selected_provider()
    if should_try_codex("audit"):
        raw, meta = call_codex_audit(ctx)
        if not meta.get("_error"):
            return raw, meta
        log(f"  codex audit failed provider={provider}: {meta.get('_error')}")
        if provider == "codex" or not should_use_claude_fallback():
            return raw, meta
    raw, meta = call_claude_audit(ctx)
    if (meta.get("_error") and fallback_provider() == "codex"
            and is_claude_auth_error(meta.get("_error"), raw)):
        log(f"  Claude audit auth failed; trying Codex fallback: {meta.get('_error')}")
        codex_raw, codex_meta = call_codex_audit(ctx)
        codex_meta["_fallback_from"] = "claude_audit_auth"
        if not codex_meta.get("_error"):
            return codex_raw, codex_meta
        log(f"  Codex audit fallback failed: {codex_meta.get('_error')}")
    return raw, meta


def audit_model_label() -> str:
    if selected_provider() == "codex":
        return codex_model_for_tier("audit")
    if selected_provider() == "auto":
        return f"auto:{codex_model_for_tier('audit')}|{MODEL_FULL_ID}"
    return MODEL_FULL_ID


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------

def aggregate(samples: list, results: list) -> dict:
    qwen_n, qwen_correct = 0, 0
    haiku_n, haiku_correct = 0, 0
    failure_modes = {}
    qwen_disagreements = 0
    haiku_disagreements = 0
    for sample, parsed in zip(samples, results):
        if not parsed:
            continue
        if parsed.get("qwen_correct") is True:
            qwen_n += 1
            qwen_correct += 1
        elif parsed.get("qwen_correct") is False:
            qwen_n += 1
            qwen_disagreements += 1
        haiku_c = parsed.get("haiku_correct")
        if haiku_c is True:
            haiku_n += 1
            haiku_correct += 1
        elif haiku_c is False:
            haiku_n += 1
            haiku_disagreements += 1
        fm = parsed.get("failure_mode", "")
        if fm and fm != "none":
            failure_modes[fm] = failure_modes.get(fm, 0) + 1
    qwen_acc = (qwen_correct / qwen_n) if qwen_n else None
    haiku_acc = (haiku_correct / haiku_n) if haiku_n else None
    return {
        "qwen_acc": qwen_acc,
        "haiku_acc": haiku_acc,
        "qwen_disagreements": qwen_disagreements,
        "haiku_disagreements": haiku_disagreements,
        "failure_modes": failure_modes,
    }


def decide_alert(qwen_acc: float | None, haiku_acc: float | None) -> str:
    # treat None as "not enough samples" → none alert (skip)
    q = qwen_acc if qwen_acc is not None else 1.0
    h = haiku_acc if haiku_acc is not None else 1.0
    if q < QWEN_FLOOR_CRIT or h < HAIKU_FLOOR_CRIT:
        return "critical"
    if q < QWEN_FLOOR_WARN or h < HAIKU_FLOOR_WARN:
        return "warning"
    return "none"


def maybe_trigger_improvement(audit_row_id: int, alert_level: str,
                              agg: dict, samples: list) -> str | None:
    """If alert_level >= warning, draft a proposal via P6. Returns path or None."""
    if alert_level == "none":
        return None
    try:
        from processors.pipeline.improvement import draft_proposal
    except Exception as e:
        log(f"improvement module unavailable: {type(e).__name__}: {e}")
        return None
    return draft_proposal(audit_row_id, alert_level, agg, samples)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def run_audit(audit_kind: str, conn) -> dict:
    if audit_kind == "daily":
        samples = sample_daily(conn)
    elif audit_kind == "weekly":
        samples = sample_weekly(conn)
    else:
        raise ValueError(f"unknown audit_kind={audit_kind!r}")

    log(f"audit_kind={audit_kind} sample_size={len(samples)}")
    if not samples:
        log("no samples available — skip audit")
        return {"skipped": True}

    parsed_results = []
    sample_mix = {}
    sampled_ids = []
    for i, s in enumerate(samples, 1):
        sample_mix[s["bucket"]] = sample_mix.get(s["bucket"], 0) + 1
        sampled_ids.append(s["media_row_id"])
        ctx = fetch_context(conn, s["media_row_id"])
        if not ctx:
            parsed_results.append(None)
            continue
        raw, meta = call_audit(ctx)
        if meta.get("_error"):
            log(f"  sample {i} media={s['media_row_id']} bucket={s['bucket']} "
                f"ERR {meta.get('_error')}")
            parsed_results.append(None)
            continue
        parsed = parse_json(raw)
        parsed_results.append(parsed)
        if parsed:
            log(f"  sample {i}/{len(samples)} media={s['media_row_id']} "
                f"bucket={s['bucket']} verdict={parsed.get('your_verdict')} "
                f"qwen_ok={parsed.get('qwen_correct')} "
                f"haiku_ok={parsed.get('haiku_correct')} "
                f"fm={parsed.get('failure_mode')}")
        else:
            log(f"  sample {i} media={s['media_row_id']} parse_fail")

    agg = aggregate(samples, parsed_results)
    alert_level = decide_alert(agg["qwen_acc"], agg["haiku_acc"])
    log(f"AGG qwen_acc={agg['qwen_acc']} haiku_acc={agg['haiku_acc']} "
        f"alert={alert_level} failures={agg['failure_modes']}")

    cur = conn.execute(
        """INSERT INTO pipeline_audit
           (audit_kind, sample_size, sample_mix_json, sampled_media_ids,
            qwen_acc, haiku_acc, qwen_disagreements, haiku_disagreements,
            failure_modes_json, alert_level, audit_model, audited_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            audit_kind, len(samples),
            json.dumps(sample_mix),
            json.dumps(sampled_ids),
            agg["qwen_acc"], agg["haiku_acc"],
            agg["qwen_disagreements"], agg["haiku_disagreements"],
            json.dumps(agg["failure_modes"], ensure_ascii=False),
            alert_level, audit_model_label(), now_iso(),
        ),
    )
    conn.commit()
    audit_row_id = cur.lastrowid

    # Trigger improvement loop if needed
    proposal_path = maybe_trigger_improvement(audit_row_id, alert_level, agg, samples)
    if proposal_path:
        conn.execute(
            "UPDATE pipeline_audit SET improvement_proposed=? WHERE row_id=?",
            (proposal_path, audit_row_id),
        )
        conn.commit()
        log(f"improvement proposal drafted: {proposal_path}")

    return {
        "audit_row_id": audit_row_id,
        "qwen_acc": agg["qwen_acc"],
        "haiku_acc": agg["haiku_acc"],
        "alert_level": alert_level,
        "failure_modes": agg["failure_modes"],
        "improvement_proposed": proposal_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()
    conn = get_connection()
    if args.dry_run:
        if args.kind == "daily":
            samples = sample_daily(conn)
        else:
            samples = sample_weekly(conn)
        log(f"dry_run kind={args.kind} sample_size={len(samples)} "
            f"buckets={ {s['bucket']: 1 for s in samples} }")
        return

    result = run_audit(args.kind, conn)
    log(f"DONE {result}")
    conn.close()


if __name__ == "__main__":
    main()
