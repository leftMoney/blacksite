"""Stage 2 — Haiku 4.5 OAuth Bearer structured precision (CLAUDE.md §2.1).

Input:  media_signal_filter rows where verdict='signal' AND not yet in
        media_kb_decision.
Output: media_kb_decision row with full structured KB judgment:
        kb_admit, kb_value_class, kb_value_score (0-100), decision_tags,
        rationale.

Cron:   */30 min, batch 100. Runs AFTER Stage 1 in cron sequence.

Why Haiku, not Sonnet/Opus:
  - OAuth Bearer + api.anthropic.com is hard-gated to Haiku only (5/8
    finding; Sonnet/Opus return 429 even on first-shot, NOT quota).
  - Sonnet/Opus go through claude.exe agent path (Pro plan quota), which is
    Stage 3 only.
  - Haiku gives 95% of structured-judgment quality at <5% of Opus cost.

Cost guard:
  STAGE2_DAILY_BUDGET (default 2000) caps rows/day. Cron stops when budget
  exhausted; resumes next UTC+7 day.
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
import urllib.error
import urllib.request
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
from processors.claude_auth import current_oauth_access_token, is_claude_auth_error
from processors.prompt_sanitize import sanitize_untrusted
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
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
CONTROL_DIR = RUNTIME_DIR / "control"
PAUSE_FLAG = CONTROL_DIR / "pipeline_stage2_haiku.paused"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))

# Model id resolution: explicit STAGE2_MODEL env > config/llm_providers.yaml
# `claude.fast` tier. Haiku is the only model the OAuth Bearer API path can
# call (Sonnet/Opus return 429 on that endpoint per CLAUDE.md §2.1).
MODEL_ID = os.environ.get("STAGE2_MODEL") or llm_profiles.tier_model("claude", "fast")
DAILY_BUDGET = int(os.environ.get("STAGE2_DAILY_BUDGET", "2000"))
DEFAULT_BATCH = int(os.environ.get("STAGE2_BATCH", "100"))
PER_REQ_TIMEOUT_S = int(os.environ.get("STAGE2_TIMEOUT_S", "120"))
MAX_RUNTIME_SEC_DEFAULT = int(os.environ.get("STAGE2_MAX_RUNTIME_SEC", "0"))
MAX_TOKENS = int(os.environ.get("STAGE2_MAX_TOKENS", "800"))

PROMPT_V1 = """You are reviewing one image flagged as a "signal" candidate by Stage 1
(Qwen2.5-VL noise filter). Your job is the structured precision call: decide
whether it ENTERS the Blacksite library and assign value class + tags.

# === INSTANCE BRAND CONTEXT (customize per instance — see instances/_TEMPLATE/INSTANCE.md) ===
# Replace the block below with the active instance's client brand legitimacy facts.
# Per-instance, state: (1) what legal/licensed product the client brand sells,
# (2) under which legal framework any prize-draw / promotion mechanism runs, and
# (3) why TA overlap with the grey-market is a marketing fact, not a legal one.

WHO IS THE CLIENT BRAND (<INSTANCE_BRAND>) — three legal-status facts (generic example):
1. The client sells a licensed, tangible commercial product (e.g. sports
   collection cards, digital + physical) — NOT a gambling instrument.
