"""processors/strategist_daily_pulse.py — Tier 3 daily pulse memo.

Daily 200-word strategist synthesis. Bridges between weekly memo (Sun 21:00)
and the rest of the week — solves the strategist 「reactive only」 antipattern
boss raised 5/7: 7 days only 1 directive issued by strategist; weekly cron
left strategist 「停擺」 between Sundays.

Cron: 20:00 daily GMT+7 via blacksite_daemon.
On-demand: `py processors/strategist_daily_pulse.py --force`.

Flow:
  1. Pull past 24h system_history (decision / milestone / warning / directive / crash)
  2. Pull fleet KPI summary (green/yellow/red counts; yield deltas)
  3. Pull library admission delta (cards / kb_documents / kb_chunks / kb_leads since yesterday)
  4. Pull pending counts (incidents.open / funnel review_state=pending)
  5. Threshold alerts (boss 5/7 design):
        incidents_open > 10        → [ALERT] incident_backlog
        funnel_pending > 50        → [ALERT] funnel_admission_stalled
        cards_24h < 1              → [ALERT] library_admission_dry
        yield_drop > 50% baseline  → [ALERT] yield_collapse
  6. Spawn LLM via _llm_synth.claude_run with CHIEF_STRATEGIST.md skill
  7. Write 200-word pulse to runtime/strategy_memos/daily_pulse_<YYYY-MM-DD>.md
  8. Insert 3-line boss-facing summary to brief queue (DM commander via tg_listen)
  9. Log system_history milestone (or warning if alerts present)

Per CLAUDE.md §6.4: timestamps ISO 8601 +07:00.
Per CLAUDE.md §13.6: log_event milestone on completion.

CLI:
  py processors/strategist_daily_pulse.py            # daily cron entry
  py processors/strategist_daily_pulse.py --force    # boss-trigger / manual rerun today
  py processors/strategist_daily_pulse.py --date 2026-05-07
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

INDEX_DB = RUNTIME_DIR / "index.db"
MEMOS_DIR = RUNTIME_DIR / "strategy_memos"
BRIEFS_QUEUE = RUNTIME_DIR / "briefs" / "queue"
INCIDENTS_DIR = RUNTIME_DIR / "agent_incidents"
KPI_DIR = RUNTIME_DIR / "agent_kpi"

THRESHOLDS = {
    "incidents_open_max": 10,
    "funnel_pending_max": 50,
    "cards_24h_min": 1,
    "yield_drop_pct_max": 0.5,
    "instance_decision_cards_7d_min": 3,        # boss 5/7 商業 KPI: ≥3 high+medium relevance cards/week
    "queue_stale_hours_max": 6,            # queue file 超 6h 未 compose = synthesis 層斷電
}

# decision_tags that count as 「對 the client brand 商業有實質價值」 (per 5/7 audit):
# these exclude `bot_pump_noise_filter` which is purely noise-filtering.
CYP_RELEVANT_DECISION_TAGS = {
    "TA_acquisition", "funnel_competitor_intel", "regulatory_weather",
    "KOL_safety_audit", "payment_behavior", "folk-belief_x_lottery_overlap",
    "brand_seed_pulse", "operator_graph",
}


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def _log(msg: str) -> None:
    line = f"[{now_iso()}] [strategist_daily_pulse] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"strategist_daily_pulse_{now().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def gather_signals(date: datetime) -> dict:
    """Pull 24h fleet snapshot from index.db + filesystem."""
    end_ts = date.replace(hour=20, minute=0, second=0, microsecond=0).isoformat()
    start_dt = date - timedelta(hours=24)
    start_ts = start_dt.isoformat()

    out: dict = {"window_start": start_ts, "window_end": end_ts, "alerts": []}

    con = sqlite3.connect(str(INDEX_DB))
    cur = con.cursor()

    cur.execute(
        "SELECT scope, kind, COUNT(*) FROM system_history "
        "WHERE ts BETWEEN ? AND ? GROUP BY scope, kind ORDER BY 3 DESC",
        (start_ts, end_ts),
    )
    out["history_24h"] = [{"scope": r[0], "kind": r[1], "n": r[2]} for r in cur.fetchall()]

    cur.execute(
        "SELECT id, scope, kind, title FROM system_history "
        "WHERE ts BETWEEN ? AND ? AND kind IN ('decision','milestone','warning','directive','crash') "
        "ORDER BY id DESC LIMIT 25",
        (start_ts, end_ts),
    )
    out["history_top"] = [
        {"id": r[0], "scope": r[1], "kind": r[2], "title": r[3]} for r in cur.fetchall()
    ]

    cur.execute("SELECT COUNT(*) FROM cards WHERE last_built_at >= ?", (start_ts,))
    out["cards_24h"] = cur.fetchone()[0]

    # boss 5/7 商業 KPI: instance_decision_cards_per_week (rolling 7d)
    seven_days_ago = (date - timedelta(days=7)).isoformat()
    cur.execute(
        "SELECT decision_tags FROM cards WHERE last_built_at >= ? AND state='active'",
        (seven_days_ago,),
    )
    cards_7d_tags = [r[0] or "" for r in cur.fetchall()]
    out["cards_7d_total"] = len(cards_7d_tags)
    instance_relevant = 0
    for tags_str in cards_7d_tags:
        tags = {t.strip() for t in tags_str.split(",") if t.strip()}
        if tags & CYP_RELEVANT_DECISION_TAGS:
            instance_relevant += 1
    out["instance_decision_cards_7d"] = instance_relevant
    cur.execute("SELECT COUNT(*) FROM kb_documents WHERE indexed_at >= ?", (start_ts,))
    out["kb_documents_24h"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM kb_chunks WHERE indexed_at >= ?", (start_ts,))
    out["kb_chunks_24h"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM kb_leads WHERE emitted_at >= ?", (start_ts,))
    out["kb_leads_24h"] = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM messages WHERE ts >= ?", (start_ts,))
    out["raw_messages_24h"] = cur.fetchone()[0]

    cur.execute(
        "SELECT review_state, COUNT(*) FROM funnel_edges GROUP BY review_state"
    )
    out["funnel_review_state"] = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute(
        "SELECT join_state, COUNT(*) FROM funnel_edges GROUP BY join_state"
    )
    out["funnel_join_state"] = {r[0]: r[1] for r in cur.fetchall()}

    incidents_open = 0
    if INCIDENTS_DIR.exists():
        for f in INCIDENTS_DIR.glob("INC-*.md"):
            txt = f.read_text(encoding="utf-8", errors="replace")
            if "state: open" in txt:
                incidents_open += 1
    out["incidents_open"] = incidents_open

    fleet = {"green": 0, "yellow": 0, "red": 0, "verify_only": 0, "active": 0, "unknown": 0}
    if KPI_DIR.exists():
        try:
            import yaml as _yaml
        except ImportError:
            _yaml = None
        for f in KPI_DIR.glob("*.yaml"):
            if _yaml is None:
                continue
            try:
                d = _yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            st = d.get("status") or "unknown"
            fleet[st] = fleet.get(st, 0) + 1
            tk = (d.get("target_kpi") or {})
            if tk.get("is_verify_only", True):
                fleet["verify_only"] += 1
            else:
                fleet["active"] += 1
    out["fleet"] = fleet

    # Recent card content for LLM intelligence-governance context
    cur.execute(
        "SELECT title, decision_tags, actionability_score, body_md FROM cards "
        "WHERE last_built_at >= ? AND state='active' "
        "ORDER BY actionability_score DESC LIMIT 10",
        (start_ts,),
    )
    out["recent_cards_sample"] = [
        {
            "title": r[0],
            "decision_tags": r[1],
            "score": r[2],
            "excerpt": (r[3] or "")[:300],
        }
        for r in cur.fetchall()
    ]

    # Stage 3 strategic briefs (top 5, most recent)
    try:
        cur.execute(
            "SELECT commercial_action, cross_case_pattern FROM media_strategic_brief "
            "WHERE processed_at >= ? ORDER BY processed_at DESC LIMIT 5",
            (start_ts,),
        )
        out["stage3_briefs"] = [
            {"commercial_action": r[0], "cross_case_pattern": r[1]}
            for r in cur.fetchall()
        ]
    except Exception:
        out["stage3_briefs"] = []

    con.close()

    if out["incidents_open"] > THRESHOLDS["incidents_open_max"]:
        out["alerts"].append({
            "kind": "incident_backlog",
            "value": out["incidents_open"],
            "threshold": THRESHOLDS["incidents_open_max"],
        })
    pending_funnel = out["funnel_review_state"].get("pending", 0)
    if pending_funnel > THRESHOLDS["funnel_pending_max"]:
        out["alerts"].append({
            "kind": "funnel_admission_stalled",
            "value": pending_funnel,
            "threshold": THRESHOLDS["funnel_pending_max"],
        })
    if out["cards_24h"] < THRESHOLDS["cards_24h_min"]:
        out["alerts"].append({
            "kind": "library_admission_dry",
            "value": out["cards_24h"],
            "threshold": THRESHOLDS["cards_24h_min"],
        })

    # boss 5/7 商業 KPI alert
    if out["instance_decision_cards_7d"] < THRESHOLDS["instance_decision_cards_7d_min"]:
        out["alerts"].append({
            "kind": "instance_commercial_value_low",
            "value": out["instance_decision_cards_7d"],
            "threshold": THRESHOLDS["instance_decision_cards_7d_min"],
            "detail": "對 the client brand 商業有 actionable 價值的 cards 7d < 3 = fleet 對 the client brand 商業綁定 broken",
        })

    # Queue stale detection (synthesis 層斷電 alert)
    queue_dir = RUNTIME_DIR / "cards" / "queue"
    if queue_dir.exists():
        from datetime import datetime as _dt
        oldest_pending_age_h = None
        for qf in queue_dir.glob("pending_*.json"):
            age_h = (date.timestamp() - qf.stat().st_mtime) / 3600
            if oldest_pending_age_h is None or age_h > oldest_pending_age_h:
                oldest_pending_age_h = age_h
        out["queue_oldest_pending_hours"] = round(oldest_pending_age_h, 1) if oldest_pending_age_h else 0
        if oldest_pending_age_h and oldest_pending_age_h > THRESHOLDS["queue_stale_hours_max"]:
            out["alerts"].append({
                "kind": "synthesis_layer_stalled",
                "value": round(oldest_pending_age_h, 1),
                "threshold": THRESHOLDS["queue_stale_hours_max"],
                "detail": "card_builder queue file 超 6h 未 compose; LLM compose 環節斷電",
            })

    return out


def render_pulse(
    signals: dict,
    llm_text: str | None = None,
    date_str: str | None = None,
) -> str:
    """Compose the daily pulse markdown."""
    date_str = date_str or now().strftime("%Y-%m-%d")
    alert_block = ""
    if signals["alerts"]:
        alert_block = "\n## 🔴 ALERT\n\n"
        for a in signals["alerts"]:
            alert_block += f"- `{a['kind']}` — value={a['value']} threshold={a['threshold']}\n"

    fleet = signals["fleet"]
    fleet_line = (
        f"green={fleet.get('green',0)} yellow={fleet.get('yellow',0)} red={fleet.get('red',0)} "
        f"| verify_only={fleet['verify_only']} active={fleet['active']}"
    )

    funnel = signals["funnel_review_state"]
    funnel_line = (
        f"approved={funnel.get('approved',0)} pending={funnel.get('pending',0)} "
        f"rejected={funnel.get('rejected',0)} | joined={signals['funnel_join_state'].get('joined',0)}"
    )

    # Lead with intelligence-governance synthesis, metrics as appendix
    if llm_text:
        synthesis_block = llm_text.strip()
    else:
        synthesis_block = (
            "_(LLM synthesis 未跑 / 失敗 — 請用 `策略長上工` 手動觸發)_"
        )

    body = (
        f"---\n"
        f"memo_kind: strategist_daily_pulse\n"
        f"date: \"{date_str}\"\n"
        f"window: \"{signals['window_start']} → {signals['window_end']}\"\n"
        f"alerts: {len(signals['alerts'])}\n"
        f"---\n\n"
        f"# 策略長日報 — {date_str}\n\n"
        f"{synthesis_block}\n"
        f"{alert_block}"
        f"\n---\n\n"
        f"## 系統數字（附錄）\n\n"
        f"**Fleet** {fleet_line}\n\n"
        f"**Library 24h** raw_msg={signals['raw_messages_24h']} → "
        f"docs={signals['kb_documents_24h']} chunks={signals['kb_chunks_24h']} "
        f"cards={signals['cards_24h']} leads={signals['kb_leads_24h']}\n\n"
        f"**the client brand 商業 KPI** instance_decision_cards_7d={signals.get('instance_decision_cards_7d', 0)} "
        f"/ threshold {THRESHOLDS['instance_decision_cards_7d_min']} "
        f"(total cards 7d={signals.get('cards_7d_total', 0)})\n\n"
        f"**TG funnel** {funnel_line}\n\n"
        f"**Synthesis queue** oldest pending={signals.get('queue_oldest_pending_hours', 0)}h "
        f"(threshold {THRESHOLDS['queue_stale_hours_max']}h) | "
        f"open incidents={signals['incidents_open']}\n\n"
        f"## Top 24h history events\n\n"
    )
    for h in signals["history_top"][:10]:
        body += f"- #{h['id']} `{h['kind']}` `{h['scope']}` — {h['title']}\n"

    return body


def synthesize_with_llm(signals: dict) -> str | None:
    """Optional: spawn subscription-backed LLM for narrative synthesis."""

    # Build compact context — cards + stage3 briefs first, metrics last
    cards_ctx = ""
    for c in signals.get("recent_cards_sample", [])[:8]:
        cards_ctx += f"  [{c.get('score','?')}分] {c.get('title','')} | tags={c.get('decision_tags','')} | {c.get('excerpt','')[:150]}\n"
    briefs_ctx = ""
    for b in signals.get("stage3_briefs", [])[:4]:
        briefs_ctx += f"  [Stage3] pattern={b.get('cross_case_pattern','')}\n"
    alerts_ctx = "; ".join(a["kind"] for a in signals.get("alerts", [])) or "none"

    task = (
        "你是 Blacksite _TEMPLATE 策略長。每日 20:00 你寫一份 200 字內的情報日報給 boss。\n"
        "日報的讀者只有一個人：boss。他要的是「今天情報有什麼商業意義、他該做什麼決定」，\n"
        "不是系統健康報告。系統數字只在出現 ALERT 時才提，且放最後。\n\n"
        "Constitutional north star: 每個 insight 必須回答「這改變 the client brand 的什麼商業決定？」\n\n"
        "輸入資料:\n"
        "--- 24h 高分 cards (最重要) ---\n"
        f"{cards_ctx or '(今日無新 cards)'}\n"
        "--- Stage 3 strategic briefs ---\n"
        f"{briefs_ctx or '(今日無 Stage 3 briefs)'}\n"
        f"--- System alerts: {alerts_ctx} ---\n\n"
        "寫法規則:\n"
        "1. 第一段：今天最重要的 1-2 個商業訊號（operator / KOL / funnel / 監管）是什麼，對 the client brand 意味著什麼\n"
        "2. 第二段：boss 今天應該批准 / 決定 / 觀察什麼（具體行動，不是「繼續監控」這種廢話）\n"
        "3. 如有 alerts，一句帶過放最後（不要放第一段）\n"
        "4. 沒有商業訊號時，直說「今日無高價值情報訊號，系統正常採集中」+ alert 如有\n\n"
        "格式: 繁體中文，純文字兩段，≤200 字。不要 list 不要 emoji 不要表格。直接給結論。"
    )

    task = (
        "You are Blacksite _TEMPLATE Chief Strategist writing the daily pulse for boss.\n"
        "Boss directive 2026-05-14: strategist does NOT design countermeasures, growth plays, or market attacks.\n"
        "Your role is intelligence governance only: assess coverage balance, collection objectivity, evidence strength, "
        "and whether the KB is grounded enough for decision-making.\n\n"
        "Output in Traditional Chinese, around 180-220 words, short paragraphs, no bullets unless necessary.\n"
        "Do NOT tell the client brand what campaign, product, or KOL move to execute.\n"
        "You MAY tell boss what intelligence gap, evidence weakness, login gap, or scope imbalance needs approval.\n\n"
        "Inputs:\n"
        "--- 24h cards (top sample) ---\n"
        f"{cards_ctx or '(no cards)'}\n"
        "--- Stage 3 pattern hints ---\n"
        f"{briefs_ctx or '(no stage3 pattern hints)'}\n"
        f"--- System alerts: {alerts_ctx} ---\n\n"
        "Write 4 short parts in this order:\n"
        "1. What today's intake is strong enough to conclude.\n"
        "2. Where coverage is weak, skewed, or method-biased.\n"
        "3. Whether current KB grounding is objective enough, with weak claims called out.\n"
        "4. If boss approval is needed, only ask for intelligence resources, scope, or account recovery help.\n"
    )

    try:
        from processors.llm_router import (
            codex_model_for_tier,
            run_codex,
            selected_provider,
            should_try_codex,
            should_use_claude_fallback,
        )

        provider = selected_provider()
        if should_try_codex("strategic"):
            result = run_codex(
                task,
                tier="strategic",
                model=codex_model_for_tier("strategic"),
                timeout_s=300,
                sandbox="read-only",
            )
            if result.ok and result.text.strip():
                return result.text.strip()
            _log(f"Codex synthesis failed provider={provider}: {result.error}")

        if should_use_claude_fallback():
            try:
                from processors._llm_synth import claude_run
            except ImportError:
                _log("_llm_synth unavailable — skipping Claude fallback")
                return None
            ok, out = claude_run(
                task=task,
                skill_prefix=False,
                extra_system="",
                agent_memory_id="CHIEF_STRATEGIST",
                timeout_s=300,
                max_retries=2,
            )
            if ok and out and out.strip():
                return out.strip()
    except Exception as e:
        _log(f"LLM synthesis failed: {e}")
    return None


def emit_to_brief_queue(
    signals: dict,
    pulse_path: Path,
    llm_text: str | None = None,
    date_str: str | None = None,
) -> None:
    """Write intelligence-governance-led summary into brief queue for tg_listen DM."""
    BRIEFS_QUEUE.mkdir(parents=True, exist_ok=True)
    date_str = date_str or now().strftime("%Y-%m-%d")
    out_path = BRIEFS_QUEUE / f"pending_{date_str}_strategist_pulse.md"

    alert_suffix = ""
    if signals["alerts"]:
        kinds = ", ".join(a["kind"] for a in signals["alerts"])
        alert_suffix = f" ⚠ {kinds}"

    if llm_text and llm_text.strip():
        # Truncate to first ~250 chars for TG readability
        narrative = llm_text.strip()[:250]
        if len(llm_text.strip()) > 250:
            narrative += "…"
    else:
        narrative = (
            f"今日無 LLM 摘要——cards_24h={signals['cards_24h']} "
            f"instance_cards_7d={signals.get('instance_decision_cards_7d',0)}"
        )

    md = (
        f"[STRATEGY_PULSE] 策略長日報 — {date_str}{alert_suffix}\n\n"
        f"{narrative}\n\n"
        f"full memo: `{pulse_path.relative_to(ROOT).as_posix()}`\n"
    )
    out_path.write_text(md, encoding="utf-8")
    _log(f"brief queue wrote {out_path.name}")


def run_once(force: bool = False, date_override: str | None = None) -> dict:
    if date_override:
        date = datetime.fromisoformat(date_override).replace(tzinfo=TZ)
    else:
        date = now()

    date_str = date.strftime("%Y-%m-%d")
    pulse_path = MEMOS_DIR / f"daily_pulse_{date_str}.md"
    MEMOS_DIR.mkdir(parents=True, exist_ok=True)

    if pulse_path.exists() and not force:
        _log(f"pulse for {date_str} already exists; skip (use --force to rerun)")
        return {"skipped": True, "path": str(pulse_path)}

    _log(f"gathering signals for {date_str}")
    signals = gather_signals(date)
    _log(
        f"signals: raw_msg={signals['raw_messages_24h']} cards_24h={signals['cards_24h']} "
        f"docs_24h={signals['kb_documents_24h']} alerts={len(signals['alerts'])} "
        f"incidents_open={signals['incidents_open']}"
    )

    llm_text = synthesize_with_llm(signals)

    md = render_pulse(signals, llm_text, date_str=date_str)
    pulse_path.write_text(md, encoding="utf-8")
    _log(f"wrote {pulse_path}")

    emit_to_brief_queue(signals, pulse_path, llm_text=llm_text, date_str=date_str)

    try:
        from processors.history_log import log_event
        kind = "warning" if signals["alerts"] else "milestone"
        log_event(
            actor="CHIEF_STRATEGIST",
            kind=kind,
            scope="strategist",
            title=f"Daily pulse {date_str} — alerts={len(signals['alerts'])}",
            body=(
                f"Fleet: {signals['fleet']}\n"
                f"Library 24h: cards={signals['cards_24h']} docs={signals['kb_documents_24h']} "
                f"chunks={signals['kb_chunks_24h']} leads={signals['kb_leads_24h']}\n"
                f"Raw msg 24h: {signals['raw_messages_24h']}\n"
                f"Funnel review_state: {signals['funnel_review_state']}\n"
                f"Open incidents: {signals['incidents_open']}\n"
                f"Alerts: {signals['alerts']}\n"
            ),
            refs=[str(pulse_path.relative_to(ROOT).as_posix())],
        )
    except Exception as e:
        _log(f"log_event failed: {e}")

    try:
        from processors.org_task_audit_refresh import refresh_org_task_audit
        refresh_org_task_audit(f"strategist_daily_pulse:{date_str}")
    except Exception:
        pass

    return {"skipped": False, "path": str(pulse_path), "alerts": len(signals["alerts"])}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--force", action="store_true", help="rerun even if today's pulse exists")
    p.add_argument("--date", default=None, help="YYYY-MM-DD override (default: today GMT+7)")
    args = p.parse_args()
    out = run_once(force=args.force, date_override=args.date)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
