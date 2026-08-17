"""
M6 — Daily brief data preparer.

Collects past-24h activity stats from Blacksite KB, dumps structured JSON
into runtime/briefs/queue/pending_<date>.json for the scheduled-task to
read, compose Traditional-Chinese prose from, and write the final
brief markdown to runtime/briefs/queue/pending_<date>.md.

The TG send loop (inside tg_listen) then picks up the .md file, DMs it
to boss via P01, and moves to runtime/briefs/sent/.

Stats collected (each capped to top 10-20 to keep brief tight):
  - msg volume by platform
  - new entities surfaced (last_seen_ts within 24h)
  - top amplification clusters in last 24h
  - new funnel_push edges (with join state)
  - identifier extractions (phones / wallets / promos / lineids)
  - cards built/refreshed in last 24h
  - state transitions from decay cron (entity active→dormant etc.)
  - daemon health quick-check
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from db.schema import init_db

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
QUEUE_DIR = RUNTIME_DIR / "briefs" / "queue"
SENT_DIR = RUNTIME_DIR / "briefs" / "sent"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)
SENT_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

# Tier heuristic — used when entities.tier is unset (most rows). Client-brand scope per
# instances/_TEMPLATE/INSTANCE.md §1: yolk = grey-casino brand / folk-belief / lottery /
# funnel-mouth; white = sports / news-adjacent; shell = peripheral.
# === INSTANCE TIER KEYWORDS (customize per instance — append the target country's
# native-language lottery / folk-belief / sports terms and grey-operator brand
# fragments to the three regexes below) ===
_GREY_BRAND_RE = re.compile(
    r"(?i)(bet|slot|casino|gamble|jackpot|gift|bonus|free|"
    r"examplebet|slotbrand|betbrand|examplebrand|"
    r"baccarat|club|777)\d*"
)
_FOLK_BELIEF_RE = re.compile(
    r"(?i)(folk-belief|fortune|horoscope|moon|lucky|amulet|temple)"
)
_SPORTS_RE = re.compile(
    r"(?i)(muay|football|soccer|sport|score)"
)


def _classify_tier_heuristic(name: str | None) -> str:
    """Best-effort tier from name; used when entities.tier is NULL."""
    if not name:
        return "shell"
    n = str(name)
    if _GREY_BRAND_RE.search(n) or _FOLK_BELIEF_RE.search(n):
        return "yolk"
    if _SPORTS_RE.search(n):
        return "white"
    return "shell"


def _tier_from_actionability(score: float | None) -> str:
    """Fallback tier per kb/DESIGN.md §6.2 thresholds."""
    s = score or 0.0
    if s >= 0.65:
        return "yolk"
    if s >= 0.40:
        return "white"
    return "shell"


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_bkk().isoformat(timespec='seconds')}] [brief] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"brief_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------
# DEPRECATED 5/2 PM (boss directive): Gemini Flash Lite synthesis 拔除。
# 過濾雜訊低階 OK，但洞察是連貫性的，必用高階智商 (Claude Sonnet 4.6 / Opus 4.7)
# 經由 processors/_llm_synth.py spawn claude.exe with BUSINESS_ANALYST skill。
# 舊 _gemini_client / _llm_translate_titles_to_zh / _llm_one_line_summary
# / _deterministic_summary_zh 移除。compose() 改走 Claude analyst 路徑，
# pure-Python template 為 fallback (claude.exe unavailable 時)。
# ----------------------------------------------------------------------


def collect_24h_stats(window_hours: int = 24) -> dict:
    init_db()
    conn = get_connection()
    cutoff = (now_bkk() - timedelta(hours=window_hours)).isoformat(timespec="seconds")

    out: dict = {
        "generated_at": now_bkk().isoformat(timespec="seconds"),
        "window_hours": window_hours,
        "cutoff_ts": cutoff,
    }

    # Volume by platform
    out["msgs_by_platform"] = {r[0]: r[1] for r in conn.execute(
        "SELECT platform, COUNT(*) FROM messages WHERE ts >= ? GROUP BY platform ORDER BY 2 DESC",
        (cutoff,),
    ).fetchall()}

    # New entities (last_seen_ts within window AND first_seen_ts within window)
    out["new_entities"] = [
        {"kind": r[0], "name": r[1], "platform": r[2], "seen_count": r[3]}
        for r in conn.execute(
            """SELECT kind, name, platform, seen_count
                 FROM entities
                WHERE first_seen_ts >= ?
                ORDER BY seen_count DESC LIMIT 20""",
            (cutoff,),
        ).fetchall()
    ]

    # Top amplification clusters in window
    out["top_amplified_content"] = []
    for r in conn.execute(
        """SELECT content_hash, MAX(amplification_count) amp,
                  COUNT(*) rows, COUNT(DISTINCT chat_external_id) chats,
                  MIN(ts) first_ts, MAX(ts) last_ts
             FROM messages
            WHERE content_hash IS NOT NULL AND ts >= ? AND amplification_count > 5
            GROUP BY content_hash
            ORDER BY amp DESC LIMIT 8""",
        (cutoff,),
    ).fetchall():
        sample = conn.execute(
            "SELECT text, chat_username FROM messages WHERE content_hash=? LIMIT 1",
            (r[0],),
        ).fetchone()
        out["top_amplified_content"].append({
            "content_hash": r[0], "max_amp": r[1], "rows": r[2], "chats": r[3],
            "first_ts": r[4], "last_ts": r[5],
            "sample_text": (sample["text"] if sample else "")[:200],
            "sample_chat": sample["chat_username"] if sample else None,
        })

    # New funnel_push edges
    out["new_funnel_pushes"] = [
        {
            "edge_id": r[0], "from": r[1], "to_kind": r[2], "to": r[3],
            "push_count": r[4], "review_state": r[5], "join_state": r[6],
            "bait_intent": r[7], "first_seen": r[8],
        }
        for r in conn.execute(
            """SELECT row_id, from_chat_username, to_target_kind, to_target,
                      push_count, review_state, join_state, bait_intent, first_seen_ts
                 FROM funnel_edges
                WHERE edge_kind = 'funnel_push' AND first_seen_ts >= ?
                ORDER BY push_count DESC LIMIT 10""",
            (cutoff,),
        ).fetchall()
    ]

    # New identifier entities (phone/wallet/promo/lineid) found in window
    out["new_identifiers"] = [
        {"kind": r[0], "name": r[1], "seen_count": r[2]}
        for r in conn.execute(
            """SELECT kind, name, seen_count
                 FROM entities
                WHERE kind IN ('phone','lineid','promo','wallet','qr_mention')
                  AND first_seen_ts >= ?
                ORDER BY seen_count DESC LIMIT 15""",
            (cutoff,),
        ).fetchall()
    ]

    # Cards built/refreshed in window
    out["cards_activity"] = [
        {"card_id": r[0], "title": r[1], "actionability": r[2], "decay": r[3], "last_built": r[4]}
        for r in conn.execute(
            """SELECT row_id, title, actionability_score, time_decay_class, last_built_at
                 FROM cards
                WHERE last_built_at >= ?
                ORDER BY actionability_score DESC NULLS LAST LIMIT 15""",
            (cutoff,),
        ).fetchall()
    ]

    # Entity state transitions (set by decay cron)
    out["state_transitions"] = [
        {"entity_kind": r[0], "name": r[1], "state": r[2], "reason": r[3], "at": r[4]}
        for r in conn.execute(
            """SELECT kind, name, state, state_reason, state_changed_at
                 FROM entities
                WHERE state_changed_at >= ?
                ORDER BY state_changed_at DESC LIMIT 20""",
            (cutoff,),
        ).fetchall()
    ]

    # Auto-join outcomes (M4.5d)
    out["auto_join_outcomes"] = [
        {"edge_id": r[0], "to": r[1], "join_state": r[2], "persona": r[3],
         "join_at": r[4], "error": r[5]}
        for r in conn.execute(
            """SELECT row_id, to_target, join_state, join_persona, join_at, join_error
                 FROM funnel_edges
                WHERE join_at >= ?
                ORDER BY join_at DESC LIMIT 10""",
            (cutoff,),
        ).fetchall()
    ]

    # OCR + ASR throughput
    out["media_processed"] = {}
    n = conn.execute(
        "SELECT COUNT(*) FROM media WHERE ocr_text IS NOT NULL AND processed_at >= ?",
        (cutoff,),
    ).fetchone()[0]
    out["media_processed"]["ocr_text_filled"] = n
    n = conn.execute(
        "SELECT COUNT(*) FROM media WHERE transcript IS NOT NULL AND processed_at >= ?",
        (cutoff,),
    ).fetchone()[0]
    out["media_processed"]["transcripts_filled"] = n

    # KB total state for context
    out["kb_state"] = {
        "messages_total": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "entities_active": conn.execute("SELECT COUNT(*) FROM entities WHERE state='active'").fetchone()[0],
        "entities_dormant": conn.execute("SELECT COUNT(*) FROM entities WHERE state='dormant'").fetchone()[0],
        "cards_active": conn.execute("SELECT COUNT(*) FROM cards WHERE state='active'").fetchone()[0],
        "funnel_edges_total": conn.execute("SELECT COUNT(*) FROM funnel_edges").fetchone()[0],
        "funnel_approved_pending_join": conn.execute(
            "SELECT COUNT(*) FROM funnel_edges WHERE review_state='approved' AND join_state='not_attempted'"
        ).fetchone()[0],
    }

    # ------------------------------------------------------------------
    # 🆕 5/2 boss spec — three additions to drive the new daily brief format
    # ------------------------------------------------------------------

    # (1) Active groups by tier — every distinct (platform, chat_external_id)
    # Commander personas have ever observed messages in. Tier = entities.tier when
    # set, else heuristic on chat_username. "active" = any persona-tagged msg
    # in past 30 days (so a single ingest gap doesn't drop a known group).
    grp_cutoff = (now_bkk() - timedelta(days=30)).isoformat(timespec="seconds")
    out["groups_by_tier"] = {"yolk": 0, "white": 0, "shell": 0}
    out["groups_by_tier_examples"] = {"yolk": [], "white": [], "shell": []}
    seen_chats: set[tuple] = set()
    for r in conn.execute(
        """SELECT m.platform, m.chat_external_id, m.chat_username,
                  e.tier as entity_tier
             FROM messages m
             LEFT JOIN entities e
               ON e.kind='channel' AND e.platform=m.platform
              AND e.name=m.chat_username
            WHERE m.persona IS NOT NULL
              AND m.chat_external_id IS NOT NULL
              AND m.ts >= ?
            GROUP BY m.platform, m.chat_external_id""",
        (grp_cutoff,),
    ).fetchall():
        key = (r["platform"], r["chat_external_id"])
        if key in seen_chats:
            continue
        seen_chats.add(key)
        tier = r["entity_tier"] or _classify_tier_heuristic(r["chat_username"])
        out["groups_by_tier"][tier] = out["groups_by_tier"].get(tier, 0) + 1
        if len(out["groups_by_tier_examples"][tier]) < 5:
            out["groups_by_tier_examples"][tier].append(r["chat_username"] or "(unnamed)")

    # (2) Cards by tier (24h window). Tier = entity.tier OR heuristic on entity
    # name OR fallback derived from actionability_score per kb/DESIGN.md §6.2.
    out["cards_by_tier"] = {"yolk": [], "white": [], "shell": []}
    for r in conn.execute(
        """SELECT c.row_id, c.title, c.actionability_score, c.last_built_at,
                  e.tier as entity_tier, e.name as entity_name, e.kind as entity_kind
             FROM cards c
             LEFT JOIN entities e ON e.row_id = c.entity_row_id
            WHERE c.last_built_at >= ?
              AND c.state='active'
            ORDER BY c.actionability_score DESC NULLS LAST""",
        (cutoff,),
    ).fetchall():
        score = r["actionability_score"]
        tier = r["entity_tier"] or _classify_tier_heuristic(r["entity_name"])
        # actionability bumps tier above shell
        if tier == "shell":
            tier = _tier_from_actionability(score)
        out["cards_by_tier"].setdefault(tier, []).append({
            "card_id": r["row_id"],
            "title": r["title"],
            "actionability": score,
            "entity": r["entity_name"],
            "entity_kind": r["entity_kind"],
            "last_built": r["last_built_at"],
        })

    # (3) Entity activity Δ over 24h (mention count delta, top 10).
    out["entity_delta_24h"] = []
    for r in conn.execute(
        """SELECT e.kind, e.name, e.platform, e.seen_count, e.tier,
                  COUNT(me.message_row_id) as delta_24h
             FROM entities e
             JOIN messages_entities me ON me.entity_row_id = e.row_id
             JOIN messages m ON m.row_id = me.message_row_id
            WHERE m.ts >= ?
            GROUP BY e.row_id
            HAVING delta_24h > 0
            ORDER BY delta_24h DESC LIMIT 10""",
        (cutoff,),
    ).fetchall():
        tier = r["tier"] or _classify_tier_heuristic(r["name"])
        out["entity_delta_24h"].append({
            "kind": r["kind"], "name": r["name"], "platform": r["platform"],
            "tier": tier, "delta_24h": r["delta_24h"], "cumulative": r["seen_count"],
        })

    # (4) Raw insights fallback — when card_builder hasn't produced fresh
    # cards in this window (cards_by_tier all empty), the 圖書館 section would
    # be useless. Fall back to building "raw signals" from the highest-Δ
    # entities + amplification clusters and bucketing them by tier. Marked
    # `synthetic_card=True` so renderer can label them appropriately.
    cards_total = sum(len(v) for v in out["cards_by_tier"].values())
    out["raw_signals_by_tier"] = {"yolk": [], "white": [], "shell": []}
    if cards_total == 0:
        # entity Δ → synthetic cards
        for d in out["entity_delta_24h"][:12]:
            tier = d.get("tier") or _classify_tier_heuristic(d.get("name"))
            # Build a 1-line title from the signal
            title = (
                f"{d['kind']} `{d['name']}` 24h 內被提及 {d['delta_24h']} 次"
                f"（累計 {d['cumulative']}）"
            )
            out["raw_signals_by_tier"].setdefault(tier, []).append({
                "title": title,
                "title_zh": title,  # already Chinese
                "signal_kind": "entity_delta",
                "actionability": None,
                "entity": d["name"],
                "synthetic_card": True,
            })
        # amplification clusters → synthetic cards
        for it in out.get("top_amplified_content", [])[:6]:
            sample = (it.get("sample_text") or "").strip()
            chat = it.get("sample_chat") or "-"
            # Heuristic tier from sample_chat name
            tier = _classify_tier_heuristic(chat)
            title = (
                f"高擴散內容 amp={it['max_amp']} · {it['rows']} rows / {it['chats']} chats "
                f"@ `{chat}` · 「{(sample[:40] + '…') if len(sample)>40 else sample}」"
            )
            out["raw_signals_by_tier"].setdefault(tier, []).append({
                "title": title,
                "title_zh": title,
                "signal_kind": "amplification",
                "actionability": None,
                "entity": chat,
                "synthetic_card": True,
            })

    # NOTE 5/2 PM: previously called Gemini Flash Lite here for title-translate
    # + one-line summary. Removed per boss directive — Claude analyst (called
    # in compose() via _llm_synth.claude_run with BUSINESS_ANALYST skill)
    # handles all synthesis + Chinese rendering + connectedness analysis.
    # collect_24h_stats stays pure data layer (no LLM, fast, deterministic).

    conn.close()
    return out


def prepare(window_hours: int = 24) -> Path:
    stats = collect_24h_stats(window_hours)
    date = now_bkk().strftime("%Y-%m-%d")
    out_path = QUEUE_DIR / f"pending_{date}.json"
    out_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    log(f"prepared brief data: {out_path.name} (window={window_hours}h)")
    print(str(out_path), flush=True)
    return out_path


# ----------------------------------------------------------------------
# compose — pure-Python template renderer, no LLM, no OAuth, 0 token.
# Replaces the old scheduled-task `blacksite-daily-brief` SKILL prompt.
# ----------------------------------------------------------------------

def _trunc(s: str, n: int = 80) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def _org_state_snippet() -> list[str]:
    """Compact 4-7 line org-activity snippet for the daily brief.

    Phase D (5/5 ship): boss directive 「五個問題：每天看不到組織活動」.
    Reads same data as scripts/org.py but inlines summary computation here
    to keep the two modules decoupled. All paths fail-soft: if any source
    is missing, that line is dropped silently and brief still ships."""
    import yaml as _yaml
    from datetime import timezone as _tz, timedelta as _td
    BKK = _tz(_td(hours=7))
    runtime = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
    today = datetime.now(BKK).date().isoformat()
    out: list[str] = []

    # Section chief activity today
    kpi_dir = runtime / "agent_kpi"
    sc_count = 0
    if kpi_dir.exists():
        for yp in kpi_dir.glob("*.yaml"):
            try:
                d = _yaml.safe_load(yp.read_text(encoding="utf-8")) or {}
                if (d.get("last_evaluated_at") or "")[:10] == today:
                    sc_count += 1
            except Exception:
                continue

    # Incidents state breakdown
    inc_dir = runtime / "agent_incidents"
    inc_states: dict[str, int] = {}
    inc_today = 0
    if inc_dir.exists():
        for ip in inc_dir.glob("INC-*.md"):
            try:
                text = ip.read_text(encoding="utf-8")
                if not text.startswith("---"):
                    continue
                end = text.find("\n---", 3)
                fm = _yaml.safe_load(text[3:end]) or {} if end > 0 else {}
            except Exception:
                continue
            st = fm.get("state", "?")
            inc_states[st] = inc_states.get(st, 0) + 1
            if (fm.get("opened_at") or "")[:10] == today:
                inc_today += 1
    inc_summary = " · ".join(f"{k}={v}" for k, v in sorted(inc_states.items())) or "0"
    out.append(f"- 小主管: 今日評 {sc_count} agents · incident open={inc_today} 新 / 全部 {inc_summary}")

    # Strategist: latest memo + latest directive file
    memos = sorted((runtime / "strategy_memos").glob("*.md")) if (runtime / "strategy_memos").exists() else []
    last_memo_name = memos[-1].name if memos else "(無)"
    dir_dir = runtime / "strategy_directives"
    last_dir = sorted(dir_dir.glob("*.yaml"))[-1] if dir_dir.exists() and any(dir_dir.glob("*.yaml")) else None
    if last_dir:
        try:
            docs = list(_yaml.safe_load_all(last_dir.read_text(encoding="utf-8")))
            merged: dict = {}
            for d in docs:
                if isinstance(d, dict):
                    merged.update(d)
            d_count = len(merged.get("directives") or [])
            expires = (merged.get("expires_at") or "?")[:10]
            out.append(f"- 策略長: memo={last_memo_name} · 最新 directives={last_dir.name} ({d_count} 條, expires {expires})")
        except Exception:
            out.append(f"- 策略長: memo={last_memo_name} · directives={last_dir.name} (parse fail)")
    else:
        out.append(f"- 策略長: memo={last_memo_name} · directives=(無)")

    # Agent learnings — count memories with mtime within 7d AND learning_lines > 0
    mem_dir = runtime / "agent_memory"
    if mem_dir.exists():
        cutoff_dt = datetime.now(BKK) - _td(days=7)
        recent_mem_with_learnings = 0
        recent_mem_total = 0
        for mp in mem_dir.glob("*.md"):
            mtime = datetime.fromtimestamp(mp.stat().st_mtime, tz=BKK)
            if mtime < cutoff_dt:
                continue
            recent_mem_total += 1
            try:
                text = mp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            in_section = False
            ll = 0
            for line in text.split("\n"):
                if line.startswith("# 我的經驗") or line.startswith("# Boss curated"):
                    in_section = True
                    continue
                if line.startswith("# "):
                    in_section = False
                    continue
                if in_section and line.strip().startswith(("-", "*", "•")):
                    ll += 1
            if ll > 0:
                recent_mem_with_learnings += 1
        if recent_mem_total > 0:
            out.append(f"- Agent learnings: 7d 內 {recent_mem_total} 個 memory mtime 變動 · 其中 {recent_mem_with_learnings} 個有實質 learning_lines")
            if recent_mem_with_learnings <= 1 and recent_mem_total >= 5:
                out.append("  ⚠ 多數 memory 只 mtime 動沒新 learning → learning_added 機制可能沒接")

    out.append("- 細節: `py scripts/org.py status` / `org.py meetings --since 24h` / `org.py directives`")
    return out


_PLAT_ABBREV = {
    "telegram": "TG", "youtube": "YT", "x": "X",
    "pantip": "PT", "tiktok": "TT", "reddit": "RD",
    "bigo": "Bigo", "facebook": "FB", "instagram": "IG",
    "trueid": "TrueID", "ch3plus": "CH3+", "aisplay": "AIS",
    "noice": "NOICE", "oned": "oneD", "nimo": "Nimo",
}
_TIER_EMOJI = {"yolk": "🟢", "white": "🟡", "shell": "⚪"}
_TIER_ZH = {"yolk": "蛋黃", "white": "蛋白", "shell": "蛋殼"}


def _render_brief(stats: dict, date_label: str) -> str:
    """精簡版 template fallback。目標 ≤1500 chars，boss 一眼掃頂部即可。
    Claude analyst path (compose_via_claude) 也要遵守同樣長度規範。"""
    lines: list[str] = []

    plat = stats.get("msgs_by_platform") or {}
    msgs_total = sum(plat.values()) if plat else 0
    media = stats.get("media_processed") or {}
    ocr_n = media.get("ocr_text_filled", 0)
    asr_n = media.get("transcripts_filled", 0)
    joins = stats.get("auto_join_outcomes") or []
    joined_ok = sum(1 for j in joins if j.get("join_state") == "joined")
    joined_fail = sum(1 for j in joins if j.get("join_state", "").startswith("failed"))
    cards_t = stats.get("cards_by_tier") or {}
    n_yolk_c = len(cards_t.get("yolk", []))
    n_white_c = len(cards_t.get("white", []))
    grp = stats.get("groups_by_tier") or {}
    kb = stats.get("kb_state") or {}

    # ── Header ──────────────────────────────────────────────────
    lines.append(f"🌅 *Blacksite 日報 {date_label}*")
    lines.append("")

    # ── 數字行（一行搞定 KPI）───────────────────────────────────
    plat_str = " / ".join(
        f"{_PLAT_ABBREV.get(p, p)} {n:,}"
        for p, n in sorted(plat.items(), key=lambda kv: -kv[1])[:4]
    )
    fail_str = f" ({joined_fail}F)" if joined_fail else ""
    lines.append(
        f"📊 {msgs_total:,} msgs（{plat_str}）"
        f"· OCR {ocr_n} · ASR {asr_n} · +{joined_ok}{fail_str} 加群"
        f"· 🟢{n_yolk_c}蛋黃 🟡{n_white_c}蛋白 | 群池蛋黃 {grp.get('yolk',0)}"
    )
    lines.append("")

    # ── 蛋黃洞察 top 5（標題截短 65 字）────────────────────────
    raw_signals_t = stats.get("raw_signals_by_tier") or {}
    cards_total = sum(len(v) for v in cards_t.values())
    yolks = cards_t.get("yolk", [])
    fallback_yolks = raw_signals_t.get("yolk", []) if cards_total == 0 else []
    items = yolks or fallback_yolks

    if items:
        suffix = "（原始訊號）" if fallback_yolks else ""
        lines.append(f"**🟢 蛋黃洞察{suffix}**")
        for c in items[:5]:
            score = c.get("actionability")
            s = f"{score:.2f} " if score is not None else ""
            lines.append(f"· {s}{_trunc(c.get('title') or '', 65)}")
    else:
        lines.append("**🟢 蛋黃洞察**")
        lines.append("· 今日無蛋黃級新洞察")
    lines.append("")

    # ── 蛋白 top 3（更精簡）─────────────────────────────────────
    whites = cards_t.get("white", [])
    if whites:
        lines.append("**🟡 蛋白（top 3）**")
        for c in whites[:3]:
            score = c.get("actionability")
            s = f"{score:.2f} " if score is not None else ""
            lines.append(f"· {s}{_trunc(c.get('title') or '', 55)}")
        if len(whites) > 3:
            lines.append(f"· …還有 {len(whites)-3} 條，見 runtime/cards/{date_label}.md")
        lines.append("")

    # ── Alerts ───────────────────────────────────────────────────
    alerts: list[str] = []
    pending_joins = kb.get("funnel_approved_pending_join", 0)
    if pending_joins:
        alerts.append(f"funnel 待加群 {pending_joins} 條")
    for t in (stats.get("state_transitions") or [])[:3]:
        if t.get("state") == "dormant":
            alerts.append(f"`{t['name']}` → dormant")
    if alerts:
        lines.append("⚠ " + " · ".join(alerts))
        lines.append("")

    # ── 活躍度 Δ（inline，不用表格）────────────────────────────
    deltas = stats.get("entity_delta_24h") or []
    if deltas:
        delta_str = " / ".join(
            f"{_TIER_EMOJI.get(d.get('tier') or 'shell','⚪')}`{d['name']}` +{d['delta_24h']}"
            for d in deltas[:6]
        )
        lines.append(f"🔥 活躍 Δ：{delta_str}")
        lines.append("")

    # ── KB 快照 + 組織（2 行）───────────────────────────────────
    lines.append(
        f"📦 {kb.get('messages_total',0):,} msgs · {kb.get('entities_active',0):,} entities"
        f" · {kb.get('cards_active',0)} cards · funnel {kb.get('funnel_edges_total',0)}"
    )
    try:
        org = _org_state_snippet()
        if org:
            lines.append(org[0])  # 只取小主管那行
    except Exception:
        pass

    body = "\n".join(lines)
    if len(body) > 2000:
        body = body[:1990] + "\n…（截斷，見 cards md）"
    return body


def _ingest_lead_sidecar(md_path: Path, target: str) -> int:
    """P1 — read `<md_path>.leads.jsonl` sidecar emitted by analyst; INSERT
    each line into kb_leads (state='pending'). Returns count of leads inserted.

    Non-fatal: missing sidecar or malformed lines logged + skipped, brief still ships.
    Idempotent guard: if origin_ref already has rows, skip (prevent re-INSERT
    on second compose run for same date)."""
    sidecar = md_path.with_suffix(md_path.suffix + ".leads.jsonl")
    if not sidecar.exists():
        log(f"lead sidecar absent for {target} — analyst did not emit (non-fatal)")
        return 0

    try:
        from db.connection import get_connection
    except ImportError as e:
        log(f"lead sidecar: cannot import db.connection: {e}")
        return 0

    origin = f"brief_{target}"
    origin_ref = md_path.relative_to(ROOT).as_posix()
    now = now_bkk().isoformat(timespec="seconds")

    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT COUNT(*) FROM kb_leads WHERE origin = ?", (origin,)
        ).fetchone()[0]
        if existing > 0:
            log(f"lead sidecar: {existing} rows already present for origin={origin}; skip re-ingest")
            return 0

        # Determine starting sequence number for today (across all origins)
        date_str = target  # YYYY-MM-DD
        n_today = conn.execute(
            "SELECT COUNT(*) FROM kb_leads WHERE lead_id LIKE ?",
            (f"L-{date_str}-%",),
        ).fetchone()[0]
        seq = n_today + 1

        inserted = 0
        skipped = 0
        with sidecar.open("r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError as e:
                    log(f"lead sidecar line {line_no}: JSON parse fail: {e}")
                    skipped += 1
                    continue
                lead_type = obj.get("type")
                action = obj.get("suggested_action") or ""
                if not lead_type or not action:
                    log(f"lead sidecar line {line_no}: missing type/suggested_action — skip")
                    skipped += 1
                    continue
                lead_id = f"L-{date_str}-{seq:03d}"
                seq += 1
                try:
                    conn.execute(
                        """INSERT INTO kb_leads(
                              lead_id, origin, origin_ref, emitted_at,
                              type, target, suggested_action, confidence,
                              actionability, reversibility, auto_safe,
                              state, refs)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                        (
                            lead_id, origin, origin_ref, now,
                            str(lead_type)[:64],
                            (obj.get("target") or "")[:512] or None,
                            action[:2000],
                            float(obj.get("confidence", 0.5)),
                            float(obj.get("actionability", 0.5)),
                            obj.get("reversibility"),
                            1 if obj.get("auto_safe") else 0,
                            json.dumps(obj.get("refs", []), ensure_ascii=False),
                        ),
                    )
                    conn.commit()
                    inserted += 1
                except Exception as e:
                    log(f"lead sidecar line {line_no} INSERT fail: {type(e).__name__}: {e}")
                    skipped += 1

        log(f"lead sidecar ingest: {inserted} inserted / {skipped} skipped from {sidecar.name}")

        # log_event milestone
        try:
            from processors.history_log import log_event
            log_event(
                actor="cron_daily_brief",
                kind="milestone",
                scope="lead_pipeline",
                title=f"sidecar ingest: +{inserted} leads from {target}",
                body=f"origin={origin}\norigin_ref={origin_ref}\ninserted={inserted}\nskipped={skipped}",
                refs=[origin_ref, sidecar.relative_to(ROOT).as_posix()],
            )
        except Exception as e:
            log(f"lead sidecar history_log fail: {type(e).__name__}: {e}")
        return inserted
    finally:
        conn.close()


