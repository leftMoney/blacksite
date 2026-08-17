"""
processors/funnel_auto_review.py — Auto-approve funnel-push edges per the client brand's scope.

Boss directive 2026-05-01: "以後自動判斷可以加就先加了" — replaces the
manual boss approval gate with engine-side classifier for routine
funnel-push edges. Runs every 15 min via daemon (after run_funnel_edges).
Edges that don't match clear approve/reject rules stay 'pending' for boss
manual review.

Policy v0 (high-confidence rules only):
  APPROVE if  sample-msg intent='promo' AND topic IN CYP_TOPICS
              (lottery / casino / sportsbook / gambling / horoscope / folk-belief)
              AND target is tg_channel_ref or tg_bot_deeplink (not invite)
  APPROVE if  edge.bait_intent='promo' AND distinct_senders >= 2
              (brand-grade promo funnel signal)
  REJECT  if  to_target_kind='tg_invite' AND sample-msg tone='desperate'
              (conditional-share referral pyramid → axis pollution + ban risk)
  REJECT  if  sample-msg topic IN OUT_OF_SCOPE_TOPICS (politics / news / drama)
  REJECT  if  from_chat known noise (cards actionability < 0.1)
  else        UNCERTAIN — leave row pending for boss review

§11 elevated-risk venues: v0 has no detection signal for police-adjacent
venues; rules favor high-confidence approvals to avoid mis-classifying
state-operated bars / protected gambling rings. Future: tag entities
with risk_class column + add risk-aware logic.

Usage:
  py processors/funnel_auto_review.py            # commit changes
  py processors/funnel_auto_review.py --dry-run  # print without DB writes
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from db.connection import get_connection
from processors.llm_router import codex_model_for_tier, json_schema_file, run_codex

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [funnel-auto-rev] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"funnel_auto_review_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# Aligned with rules_layer.py actual emit vocab (verified 2026-05-01 via DB):
#   lottery 1552 / sports 698 / folk-belief 319 / casino 274 / payment 21 / regulatory 14
# Boss observation 2026-05-01: 「目標市場玩家喜歡把玄學帶入彩票」— folk-belief+lottery
# crossover IS the egg-yolk thesis (per INSTANCE.md §1 "folk-belief belief economy
# spanning most of the local population; lucky numbers, dream interpretation,
# example-oracle-site as a large-MAU fortune surface").
CYP_TOPICS = {"lottery", "casino", "sports", "folk-belief", "payment", "regulatory"}
# rules_layer intents that signal funnel activity (vs informational):
CYP_FUNNEL_INTENTS = {"promo", "bait", "infomercial"}
OUT_OF_SCOPE_TOPICS = {"politics", "news", "drama", "unrelated"}
NOISE_ACTIONABILITY_THRESHOLD = 0.1
LLM_REVIEW_LIMIT = int(os.environ.get("FUNNEL_LLM_REVIEW_LIMIT", "12"))
LLM_REVIEW_TIMEOUT_S = int(os.environ.get("FUNNEL_LLM_TIMEOUT_S", "90"))
LLM_RETRY_HOURS = int(os.environ.get("FUNNEL_LLM_RETRY_HOURS", "6"))
LLM_REVIEW_PREFIX = "funnel_codex_fast_v1"

# Grey-market brand name patterns. Channel names like examplebet / slotbrand-a /
# betbrand-b / examplebrand are obvious grey casino/slot brands even
# when sample-msg topic is NULL (rules_layer hasn't processed yet).
# Boss directive 2026-05-01 「能加就加」: trust channel-name signal when
# target_kind is channel_ref/bot (NOT invite) and reject-rules don't fire.
# OUT-of-scope filter: out-of-scope regional / fake-news channels won't match.
# === INSTANCE BRAND FRAGMENTS (customize per instance — append the actual grey
# operator brand-name fragments observed in-market to the regex below) ===
GREY_BRAND_RE = re.compile(
    r"(bet|slot|casino|gamble|win|lucky|vip|free|gift|bonus|jackpot|"
    r"examplebet|slotbrand|betbrand|examplebrand|"
    r"royal|game|prize|cash|hot|rich|wealth)|"
    r"\d{2,4}",
    re.IGNORECASE
)
BARE_TG_LINK_RE = re.compile(
    r"^\s*(?:https?://)?t\.me/(?:\+[A-Za-z0-9_-]+|[A-Za-z0-9_]+)\s*$",
    re.IGNORECASE,
)

# === INSTANCE SOURCE BLOCKLIST (customize per instance — see instances/_TEMPLATE/INSTANCE.md) ===
# Out-of-scope source-group blocklist. Per instance, list source channels that
# look superficially in-scope by name (grey-brand-like tokens, digit tails) but
# are actually out-of-scope local chatter (e.g. neighbouring-region labour-market
# / life chats) with no commercial relevance. Funnel edges from these sources are
# rejected regardless of target name pattern. Placeholders below are illustrative.
BLOCKED_SOURCE_USERNAMES: frozenset[str] = frozenset({
    "example_blocked_group_01", "example_blocked_group_02",
    "example_blocked_group_03", "example_blocked_group_04",
})

LLM_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "review_state": {"type": "string", "enum": ["approved", "rejected", "uncertain"]},
        "review_verdict": {"type": "string", "maxLength": 80},
        "review_reason": {"type": "string", "maxLength": 220},
    },
    "required": ["review_state", "review_verdict", "review_reason"],
}

LLM_REVIEW_PROMPT = """You are Tier 2 funnel admission for the Blacksite active instance.
Rule-based auto-review could not classify this Telegram edge. Decide whether to:

