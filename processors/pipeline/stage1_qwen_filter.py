"""Stage 1 — Qwen2.5-VL 7B local noise filter (CLAUDE.md §2.1).

Goal: cheaply reject ~75% of media as commercial-intel noise so Stage 2
(Haiku) and Stage 3 (Sonnet) only see signal candidates.

Input:  media row (photo) not yet in media_signal_filter. Stored OCR text is
        optional context, not a gate.
Output: media_signal_filter row with verdict (signal/noise/error), tags,
        confidence, rationale.

Cron:   */30 min, batch 200.

Cost:   ~5-8s per image on 5070 Ti (Qwen2.5-VL 7B INT4 via Ollama).
        Local — no API quota consumed.

Why this comes BEFORE Stage 2:
  - Stage 2 is Haiku via OAuth Bearer. Daily Pro plan budget limited.
  - Skipping noise here saves ~75% of Haiku spend without losing signal.
  - Stage 1 is binary signal/noise, NOT detailed tagging — that's Stage 2.

Prompt versioning:
  Each row records prompt_hash = sha256[:12](PROMPT). When boss / audit
  loop edits PROMPT_V*, new rows get new hash so audit trends stay
  attributable. Old rows are NOT re-run unless triggered.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from db.connection import get_connection
from db.schema import init_db

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("STAGE1_QWEN_MODEL", "qwen2.5vl:7b")
KEEP_ALIVE = os.environ.get("STAGE1_OLLAMA_KEEP_ALIVE", "30s")
PER_IMG_TIMEOUT_S = int(os.environ.get("STAGE1_PER_IMG_TIMEOUT_S", "120"))
MIN_FILE_SIZE = int(os.environ.get("STAGE1_MIN_BYTES", "30000"))
DEFAULT_BATCH = int(os.environ.get("STAGE1_BATCH", "200"))
SIGNAL_MIN_CONFIDENCE = float(os.environ.get("STAGE1_SIGNAL_MIN_CONFIDENCE", "0.68"))
SIGNAL_MIN_CONFIDENCE_WITH_EVIDENCE = float(
    os.environ.get("STAGE1_SIGNAL_MIN_CONFIDENCE_WITH_EVIDENCE", "0.55")
)

# === INSTANCE DOMAIN KEYWORDS (customize per instance — see instances/_TEMPLATE/INSTANCE.md) ===
# Generic grey-market-commerce evidence cues. Per-instance deployments append
# native-language lottery / belief / gambling terms and the actual grey-operator
# brand names observed in-market. Placeholders below are framework-neutral.
DOMAIN_KEYWORD_RE = re.compile(
    r"(lottery|lucky.?number|amulet|fortune.?telling|folk.?belief|"
    r"baccarat|casino|slot|sportsbook|betting|gambling|free.?credit|"
    r"examplebet|betbrand-b|betbrand-c|examplebrand|slotbrand-a|"
    r"govwallet|epayment|"
    r"t\.me/|line\.me/)",
    re.IGNORECASE,
)

PROMPT_V1 = """You are a binary signal filter for the Blacksite intel pipeline.

# === INSTANCE DOMAIN CONTEXT (customize per instance — see instances/_TEMPLATE/INSTANCE.md) ===
# The block below is a GENERIC grey-market-commerce example. For a live deployment,
# replace it with the active instance's domain definition: the country's lottery
# ecosystem terms, its folk-belief economy cues, the grey-market gambling operators
# observed in-market, and the relevant sports/KOL ecosystem. Keep the JSON schema,
# tag vocabulary, and confidence rules unchanged; only swap the domain specifics.

Domain (generic example): national lottery (NatLottery, ExampleGovWallet payouts) +
folk-belief economy (lucky numbers, amulets, fortune-telling) + grey-market
gambling (online casinos, sportsbook funnels, slot apps) + sports KOL ecosystem.

Decide if THIS image carries commercially relevant intel signal for the client brand's
strategy, OR is pure noise (decoration, off-topic chat, generic ad
without target relevance).

Concrete evidence includes at least one of:
- lottery / folk-belief cues: lottery draw numbers, lucky-number charts,
  dream-interpretation tables, amulets, fortune-telling, "hot numbers",
  online-lottery purchase prompts.