def _compose_via_claude(target: str) -> Path | None:
    """Spawn claude.exe as analyst with BUSINESS_ANALYST skill prefix; let
    Claude read the stats JSON, query past-7d trend via Bash sqlite3 if
    needed, sample raw message text from messages table, then Write the
    final brief markdown.

    Returns md path if Claude succeeded and file exists, else None.
    """
    try:
        from processors._llm_synth import claude_run
    except ImportError:
        log("_llm_synth import failed — cannot use Claude path")
        return None

    json_path = QUEUE_DIR / f"pending_{target}.json"
    md_path = QUEUE_DIR / f"pending_{target}.md"
    if not json_path.exists():
        log(f"compose_via_claude: no JSON {json_path}")
        return None

    # Posix-style relative paths for Claude prompt
    json_rel = json_path.relative_to(ROOT).as_posix()
    md_rel = md_path.relative_to(ROOT).as_posix()
    leads_rel = md_rel + ".leads.jsonl"
    db_rel = (RUNTIME_DIR / "index.db").relative_to(ROOT).as_posix()

    task = f"""請你以 BUSINESS_ANALYST skill 規格 (system prompt 已注入) 對 24h Blacksite _TEMPLATE 情報產出每日整合報表 markdown。

## 輸入資料
- Stats JSON: `{json_rel}` (用 Read tool 讀)
- 今日: {target} (GMT+7)
- SQLite DB: `{db_rel}` — sample raw 訊息文本: `sqlite3 {db_rel} "SELECT text FROM messages WHERE chat_username='X' ORDER BY ts DESC LIMIT 3"`
- INSTANCE.md §1 三層 scope 為 source of truth

## 輸出（兩個檔案）

1. 主報表 markdown: `{md_rel}` (Write tool) — **≤1500 字，給人看，不是給 AI 看**
2. Lead sidecar JSONL: `{leads_rel}` (Write tool) — 每條建議一行 JSON，格式 per BUSINESS_ANALYST skill §11

**先做 Lifecycle 查詢**（寫報表前）：
```
sqlite3 {db_rel} "SELECT lead_id, type, target, state, resolution_at FROM kb_leads WHERE state IN ('resolved_escalate','conflict_flag','escalated') AND COALESCE(resolution_at, triaged_at, emitted_at) >= datetime('now','-1 day')"
```
有結果 → 報表加 `🚨 boss 必看` 段。無結果 → 不加。

## 🔴 報表格式（嚴格遵守，整份 ≤1500 字）

```
🌅 *Blacksite 日報 {target}*

📊 [訊息總量]（平台分布 top 4）· OCR [N] · +[N] 加群 · 🟢[N]蛋黃 🟡[N]蛋白 | 群蛋黃[N]

**🟢 蛋黃洞察**（每條 2 行：現象一句 + 商業意義一句，≤5 條）
· [0.XX] [標題 ≤60 字]
  → [商業意義 ≤30 字]

**🟡 蛋白（top 3）**
· [0.XX] [標題 ≤55 字]
  （沒有可省略這段）

[如有 escalate leads 才加] 🚨 boss 必看
· [lead 摘要一句]

⚠ [alerts，有才寫，inline]

🔥 活躍 Δ：[entity1] +N / [entity2] +N / ...（≤6 個）

📦 [msgs 總] msgs · [entities] entities · [cards] cards · funnel [N]
[小主管狀態一行]
```

## 紀律

🔴 **寫 insight 不寫 observation**：「examplebet 假 payout 截圖在 3 個 TG 群擴散，競品行銷手法」不是「examplebet 出現 N 次」
🔴 **≤1500 字**：超出就截短，不要全列
🔴 **必查 sample SQL** 再下判斷，不要瞎猜 entity 是什麼
🔴 **詞彙精確**：lottery/gambling 及目標國當地語彙直書（內部精確用語，不淨化）

完成後 print: `DONE bytes=<N> path={md_rel} leads=<N>` 到 stdout。
"""

    log(f"compose_via_claude: spawning Claude analyst for {target} ...")
    ok, stdout = claude_run(
        task,
        skill_prefix=True,
        allowed_tools="Read,Write,Edit,Bash,Grep,Glob",
        permission_mode="acceptEdits",
        timeout_s=600.0,
        agent_memory_id="SECTION_CHIEF",  # §15.Y memory injection
    )
    if not ok:
        log(f"compose_via_claude: claude_run returned ok=False; stdout={stdout[:300]}")
        return None
    if not md_path.exists():
        log(f"compose_via_claude: claude returned ok but {md_rel} doesn't exist")
        log(f"  stdout tail: {stdout[-300:]}")
        return None
    log(f"compose_via_claude: ok · {md_path.name} {md_path.stat().st_size}B")
    return md_path


