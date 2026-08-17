"""Render the boss-facing mobile audit page.

Boss-facing means readable Traditional Chinese first. Raw system strings,
mojibake names, and verbose machine logs stay out of this surface.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32" and sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
INDEX_DB = RUNTIME / "index.db"
REPORT_DIR = RUNTIME / "reports"
DEFAULT_OUTPUT = REPORT_DIR / "boss_audit_mobile.html"
MIRROR_OUTPUT = REPORT_DIR / "boss_audit_mobile" / "index.html"

WORK_AUDIT_JSON = REPORT_DIR / "section_chief_work_audit.json"
REPAIR_TASKS_JSON = RUNTIME / "field_agent_repair_tasks" / "current.json"
FACTORY_WORK_ORDERS_JSON = RUNTIME / "field_agent_work_orders" / "current.json"
FACTORY_CHECKINS_JSON = RUNTIME / "field_agent_checkins" / "current.json"
ACTIVITY_GOVERNOR_JSON = RUNTIME / "activity_governor" / "current.json"
SEED_CURRENT_JSON = RUNTIME / "seed_intelligence" / "current.json"
SEED_AUDIT_JSON = RUNTIME / "seed_intelligence" / "audit" / "current.json"
SEED_PORTFOLIO_JSON = RUNTIME / "seed_intelligence" / "strategy_portfolio.json"

KPI_DIR = RUNTIME / "agent_kpi"
INCIDENTS_DIR = RUNTIME / "agent_incidents"
STRATEGY_DIRECTIVES_DIR = RUNTIME / "strategy_directives"
STRATEGY_MEMOS_DIR = RUNTIME / "strategy_memos"
BRIEF_QUEUE_DIR = RUNTIME / "briefs" / "queue"
BRIEF_SENT_DIR = RUNTIME / "briefs" / "sent"
DIRECTIVE_AUDIT = RUNTIME / "strategy_directive_audit.jsonl"

SOURCE_FILES = [
    INDEX_DB,
    WORK_AUDIT_JSON,
    REPAIR_TASKS_JSON,
    FACTORY_WORK_ORDERS_JSON,
    FACTORY_CHECKINS_JSON,
    ACTIVITY_GOVERNOR_JSON,
    SEED_CURRENT_JSON,
    SEED_AUDIT_JSON,
    SEED_PORTFOLIO_JSON,
    DIRECTIVE_AUDIT,
]
SOURCE_DIRS = [
    KPI_DIR,
    INCIDENTS_DIR,
    STRATEGY_DIRECTIVES_DIR,
    STRATEGY_MEMOS_DIR,
    BRIEF_QUEUE_DIR,
    BRIEF_SENT_DIR,
]

STATE_ZH = {
    "collecting": "採集中",
    "login_only": "只驗登入",
    "scanner_missing": "缺採集器",
    "scaffold_only": "只有骨架",
    "blocked": "卡關",
    "no_output": "無產出",
    "dormant": "休眠",
    "assigned": "已派工",
    "accepted": "已接工",
    "dispatching": "派工中",
    "report_due": "待回報",
    "needs_repair": "需要修復",
    "strategist_review": "策略長複核",
}
STATUS_ZH = {
    "green": "綠",
    "yellow": "黃",
    "red": "紅",
    "live": "上線",
    "paused": "暫停",
    "unknown": "未知",
}
TASK_ZH = {
    "account_recovery": "帳號恢復",
    "build_or_assign_collector": "接上採集器",
    "diagnose_zero_output": "診斷零產出",
    "activate_feed_harvest": "啟動 feed 採集",
    "assign_mission_or_mark_reserve": "派任務或標備援",
    "quality_sample": "品質抽檢",
}
DECISION_ZH = {
    "continue_collecting": "繼續採集",
    "await_first_cadence": "等第一輪 4h 回報",
    "repair_or_dispatch": "修復或重派",
    "escalate_strategist": "升級策略長",
    "dispatch_failed": "派工失敗",
    "activity_governor_defer": "行為 gate 延後",
}
GATE_ZH = {
    "allow": "允許",
    "defer_outside_local_human_window": "延後：不在泰國人類使用時段",
    "defer_outside_persona_window": "延後：不在 persona 排程窗",
    "defer_cooldown": "延後：冷卻中",
    "defer_daily_budget": "延後：今日上限已滿",
}


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_window(value: str) -> timedelta:
    match = re.match(r"^(\d+)([hdw])$", value.strip().lower())
    if not match:
        raise SystemExit("window must look like 12h, 24h, 7d, or 2w")
    n = int(match.group(1))
    return {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[match.group(2)]


def cutoff_iso(window: str) -> str:
    return (now() - parse_window(window)).isoformat(timespec="seconds")


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def short_text(value: object, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def internal_text_summary(value: object, fallback: str = "任務細節已記錄；本頁只顯示人類可讀摘要。") -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "-"
    exact = {
        "Run a low-impact mobile viewport smoke, inspect page_state_check evidence, then repair selector/IP/login/target scope.": "用手機尺寸做低干擾檢測，再依截圖與 log 修 selector、IP、登入或目標範圍。",
        "Either dispatch a concrete collection job or mark this agent as reserve so it is not reported as completed work.": "派實際採集任務；若只是養號，就標成備援，不能回報成完成工作。",
        "Draw recent raw samples, judge whether the output changes the client brand decisions, then update signal_noise and repair prompt/selectors if weak.": "抽樣檢查 raw 是否能改變 the client brand 決策；若訊噪比弱，就修 prompt 或 selector。",
        "Create or connect a real collector for this surface; login verification alone must not count as work.": "接上真正採集器；只驗登入不能算完成任務。",
        "Attach feed_harvest or a platform-specific collector before marking the agent active.": "先接 feed_harvest 或平台採集器，才能標成 active。",
    }
    if text in exact:
        return exact[text]
    contains_cjk = any("\u3400" <= ch <= "\u9fff" for ch in text)
    ascii_letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if not contains_cjk and ascii_letters > 30:
        lowered = text.lower()
        if "build collector" in lowered:
            return "補上採集器，或改派給已有採集能力的情報員。"
        if "diagnose collector" in lowered or "zero mission output" in lowered:
            return "診斷零產出原因，檢查 selector、登入、IP 與目標範圍。"
        if "sample mission output" in lowered or "signal/noise" in lowered:
            return "抽樣查核輸出品質，確認是否能支援 the client brand 決策。"
        if "assign mission" in lowered or "reserve" in lowered:
            return "派實際任務，或明確標成備援帳號。"
        if "folk-belief" in lowered:
            return "在地信仰/幸運數字/彩票相關內容採集。"
        if "sports" in lowered or "football" in lowered or "combat-sport" in lowered:
            return "在地體育、KOL、賽事內容採集。"
        if "ai" in lowered or "tech" in lowered:
            return "AI、工具、科技社群內容採集。"
        return fallback
    return short_text(text)


def fmt_ts(value: object) -> str:
    if not value:
        return "-"
    text = str(value)
    try:
        dt = datetime.fromisoformat(text)
        return dt.astimezone(TZ).strftime("%m-%d %H:%M") + " GMT+7"
    except Exception:
        return text[:19]


def rel_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    try:
        fm = yaml.safe_load(text[3:end]) or {}
    except Exception:
        fm = {}
    return fm, text[end + 4 :]


def latest_mtime() -> float:
    latest = 0.0
    for path in SOURCE_FILES:
        if path.exists():
            latest = max(latest, path.stat().st_mtime)
    for directory in SOURCE_DIRS:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.is_file():
                latest = max(latest, path.stat().st_mtime)
    return latest


def needs_update(output: Path) -> bool:
    return not output.exists() or latest_mtime() > output.stat().st_mtime


def readable_name(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if "\ufffd" in text or any("\ue000" <= ch <= "\uf8ff" for ch in text):
        return None
    signal = 0
    unsupported = 0
    for ch in text:
        if ch.isspace() or ch in "_-./:@#[]()（）+&|":
            continue
        if ch.isascii() or "\u0e00" <= ch <= "\u0e7f" or "\u3400" <= ch <= "\u9fff":
            signal += 1
        else:
            unsupported += 1
    if signal == 0 or unsupported > signal:
        return None
    return short_text(text, 64)


def tone_for_status(value: object) -> str:
    text = str(value or "").lower()
    if text in {"red", "critical", "blocked", "strategist_review"}:
        return "bad"
    if text in {"yellow", "warning", "needs_repair", "report_due", "dispatching"}:
        return "warn"
    if text in {"green", "pass", "collecting", "ok"}:
        return "ok"
    return ""


def chip(label: str, value: object) -> str:
    return f'<div class="chip"><b>{esc(value)}</b><span>{esc(label)}</span></div>'


def card(title: str, meta: str = "", lines: list[object] | None = None, tone: str = "") -> str:
    cls = f" {tone}" if tone else ""
    body = ""
    if lines:
        body = "<p>" + "<br>".join(esc(line) for line in lines if line is not None and str(line) != "") + "</p>"
    return (
        f'<article class="row-card{cls}">'
        f'<div class="row-head"><h3>{esc(title)}</h3><span>{esc(meta)}</span></div>'
        f"{body}</article>"
    )


def db_counts(cutoff: str) -> dict:
    out = {"cards": 0, "leads": 0, "messages": 0}
    if not INDEX_DB.exists():
        return out
    try:
        con = sqlite3.connect(str(INDEX_DB))
        out["cards"] = con.execute(
            "SELECT COUNT(*) FROM cards WHERE last_built_at >= ? AND state='active'",
            (cutoff,),
        ).fetchone()[0]
        out["leads"] = con.execute("SELECT COUNT(*) FROM kb_leads WHERE emitted_at >= ?", (cutoff,)).fetchone()[0]
        out["messages"] = con.execute("SELECT COUNT(*) FROM messages WHERE ts >= ?", (cutoff,)).fetchone()[0]
        con.close()
    except Exception:
        pass
    return out


def load_kpis() -> dict[str, dict]:
    out = {}
    if not KPI_DIR.exists():
        return out
    for path in sorted(KPI_DIR.glob("*.yaml")):
        if path.parent.name == "_retired":
            continue
        data = load_yaml(path)
        if data:
            out[data.get("agent_id") or path.stem] = data
    return out


def latest_files(directory: Path, pattern: str, limit: int = 6) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def open_incidents() -> list[dict]:
    rows = []
    if not INCIDENTS_DIR.exists():
        return rows
    for path in sorted(INCIDENTS_DIR.glob("INC-*.md")):
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        if fm.get("state") == "open":
            fm["_path"] = path
            rows.append(fm)
    return sorted(rows, key=lambda x: x.get("opened_at", ""), reverse=True)


def recent_reports(cutoff: str) -> list[dict]:
    rows = []
    sources = [
        ("已發送 brief", BRIEF_SENT_DIR, "sent_*.md"),
        ("待發送 brief", BRIEF_QUEUE_DIR, "pending_*.md"),
        ("策略 memo", STRATEGY_MEMOS_DIR, "*.md"),
    ]
    for kind, directory, pattern in sources:
        for path in latest_files(directory, pattern, 6):
            ts = datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(timespec="seconds")
            if ts >= cutoff:
                rows.append({"kind": kind, "path": path, "ts": ts})
    return sorted(rows, key=lambda x: x["ts"], reverse=True)[:12]


def latest_directives(limit: int = 6) -> list[dict]:
    rows = []
    for path in latest_files(STRATEGY_DIRECTIVES_DIR, "*.yaml", limit):
        try:
            docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if isinstance(d, dict)]
        except Exception:
            docs = []
        merged = {}
        for doc in docs:
            merged.update(doc)
        directives = [d for d in merged.get("directives", []) if isinstance(d, dict)]
        topic = "-"
        if directives:
            first = directives[0]
            topic = first.get("topic") or first.get("agent_id") or first.get("target") or first.get("kind") or "-"
        rows.append({
            "path": path,
            "issued_at": merged.get("issued_at"),
            "status": merged.get("status") or "active",
            "count": len(directives),
            "topic": topic,
        })
    return rows


def agent_cards(kpis: dict[str, dict]) -> list[str]:
    severity = {"red": 0, "yellow": 1, "unknown": 2, "green": 3}
    items = sorted(kpis.items(), key=lambda kv: (severity.get(str(kv[1].get("status")), 2), kv[0]))
    cards = []
    for agent_id, kpi in items[:32]:
        cur = kpi.get("current_kpi") or {}
        mission = kpi.get("mission_status") or {}
        status = str(kpi.get("status") or "unknown")
        state = str(mission.get("state") or "unknown")
        cards.append(card(
            agent_id,
            f"狀態={STATUS_ZH.get(status, status)} / 任務={STATE_ZH.get(state, state)} / 最近查核={fmt_ts(kpi.get('last_evaluated_at'))}",
            [
                f"24h 產出：{cur.get('msg_yield_24h', '-')}",
                f"任務判斷：{internal_text_summary(mission.get('action') or mission.get('reason') or '-')}",
            ],
            tone_for_status(status if status != "green" else state),
        ))
    return cards


def work_audit_cards(work_audit: dict) -> list[str]:
    cards = []
    for row in (work_audit.get("rows") or [])[:40]:
        state = str(row.get("mission_state") or "unknown")
        if state == "collecting":
            continue
        cards.append(card(
            str(row.get("agent_id") or "?"),
            f"任務={STATE_ZH.get(state, state)}",
            [
                f"小主管動作：{internal_text_summary(row.get('action') or '-')}",
                f"24h 任務 raw：{row.get('raw_mission_24h', 0)} / 全部 raw：{row.get('raw_total_24h', 0)}",
            ],
            tone_for_status(state),
        ))
    return cards


def repair_cards(tasks: list[dict]) -> list[str]:
    cards = []
    for task in tasks[:30]:
        task_type = str(task.get("task_type") or "-")
        cards.append(card(
            str(task.get("agent_id") or task.get("task_id") or "?"),
            f"{task.get('priority', '-')} / {TASK_ZH.get(task_type, task_type)} / due={fmt_ts(task.get('due_at'))}",
            [
                f"下一步：{internal_text_summary(task.get('next_action') or '-')}",
                f"目前任務狀態：{STATE_ZH.get(str(task.get('mission_state')), str(task.get('mission_state') or '-'))}",
            ],
            "bad" if task.get("priority") == "P0" else "warn",
        ))
    return cards


def factory_cards(factory_orders: dict, checkins: list[dict]) -> list[str]:
    latest = {}
    for checkin in checkins:
        order_id = checkin.get("order_id")
        if order_id and order_id not in latest:
            latest[order_id] = checkin
    cards = []
    for order in (factory_orders.get("orders") or [])[:40]:
        state = str(order.get("state") or "unknown")
        dispatch = order.get("dispatch") or {}
        gate = dispatch.get("last_activity_gate") or {}
        review = (latest.get(order.get("order_id")) or {}).get("section_chief_review") or order.get("last_review") or {}
        cards.append(card(
            str(order.get("agent_id") or "?"),
            f"{STATE_ZH.get(state, state)} / 下次回報={fmt_ts(order.get('next_checkin_due_at'))}",
            [
                f"焦點：{internal_text_summary(order.get('primary_focus') or '-')}",
                f"工單：{TASK_ZH.get(str(order.get('task_type')), str(order.get('task_type') or '-'))}",
                f"小主管判斷：{DECISION_ZH.get(str(review.get('decision')), str(review.get('decision') or '-'))}",
                f"行為 gate：{GATE_ZH.get(str(gate.get('decision') or 'allow'), str(gate.get('decision') or 'allow'))}",
            ],
            tone_for_status(state if not gate or gate.get("allow", True) else "yellow"),
        ))
    return cards


def seed_cards(seed: dict, audit: dict, portfolio: dict, governor: dict) -> list[str]:
    summary = seed.get("summary") or {}
    gate_summary = governor.get("summary") or {}
    findings = audit.get("findings") or []
    lanes = portfolio.get("lanes") or []
    directives = portfolio.get("directives") or []
    watchlist = seed.get("watchlist") or []
    actions = seed.get("actions") or []
    pending = [a for a in actions if a.get("status") == "pending_boss_approval"]
    cards = [
        card(
            "Seed 智慧模式總覽",
            f"小主管={audit.get('verdict', 'missing')} / 策略長指令={len(directives)}",
            [
                f"raw 候選：{summary.get('raw_candidates', summary.get('candidates', 0))}",
                f"保留候選：{summary.get('candidates', 0)}",
                f"已剔除不可讀/不適用：{summary.get('discarded_candidates', 0)}",
                f"追蹤池：{summary.get('watchlist', 0)} / 待 Boss 批准：{len(pending)}",
            ],
            tone_for_status(audit.get("verdict")),
        ),
        card(
            "行為節制 Gate",
            "泰國人類時間 / persona 排程 / 冷卻 / 每日上限",
            [f"{GATE_ZH.get(k, k)}：{v}" for k, v in sorted(gate_summary.items())],
            "warn" if any(str(k).startswith("defer") for k in gate_summary) else "ok",
        ),
    ]
    if lanes:
        cards.append(card(
            "策略長 Portfolio",
            "每條情報線的 seed 缺口",
            [
                f"{lane.get('vertical')}: {lane.get('watchlist_count')}/{lane.get('target_watchlist')}，缺口 {lane.get('gap')}，待批准 {lane.get('pending_boss_approval')}"
                for lane in lanes
            ],
            "warn" if directives else "ok",
        ))
    if findings:
        cards.append(card(
            "小主管稽核發現",
            f"findings={len(findings)}",
            [f"[{f.get('severity')}] {short_text(f.get('finding'))}：{short_text(f.get('detail'), 160)}" for f in findings[:8]],
            "warn",
        ))
    readable_watch = []
    hidden = 0
    for item in watchlist:
        name = readable_name(item.get("display_name"))
        if not name:
            hidden += 1
            continue
        readable_watch.append(f"{item.get('source_agent')} {item.get('platform')} {name} score={item.get('seed_score')}")
    if readable_watch:
        lines = readable_watch[:10]
        if hidden:
            lines.append(f"另有 {hidden} 筆不可讀名稱已從頁面隱藏。")
        cards.append(card("目前追蹤池 Top Seed", "只顯示可讀名稱", lines, "ok"))
    return cards


def directive_cards() -> list[str]:
    return [
        card(row["path"].name, f"{fmt_ts(row.get('issued_at'))} / {row.get('status')} / {row.get('count')} directives", [short_text(row.get("topic")), rel_path(row["path"])])
        for row in latest_directives()
    ]


def incident_cards(incidents: list[dict]) -> list[str]:
    return [
        card(
            str(inc.get("incident_id")),
            f"{inc.get('severity')} / {fmt_ts(inc.get('opened_at'))}",
            [f"agent={inc.get('agent_id')}", f"類型={inc.get('violation_kind')}", rel_path(inc["_path"])],
            tone_for_status(inc.get("severity")),
        )
        for inc in incidents
    ]


def report_cards(reports: list[dict]) -> list[str]:
    return [card(row["path"].name, f"{row['kind']} / {fmt_ts(row['ts'])}", [rel_path(row["path"])]) for row in reports]


def render_html(since: str, reason: str) -> str:
    cutoff = cutoff_iso(since)
    generated_at = now_iso()
    kpis = load_kpis()
    work_audit = load_json(WORK_AUDIT_JSON, {"counts": {}, "health_counts": {}, "rows": []})
    repair_tasks = (load_json(REPAIR_TASKS_JSON, {"tasks": []}) or {}).get("tasks", [])
    factory_orders = load_json(FACTORY_WORK_ORDERS_JSON, {"state_counts": {}, "orders": []})
    factory_checkins = (load_json(FACTORY_CHECKINS_JSON, {"checkins": []}) or {}).get("checkins", [])
    governor = load_json(ACTIVITY_GOVERNOR_JSON, {"summary": {}, "gates": []})
    seed = load_json(SEED_CURRENT_JSON, {"summary": {}, "watchlist": [], "actions": [], "candidates": []})
    seed_audit = load_json(SEED_AUDIT_JSON, {"verdict": "missing", "summary": {}, "findings": []})
    seed_portfolio = load_json(SEED_PORTFOLIO_JSON, {"audit_verdict": "missing", "lanes": [], "directives": []})
    incidents = open_incidents()
    reports = recent_reports(cutoff)
    counts = db_counts(cutoff)
    status_counts = {}
    for kpi in kpis.values():
        key = str(kpi.get("status") or "unknown")
        status_counts[key] = status_counts.get(key, 0) + 1
    mission_counts = work_audit.get("counts") or {}
    seed_summary = seed.get("summary") or {}

    work_html = "".join(work_audit_cards(work_audit))
    repair_html = "".join(repair_cards(repair_tasks))
    factory_html = "".join(factory_cards(factory_orders, factory_checkins))
    seed_html = "".join(seed_cards(seed, seed_audit, seed_portfolio, governor))
    agent_html = "".join(agent_cards(kpis))
    directive_html = "".join(directive_cards())
    incident_html = "".join(incident_cards(incidents))
    report_html = "".join(report_cards(reports))

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Blacksite 任務查核</title>
  <style>
    :root {{
      --bg:#f7f6f2; --paper:#fff; --ink:#181818; --muted:#666; --line:#dedbd2;
      --accent:#176b87; --ok:#1e7b4f; --warn:#9a650d; --bad:#b3261e;
      --soft-green:#e9f6ef; --soft-amber:#fff3d8; --soft-red:#fdebea; --soft-muted:#f0efeb;
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0; background:var(--bg); color:var(--ink);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft JhengHei",sans-serif;
      line-height:1.45;
    }}
    main {{ width:min(760px,100%); margin:0 auto; padding:18px 14px 34px; }}
    header {{ padding:8px 2px 14px; }}
    .eyebrow {{ color:var(--accent); font-size:13px; font-weight:700; }}
    h1 {{ margin:5px 0 8px; font-size:26px; line-height:1.14; letter-spacing:0; }}
    .sub {{ margin:0; color:var(--muted); font-size:13px; }}
    .chips {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:14px 0 18px; }}
    .chip {{ min-height:70px; border:1px solid var(--line); background:var(--paper); border-radius:8px; padding:10px; }}
    .chip b {{ display:block; font-size:21px; line-height:1; margin-bottom:8px; }}
    .chip span {{ display:block; color:var(--muted); font-size:12px; }}
    section {{ margin-top:18px; }}
    h2 {{ margin:0 0 8px; font-size:18px; letter-spacing:0; }}
    .note {{ margin:-3px 0 10px; color:var(--muted); font-size:13px; }}
    .stack {{ display:grid; gap:8px; }}
    .row-card {{
      border:1px solid var(--line); border-left:4px solid var(--accent);
      background:var(--paper); border-radius:8px; padding:10px 11px; overflow-wrap:anywhere;
    }}
    .row-card.ok {{ border-left-color:var(--ok); background:var(--soft-green); }}
    .row-card.warn {{ border-left-color:var(--warn); background:var(--soft-amber); }}
    .row-card.bad {{ border-left-color:var(--bad); background:var(--soft-red); }}
    .row-card.muted {{ border-left-color:var(--muted); background:var(--soft-muted); }}
    .row-head {{ display:grid; gap:4px; }}
    .row-head h3 {{ margin:0; font-size:15px; line-height:1.25; }}
    .row-head span {{ color:var(--muted); font-size:12px; }}
    .row-card p {{ margin:7px 0 0; font-size:13px; color:#303030; }}
    footer {{ margin-top:20px; color:var(--muted); font-size:12px; }}
    @media (min-width:640px) {{
      .chips {{ grid-template-columns:repeat(4,minmax(0,1fr)); }}
      h1 {{ font-size:30px; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div class="eyebrow">Blacksite 任務情報查核</div>
    <h1>情報員 / 小主管 / 策略長</h1>
    <p class="sub">instance={esc(ACTIVE_INSTANCE)} / window={esc(since)} / generated_at={esc(generated_at)} / trigger={esc(reason or "manual")}</p>
  </header>

  <div class="chips">
    {chip("情報員 綠/黃/紅", f"{status_counts.get('green',0)}/{status_counts.get('yellow',0)}/{status_counts.get('red',0)}")}
    {chip("採集中 / 只驗登入", f"{mission_counts.get('collecting',0)}/{mission_counts.get('login_only',0)}")}
    {chip("缺採集器 / 無產出", f"{mission_counts.get('scanner_missing',0)}/{mission_counts.get('no_output',0)}")}
    {chip("修復任務", len(repair_tasks))}
    {chip("工廠線工單", len(factory_orders.get("orders") or []))}
    {chip("Seed 候選 / 追蹤池", f"{seed_summary.get('candidates',0)} / {seed_summary.get('watchlist',0)}")}
    {chip("已剔除不可讀", seed_summary.get("discarded_candidates", 0))}
    {chip("cards / leads", f"{counts['cards']} / {counts['leads']}")}
  </div>

  <section>
    <h2>Seed 智慧模式</h2>
    <p class="note">候選人、追蹤池、行為節制、小主管稽核與策略長 portfolio。不可讀名稱已從這份頁面與 seed 輸出刪除。</p>
    <div class="stack">{seed_html or card("Seed 報告尚未產生", "", ["等待下一輪 seed pipeline。"], "warn")}</div>
  </section>

  <section>
    <h2>小主管查核焦點</h2>
    <p class="note">只列需要注意的情報員；已正常採集者不佔版面。</p>
    <div class="stack">{work_html or card("沒有異常情報員", "", ["目前小主管沒有標出任務層異常。"], "ok")}</div>
  </section>

  <section>
    <h2>修復任務</h2>
    <p class="note">情報員不達標時，小主管應該派修復任務，不是只回報登入正常。</p>
    <div class="stack">{repair_html or card("沒有待修復任務", "", ["目前沒有等待修復的情報員。"], "ok")}</div>
  </section>

  <section>
    <h2>工廠線派工與 4h 回報</h2>
    <p class="note">情報員接工單後定期回報；派工前先過行為 gate。</p>
    <div class="stack">{factory_html or card("沒有工廠線工單", "", ["目前沒有等待回報的情報員工單。"], "ok")}</div>
  </section>

  <section>
    <h2>情報員狀態</h2>
    <p class="note">KPI 與任務狀態摘要。這裡不展開 raw log。</p>
    <div class="stack">{agent_html or card("沒有 KPI", "", ["尚未找到 agent_kpi YAML。"], "warn")}</div>
  </section>

  <section>
    <h2>策略長 / Directive</h2>
    <p class="note">最近策略長交辦與組織調整。</p>
    <div class="stack">{directive_html or card("沒有 directive", "", ["尚未找到 strategy_directives。"], "warn")}</div>
  </section>

  <section>
    <h2>事件與報告</h2>
    <p class="note">open incident 與最近 brief/memo。</p>
    <div class="stack">{incident_html or card("沒有 open incident", "", ["目前沒有 open incident。"], "ok")}{report_html}</div>
  </section>

  <footer>
    file={esc(str(DEFAULT_OUTPUT))}<br>
    cutoff={esc(cutoff)}<br>
    refresh policy=event-triggered on review / directive / report / Commander command
  </footer>
</main>
</body>
</html>
"""


def write_outputs(html_text: str, output: Path, mirror: bool = True) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html_text, encoding="utf-8")
    if mirror:
        MIRROR_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        MIRROR_OUTPUT.write_text(html_text, encoding="utf-8")


def refresh(reason: str = "manual", since: str = "7d", output: Path | None = None) -> Path:
    target = output or DEFAULT_OUTPUT
    write_outputs(render_html(since, reason), target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Render mobile task-intelligence audit HTML")
    parser.add_argument("--since", default="7d", help="window: 12h, 24h, 7d, 2w")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--reason", default="manual")
    parser.add_argument("--if-needed", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--print-path", action="store_true")
    args = parser.parse_args()

    output = Path(args.output)
    if args.if_needed and not args.force and not needs_update(output):
        if args.print_path:
            print(output)
        return 0
    path = refresh(args.reason, args.since, output)
    if args.print_path:
        print(path)
    else:
        print(json.dumps({"output": str(path), "generated_at": now_iso()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