2. Any prize-draw mechanism runs under the relevant trade-promotion / lawful
   prize-draw framework (non-cash prizes, manual draw — explicitly outside the
   target country's gambling statutes).
3. TA overlap with the grey-market is a marketing fact, not a legal one —
   it does not make the client brand illegal (analogous to a legal bank vs.
   the scam targets that impersonate it).

The client brand is on the LEGAL side; Blacksite is its competitive intelligence
platform monitoring grey-market COMPETITORS (illegal online casinos,
unlicensed sportsbooks, scam funnels) so the client brand can win audience back with
its legal product.

CONTEXT:
- Library admission criterion: a card/document enters only if it
  contributes commercial intel value for the client brand's strategic decisions
  (per CLAUDE.md §1 north star: build advantage-creating commercial
  strategy).
- Stage 1 already filtered ~75% noise; reject only true false-positives here.

Stage 1 verdict: signal (confidence may be noisy — re-judge from scratch).

Stored OCR text:
<stored_ocr>
{ocr_text}
</stored_ocr>

Output ONE JSON object on the LAST line, no markdown fences, no preface:

{{
  "kb_admit": <true|false>,
  "kb_value_class": "<high|medium|low|noise>",
  "kb_value_score": <int 0-100>,
  "decision_tags": "<comma-list from: lottery, folk-belief, gambling, scam_template, kol, sports, regulatory, competitor, bot_pump_noise, payment, kol_persona, off_topic, advertising, funnel_invite, athlete_named, team_named, league_named, sports_event, sports_prediction, sports_collectible, fan_club, fan_economy, sports_kol, instance_endorsement_funnel, sports_betting_meta>",
  "rationale": "<<=140 chars: what you saw + commercial relevance>"
}}

TAG DEFINITIONS (OCR examples below are generic; per instance, swap in the target
country's native-language keywords and the actual grey-operator brand names):
- lottery: lottery numbers, NatLottery/ExampleGovWallet, prediction content, draw timing.
  USE: lottery-number text / draw-date graphic / ExampleGovWallet app. SKIP: generic cash/number without lottery keyword.
- folk-belief: amulets, charms, lucky items, dream/fortune interpretation.
  USE: amulet photo / folk-belief text / dream-number chart. SKIP: generic religious imagery without commercial fortune angle.
- gambling: casino, slots, sportsbook, betting operator, grey-market offer.
  USE: baccarat/slots/sportsbook text / examplebet betbrand-b betbrand-c brand text / slot-app UI.
  OCR examples that CONFIRM: "register examplebet", "play baccarat", "online sports betting", "slotbrand-a slots".
  SKIP: general retail credit-card ad / bank promo / unrelated finance — these are NOT gambling.
  OCR examples that REJECT: "credit card", "personal loan", "insurance", "cash loan" without any casino/slot/operator term.
- scam_template: fake payout proof, adult bait, urgency/free-credit bait,
  testimonial bait, or repeated acquisition creative template.
  USE: "real withdrawal" screenshot / countdown timer / adult image hook / fabricated winning amount.
  OCR examples that CONFIRM: "guaranteed withdrawal", "100% bonus", "free credit", urgency countdown, "verified win".
  SKIP: clean brand banner without fake-payout/urgency/adult marker.
  OCR examples that REJECT: standard brand logo / team photo / news screenshot without urgency/fake-win markers.
- kol: named/public creator, athlete, streamer, or influencer promotion.
  USE: ExampleAthlete photo + brand link / named streamer + product CTA. SKIP: generic person or silhouette → use kol_persona instead.
- sports: combat sports, football, esports, sports content, or sports audience hook.
  USE: fight graphic / football league logo / esports tournament.
  OCR examples that CONFIRM: "muay" / combat-sport name, national-league name, "football", "esport", "match odds", "predict result".
  SKIP: casino/slot promo with zero athletic content — operator sponsoring sports ≠ sports tag unless sports imagery is present.
  OCR examples that REJECT: casino-only banner that mentions a team name only as sponsor text.
- regulatory: sports-regulator / NatLottery / police / court / regulator news tied to gambling/lottery/sports.
- competitor: identifiable operator/brand/domain/account competing for the client brand's TA.
  USE: literal "examplebet" / "examplebrand" / "examplebrand.com" / "@examplebet" text visible.
  OCR examples that CONFIRM: "examplebet", "examplebrand.bet", "betbrand-b", "examplebrand.com", "@betbrand-c".
  SKIP: similar visual style / similar colour scheme without literal brand-text fragment — visual similarity is NOT brand evidence.
  OCR examples that REJECT: any promo with no legible operator name/domain/handle.
- bot_pump_noise: repeated pump/rebroadcast with weak original value but a visible
  operator or funnel marker.
- payment: e-wallet, gov-wallet, bank QR/slip, deposit/withdraw/payment rail.
  USE: visible payment QR / e-wallet logo / bank slip screenshot. SKIP: generic numeric amount without payment rail fragment.
- kol_persona: persona/archetype style useful for KOL targeting, even if no named KOL.
  USE: lifestyle woman with lucky charm / young male gambler archetype. SKIP: when an actual named person is identifiable → use kol instead.
- off_topic: food, jobs, generic news, personal chat, unrelated local content.
- advertising: generic ad mechanics without enough domain specificity.
- funnel_invite: t.me/LINE/@handle/link that moves users into a channel, bot, OA, or group.

# ─── Sports / fan-economy dimensions (the client brand is a sports collection-card
# product; intel must surface athlete / fan / event / collectible signal, not just
# grey-market gambling funnel). All require native-script OR romanised local entity
# evidence in OCR; visual-only inference is not enough. Per instance, substitute the
# target country's real athlete / team / league names for the placeholders below. ─
- athlete_named: identifiable named athlete (face + name text / handle visible).
  USE: ExampleAthlete, ExampleAthlete2, named UFC/MMA/eSport athlete + photo.
  SKIP: generic athletic body / silhouette without legible name → use kol_persona.
- team_named: identifiable football / combat-sport gym / esports team.
  USE: "Example FC United", "Bacon Time", "MiTH", official team logo + name.
  SKIP: jersey colour alone, generic team-like graphic without legible name.
- league_named: identifiable league/tournament name.
  USE: "Example League 1", "AFC Champions", "Premier League", regional esports league.
  SKIP: "football" / "esport" general — too broad.
- sports_event: specific scheduled game / match / tournament instance.
  USE: "Team A vs Team B 2 June", fight card poster with date+opponents.
  SKIP: generic "watch tonight" without legible teams+date.
- sports_prediction: pre-match analysis, expert pick, edge discussion.
  USE: "tonight's match prediction", expert preview thread, stat-based prediction.
  SKIP: post-match recap, pure highlight — those are sports not sports_prediction.
  HARD LINE: predictions tied to a gambling operator brand → also tag gambling.
  Standalone fan-side prediction (no operator) → sports_prediction only.
- sports_collectible: trading cards / jerseys / signed merch / fan-trading content.
  USE: card pack opening / sticker album / signed jersey / NFT athlete card visible.
  Note: this is THE client brand's product category — direct commercial alignment signal.
- fan_club: identifiable fan community / fan page / fan group.
  USE: "@ExampleAthlete_fanclub", "Example NBA Fans", fan-page header / group name visible.
  SKIP: lone fan post not part of an identifiable group.
- fan_economy: fan-driven commerce — merch sales, fan votes, supporter perks,
  paid fan tier, donation drive, group buy.
  USE: "pre-order" jersey + team, fan-club merch shop, Patreon-style supporter tier.
  SKIP: any cash-flow that points to a gambling operator → that's gambling.
- sports_kol: sports content creator (commentator / ex-pro analyst / fan-creator).
  USE: face + sports-show graphic / podcast cover / analyst handle.
  Subset of `kol` — apply BOTH when both apply.
- instance_endorsement_funnel: athlete-led promotion that drives toward a
  collectible / membership / digital-card style product (NOT a gambling deposit).
  USE: athlete photo + "limited card drop" / "fan token" / "membership" CTA.
  Direct the client brand commercial-pattern signal — high library priority.
- sports_betting_meta: sports-related content tied to gambling operator brands
  (odds boards, "match odds", betting-app screenshots, betting expert).
  ⚠ Grey-adjacent — kept for noise-labelling and competitor mapping; NOT a
  the client brand-positive signal. ALSO tag `gambling` whenever this fires.

Use only tags supported by visible evidence or OCR. Do not tag by vibes.
Tag selection rule: each tag MUST correspond to a visible evidence fragment.
In your rationale, cite that fragment in <=8 chars (e.g. "examplebet.com" /
a lottery-number keyword / "t.me/xxxx"). Do not tag by inference alone.

TAG SELF-CHECK (run mentally BEFORE emitting decision_tags) — for each
candidate tag ask: "can I point to a specific visible fragment (OCR text,
visible logo text, domain, handle, layout marker) that justifies this tag?"
If no fragment → DROP the tag. Common Haiku errors observed in audit:
  * gambling / competitor on a recruitment / retail-credit-card / generic
    news ad that has zero operator / casino / sportsbook fragment → DROP.
  * sports on a slot/casino promo with no athletic content visible → DROP.
  * kol_persona on a stock banner with no actual human archetype → DROP.
  * scam_template on a clean brand banner with no fake-payout / urgency /
    adult-bait / fake-testimonial marker → DROP.
  * payment without a visible e-wallet / gov-wallet / bank slip / QR fragment → DROP.
Over-tagging is the dominant Haiku failure mode. Tighter is better.
If after the self-check fewer than 1 evidence-grounded tag survives,
ALSO drop kb_admit to false (no grounded tags ⇒ no real client-brand signal).

BRAND IDENTIFICATION HARD RULE:
- Do NOT attribute content to any specific brand (the client brand or any
  competitor) unless you can cite a LITERAL evidence fragment: brand name text,
  domain, logo text, or explicit handle visible in the image or OCR.
- Indirect signals (has a chat-app OA, has QR code, uses similar colours, KOL style)
  are NOT brand evidence — they are shared patterns across hundreds of operators.
- If you cannot cite a literal brand fragment → rationale must say "brand
  unconfirmed" and do NOT name the brand in the rationale or title.

# === INSTANCE BRAND FUZZY TEXT DETECTION (customize per instance — see instances/_TEMPLATE/INSTANCE.md) ===
CLIENT-BRAND FUZZY TEXT DETECTION (logo not required — text match IS brand evidence):
- The following text patterns visible in image OCR or caption COUNT as confirmed
  client-brand evidence and MUST be cited in rationale (substitute the active
  instance's brand spellings, including native-script and romanised variants):
  * "<INSTANCE_BRAND>" in any of its name / spacing / native-script variants
    (any case, any spacing)
  * "the client brand" as standalone token (not part of another word)
  * the sports regulator's acronym as a standalone token when context is
    sports-licensing / sports-card (it is the client brand's licensing body)
- Partial/fuzzy matches (e.g. a truncated or hyphenated brand fragment without
  the full name) count as PROBABLE the client brand — tag instance_brand with note
  "partial match: <fragment>" and score accordingly (do not hard-confirm brand,
  but do not ignore either).

ADMISSION HEURISTICS:
- ADMIT (kb_value_score >= 40):
  * Gambling/lottery operators in any form: full brand name, abbreviation,
    bio link, or secondary KOL promotion (no full name required to ADMIT)
  * folk-belief dream interpretation / lucky number prediction / amulet offer even
    without explicit NatLottery / ExampleGovWallet reference
  * Funnel @handle or t.me invite even without accompanying content description
  * Scam funnel templates (adult bait, fake winning testimony, urgency triggers)
  * Sports KOL signals (athlete name + product mention)
  * Regulatory / police news screenshots
  * Payment infrastructure (e-wallet flows, gov-wallet specifics)
  * Welcome-bonus / free-credit template creatives on local promotional graphics
    EVEN WHEN BRAND IS UNIDENTIFIABLE. Markers: "free credit"
    / numeric promo amount ("100" / "150" / "200") + "for new members"
    / fake-payout numeric display / app-store
    badge / numeric deposit-bonus tier table (e.g. "199→999", "100→300").
    >=2 such markers + local promotional graphic styling ⇒ ADMIT at
    kb_value_score 45-55 with tags gambling, scam_template, advertising and
    rationale "brand unconfirmed; grey-market welcome-bonus template".
    Brand-unconfirmed is EXPECTED for these — they are anonymous operator
    acquisition funnel creatives by design. DO NOT reject as off_topic /
    generic-retail when the welcome-bonus + numeric-promo pattern is visible.
- BIAS TOWARD ADMIT for borderline cases (minimize false negatives at Stage 2):
  * Any image with a "register" CTA + numeric promo amount + local promotional
    styling → ADMIT at 40-45 even without legible brand name.
  * Partial operator mention (truncated domain, first word of brand only) → ADMIT at 40.
  * Funnel sequence visual (step-by-step flow / QR→chat→deposit arrow chain)
    even without brand → ADMIT at 42-48, tags funnel_invite + advertising.
  * When equally uncertain between admit/reject: choose ADMIT. Stage 3 Sonnet
    is the quality gate; Stage 2 under-admission starves Stage 3 of signal context.
  * Visible grey-market app UI (slot machine reels, casino lobby tiles,
    sportsbook odds table, chat-app OA registration flow, chat invite with
    deposit instructions) → ADMIT at 40+ even without legible operator brand text.
- REJECT (kb_value_score < 40):
  * Bot-pump pure forwards (sticker / one-emoji / decorative-only)
  * Off-topic (food pics, memes, personal selfies unrelated to TA)
  * Garbled OCR with no recoverable signal
  * Bank transaction records (withdrawal-history / bank statement with account
    numbers and dates but ZERO gambling operator name → off_topic at score 5)
- TAG bot_pump_noise IF: image is part of mass-forward rebroadcast pump but
  still has a recognizable operator name -> kb_admit=true at low value (40-50).
- LICENSED SPORTS CARD COMPETITORS (haiku_under_admit fix):
  * Other licensed sports collection card products (non-client-brand, non-grey-market)
    competing for the same local sports fan TA → ADMIT at kb_value_score 40-48,
    tags: competitor, advertising.
  * Recognition pattern: official sports licensed product + member-signup CTA
    + sports card / sticker / collection language + NO gambling
    operator name. These are the client brand's direct market competitors (same TA, same
    product category) even though they are legal. Tag competitor=true.
  * Examples that qualify: sports card collection announcements from other
    regulator-adjacent brands, official football club card releases, trading-card
    sets targeting local sports fans.
  * Do NOT apply to grey-market operators using "sports card" as cover — those
    still get the gambling/scam_template path above.

SCORE ANCHORS:
- 70–85 (first-occurrence): new operator / new funnel / new KOL pivot
  seen for the FIRST TIME (novel competitor brand, fresh @handle, KOL switching
  operators, new regulatory ruling, new police raid on grey-market venue).
  Stage 3 (Sonnet strategic) escalates these.
- 55–69: Known operator / funnel repeating without strong stack — single-marker
  presence (brand-only OR scam_template-only) OR a known operator re-pumping
  the same creative with only a cosmetic change.
- 70–85 (ALSO triggered by stacking): if a single creative shows >=3 stacked
  markers from {brand_fragment, scam_template, funnel_invite, payment_rail,
  kol_persona, fake_payout_proof, app_store_funnel}, score 70+ even for a
  known operator — each stacked iteration teaches the client brand about playbook evolution.
- 40–54: Bot-pump or known replay but carries recognizable brand / payment
  evidence (useful for noise-labeling and competitor frequency tracking).
- < 40:  Reject — no recoverable commercial signal.
- SCORE ELEVATION TRIGGERS (guard against under-scoring):
  * New @handle / t.me link / chat-app OA not previously in context as "known" →
    treat as first-occurrence → 70+.
  * gambling + funnel_invite + scam_template all present in one creative → 70+
    regardless of brand familiarity. ALL THREE must have explicit OCR/visual
    evidence — do not infer missing tags from visual style alone.
  * Regulatory / enforcement news (police raid, court ruling, new legislation) → 75+.
  * KOL visibly switching from one operator brand to another → 72.
  * NEVER score < 55 for an image with a legible operator brand name AND a
    funnel CTA (register/deposit/bonus link) — brand + CTA = hard floor 55.
- DUPLICATE / REPEAT CREATIVE CEILING (guard against score inflation):
  * If the rationale acknowledges this is the SAME operator brand AND the SAME
    creative template/layout seen previously (same bonus amount, same funnel
    handle, same visual structure) → cap score at 55 MAX. Novel playbook
    evolution (new bonus tier, new funnel handle, new KOL face) remains 70+.
  * Batch rebroadcasts of identical grey-market acquisition creatives are
    useful for frequency tracking (hence admit at 40-55) but NOT for strategic
    intelligence (do not score 70+ on repetition alone).
  * Scoring 70+ requires: at least ONE element that is observably different
    from known variants — new handle, new amount structure, new KOL, new domain.
"""

PROMPT_HASH = hashlib.sha256(PROMPT_V1.encode("utf-8")).hexdigest()[:12]

JSON_RE = re.compile(r"\{[\s\S]*?\"kb_admit\"[\s\S]*?\}", re.MULTILINE)

STAGE2_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kb_admit": {"type": "boolean"},
        "kb_value_class": {"type": "string", "enum": ["high", "medium", "low", "noise"]},
        "kb_value_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "decision_tags": {"type": "string"},
        "rationale": {"type": "string", "maxLength": 220},
    },
    "required": ["kb_admit", "kb_value_class", "kb_value_score", "decision_tags", "rationale"],
}


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [stage2] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"stage2_haiku_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def stage2_pause_reason() -> str | None:
    env_pause = os.environ.get("BLACKSITE_STAGE2_HAIKU_PAUSED", "").strip().lower()
    if env_pause in {"1", "true", "yes", "on"}:
        return f"BLACKSITE_STAGE2_HAIKU_PAUSED={env_pause}"
    if PAUSE_FLAG.exists():
        try:
            text = PAUSE_FLAG.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as e:
            text = f"pause flag unreadable: {type(e).__name__}"
        return text or f"pause flag present: {PAUSE_FLAG}"
    return None


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
        candidates = [b for b in blocks if "kb_admit" in b]
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


def call_haiku(token: str, model: str, ocr_text: str,
               image_bytes: bytes, image_path: Path,
               _attempt: int = 0) -> tuple[str, dict]:
    """Returns (raw_text_response, metadata{usage,model,duration_ms,error?})."""
    img_b64 = base64.b64encode(image_bytes).decode("ascii")
    suffix = image_path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif suffix == ".png":
        mime = "image/png"
    elif suffix == ".webp":
        mime = "image/webp"
    elif suffix == ".gif":
        mime = "image/gif"
    else:
        mime = "image/jpeg"  # fallback; api will reject if truly mismatched

    prompt = PROMPT_V1.replace("{ocr_text}", (ocr_text or "<EMPTY>")[:1500])
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "temperature": 0.0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": mime, "data": img_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=PER_REQ_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")[:400]
        if e.code == 429 and _attempt < 2:
            sleep_s = 15 + _attempt * 30
            log(f"  429 rate-limit attempt={_attempt} sleeping {sleep_s}s")
            time.sleep(sleep_s)
            return call_haiku(token, model, ocr_text, image_bytes, image_path,
                              _attempt=_attempt + 1)
        return ("", {
            "_error": f"http {e.code}",
            "_resp": body_err,
            "_duration_ms": int((time.time() - t0) * 1000),
            "_attempts": _attempt + 1,
        })
    except Exception as e:
        return ("", {
            "_error": f"{type(e).__name__}: {str(e)[:200]}",
            "_duration_ms": int((time.time() - t0) * 1000),
        })

    duration_ms = int((time.time() - t0) * 1000)
    meta = {"_duration_ms": duration_ms,
            "_model": data.get("model"),
            "_usage": data.get("usage", {})}
    try:
        text = data["content"][0]["text"]
        return (text, meta)
    except (KeyError, IndexError) as e:
        meta["_error"] = f"resp_shape: {e}"
        meta["_resp"] = json.dumps(data)[:400]
        return ("", meta)


def call_codex_precision(ocr_text: str, image_path: Path) -> tuple[str, dict]:
    prompt = PROMPT_V1.replace("{ocr_text}", (ocr_text or "<EMPTY>")[:1500])
    schema_path = json_schema_file("stage2_precision", STAGE2_SCHEMA)
    result = run_codex(
        prompt,
        tier="stage2",
        model=codex_model_for_tier("stage2"),
        image_path=image_path,
        output_schema=schema_path,
        timeout_s=PER_REQ_TIMEOUT_S,
    )
    return result.text, result.meta()


def call_precision(token: str, ocr_text: str, image_bytes: bytes,
                   image_path: Path) -> tuple[str, dict]:
    # 5/18 security: sanitize attacker-controlled OCR text before injecting
    # into the prompt. OCR text originates from images posted on foreign
    # platforms — a hostile image can carry prompt-injection payloads via
    # rendered text that Stage 1 Qwen faithfully extracts. The sanitizer
    # strips code fences, flags injection markers, and caps length.
    # Sanitize summary rides into meta for downstream audit.
    sanitized = sanitize_untrusted(ocr_text or "<EMPTY>", max_chars=1500,
                                   label="stage2_ocr_text")
    safe_ocr = sanitized.text
    sanitize_summary = sanitized.summary()

    provider = selected_provider()
    if should_try_codex("stage2"):
        raw, meta = call_codex_precision(safe_ocr, image_path)
        meta["_ocr_sanitize"] = sanitize_summary
        if not meta.get("_error"):
            return raw, meta
        log(f"  codex stage2 failed provider={provider}: {meta.get('_error')}")
        if provider == "codex" or not should_use_claude_fallback():
            return raw, meta

    if not token:
        if fallback_provider() == "codex":
            codex_raw, codex_meta = call_codex_precision(safe_ocr, image_path)
            codex_meta["_ocr_sanitize"] = sanitize_summary
            codex_meta["_fallback_from"] = "claude_stage2_token_missing"
            return codex_raw, codex_meta
        return "", {"_error": "Claude OAuth token unavailable; fallback unavailable",
                    "_ocr_sanitize": sanitize_summary}
    raw, meta = call_haiku(token, MODEL_ID, safe_ocr, image_bytes, image_path)
    meta["_ocr_sanitize"] = sanitize_summary
    if (meta.get("_error") and fallback_provider() == "codex"
            and is_claude_auth_error(meta.get("_error"), meta.get("_resp"))):
        log(f"  Claude stage2 auth failed; trying Codex fallback: {meta.get('_error')}")
        codex_raw, codex_meta = call_codex_precision(safe_ocr, image_path)
        codex_meta["_ocr_sanitize"] = sanitize_summary
        codex_meta["_fallback_from"] = "claude_stage2_auth"
        if not codex_meta.get("_error"):
            return codex_raw, codex_meta
        log(f"  Codex stage2 fallback failed: {codex_meta.get('_error')}")
    return raw, meta


def fetch_pending(conn, limit: int) -> list:
    # 🔴 5/30 boss pivot — priority bucket per strategy_directives/2026-05-30.yaml
    # backlog_selective_rerun_priority block. Drains sports-adjacent + the client brand-aligned
    # sources BEFORE grey-market backlog, so new tag set (athlete_named, fan_economy,
    # instance_endorsement_funnel, etc.) fires diagnostically on early batches.
    # Buckets matched by m.file_path substrings (raw JSONL or media path conventions):
    #   P1 = P04_* sports lane + *_sports anon collectors + fan community sources
    #   P2 = P03_* folk-belief (still valid yolk) + anything not classified
    #   P3 = lottery_eco / examplebrand / examplebet (grey-market deep-dive deferred)
    return conn.execute(
        """SELECT s.media_row_id, s.verdict, s.confidence,
                  m.file_path, m.file_size, m.ocr_text
             FROM media_signal_filter s
             JOIN media m ON m.row_id = s.media_row_id
        LEFT JOIN media_kb_decision d ON d.media_row_id = s.media_row_id
            WHERE s.verdict = 'signal'
              AND d.media_row_id IS NULL
              AND m.media_kind = 'photo'
         ORDER BY
            CASE
              WHEN m.file_path LIKE '%P04_%'
                OR m.file_path LIKE '%_sports%'
                OR m.file_path LIKE '%sports_kol%'
                OR m.file_path LIKE '%fan_%'
                OR m.file_path LIKE '%tl1_%'
                OR m.file_path LIKE '%esports_%'
                OR m.file_path LIKE '%example_fanclub%'
                THEN 1
              WHEN m.file_path LIKE '%lottery_eco%'
                OR m.file_path LIKE '%examplebrand%'
                OR m.file_path LIKE '%examplebet%'
                THEN 3
              ELSE 2
            END,
            s.media_row_id ASC
            LIMIT ?""",
        (limit,),
    ).fetchall()


def fetch_by_ids(conn, media_row_ids: list[int]) -> list:
    """Boss 5/8: redo specific media rows by row_id. Idempotent via
    INSERT OR REPLACE in insert_result(). Bypasses the LEFT JOIN filter
    AND the verdict='signal' gate — Commander can force re-eval of any row
    that exists in media_signal_filter."""
    if not media_row_ids:
        return []
    placeholders = ",".join("?" * len(media_row_ids))
    return conn.execute(
        f"""SELECT s.media_row_id, s.verdict, s.confidence,
                   m.file_path, m.file_size, m.ocr_text
              FROM media_signal_filter s
              JOIN media m ON m.row_id = s.media_row_id
             WHERE s.media_row_id IN ({placeholders})
               AND m.media_kind = 'photo'""",
        tuple(media_row_ids),
    ).fetchall()


def total_pending(conn) -> int:
    r = conn.execute(
        """SELECT COUNT(*)
             FROM media_signal_filter s
        LEFT JOIN media_kb_decision d ON d.media_row_id = s.media_row_id
            WHERE s.verdict = 'signal'
              AND d.media_row_id IS NULL""",
    ).fetchone()
    return r[0] if r else 0


def today_count(conn) -> int:
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    r = conn.execute(
        "SELECT COUNT(*) FROM media_kb_decision WHERE processed_at LIKE ? "
        "AND prompt_hash = ?",
        (f"{today}%", PROMPT_HASH),
    ).fetchone()
    return r[0] if r else 0


def insert_result(conn, media_row_id: int, parsed: dict | None, raw: str,
                  meta: dict, stage1_verdict: str, stage1_conf: float | None) -> None:
    if parsed:
        kb_admit_raw = parsed.get("kb_admit", False)
        kb_admit = 1 if (kb_admit_raw is True or str(kb_admit_raw).lower() == "true") else 0
        kb_value_class = parsed.get("kb_value_class")
        kb_value_score = parsed.get("kb_value_score")
        try:
            kb_value_score = int(kb_value_score) if kb_value_score is not None else None
        except Exception:
            kb_value_score = None
        decision_tags = parsed.get("decision_tags")
        if isinstance(decision_tags, list):
            decision_tags = ",".join(decision_tags)
        rationale = parsed.get("rationale")
    else:
        kb_admit = 0
        kb_value_class = None
        kb_value_score = None
        decision_tags = None
        rationale = meta.get("_error") or "[parse_fail]"

    usage = meta.get("_usage", {}) or {}
    conn.execute(
        """INSERT OR REPLACE INTO media_kb_decision
           (media_row_id, kb_admit, kb_value_class, kb_value_score,
            decision_tags, rationale, stage1_verdict, stage1_confidence,
            raw_response, model_used, prompt_hash, duration_ms,
            input_tokens, output_tokens, processed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            media_row_id, kb_admit, kb_value_class, kb_value_score,
            decision_tags, rationale, stage1_verdict, stage1_conf,
            raw[:8000] if raw else None,
            meta.get("_model") or MODEL_ID,
            PROMPT_HASH,
            meta.get("_duration_ms"),
            usage.get("input_tokens"), usage.get("output_tokens"),
            now_iso(),
        ),
    )
    conn.commit()


def process_one(conn, row, token: str) -> str:
    abs_path = ROOT / row["file_path"]
    if not abs_path.exists():
        insert_result(conn, row["media_row_id"], None, "[file_missing]",
                      {"_error": "file_missing", "_duration_ms": 0},
                      row["verdict"], row["confidence"])
        return "missing"
    try:
        img_bytes = abs_path.read_bytes()
    except Exception as e:
        insert_result(conn, row["media_row_id"], None,
                      f"[read_error: {type(e).__name__}]",
                      {"_error": str(e), "_duration_ms": 0},
                      row["verdict"], row["confidence"])
        return "read_err"

    raw, meta = call_precision(token, row["ocr_text"] or "", img_bytes, abs_path)
    if meta.get("_error"):
        insert_result(conn, row["media_row_id"], None, raw, meta,
                      row["verdict"], row["confidence"])
        return "api_err"

    parsed = parse_json(raw)
    insert_result(conn, row["media_row_id"], parsed, raw, meta,
                  row["verdict"], row["confidence"])
    if parsed and parsed.get("kb_admit") in (True, "true", 1):
        return "admit"
    if parsed:
        return "reject"
    return "parse_err"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH,
                        help="cap rows processed this run")
    parser.add_argument("--max-runtime-sec", type=int, default=MAX_RUNTIME_SEC_DEFAULT,
                        help="gracefully stop before daemon timeout; 0 disables")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--media-id", type=int, action="append", default=None,
                        metavar="ROW_ID",
                        help="redo specific media row_id (repeat for multiple). "
                             "Bypasses pending filter + signal-gate; INSERT OR "
                             "REPLACE handles idempotency. Boss 5/8 Commander redo entry.")
    args = parser.parse_args()

    pause_reason = stage2_pause_reason()
    if pause_reason:
        log(f"paused reason={pause_reason!r}; exiting before DB scan/model call")
        return

    init_db()
    conn = get_connection()

    token = current_oauth_access_token()
    if selected_provider() == "claude" and not token and fallback_provider() != "codex":
        log("ABORT: Claude OAuth access token unavailable")
        sys.exit(1)

    if args.media_id:
        rows = fetch_by_ids(conn, args.media_id)
        log(f"start REDO provider={selected_provider()} model={MODEL_ID} "
            f"codex_model={codex_model_for_tier('stage2')} prompt_hash={PROMPT_HASH} "
            f"target_ids={args.media_id} fetched={len(rows)} "
            f"dry_run={args.dry_run} (bypassing daily budget)")
        if args.dry_run or not rows:
            return
    else:
        pending = total_pending(conn)
        used = today_count(conn)
        remaining_budget = max(0, DAILY_BUDGET - used)
        cap = min(args.limit, remaining_budget)
        log(f"start provider={selected_provider()} model={MODEL_ID} "
            f"codex_model={codex_model_for_tier('stage2')} prompt_hash={PROMPT_HASH} "
            f"pending={pending} used_today={used} budget={DAILY_BUDGET} "
            f"cap={cap} dry_run={args.dry_run}")
        if args.dry_run or pending == 0 or cap == 0:
            return
        rows = fetch_pending(conn, cap)

    log(f"processing batch_size={len(rows)}")

    stats = {"admit": 0, "reject": 0, "missing": 0, "read_err": 0,
             "api_err": 0, "parse_err": 0}
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        if args.max_runtime_sec and i > 1:
            elapsed = time.time() - t0
            if elapsed >= args.max_runtime_sec:
                log(f"  runtime_budget_stop before row {i}/{len(rows)} "
                    f"elapsed={elapsed:.1f}s budget={args.max_runtime_sec}s "
                    f"stats={stats}")
                break
        result = process_one(conn, row, token)
        stats[result] = stats.get(result, 0) + 1
        if i % 10 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            log(f"  progress {i}/{len(rows)} {stats} rate={rate:.2f} req/s")

    elapsed = time.time() - t0
    n = max(1, sum(stats.values()))
    log(f"done {stats} elapsed={elapsed:.1f}s avg={elapsed/n:.2f}s/req")
    conn.close()


if __name__ == "__main__":
    main()