def compose(date: str | None = None, delete_json: bool = False) -> Path:
    """Render brief markdown. Tries Claude analyst (high-tier synthesis with
    BUSINESS_ANALYST skill prefix) first, falls back to pure-Python template
    if Claude unavailable / fails (so cron never produces no brief)."""
    target = date or now_bkk().strftime("%Y-%m-%d")
    json_path = QUEUE_DIR / f"pending_{target}.json"
    if not json_path.exists():
        log(f"compose: no JSON for {target} at {json_path}")
        raise FileNotFoundError(json_path)

    # Path 1: Claude analyst (preferred per boss 5/2 directive)
    claude_path = _compose_via_claude(target)
    if claude_path and claude_path.exists():
        size = claude_path.stat().st_size
        log(f"composed brief md (Claude): {claude_path.name} ({size}B)")
        # P1: ingest lead sidecar emitted by analyst (non-fatal if absent)
        try:
            _ingest_lead_sidecar(claude_path, target)
        except Exception as e:
            log(f"compose: lead sidecar ingest err {type(e).__name__}: {e}")
        if delete_json:
            try:
                json_path.unlink()
            except Exception as e:
                log(f"compose: rm json err {e}")
        print(str(claude_path), flush=True)
        return claude_path

    # Path 2: pure-Python template fallback
    log("compose: falling back to pure-Python template (no LLM)")
    stats = json.loads(json_path.read_text(encoding="utf-8"))
    body = _render_brief(stats, target)
    md_path = QUEUE_DIR / f"pending_{target}.md"
    md_path.write_text(body, encoding="utf-8")
    log(f"composed brief md (template fallback): {md_path.name} ({len(body)} chars)")
    if delete_json:
        try:
            json_path.unlink()
        except Exception as e:
            log(f"compose: rm json err {e}")
    print(str(md_path), flush=True)
    return md_path