- grey operator cues: named gambling brands (e.g. examplebet, slotbrand-a,
  betbrand-b), baccarat, slots, sportsbook odds, casino credits, prediction tips.
- funnel/payment cues: t.me invite, LINE/chat handoff, QR code, e-wallet /
  gov-wallet transfer, deposit/withdraw slip, bonus/free-credit offer, payout proof.
- sports/KOL/regulatory cues: athlete or KOL endorsement, the sports regulator /
  NatLottery, a police/regulator article tied to lottery/gambling/sports commerce.

OCR text (already extracted by upstream Qwen pass):
<ocr>
{ocr_text}
</ocr>

Output ONE JSON object on the LAST line, no markdown fences, no preface:

{{
  "verdict": "signal" | "noise",
  "confidence": <float 0.0-1.0>,
  "tags": [<zero or more from the vocab below>],
  "rationale": "<<=80 chars: what you saw and why>"
}}

Tag vocabulary:
  signal-leaning: lottery_promotion, gambling_url, folk-belief_amulet,
                  sports_betting, kol_promo, funnel_invite, scam_template,
                  payment_evidence, regulatory_news, competitor_brand
  noise-leaning:  casual_chat, decoration, ad_template, unrelated_brand,
                  off_topic, garbled

Rules:
  - "signal" REQUIRES at least ONE concrete domain cue visible in the image
    or OCR: named operator brand (examplebet / slotbrand-a / betbrand-b /
    etc.), a lottery number string or lottery-formula keyword, an amulet or
    folk-belief visual/text, KOL face + product promotion, funnel/invite link
    (t.me / @handle / chat app), scam-bait template (winning testimony /
    urgency timer / adult bait), or payment proof (QR / e-wallet slip /
    deposit slip). No concrete cue → noise.
  - "noise": generic local text, lifestyle ads, personal selfies, food, job ads,
    local news, decoration, sticker reactions — unless a domain cue
    from the list above is clearly present.
  - Confidence calibration:
    * >= 0.80 → cue clearly visible → use verdict as-is.
    * 0.70–0.79 → partial evidence → "signal" ONLY when BOTH conditions hold:
      (a) a NAMED entity (operator brand text, @handle, t.me/domain URL,
      lottery keyword cluster) is clearly legible, AND (b) at least one
      additional independent domain marker is present (payment QR/slip, promo
      amount, lottery keyword, funnel URL). Single vague marker without
      a legible named entity → "noise".
    * < 0.70 → ambiguous or generic promotional aesthetic → default "noise".
      Do NOT signal on local promotional colour scheme / layout alone.
  - Two-cue rule (confidence 0.70–0.79): both a legible named entity AND a
    second independent domain marker must be visible. Local promotional styling
    alone (colours, layout, generic local script) is NOT a domain cue.
  - Empty tags [] is allowed when nothing fits.
  - BANK STATEMENT ANTI-HALLUCINATION: If OCR text reads as a bank financial
    record (withdrawal history, transaction id, account balance, withdrawal
    status, a bank name) → verdict MUST be "noise" even if numbers visually
    resemble lottery strings. Bank transaction numbers ARE NOT lottery cues.
  - LEGITIMATE SPORTS CARD PRODUCTS: Licensed sports collection card products
    (set releases, membership campaigns, official sports licensees — NOT
    grey-market operators) → verdict "noise". Distinguishing cue: if the primary
    product is a physical/digital sports card or sticker collection (not gambling
    chips, not casino credits, not sportsbook bets) → noise. Only escalate to
    "signal" if an explicit grey-market gambling operator name co-appears.
  - FUNNEL/PAYMENT STANDALONE RULE: QR code, chat-app invitation, or @handle
    ALONE (without any lottery/gambling/operator keyword in the same image) →
    ambiguous → treat as noise unless confidence >= 0.80 AND a named operator
    or lottery entity is clearly legible alongside.
  - FREE-CREDIT / SIGNUP-BONUS RULE (overrides the two-cue and funnel-standalone
    rules above): an explicit free-credit or new-member signup-bonus offer is a
    STANDALONE concrete grey-market gambling acquisition cue → verdict "signal"
    at confidence >= 0.80, even with NO named operator brand visible. Trigger
    phrases (any one suffices): "free credit", "new-member bonus", "signup
    bonus", "no deposit + credit", engagement-free credit bait ("no comment /
    no share needed"). In most markets these templates are gambling-specific
    (legitimate retail promos do not give "free credit"); do NOT downgrade them
    to ad_template/noise. Tag funnel_invite + scam_template. (Bank-statement and
    licensed-sports-card anti-FP rules above still take precedence when those
    exact conditions hold.)
