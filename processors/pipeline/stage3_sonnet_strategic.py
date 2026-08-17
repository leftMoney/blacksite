"""Stage 3 — Sonnet via claude.exe agent path (CLAUDE.md §2.1).

Input:  media_kb_decision rows where kb_value_score >= 70 AND not yet in
        media_strategic_brief (1:1 default; re-evals create new rows).
Output: media_strategic_brief row with commercial_action +
        cross_case_pattern (natural language, NOT structured JSON — Sonnet
        produces qualitative interpretation per §1 north star).

Cron:   daily 19:00 GMT+7 after Stage 2 has finished its day's batch.

Why claude.exe (not direct API):
  - OAuth Bearer + api.anthropic.com is hard-gated to Haiku only (5/8).
  - Sonnet/Opus must go through claude.exe agent path (Pro plan quota,
    separate from API quota).
  - Proven 5/8 experiment: `--model sonnet --bare` with ANTHROPIC_API_KEY
    = OAuth token works (Pro plan agent path).

Volume guard:
  STAGE3_DAILY_BUDGET (default 100) caps rows/day. Sonnet is the
  most expensive tier; budget conservative until value proven.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from processors.prompt_sanitize import sanitize_untrusted
from processors._llm_synth import find_claude_exe
from processors.llm_router import (
    codex_model_for_tier,
    run_codex,
    selected_provider,
    should_try_codex,
    should_use_claude_fallback,
)

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))

# Model resolution: env override > config/llm_providers.yaml `claude.strategic`.
# 5/8 evening verified: claude.exe host OAuth path honors --model flag when
# claude_run(pass_model_flag=True). Earlier 5/2 warning was --bare-specific.
# To swap model versions, edit config/llm_providers.yaml; to override per-run,
# set STAGE3_MODEL (alias) + STAGE3_MODEL_FULL (canonical id) env vars.
MODEL_ALIAS = (
    os.environ.get("STAGE3_MODEL")
    or llm_profiles.tier_model_for_claude_exe("claude", "strategic")
)
MODEL_FULL_ID = (
    os.environ.get("STAGE3_MODEL_FULL")
    or llm_profiles.tier_model("claude", "strategic")
)
DAILY_BUDGET = int(os.environ.get("STAGE3_DAILY_BUDGET", "100"))
DEFAULT_BATCH = int(os.environ.get("STAGE3_BATCH", "20"))
PER_REQ_TIMEOUT_S = int(os.environ.get("STAGE3_TIMEOUT_S", "300"))
HIGH_VALUE_THRESHOLD = int(os.environ.get("STAGE3_THRESHOLD", "70"))

PROMPT_V1 = """You are Stage 3 of the Blacksite intel pipeline (CLAUDE.md §2.1).
You are Claude Sonnet operating in strategic-interpretation mode.

# === INSTANCE BRAND CONTEXT (customize per instance — see instances/_TEMPLATE/INSTANCE.md) ===
# Replace the three legal-status facts below with the active instance's client
# brand. State: (1) the licensed product the client sells, (2) the lawful framework
# any prize-draw / promotion runs under, and (3) why TA overlap with the grey-market
# is a marketing fact, not a legal one. Keep the schema and output labels unchanged.

WHO IS THE CLIENT BRAND (<INSTANCE_BRAND>) — three legal-status facts (generic example):

1. **Product = a licensed, tangible commercial good (e.g. sports collection
   cards, digital + physical).** The client holds an authorisation from the
   relevant government agency (the sports regulator) to sell its product. The
   product is a tangible commercial good (analogous to digital + physical loyalty
   / collectible cards), NOT a gambling instrument.

2. **Lottery / prize-draw mechanism = lawful-promotion compliant.** Any
   prize-draw the client brand runs sits under the target country's
   commercial-promotion / trade-promotion framework (NOT its gambling statutes),
   with two hard rules:
     - **Prizes are NEVER cash** (non-cash prizes → falls in commercial
       promotion, not gambling).
     - **Draws are NEVER computer-program-driven** (manual draws →
       outside the electronic-gambling definition).
   These two design rules are precisely what legally separates the client brand from
   the gambling-law regime.