def run_daily(window_hours: int = 24, delete_json: bool = True) -> Path:
    """Daemon entry-point: prepare + compose in one shot.
    Wired from blacksite_daemon.py CronTrigger; runs daily.

    delete_json default True: queue otherwise accumulates stale stats bundles
    (per 2026-04-30 audit: pending_2026-04-29.json stuck 24h+ since
    brief_send glob is *.md — bundles get rendered, sent, but raw JSON leaks).
    Pass --no-delete-json (CLI) or delete_json=False to keep for debugging."""
    prepare(window_hours)
    return compose(delete_json=delete_json)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)

    p_prep = sub.add_parser("prepare", help="emit JSON stats bundle to queue/")
    p_prep.add_argument("--hours", type=int, default=24)

    p_show = sub.add_parser("show", help="print the most recent stats bundle")

    p_comp = sub.add_parser("compose", help="render JSON → markdown via pure-Python template")
    p_comp.add_argument("--date", default=None, help="YYYY-MM-DD; defaults to today (Bangkok)")
    p_comp.add_argument("--delete-json", action="store_true", help="rm JSON after composing")

    p_daily = sub.add_parser("daily", help="prepare + compose in one shot (daemon entry)")
    p_daily.add_argument("--hours", type=int, default=24)
    p_daily.add_argument("--no-delete-json", dest="delete_json", action="store_false",
                         default=True,
                         help="(debug) keep JSON in queue after rendering")

    args = parser.parse_args()

    if args.mode == "prepare":
        prepare(args.hours)
    elif args.mode == "show":
        files = sorted(QUEUE_DIR.glob("pending_*.json"))
        if not files:
            print("(no pending briefs)")
        else:
            print(files[-1].read_text(encoding="utf-8"))
    elif args.mode == "compose":
        compose(args.date, args.delete_json)
    elif args.mode == "daily":
        run_daily(args.hours, args.delete_json)


if __name__ == "__main__":
    main()
