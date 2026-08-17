"""Full bulk re-audit of every OCR'd image — boss directive 2026-05-07.

> "現在使用CC智商把所有OCR跑過一遍，所有喔。然後重新判斷需不需要入庫"

Mechanism:
  For each row in `media WHERE media_kind='photo' AND ocr_text IS NOT NULL`
  AND not already in `media_reaudit`:
    1. Spawn claude.exe with Read tool + image path + stored ocr_text
    2. Claude returns JSON verdict on:
       (a) OCR accuracy (qwen/gemini extraction was good?)
       (b) KB admission decision (does this contribute to the client brand commercial intel?)
       (c) Tags + rationale
    3. Insert row into media_reaudit immediately (resume-safe)

Why:
  - Existing ocr_quality_audit.py samples 10/day for trend monitoring (kept untouched)
  - This script is one-shot bulk decision: "what enters the library?"
  - 0 media rows currently in kb_documents → first-time admission decision

Volume: 5,359 images. Pro plan rate limit 1,500 msgs / 5h. Wall clock estimate
  30-50 hours sequential. Designed to run as background process; resume on crash.

Usage:
  py processors/ocr_full_reaudit.py --limit 3 --dry-run    # preview prompts
  py processors/ocr_full_reaudit.py --limit 3              # test 3 real
  py processors/ocr_full_reaudit.py                        # full run
  py processors/ocr_full_reaudit.py --batch 100            # 100 then exit (cron-friendly)

Cost: 0 USD (Claude Pro plan via OAuth, same path as ocr_quality_audit.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
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
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MODEL_USED = "claude-sonnet-4-6"  # via _llm_synth.claude_run default


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [ocr_reaudit] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"ocr_reaudit_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------

REAUDIT_PROMPT = """You are reviewing one OCR'd image from the Blacksite _TEMPLATE intel pipeline.

CONTEXT:
- Project: Blacksite — digital intelligence collection for the client brand (local lottery
  ecosystem + folk-belief belief economy + grey-market gambling + sports KOL).
- Library admission criterion: a card/document enters the library only if it
  contributes commercial intel value for the client brand strategic decisions.
  Pure noise (bot pumps, off-topic memes, decorative junk) is REJECTED.

The image was OCR'd previously. Stored OCR text:

<stored_ocr>
{ocr_text}
</stored_ocr>

Read the image at: {image_path}

Then output ONE JSON object on the LAST line of your response. Schema:

{{
  "ocr_score": <int 0-100; 100=perfect, 80=mostly correct minor errors, 60=partial, 30=mostly wrong, 0=catastrophic/loop/empty when text exists>,
  "ocr_verdict": "<one of: PASS, MINOR_ERRORS, PARTIAL, MAJOR_MISS, HALLUCINATION, EMPTY_BUT_TEXT_EXISTS, EMPTY_CORRECT, LOOP_DETECTED>",
  "kb_admit": <true | false>,
  "kb_value_class": "<one of: high, medium, low, noise>",
  "kb_value_score": <int 0-100; 100=must-keep strategic gold, 70=clearly relevant intel, 40=marginal, 0=pure noise>,
  "decision_tags": "<comma-list from: lottery, folk-belief, gambling, scam_template, kol, sports, regulatory, competitor, bot_pump_noise, payment, kol_persona, off_topic, advertising>",
  "rationale": "<≤120 chars: why admit/reject + what the image actually shows>"
}}

ADMISSION HEURISTICS:
- ADMIT (kb_value_score ≥ 40):
  * Gambling/lottery operators advertising (operator name + prediction/bonus offer)
  * Scam funnel templates (adult bait, fake winning testimony, urgency triggers)
  * folk-belief / lucky number content with real interpretive content
  * Sports KOL signals (athlete name + product mention)
  * Regulatory / police news screenshots
  * Payment infrastructure (PromptPay flows, TrueMoney specifics)