- approved: likely joinable funnel target relevant to the instance domain
- rejected: noise / out-of-scope / not a funnel target
- uncertain: genuinely insufficient evidence; keep pending

Approve if the target is likely a grey-market gambling / lottery / sportsbook /
folk-belief acquisition surface, especially channel refs / bot deeplinks with promo,
brand, recruiter, operator, or repeated push behavior.

Reject if it is clearly out-of-scope local life chat, generic social chatter,
politics/news/drama, personal commerce, or a non-funnel mention.

Use uncertain sparingly. Return JSON only.

Edge:
- target_kind: {to_target_kind}
- target: {to_target}
- edge_kind: {edge_kind}
- bait_intent: {bait_intent}
- push_count: {push_count}
- distinct_senders: {distinct_senders}
- avg_amplification: {avg_amplification}
- from_chat_id: {from_chat_id}
- from_chat_username: {from_chat_username}
- sample_intent: {intent}
- sample_topic: {topic}
- sample_tone: {tone}
- sample_text: {sample_text}
"""


def latest_sample_msg(conn, sample_msg_row_id):
    """funnel_edges already cached the canonical sample_msg_row_id; just look it up."""
    if not sample_msg_row_id:
        return None
    cur = conn.execute(
        "SELECT intent, topic, tone, substr(COALESCE(text,''), 1, 500) FROM messages WHERE row_id = ?",
        (sample_msg_row_id,)
    )
    return cur.fetchone()


# Out-of-scope-neighbour-language detector. The example below uses the Khmer
# Unicode block (U+1780–U+17FF) as a stand-in for a neighbouring-region language.
# Per instance, swap in the Unicode block of whatever out-of-scope neighbouring
# language tends to false-positive against the grey-brand name regex.
# Rationale: a source group can match the name regex by accident (digit tail +
# grey-brand keyword) while its actual content is out-of-scope life chat in a
# neighbouring language — detect language-dominant sample text and reject before
# the name-regex fallback fires.
_KHMER_CHAR_RE = re.compile(r"[ក-៿]")


def is_khmer_dominant(conn, from_chat_id: str, threshold: float = 0.30) -> tuple[bool, float, int]:
    """Sample last N messages from chat; compute neighbouring-language-char ratio
    over total non-whitespace chars. Returns (is_dominant, ratio, samples_n).

    Threshold 0.30 chosen empirically: legitimate local/English chats with
    occasional neighbour-language mention < 5%; out-of-scope neighbour-language
    chats > 60%.
    """
    rows = conn.execute(
        """SELECT text FROM messages
            WHERE (chat_external_id = ? OR chat_username = ?)
              AND text IS NOT NULL AND length(text) > 0
            ORDER BY ts DESC LIMIT 30""",
        (str(from_chat_id), str(from_chat_id))
    ).fetchall()
    if not rows:
        return (False, 0.0, 0)
    total = 0
    khmer = 0
    for r in rows:
        text = r[0] or ""
        for ch in text:
            if not ch.isspace():
                total += 1
                if _KHMER_CHAR_RE.match(ch):
                    khmer += 1
    if total == 0:
        return (False, 0.0, len(rows))
    ratio = khmer / total
    return (ratio >= threshold, ratio, len(rows))


def from_chat_actionability(conn, from_chat_id) -> tuple[float | None, str | None]:
    """cards table noise lookup. None if no card / column missing.
    Schema-tolerant: cards may not have actionability column on older DB."""
    try:
        cur = conn.execute(
            """SELECT actionability, title FROM cards
                WHERE entity_kind = 'chat' AND entity_id = ?""",
            (str(from_chat_id),)
        )
        row = cur.fetchone()
        if not row:
            return (None, None)
        return (row[0], row[1])
    except Exception:
        return (None, None)  # schema mismatch — skip noise filter


def classify_edge(conn, edge: dict) -> tuple[str, str, str]:
    """Returns (review_state, verdict, reason)."""
    sample = latest_sample_msg(conn, edge.get("sample_msg_row_id"))
    intent, topic, tone, sample_text = (
        (sample[0] or "", sample[1] or "", sample[2] or "", sample[3] or "")
        if sample else ("", "", "", "")
    )

    # 0. Blocked source group → REJECT immediately (out-of-scope source groups)
    if (edge.get("from_chat_username") or "") in BLOCKED_SOURCE_USERNAMES:
        return ("rejected", "blocked source group (out_of_scope)",
                f"source='{edge.get('from_chat_username')}' is in BLOCKED_SOURCE_USERNAMES "
                f"(out-of-scope source group, zero commercial relevance to the instance domain)")

    # 1. tg_invite + desperate = referral pyramid → REJECT
    if edge["to_target_kind"] == "tg_invite" and tone == "desperate":
        return ("rejected", "referral pyramid (invite + desperate)",
                "tg_invite + tone=desperate = conditional-share referral chain; auto-reject to avoid axis pollution + ban risk")

    # 1b. Bare invite with no semantic cues = reject. Telegram invite links are
    # opaque until joined; approving link-only pushes lets LLM over-weight repeat
    # behavior and can send personas into border-life / referral-pyramid noise.
    if (edge["to_target_kind"] == "tg_invite"
            and BARE_TG_LINK_RE.match(sample_text or "")
            and not intent and not topic and not (edge.get("bait_intent") or "")
            and (edge.get("distinct_senders") or 0) <= 1):
        return ("rejected", "bare tg_invite without in-scope cues",
                "link-only tg_invite with no intent/topic/bait and <=1 distinct sender; reject to avoid blind join risk")

    # 2. Out-of-scope topic = REJECT
    if topic and topic in OUT_OF_SCOPE_TOPICS:
        return ("rejected", f"out-of-scope (topic={topic})",
                f"sample msg topic={topic!r} not in instance scope (lottery/gambling/folk-belief/sports)")

    # 3. Known noise from_chat = REJECT
    actionability, title = from_chat_actionability(conn, edge["from_chat_id"])
    if actionability is not None and actionability < NOISE_ACTIONABILITY_THRESHOLD:
        return ("rejected", "known-noise from_chat",
                f"cards actionability={actionability:.2f} title={title!r} < {NOISE_ACTIONABILITY_THRESHOLD}")

    # 3b. Neighbour-language-dominant chat = REJECT.
    # An out-of-scope neighbouring-region chat can look superficially like a
    # grey-brand by name (grey keyword + digit tail) while its content is
    # life-talk in a neighbouring language. Out of the instance's scope (the
    # target market). Cross-language grey-casino activity exists but surfaces
    # through local-language signal first — a neighbour-language-only chat is not it.
    if edge["to_target_kind"] in ("tg_channel_ref", "tg_bot_deeplink"):
        is_kh, kh_ratio, n_samples = is_khmer_dominant(conn, edge["to_target"])
        if is_kh:
            return ("rejected", f"neighbour-language-dominant content ({kh_ratio:.0%})",
                    f"target='{edge['to_target']}' last {n_samples} msgs = "
                    f"{kh_ratio:.0%} neighbour-language chars; out of instance scope")

    # 4. in-scope funnel intent+topic = APPROVE (channel_ref or bot, not invite)
    if (edge["to_target_kind"] in ("tg_channel_ref", "tg_bot_deeplink")
            and intent in CYP_FUNNEL_INTENTS and topic in CYP_TOPICS):
        return ("approved", f"in-scope {topic} {intent}",
                f"intent={intent} + topic={topic} → in-scope grey-market funnel target (yolk if folk-belief/lottery, white if sports/casino)")

    # 5. bait_intent='promo' + multi-sender = APPROVE (brand-grade signal,
    #    even when sample-msg intent/topic empty — edge-level promo signal)
    if ((edge.get("bait_intent") or "") == "promo"
            and (edge.get("distinct_senders") or 0) >= 2
            and edge["to_target_kind"] in ("tg_channel_ref", "tg_bot_deeplink")):
        return ("approved", "bait_intent=promo + multi-sender",
                f"edge bait_intent=promo + {edge.get('distinct_senders')} distinct senders = brand-grade promo funnel")

    # 6. Channel-name grey-brand pattern + tg_channel_ref/bot = APPROVE
    #    (boss 2026-05-01 directive 「能加就加」: when sample-msg classification
    #    is sparse, trust target-name keyword/digit signal. Out-of-scope
    #    targets (neighbouring-region / fake-news channels) won't match the regex).
    if (edge["to_target_kind"] in ("tg_channel_ref", "tg_bot_deeplink")
            and GREY_BRAND_RE.search(edge.get("to_target") or "")):
        return ("approved", "grey-brand name pattern",
                f"target='{edge['to_target']}' matches grey-market keyword/digit pattern; boss 2026-05-01 'auto-approve when funnel-target-shaped'")

    # 7. else — uncertain (boss review)
    return ("uncertain", "no rule fired",
            f"intent={intent!r} topic={topic!r} tone={tone!r} target_kind={edge['to_target_kind']!r}; "
            f"no auto-rule matched; leaving pending for boss review")


def parse_llm_json(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def llm_retry_due(edge: dict) -> bool:
    review_model = str(edge.get("review_model") or "")
    review_at = edge.get("review_at")
    if not review_model.startswith(LLM_REVIEW_PREFIX):
        return True
    if not review_at:
        return True
    try:
        reviewed = datetime.fromisoformat(review_at)
    except ValueError:
        return True
    if reviewed.tzinfo is None:
        reviewed = reviewed.replace(tzinfo=TZ)
    return reviewed <= datetime.now(TZ) - timedelta(hours=LLM_RETRY_HOURS)


def llm_review_edge(conn, edge: dict) -> tuple[str, str, str, str | None]:
    sample = latest_sample_msg(conn, edge.get("sample_msg_row_id"))
    intent, topic, tone, sample_text = (
        (sample[0] or "", sample[1] or "", sample[2] or "", sample[3] or "")
        if sample else ("", "", "", "")
    )
    prompt = LLM_REVIEW_PROMPT.format(
        to_target_kind=edge.get("to_target_kind") or "",
        to_target=edge.get("to_target") or "",
        edge_kind=edge.get("edge_kind") or "",
        bait_intent=edge.get("bait_intent") or "",
        push_count=edge.get("push_count") or 0,
        distinct_senders=edge.get("distinct_senders") or 0,
        avg_amplification=edge.get("avg_amplification") or 0,
        from_chat_id=edge.get("from_chat_id") or "",
        from_chat_username=edge.get("from_chat_username") or "",
        intent=intent,
        topic=topic,
        tone=tone,
        sample_text=sample_text.replace("\r", " ").replace("\n", " ").strip(),
    )
    schema_path = json_schema_file("funnel_edge_review", LLM_REVIEW_SCHEMA)
    result = run_codex(
        prompt,
        tier="fast",
        model=codex_model_for_tier("fast"),
        output_schema=schema_path,
        timeout_s=LLM_REVIEW_TIMEOUT_S,
        sandbox="read-only",
    )
    meta = result.meta()
    model_used = meta.get("_model") or codex_model_for_tier("fast")
    if not result.ok:
        return ("uncertain", "llm_failed", result.error or "codex fast failed", None)
    parsed = parse_llm_json(result.text)
    if not parsed:
        return ("uncertain", "llm_parse_fail", "codex fast returned non-JSON", None)
    return (
        parsed.get("review_state", "uncertain"),
        parsed.get("review_verdict", "llm verdict")[:80],
        parsed.get("review_reason", "llm review")[:220],
        f"{LLM_REVIEW_PREFIX}:{model_used}",
    )


def run_pass(dry_run: bool = False) -> dict:
    conn = get_connection()
    # Process all pending edges, not only funnel_push: boss directive
    # 2026-05-01「能加就加」extends to casual tg_channel_ref + tg_bot_deeplink
    # candidates too. tg_invite still filtered by referral-pyramid rule.
    cur = conn.execute(
        """SELECT row_id, from_chat_id, from_chat_username, to_target_kind, to_target, edge_kind,
                  bait_intent, push_count, distinct_senders, avg_amplification,
                  sample_msg_row_id, review_at, review_model
             FROM funnel_edges
            WHERE review_state = 'pending'
         ORDER BY COALESCE(distinct_senders, 0) DESC,
                  COALESCE(push_count, 0) DESC,
                  COALESCE(avg_amplification, 0) DESC,
                  row_id ASC"""
    )
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    edges = [dict(zip(cols, r)) for r in rows]

    if not edges:
        log("no pending funnel_push edges; idle")
        conn.close()
        return {"approved": 0, "rejected": 0, "uncertain": 0, "total": 0}

    counts = {"approved": 0, "rejected": 0, "uncertain": 0}
    uncertain_edges: list[dict] = []
    llm_reviewed = 0
    for edge in edges:
        state, verdict, reason = classify_edge(conn, edge)
        if state == "uncertain":
            uncertain_edges.append(edge)
            continue
        counts[state] += 1
        if dry_run:
            log(f"DRY {state}: edge#{edge['row_id']} {edge['to_target_kind']}:{edge['to_target']} ({verdict})")
            continue
        conn.execute(
            """UPDATE funnel_edges
                  SET review_state   = ?,
                      review_verdict = ?,
                      review_reason  = ?,
                      review_at      = ?,
                      review_model   = ?
                WHERE row_id = ?""",
            (state, verdict, reason, now_iso(), "funnel_auto_review_v0", edge["row_id"]),
        )
        log(f"{state}: edge#{edge['row_id']} {edge['to_target_kind']}:{edge['to_target']} ({verdict})")
        if not dry_run and state in ("approved", "rejected"):
            try:
                from processors.history_log import log_event
                log_event(
                    actor="cron_funnel_auto_review",
                    kind="decision",
                    scope="funnel",
                    title=f"{state} edge#{edge['row_id']} {edge['to_target_kind']}:{edge['to_target']}",
                    body=f"verdict: {verdict}\nreason: {reason}\n"
                         f"from_chat: {edge.get('from_chat_username') or edge.get('from_chat_id')}",
                    refs=[f"funnel_edges#{edge['row_id']}"],
                )
            except Exception as e:
                log(f"  history_log fail: {type(e).__name__}: {e}")

    for edge in uncertain_edges:
        if dry_run:
            counts["uncertain"] += 1
            log(f"DRY pending: edge#{edge['row_id']} {edge['to_target_kind']}:{edge['to_target']} (no rule fired)")
            continue
        if llm_reviewed >= LLM_REVIEW_LIMIT or not llm_retry_due(edge):
            counts["uncertain"] += 1
            continue
        llm_state, llm_verdict, llm_reason, llm_model = llm_review_edge(conn, edge)
        llm_reviewed += 1
        if llm_model is None:
            counts["uncertain"] += 1
            log(f"LLM pending: edge#{edge['row_id']} {edge['to_target_kind']}:{edge['to_target']} ({llm_verdict})")
            continue
        persisted_state = llm_state if llm_state in ("approved", "rejected") else "pending"
        counts["approved" if llm_state == "approved" else "rejected" if llm_state == "rejected" else "uncertain"] += 1
        conn.execute(
            """UPDATE funnel_edges
                  SET review_state   = ?,
                      review_verdict = ?,
                      review_reason  = ?,
                      review_at      = ?,
                      review_model   = ?
                WHERE row_id = ?""",
            (persisted_state, llm_verdict, llm_reason, now_iso(), llm_model, edge["row_id"]),
        )
        log(f"LLM {persisted_state}: edge#{edge['row_id']} {edge['to_target_kind']}:{edge['to_target']} ({llm_verdict})")
    if not dry_run:
        conn.commit()
    conn.close()
    counts["total"] = sum(counts.values())
    log(
        f"pass: {counts['approved']} approved, {counts['rejected']} rejected, "
        f"{counts['uncertain']} pending (total {counts['total']}, llm_reviewed={llm_reviewed})"
    )
    if not dry_run and counts["total"] > 0:
        try:
            from processors.history_log import log_event
            log_event(
                actor="cron_funnel_auto_review", kind="metric", scope="funnel",
                title=f"auto-review pass: {counts['approved']}A {counts['rejected']}R {counts['uncertain']}P",
                body=f"{counts} llm_reviewed={llm_reviewed}",
            )
        except Exception:
            pass
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="print decisions without DB writes")
    args = parser.parse_args()
    run_pass(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