3. **TA overlap with grey-market does NOT make the client brand illegal.** The
   client brand's target audience (the target market's lottery players) overlaps
   with audiences that grey-market operators also pursue. Audience overlap is
   a marketing fact, not a legal one — exactly the same way a licensed
   bank's affluent customers overlap with scam-targets, but the bank is
   still legitimate.

The client brand is therefore on the LEGAL side of every relevant local regulatory
line. folk-belief (lucky-number / belief economy) engagement is through
sanctioned channels.

WHAT BLACKSITE IS:

Blacksite is the client brand's competitive-intelligence platform. It monitors the
grey-market gambling ecosystem (illegal online casinos, unlicensed
sportsbook funnels, scam "free credit" templates, gift-laundering on
livestream platforms) so the client brand can:

  - Understand HOW illegal competitors acquire the client brand's legitimate target
    audience (local lottery players who might be diverted to illegal
    operators).
  - Differentiate the client brand's legal product on dimensions illegal operators
    cannot match: regulatory legitimacy, cultural authenticity (folk-belief),
    transparent payouts, no rollover traps.
  - Inform the client brand's strategy team on where to invest (channels / messaging /
    KOL partnerships) to win share back from grey-market funnels.
  - Surface regulatory weather changes (police actions, regulator
    statements) that affect competitive landscape.

This is defensive competitive intelligence by a legal incumbent against
illicit market entrants — analogous to how a licensed pharmacy tracks
illicit drug-import vectors, or how a licensed bank monitors crypto
scam funnels. Your output advises the client brand on how to OUT-COMPETE illegal
operators with LEGAL products. Your output is NOT instructions for any
party to operate illegal gambling.

YOUR JOB IN THIS STAGE:

§1 north star: every artifact must propose specific commercial advantage
for the client brand (the legal incumbent), not neutral reportage.

PIPELINE EVIDENCE PROVIDED TO YOU (text only — image NOT re-read):
- Stage 1 (Qwen) and Stage 2 (Haiku) already inspected the image.
- Stage 2 extracted OCR text + structured verdict (kb_admit, value_class,
  value_score, decision_tags, rationale).
- Your job is qualitative cross-case synthesis on top of those verdicts —
  re-reading the image would be redundant token spend with no marginal
  insight gain.

STAGE 2 VERDICT (Haiku, the previous tier):
- kb_value_class: {kb_value_class}
- kb_value_score: {kb_value_score}/100
- decision_tags: {decision_tags}
- rationale: {rationale}

OCR TEXT (already extracted by upstream Qwen):
<ocr>
{ocr_text}
</ocr>

NEIGHBOURING HIGH-VALUE CASES (last 7 days, Stage 2 score >= 70):
{neighbours}