"""

PROMPT_HASH = hashlib.sha256(PROMPT_V1.encode("utf-8")).hexdigest()[:12]

JSON_RE = re.compile(r"\{[\s\S]*?\"verdict\"[\s\S]*?\}", re.MULTILINE)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [stage1] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"stage1_qwen_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates = [fenced[-1]] if fenced else JSON_RE.findall(raw)
    if not candidates:
        # last-resort balanced-brace scan
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
        candidates = [b for b in blocks if "verdict" in b]
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


def call_ollama(img_bytes: bytes, ocr_text: str, model: str) -> str:
    import requests
    b64 = base64.b64encode(img_bytes).decode("ascii")
    snippet = (ocr_text or "")[:2000]
    prompt = PROMPT_V1.format(ocr_text=snippet)
    resp = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": 0.0, "num_predict": 512},
        },
        timeout=PER_IMG_TIMEOUT_S,
    )
    resp.raise_for_status()
    return (resp.json().get("response") or "").strip()


def fetch_pending(conn, limit: int) -> list:
    return conn.execute(
        """SELECT m.row_id, m.file_path, m.file_size, m.ocr_text
             FROM media m
        LEFT JOIN media_signal_filter s ON s.media_row_id = m.row_id
            WHERE m.media_kind = 'photo'
              AND s.media_row_id IS NULL
              AND (m.file_size IS NULL OR m.file_size >= ?)
         ORDER BY m.row_id ASC
            LIMIT ?""",
        (MIN_FILE_SIZE, limit),
    ).fetchall()


def fetch_by_ids(conn, media_row_ids: list[int]) -> list:
    """Boss 5/8: redo specific media rows by row_id. Idempotent via
    INSERT OR REPLACE in insert_result(). Bypasses the LEFT JOIN filter
    so already-processed rows can be re-evaluated."""
    if not media_row_ids:
        return []
    placeholders = ",".join("?" * len(media_row_ids))
    return conn.execute(
        f"""SELECT m.row_id, m.file_path, m.file_size, m.ocr_text
              FROM media m
             WHERE m.row_id IN ({placeholders})
               AND m.media_kind = 'photo'""",
        tuple(media_row_ids),
    ).fetchall()


def total_pending(conn) -> int:
    r = conn.execute(
        """SELECT COUNT(*)
             FROM media m
        LEFT JOIN media_signal_filter s ON s.media_row_id = m.row_id
            WHERE m.media_kind = 'photo'
              AND s.media_row_id IS NULL
              AND (m.file_size IS NULL OR m.file_size >= ?)""",
        (MIN_FILE_SIZE,),
    ).fetchone()
    return r[0] if r else 0


def insert_result(conn, media_row_id: int, verdict: str, qwen_tags: list | None,
                  confidence: float | None, raw_response: str, model: str,
                  duration_ms: int) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO media_signal_filter
           (media_row_id, verdict, qwen_tags, confidence, raw_response,
            model_used, prompt_hash, duration_ms, processed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            media_row_id,
            verdict,
            json.dumps(qwen_tags, ensure_ascii=False) if qwen_tags is not None else None,
            confidence,
            raw_response[:8000] if raw_response else None,
            model,
            PROMPT_HASH,
            duration_ms,
            now_iso(),
        ),
    )
    conn.commit()


def process_one(conn, row, model: str) -> str:
    abs_path = ROOT / row["file_path"]
    if not abs_path.exists():
        insert_result(conn, row["row_id"], "error", None, None,
                      "[file_missing]", model, 0)
        return "missing"
    try:
        img_bytes = abs_path.read_bytes()
    except Exception as e:
        insert_result(conn, row["row_id"], "error", None, None,
                      f"[read_error: {type(e).__name__}: {e}]", model, 0)
        return "read_err"

    t0 = time.time()
    try:
        raw = call_ollama(img_bytes, row["ocr_text"] or "", model)
    except Exception as e:
        msg = str(e).lower()
        is_oom = "out of memory" in msg or "cuda oom" in msg
        if is_oom:
            log(f"VRAM OOM row={row['row_id']} — abort batch")
            return "oom"
        dur = int((time.time() - t0) * 1000)
        insert_result(conn, row["row_id"], "error", None, None,
                      f"[ollama_error: {type(e).__name__}: {str(e)[:200]}]",
                      model, dur)
        return "qwen_err"
    dur = int((time.time() - t0) * 1000)

    parsed = parse_json(raw)
    if not parsed or "verdict" not in parsed:
        insert_result(conn, row["row_id"], "error", None, None,
                      raw[:4000], model, dur)
        return "parse_err"

    verdict = str(parsed.get("verdict", "")).lower().strip()
    if verdict not in ("signal", "noise"):
        verdict = "error"
    tags = parsed.get("tags") if isinstance(parsed.get("tags"), list) else None
    conf = parsed.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except Exception:
        conf = None

    if verdict == "signal":
        has_evidence = bool(DOMAIN_KEYWORD_RE.search(row["ocr_text"] or ""))
        min_conf = (
            SIGNAL_MIN_CONFIDENCE_WITH_EVIDENCE
            if has_evidence
            else SIGNAL_MIN_CONFIDENCE
        )
        if conf is not None and conf < min_conf:
            post_filter = (
                f"signal downgraded: confidence {conf:.2f} < {min_conf:.2f}"
            )
            raw = raw[:3600] + "\n" + json.dumps(
                {"post_filter": post_filter}, ensure_ascii=False
            )
            verdict = "noise"
    insert_result(conn, row["row_id"], verdict, tags, conf, raw[:4000], model, dur)
    return verdict


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH,
                        help="cap rows processed this run")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--media-id", type=int, action="append", default=None,
                        metavar="ROW_ID",
                        help="redo specific media row_id (repeat for multiple). "
                             "Bypasses pending filter; INSERT OR REPLACE handles "
                             "idempotency. Boss 5/8 Commander redo entry.")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    if args.media_id:
        rows = fetch_by_ids(conn, args.media_id)
        log(f"start REDO model={args.model} prompt_hash={PROMPT_HASH} "
            f"target_ids={args.media_id} fetched={len(rows)} "
            f"dry_run={args.dry_run}")
        if args.dry_run or not rows:
            return
    else:
        pending = total_pending(conn)
        log(f"start model={args.model} prompt_hash={PROMPT_HASH} "
            f"pending={pending} limit={args.limit} dry_run={args.dry_run}")
        if args.dry_run or pending == 0:
            return
        rows = fetch_pending(conn, args.limit)

    log(f"processing batch_size={len(rows)}")

    stats = {"signal": 0, "noise": 0, "error": 0, "missing": 0,
             "read_err": 0, "qwen_err": 0, "parse_err": 0, "oom": 0}
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        result = process_one(conn, row, args.model)
        stats[result] = stats.get(result, 0) + 1
        if result == "oom":
            log("aborting batch on OOM")
            break
        if i % 25 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            log(f"  progress {i}/{len(rows)} {stats} rate={rate:.2f} img/s")

    elapsed = time.time() - t0
    n = max(1, sum(stats.values()))
    log(f"done {stats} elapsed={elapsed:.1f}s avg={elapsed/n:.2f}s/img")
    conn.close()

    # Boss directive 2026-05-08: explicit unload to free VRAM (Ollama
    # keep_alive=30s is passive and not trustworthy for cron-driven flows).
    try:
        from processors.pipeline._qwen_unload import unload_qwen
        unload_qwen(args.model, log_fn=log)
    except Exception as e:
        log(f"unload_qwen failed (non-fatal): {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