- REJECT (kb_value_score < 40):
  * Bot-pump pure forwards (sticker / one-emoji / decorative-only)
  * Off-topic (food pics, memes, personal selfies unrelated to TA)
  * Garbled OCR with no recoverable signal
  * Pure links/QR codes without context
- TAG bot_pump_noise IF: image is part of mass-forward rebroadcast pump but
  still has a recognizable operator name → kb_admit=true at low value (used
  for noise-labeling per CLAUDE.md §1.1)
"""

JSON_RE = re.compile(r"\{[\s\S]*?\"kb_admit\"[\s\S]*?\}", re.MULTILINE)


def parse_response(raw: str) -> dict | None:
    if not raw:
        return None
    candidates = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    if not candidates:
        candidates = JSON_RE.findall(raw)
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
            candidates = [b for b in blocks if "kb_admit" in b]
    if not candidates:
        return None
    try:
        return json.loads(candidates[-1])
    except Exception:
        try:
            # Try to clean trailing commas / unquoted bools
            cleaned = candidates[-1].replace("True", "true").replace("False", "false")
            cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
            return json.loads(cleaned)
        except Exception:
            return None


# ----------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------

def pending_rows(limit: int | None = None) -> list[dict]:
    """All OCR'd photo rows not yet in media_reaudit, oldest first (deterministic)."""
    conn = get_connection()
    q = """
        SELECT m.row_id, m.file_path, m.file_size, m.ocr_text, m.platform, m.processed_at
          FROM media m
     LEFT JOIN media_reaudit r ON r.media_row_id = m.row_id
         WHERE m.media_kind = 'photo'
           AND m.ocr_text IS NOT NULL
           AND r.media_row_id IS NULL
      ORDER BY m.row_id
    """
    if limit:
        q += f" LIMIT {int(limit)}"
    return [
        {"row_id": r[0], "file_path": r[1], "file_size": r[2],
         "ocr_text": r[3] or "", "platform": r[4], "processed_at": r[5]}
        for r in conn.execute(q).fetchall()
    ]


def total_pending() -> int:
    conn = get_connection()
    return conn.execute("""
        SELECT COUNT(*)
          FROM media m
     LEFT JOIN media_reaudit r ON r.media_row_id = m.row_id
         WHERE m.media_kind='photo'
           AND m.ocr_text IS NOT NULL
           AND r.media_row_id IS NULL
    """).fetchone()[0]


def total_done() -> int:
    conn = get_connection()
    return conn.execute("SELECT COUNT(*) FROM media_reaudit").fetchone()[0]