YOUR OUTPUT — natural language preferred (Sonnet's qualitative density),
but STRUCTURE the response with these labels so the parser can extract:

COMMERCIAL_ACTION:
<2-4 sentences: what specific move or observation should the client brand
strategy team consider. Be concrete — name a tactic, a competitor move
to counter, a market window, a regulatory risk. Avoid generic platitudes.>

CROSS_CASE_PATTERN:
<2-3 sentences: how does this case fit a recurring pattern in the recent
neighbours? Or is it a fresh signal? If pattern, name it.>

CONFIDENCE: <high | medium | low>
RELATED_CASES: <comma list of media_row_ids from neighbours that anchor
the pattern, or "none" if isolated>
"""

PROMPT_HASH = hashlib.sha256(PROMPT_V1.encode("utf-8")).hexdigest()[:12]


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [stage3] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"stage3_sonnet_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


COMMERCIAL_RE = re.compile(r"COMMERCIAL_ACTION\s*:\s*(.+?)(?=CROSS_CASE_PATTERN|CONFIDENCE|RELATED_CASES|$)",
                           re.IGNORECASE | re.DOTALL)
PATTERN_RE = re.compile(r"CROSS_CASE_PATTERN\s*:\s*(.+?)(?=CONFIDENCE|RELATED_CASES|$)",
                        re.IGNORECASE | re.DOTALL)
CONFIDENCE_RE = re.compile(r"CONFIDENCE\s*:\s*(\w+)", re.IGNORECASE)
RELATED_RE = re.compile(r"RELATED_CASES\s*:\s*([^\n]+)", re.IGNORECASE)


def parse_response(raw: str) -> dict:
    out = {"commercial_action": None, "cross_case_pattern": None,
           "confidence": None, "related_media_ids": []}
    if not raw:
        return out
    m = COMMERCIAL_RE.search(raw)
    if m:
        out["commercial_action"] = m.group(1).strip()
    m = PATTERN_RE.search(raw)
    if m:
        out["cross_case_pattern"] = m.group(1).strip()
    m = CONFIDENCE_RE.search(raw)
    if m:
        c = m.group(1).strip().lower()
        if c in ("high", "medium", "low"):
            out["confidence"] = {"high": 0.85, "medium": 0.6, "low": 0.35}[c]
    m = RELATED_RE.search(raw)
    if m:
        ids_raw = m.group(1).strip()
        if ids_raw and ids_raw.lower() != "none":
            ids = []
            for tok in re.split(r"[,\s]+", ids_raw):
                if tok.isdigit():
                    ids.append(int(tok))
            out["related_media_ids"] = ids
    return out


def fetch_pending(conn, limit: int) -> list:
    return conn.execute(
        """SELECT d.media_row_id, d.kb_value_class, d.kb_value_score,
                  d.decision_tags, d.rationale,
                  m.file_path, m.ocr_text
             FROM media_kb_decision d
             JOIN media m ON m.row_id = d.media_row_id
        LEFT JOIN media_strategic_brief b ON b.media_row_id = d.media_row_id
            WHERE d.kb_admit = 1
              AND d.kb_value_score >= ?
              AND b.media_row_id IS NULL
         ORDER BY d.kb_value_score DESC, d.media_row_id ASC
            LIMIT ?""",
        (HIGH_VALUE_THRESHOLD, limit),
    ).fetchall()


def fetch_by_ids(conn, media_row_ids: list[int]) -> list:
    """Boss 5/8: redo specific media rows by row_id. Bypasses kb_admit gate
    AND HIGH_VALUE_THRESHOLD AND existing-brief filter — Commander can force
    Stage 3 strategic re-eval on any row that has a Stage 2 decision."""
    if not media_row_ids:
        return []
    placeholders = ",".join("?" * len(media_row_ids))
    return conn.execute(
        f"""SELECT d.media_row_id, d.kb_value_class, d.kb_value_score,
                   d.decision_tags, d.rationale,
                   m.file_path, m.ocr_text
              FROM media_kb_decision d
              JOIN media m ON m.row_id = d.media_row_id
             WHERE d.media_row_id IN ({placeholders})""",
        tuple(media_row_ids),
    ).fetchall()


def total_pending(conn) -> int:
    r = conn.execute(
        """SELECT COUNT(*)
             FROM media_kb_decision d
        LEFT JOIN media_strategic_brief b ON b.media_row_id = d.media_row_id
            WHERE d.kb_admit = 1
              AND d.kb_value_score >= ?
              AND b.media_row_id IS NULL""",
        (HIGH_VALUE_THRESHOLD,),
    ).fetchone()
    return r[0] if r else 0


def today_count(conn) -> int:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    r = conn.execute(
        "SELECT COUNT(*) FROM media_strategic_brief WHERE processed_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    return r[0] if r else 0


def neighbours_for(conn, media_row_id: int, limit: int = 5) -> list:
    """Recent high-value Stage 2 verdicts (last 7 days), excluding self."""
    cutoff = (datetime.now(TZ) - timedelta(days=7)).isoformat(timespec="seconds")
    return conn.execute(
        """SELECT media_row_id, kb_value_class, kb_value_score,
                  decision_tags, rationale
             FROM media_kb_decision
            WHERE kb_admit = 1
              AND kb_value_score >= ?
              AND media_row_id != ?
              AND processed_at >= ?
         ORDER BY kb_value_score DESC, processed_at DESC
            LIMIT ?""",
        (HIGH_VALUE_THRESHOLD, media_row_id, cutoff, limit),
    ).fetchall()


def format_neighbours(rows: list) -> str:
    """5/18 security: emit STRUCTURED tuple only (id / class / score / tags).

    Previously included rationale free-text — that field is Stage-2-LLM-
    generated, which can echo OCR-injected payloads from upstream. By
    dropping rationale entirely we cut the cross-row injection-propagation
    chain. Tags are still included because the tag vocabulary is closed
    (whitelist defined in Stage 2 prompt) so a hostile tag value can't
    contain arbitrary natural-language payloads.
    """
    if not rows:
        return "(none — first high-value case this week)"
    lines = []
    for r in rows:
        tags = (r["decision_tags"] or "")[:60]
        # Whitelist-validate tags: only the closed vocab survives.
        # Anything else collapses to '(?)'.
        ALLOWED_TAGS = {
            "lottery", "folk-belief", "gambling", "scam_template", "kol", "sports",
            "regulatory", "competitor", "bot_pump_noise", "payment",
            "kol_persona", "off_topic", "advertising", "funnel_invite",
        }
        clean_tags = ",".join(
            t.strip() for t in tags.split(",")
            if t.strip() in ALLOWED_TAGS
        ) or "(?)"
        lines.append(
            f"  - id={r['media_row_id']} class={r['kb_value_class']} "
            f"score={r['kb_value_score']} tags=[{clean_tags}]"
        )
    return "\n".join(lines)


def call_codex_strategic(prompt: str) -> tuple[str, dict]:
    result = run_codex(
        prompt,
        tier="stage3",
        model=codex_model_for_tier("stage3"),
        timeout_s=PER_REQ_TIMEOUT_S,
    )
    return result.text, result.meta()


def call_strategic(ocr_text: str, kb_value_class: str,
                   kb_value_score: int, decision_tags: str, rationale: str,
                   neighbours_block: str) -> tuple[str, dict]:
    """Text-only Sonnet call via _llm_synth.claude_run — host OAuth path
    (proven 5/7: 3,509 row Opus re-audit ran clean). NOT --bare (which
    treated OAuth refresh token as plain API key and the server eventually
    rejected with "Invalid API key", per 5/8 incident at 14:50-14:53).

    No --model flag (5/2 finding: --model + OAuth refresh-token conflict
    in claude.exe). Pro plan default model applies — boss confirmed
    quota healthy + extra-use enabled, so Opus 4.7 fallback is acceptable
    per north star (more qualitative density, more cost OK)."""
    from processors._llm_synth import claude_run

    # 5/18 security: sanitize the two free-text fields that originate from
    # untrusted source content. `ocr_text` is what Stage 1 Qwen extracted
    # from the attacker-supplied image; `rationale` is Stage 2 Haiku's
    # explanation which can echo the OCR back. Both can carry injection
    # payloads. The neighbours block is already structured (no rationale)
    # per format_neighbours() — so it doesn't need re-sanitize here.
    safe_ocr = sanitize_untrusted(ocr_text or "", max_chars=1500,
                                   label="stage3_ocr_text").text
    safe_rat = sanitize_untrusted(rationale or "", max_chars=300,
                                   label="stage3_rationale").text
    prompt = PROMPT_V1.format(
        kb_value_class=kb_value_class or "?",
        kb_value_score=kb_value_score if kb_value_score is not None else "?",
        decision_tags=decision_tags or "(none)",
        rationale=safe_rat,
        ocr_text=safe_ocr,
        neighbours=neighbours_block,
    )

    provider = selected_provider()
    if should_try_codex("stage3"):
        raw, meta = call_codex_strategic(prompt)
        if not meta.get("_error"):
            return raw, meta
        log(f"  codex stage3 failed provider={provider}: {meta.get('_error')}")
        if provider == "codex" or not should_use_claude_fallback():
            return raw, meta

    t0 = time.time()
    try:
        ok, out = claude_run(
            task=prompt,
            skill_prefix=False,        # Stage 3 prompt is self-contained
            extra_system="",
            allowed_tools="",          # text-only, no tools
            permission_mode="default",
            model=MODEL_ALIAS,         # 'sonnet' / 'opus' / 'haiku' alias
            pass_model_flag=True,      # 5/8 verified: host OAuth honors --model
            timeout_s=float(PER_REQ_TIMEOUT_S),
            max_retries=2,
        )
    except Exception as e:
        return "", {"_error": f"{type(e).__name__}: {str(e)[:200]}",
                    "_duration_ms": int((time.time() - t0) * 1000)}
    duration_ms = int((time.time() - t0) * 1000)
    if not ok or not out:
        return out or "", {"_error": "claude_run returned not-ok",
                            "_duration_ms": duration_ms}
    return out, {"_duration_ms": duration_ms}


def insert_result(conn, media_row_id: int, parsed: dict, raw: str,
                  meta: dict) -> None:
    related = parsed.get("related_media_ids") or []
    conn.execute(
        """INSERT INTO media_strategic_brief
           (media_row_id, commercial_action, cross_case_pattern,
            confidence, raw_response, related_media_ids,
            model_used, prompt_hash, duration_ms, processed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            media_row_id,
            parsed.get("commercial_action"),
            parsed.get("cross_case_pattern"),
            parsed.get("confidence"),
            raw[:8000] if raw else None,
            json.dumps(related) if related else None,
            meta.get("_model") or MODEL_FULL_ID,
            PROMPT_HASH,
            meta.get("_duration_ms"),
            now_iso(),
        ),
    )
    conn.commit()


def process_one(conn, row) -> str:
    nbr_rows = neighbours_for(conn, row["media_row_id"])
    neighbours_block = format_neighbours(nbr_rows)

    raw, meta = call_strategic(
        row["ocr_text"] or "",
        row["kb_value_class"] or "", row["kb_value_score"] or 0,
        row["decision_tags"] or "", row["rationale"] or "",
        neighbours_block,
    )
    if meta.get("_error") and not raw:
        insert_result(conn, row["media_row_id"], {}, raw, meta)
        return "spawn_err"

    parsed = parse_response(raw)
    insert_result(conn, row["media_row_id"], parsed, raw, meta)
    if parsed.get("commercial_action"):
        return "ok"
    return "parse_err"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--media-id", type=int, action="append", default=None,
                        metavar="ROW_ID",
                        help="redo specific media row_id (repeat for multiple). "
                             "Bypasses kb_admit gate + threshold + existing-brief "
                             "filter + daily budget. Boss 5/8 Commander redo entry.")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    if args.media_id:
        rows = fetch_by_ids(conn, args.media_id)
        log(f"start REDO provider={selected_provider()} "
            f"model={MODEL_ALIAS} ({MODEL_FULL_ID}) "
            f"codex_model={codex_model_for_tier('stage3')} "
            f"prompt_hash={PROMPT_HASH} target_ids={args.media_id} "
            f"fetched={len(rows)} dry_run={args.dry_run} (bypassing daily budget)")
        if args.dry_run or not rows:
            return
    else:
        pending = total_pending(conn)
        used = today_count(conn)
        remaining = max(0, DAILY_BUDGET - used)
        cap = min(args.limit, remaining)
        log(f"start provider={selected_provider()} "
            f"model={MODEL_ALIAS} ({MODEL_FULL_ID}) "
            f"codex_model={codex_model_for_tier('stage3')} prompt_hash={PROMPT_HASH} "
            f"threshold={HIGH_VALUE_THRESHOLD} pending={pending} used_today={used} "
            f"budget={DAILY_BUDGET} cap={cap} dry_run={args.dry_run}")
        if args.dry_run or pending == 0 or cap == 0:
            return
        rows = fetch_pending(conn, cap)

    log(f"processing batch_size={len(rows)}")

    stats = {"ok": 0, "parse_err": 0, "missing": 0, "spawn_err": 0}
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        result = process_one(conn, row)
        stats[result] = stats.get(result, 0) + 1
        elapsed = time.time() - t0
        log(f"  row {i}/{len(rows)} media_row_id={row['media_row_id']} -> "
            f"{result} (elapsed={elapsed:.1f}s)")

    elapsed = time.time() - t0
    n = max(1, sum(stats.values()))
    log(f"done {stats} elapsed={elapsed:.1f}s avg={elapsed/n:.2f}s/req")
    conn.close()


if __name__ == "__main__":
    main()