def write_row(row_id: int, parsed: dict | None, raw_excerpt: str, model: str) -> None:
    conn = get_connection()
    if parsed:
        conn.execute("""
            INSERT OR REPLACE INTO media_reaudit
                (media_row_id, audit_score_0_100, audit_verdict, kb_admit,
                 kb_value_class, kb_value_score_0_100, decision_tags, kb_rationale,
                 audited_at, model_used, raw_response)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row_id,
            parsed.get("ocr_score"),
            parsed.get("ocr_verdict"),
            1 if parsed.get("kb_admit") else 0,
            parsed.get("kb_value_class"),
            parsed.get("kb_value_score"),
            parsed.get("decision_tags") or "",
            (parsed.get("rationale") or "")[:500],
            now_iso(),
            model,
            raw_excerpt[:1000],
        ))
    else:
        # Mark as failed-parse so we don't loop forever on it
        conn.execute("""
            INSERT OR REPLACE INTO media_reaudit
                (media_row_id, audit_score_0_100, audit_verdict, kb_admit,
                 kb_value_class, kb_value_score_0_100, decision_tags, kb_rationale,
                 audited_at, model_used, raw_response)
            VALUES (?,NULL,'PARSE_FAIL',0,'parse_fail',0,'','parse failed',
                    ?,?,?)
        """, (row_id, now_iso(), model, raw_excerpt[:1000]))
    conn.commit()


# ----------------------------------------------------------------------
# Single-row audit
# ----------------------------------------------------------------------

def audit_one(sample: dict, dry_run: bool = False) -> dict:
    img_path = ROOT / sample["file_path"]
    if not img_path.exists():
        return {"row_id": sample["row_id"], "ocr_verdict": "MISSING_FILE",
                "parsed": None, "raw": ""}

    ocr = sample["ocr_text"][:2000] + ("...[truncated]" if len(sample["ocr_text"]) > 2000 else "")
    if not ocr.strip():
        ocr = "<EMPTY>"
    prompt = REAUDIT_PROMPT.format(
        ocr_text=ocr,
        image_path=str(img_path).replace("\\", "/"),
    )

    if dry_run:
        return {"row_id": sample["row_id"], "ocr_verdict": "DRY_RUN",
                "parsed": None, "raw": prompt[:300]}

    ok, raw = claude_run(
        task=prompt,
        skill_prefix=False,
        allowed_tools="Read",
        permission_mode="default",
        timeout_s=120.0,
        max_retries=2,
    )
    if not ok:
        return {"row_id": sample["row_id"], "ocr_verdict": "CLAUDE_ERROR",
                "parsed": None, "raw": (raw or "")[:500]}

    parsed = parse_response(raw)
    return {"row_id": sample["row_id"], "ocr_verdict": parsed.get("ocr_verdict") if parsed else "PARSE_FAIL",
            "parsed": parsed, "raw": raw[:500]}


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="cap rows this run; default = unlimited")
    ap.add_argument("--batch", type=int, default=None,
                    help="process this many then exit (cron-friendly); alias for --limit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=2.0,
                    help="sleep seconds between calls (rate-limit cushion)")
    ap.add_argument("--progress-every", type=int, default=10)
    args = ap.parse_args()

    cap = args.batch or args.limit
    pending_total = total_pending()
    done_total = total_done()
    log(f"=== ocr_full_reaudit start ===")
    log(f"already audited: {done_total}; pending: {pending_total}; this run cap: {cap or 'all'}")

    rows = pending_rows(limit=cap)
    if not rows:
        log("nothing to audit — exit clean")
        return

    log_event(actor="ocr_reaudit", kind="milestone", scope="ocr",
              title=f"ocr_full_reaudit run start n={len(rows)}",
              body=f"already_done={done_total}, pending={pending_total}, this_run={len(rows)}")

    t0 = time.time()
    success = 0
    parse_fail = 0
    admit = 0
    reject = 0
    high_value = 0

    for i, s in enumerate(rows, 1):
        result = audit_one(s, dry_run=args.dry_run)
        if not args.dry_run:
            write_row(s["row_id"], result["parsed"], result["raw"], MODEL_USED)
            p = result["parsed"]
            if p:
                success += 1
                if p.get("kb_admit"):
                    admit += 1
                else:
                    reject += 1
                if p.get("kb_value_score", 0) >= 70:
                    high_value += 1
            else:
                parse_fail += 1

        if i % args.progress_every == 0 or i == len(rows):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(rows) - i) / rate if rate > 0 else 0
            log(f"[{i}/{len(rows)}] ok={success} fail={parse_fail} admit={admit} reject={reject} high={high_value} rate={rate:.2f}/s eta={eta/60:.0f}min")

        if not args.dry_run and args.sleep > 0:
            time.sleep(args.sleep)

    elapsed = time.time() - t0
    log(f"=== run done elapsed={elapsed/60:.1f}min n={len(rows)} ok={success} fail={parse_fail} ===")
    log(f"   library admission: admit={admit} reject={reject} high_value={high_value}")

    log_event(actor="ocr_reaudit", kind="milestone", scope="ocr",
              title=f"ocr_full_reaudit batch done n={len(rows)} admit={admit}",
              body=f"elapsed_min={elapsed/60:.1f}\nok={success}\nparse_fail={parse_fail}\n"
                   f"admit={admit}\nreject={reject}\nhigh_value={high_value}\n"
                   f"remaining_pending={total_pending()}")


if __name__ == "__main__":
    main()
