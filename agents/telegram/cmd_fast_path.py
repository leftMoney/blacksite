"""
M6.5 fast-path — pattern-match boss's TG DM to a known intent and reply
directly from the listener via Python (no Claude tokens, sub-second latency).

Architecture (boss-corrected 2026-04-29):
  Boss DMs → boss_dm_capturer detects → cmd_handler.write_inbox calls
  try_fast_path(text). If a pattern matches → reply written DIRECTLY to
  cmd/outbox (skips scheduled-task entirely). If no pattern matches →
  message goes to cmd/inbox/ with `freeform=True` flag for the (now lower-
  frequency cron `*/15`) scheduled-task to pick up and reason about.

Goal: 90%+ of boss's DMs (status / 卡 X / approve N / etc.) handled in
< 1 second at 0 Claude-token cost. Only genuinely free-form questions
("下個 example_event 該推嗎") trigger the scheduled-task.

Token impact: ~10× reduction in scheduled-task baseline burn (2.2M → ~150K
tokens/day for cmd path), plus user-perceived speed jumps from 2-min latency
to instant for common queries.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
DB_PATH = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "index.db"
CMD_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "cmd"
SENT_DIR = CMD_DIR / "sent"
OUTBOX_DIR = CMD_DIR / "outbox"
PROCESSED_DIR = CMD_DIR / "processed"
REPORT_HTML = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "reports" / "boss_audit_mobile.html"

def _first_existing(*paths: str | None) -> str | None:
    for raw in paths:
        if raw and Path(raw).exists():
            return raw
    return None


PYTHON = (
    _first_existing(
        os.environ.get("BLACKSITE_PYTHON"),
        os.environ.get("BLACKSITE_HOST_PYTHON"),
        "C:/Users/<YOUR_USERNAME>/AppData/Local/Programs/Python/Python313/python.exe",
        "C:/Users/<YOUR_USERNAME>/AppData/Local/Programs/Python/Python312/python.exe",
        "C:/Users/<YOUR_USERNAME>/AppData/Local/Programs/Python/Launcher/py.exe",
    )
    or "py"
)
PYTHONW = (
    _first_existing(
        os.environ.get("BLACKSITE_PYTHONW"),
        os.environ.get("BLACKSITE_HOST_PYTHONW"),
        "C:/Users/<YOUR_USERNAME>/AppData/Local/Programs/Python/Python313/pythonw.exe",
        "C:/Users/<YOUR_USERNAME>/AppData/Local/Programs/Python/Python312/pythonw.exe",
    )
    or PYTHON
)
TZ = timezone(timedelta(hours=7))


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# Subprocess + DB helpers
# ----------------------------------------------------------------------

def _run_kb_query(args: list[str]) -> str:
    cmd = [PYTHON, str(ROOT / "processors" / "kb_query.py")] + args
    # Suppress console window: PYTHON=py.exe (console subsystem) spawned
    # from listener (pythonw, GUI subsystem) otherwise pops cmd per fast-path
    # query (every regex-matched DM hits this).
    no_window_kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=30, cwd=str(ROOT),
                           **no_window_kw)
        if r.returncode != 0:
            return f"⚠ err: {(r.stderr or '')[:200]}"
        return r.stdout
    except subprocess.TimeoutExpired:
        return "⚠ kb_query timeout"
    except Exception as e:
        return f"⚠ subprocess err: {type(e).__name__}: {e}"


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _spawn_commander_op(args: list[str]) -> tuple[bool, str]:
    """Launch a whitelisted Commander maintenance op detached from tg_listen."""
    script = ROOT / "scripts" / "commander_ops.py"
    if not script.exists():
        return False, f"missing {script}"

    exe_path = Path(PYTHONW)
    exe = str(exe_path if exe_path.exists() else PYTHON)
    ps_args = [str(script), *args]
    arglist = ", ".join(_ps_quote(a) for a in ps_args)
    cmd = [
        "powershell",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-Command",
        (
            "Start-Process -WindowStyle Hidden "
            f"-FilePath {_ps_quote(exe)} "
            f"-ArgumentList @({arglist}) "
            f"-WorkingDirectory {_ps_quote(str(ROOT))}"
        ),
    ]
    flags = 0
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    try:
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _render_boss_audit_html() -> tuple[bool, str]:
    no_window_kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    pre_cmds = [
        [PYTHON, str(ROOT / "processors" / "section_chief_work_audit.py")],
        [PYTHON, str(ROOT / "processors" / "field_agent_intervention_router.py")],
        [PYTHON, str(ROOT / "processors" / "field_agent_factory.py"), "--no-dispatch"],
    ]
    for pre_cmd in pre_cmds:
        try:
            pre = subprocess.run(
                pre_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                cwd=str(ROOT),
                **no_window_kw,
            )
        except subprocess.TimeoutExpired:
            return False, f"pre-refresh timeout: {Path(pre_cmd[1]).name}"
        except Exception as e:
            return False, f"pre-refresh {Path(pre_cmd[1]).name}: {type(e).__name__}: {e}"
        if pre.returncode != 0:
            return False, f"pre-refresh {Path(pre_cmd[1]).name}: {(pre.stderr or pre.stdout or 'failed')[:600]}"

    cmd = [
        PYTHON,
        str(ROOT / "scripts" / "render_boss_audit_html.py"),
        "--since",
        "7d",
        "--force",
        "--print-path",
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            cwd=str(ROOT),
            **no_window_kw,
        )
    except subprocess.TimeoutExpired:
        return False, "render timeout"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "renderer failed")[:600]
    path = (r.stdout or str(REPORT_HTML)).strip().splitlines()[-1].strip()
    return True, path


def _truncate_for_tg(s: str, limit: int = 3500) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "\n\n…（截斷，更多用 Claude Code 直接查）"


# ----------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------

def _h_ping(m, text):
    return "🟢 在", "ping"


def _h_help(m, text):
    return ("""📖 *指令清單*（Commander 直接回，0 token、秒回）

*狀態類*
• `status` / `狀態` / `現在呢` / `咋樣` / `啥情況` — KB 快照
• `ping` / `你好` / `在嗎` — 心跳

*查詢類*
• `查 X` / `卡 X` / `X 的卡` — 查 X 相關決策卡
• `operator X` — X 的 operator graph
• `funnel queue` / `漏斗隊列` — 待審 funnel edges

*動作類*
• `approve edge N` / `批准 N` — 批准 edge#N
• `reject edge N` / `拒絕 N` — 拒絕 edge#N
• `重發` — 重新發送上一則回覆

*Housekeeping*
• `/clear` / `清隊列` — 清掉 cmd queue 中 >24h 舊檔
• `/compact` / `壓縮` — VACUUM SQLite index DB
• `help` — 這頁

⚠ TG 上的 `/clear` `/compact` *無法*觸發 boss 主 Claude session 的 /clear /compact
（兩個 process 之間沒有 IPC）。它只動 Blacksite 端的 queue + DB。

🤖 *自由問題*（走 Claude bridge，listener spawn `claude.exe --print`）：
> 下個 example_event 該推嗎？
> examplebrand 跟誰有關係？
> 今天有什麼新訊號？

⏱️ 桌前 Claude Code 開著（OAuth 有效）→ 10-30s 回，帶 6 輪 sliding-window 記憶
🔌 桌前沒開（OAuth 過期）→ 友善訊息提示你開 Claude Code 後重發

📅 *自動推播*：每天 20:00 Taipei daily brief（純 Python，不依賴 OAuth，必發）""",
            "help")


def _h_status(m, text):
    raw = _run_kb_query(["state"])
    try:
        s = json.loads(raw)
    except Exception as e:
        return f"⚠ status JSON parse err: {e}\n\n```\n{raw[:500]}\n```", "status_err"

    ent = s.get("entities_by_state", {})
    cards_by = s.get("cards_by_state", {})
    f_rev = s.get("funnel_by_review", {})
    f_join = s.get("funnel_by_join", {})

    def fmt_dict(d):
        if not d:
            return "—"
        return " / ".join(f"{k}:{v}" for k, v in d.items())

    reply = (
        f"🟢 *KB 快照*\n\n"
        f"📊 messages: *{s.get('messages_total', 0):,}*\n"
        f"🏷️ entities: *{s.get('entities_total', 0):,}* total — {fmt_dict(ent)}\n"
        f"📇 cards: *{s.get('active_cards', 0)}* active — {fmt_dict(cards_by)}\n"
        f"🚪 funnel review: {fmt_dict(f_rev)}\n"
        f"🚪 funnel join: {fmt_dict(f_join)}"
    )
    return reply, "status"


def _h_search_card(m, text):
    keyword = (m.group(1) or "").strip()
    if not keyword:
        return "⚠ 沒給關鍵字", "search_no_keyword"
    raw = _run_kb_query(["cards", "--search", keyword, "--limit", "3"])
    if "(no cards matched)" in raw or not raw.strip():
        return f"🔍 沒查到「*{keyword}*」相關卡", "search_no_match"
    return f"🔍 *查「{keyword}」*\n\n```\n{_truncate_for_tg(raw)}\n```", "search_card"


def _h_operator_cluster(m, text):
    name = (m.group(1) or "").strip()
    if not name:
        return "⚠ 沒給 entity 名稱", "operator_no_name"
    raw = _run_kb_query(["operator-cluster", name])
    if "not found" in raw.lower() or not raw.strip():
        return f"🕸️ 沒找到 entity「*{name}*」", "operator_no_match"
    return f"🕸️ *Operator cluster: {name}*\n\n```\n{_truncate_for_tg(raw)}\n```", "operator_cluster"


def _h_funnel_queue(m, text):
    raw = _run_kb_query(["funnel", "--kind", "funnel_push"])
    if not raw.strip():
        raw = "目前沒有 funnel_push edges"
    return f"🚪 *Funnel push edges*\n\n```\n{_truncate_for_tg(raw)}\n```", "funnel_queue"


def _h_edge_state(m, text, new_state):
    try:
        eid = int(m.group(1))
    except Exception:
        return "⚠ edge ID 解析失敗", "edge_parse_err"
    try:
        conn = sqlite3.connect(str(DB_PATH))
        row = conn.execute(
            "SELECT to_target_kind, to_target, review_state, edge_kind FROM funnel_edges WHERE row_id = ?",
            (eid,),
        ).fetchone()
        if not row:
            conn.close()
            return f"⚠ 找不到 edge#{eid}", "edge_not_found"
        target_kind, target, old_state, edge_kind = row
        conn.execute(
            "UPDATE funnel_edges SET review_state=?, review_at=datetime('now','localtime'), review_model='listener-fast-path' WHERE row_id=?",
            (new_state, eid),
        )
        conn.commit()
        conn.close()
        return (f"✅ edge#{eid} `{old_state}` → *{new_state}*\n"
                f"   `{edge_kind}` {target_kind} → {target}",
                f"edge_{new_state}")
    except Exception as e:
        return f"⚠ DB err: {type(e).__name__}: {e}", "edge_db_err"


def _h_approve(m, text):
    return _h_edge_state(m, text, "approved")


def _h_reject(m, text):
    return _h_edge_state(m, text, "rejected")


def _h_resend(m, text):
    """Return the most recently sent reply body wrapped with a 重發 header.
    cmd_handler writes the actual outbox file."""
    SENT_DIR.mkdir(parents=True, exist_ok=True)
    sent_files = sorted(SENT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not sent_files:
        return "⚠ sent/ 是空的，沒得重發", "resend_empty"
    latest = sent_files[0]
    body = latest.read_text(encoding="utf-8")
    return f"🔁 *重發 {latest.name}*\n\n{body}", "resend"


def _h_boss_audit_html(m, text):
    ok, result = _render_boss_audit_html()
    if not ok:
        return f"查核頁生成失敗：\n`{result}`", "boss_audit_html_err"
    path = Path(result)
    if not path.exists():
        return f"查核頁生成完成但找不到檔案：`{path}`", "boss_audit_html_missing"
    return (
        f"已生成最新版任務情報查核頁。\n\n"
        f"時間：`{_now_iso()}`\n"
        f"檔案：`{path}`\n"
        f"視窗：最近 7d\n\n"
        f"<!--BLACKSITE_ATTACH_FILE:{path}-->",
        "boss_audit_html",
    )


def _h_clear_queue(m, text):
    """Drop stale cmd-queue files (inbox + outbox + processed/sent older than 24h)."""
    now = time.time()
    cutoff = now - 24 * 3600
    counts = {"inbox": 0, "outbox": 0, "processed": 0, "sent": 0}
    for sub, key in (
        ("inbox", "inbox"),
        ("outbox", "outbox"),
        ("processed", "processed"),
        ("sent", "sent"),
    ):
        d = CMD_DIR / sub
        if not d.exists():
            continue
        for p in d.iterdir():
            if not p.is_file():
                continue
            if p.stat().st_mtime < cutoff:
                try:
                    p.unlink()
                    counts[key] += 1
                except Exception:
                    pass
    total = sum(counts.values())
    return (f"🧹 *Queue cleared* (>24h 舊檔)\n"
            f"• inbox: {counts['inbox']}\n"
            f"• outbox: {counts['outbox']}\n"
            f"• processed: {counts['processed']}\n"
            f"• sent: {counts['sent']}\n"
            f"= 共 {total} 個檔案刪除\n\n"
            f"_（注意：這只清 cmd queue 檔案，不影響 boss 主 session 的 /clear /compact）_",
            "clear_queue")


def _h_compact_db(m, text):
    """SQLite VACUUM + integrity quick-check on the index DB."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        size_before = DB_PATH.stat().st_size
        conn.execute("VACUUM")
        conn.commit()
        conn.close()
        size_after = DB_PATH.stat().st_size
        saved = size_before - size_after
        pct = (saved / size_before * 100) if size_before else 0
        return (f"📦 *DB compacted*\n"
                f"• before: {size_before / 1024 / 1024:.1f} MB\n"
                f"• after: {size_after / 1024 / 1024:.1f} MB\n"
                f"• saved: {saved / 1024 / 1024:.2f} MB ({pct:.1f}%)\n\n"
                f"_（這壓縮 SQLite index，不影響 boss 主 session）_",
                "compact_db")
    except Exception as e:
        return f"⚠ VACUUM err: {type(e).__name__}: {e}", "compact_err"


def _h_strategist_run(m, text):
    """Boss-trigger: 「策略長 上工」/「chief strategist run」 → spawn
    processors/chief_strategist.py --force in background. Strategist run
    takes 3-5 min (Opus 4.7 1M with full KB read), so we Popen-spawn and
    return immediately. The strategist memo lands in brief queue with
    [STRATEGY] prefix; brief_send_loop picks it up + DMs boss."""
    script = ROOT / "processors" / "chief_strategist.py"
    if not script.exists():
        return f"⚠ chief_strategist.py 不存在: {script}", "strategist_missing"
    log_path = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs" / \
        f"chief_strategist_trigger_{datetime.now(TZ).strftime('%Y-%m-%dT%H-%M')}.log"
    try:
        log_fh = open(log_path, "w", encoding="utf-8")
        # Detached: parent process can exit; strategist runs to completion
        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        subprocess.Popen(
            [PYTHON, str(script), "--force"],
            cwd=str(ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        log_rel = log_path.relative_to(ROOT).as_posix()
        return (
            f"📣 *策略長 已上工*\n\n"
            f"模型: Claude Opus 4.7 1M\n"
            f"輸入: 過去 7d cards / leads / opinions / digest / open incidents\n"
            f"輸出: strategy memo + directive yaml + `[STRATEGY]` brief\n"
            f"預計: 3-5 min（spawn 已 detached）\n"
            f"日誌: `{log_rel}`\n\n"
            f"_完成後 brief_send 會 DM 你 `[STRATEGY]` 報表（lift 到下次 daily 頂部）_",
            "strategist_run",
        )
    except Exception as e:
        return f"⚠ strategist spawn err: {type(e).__name__}: {e}", "strategist_err"


def _h_restart_daemon(m, text):
    ok, err = _spawn_commander_op(["restart-daemon", "--delay", "75"])
    if not ok:
        return f"⚠ Commander 重啟排程失敗: `{err}`", "restart_daemon_err"
    return (
        "收到：75 秒後重啟 Blacksite daemon。先讓 Commander 把這則回報送出；"
        "完成後會再回報 session_status。",
        "restart_daemon",
    )


def _h_commander_rerun(m, text):
    job = (m.group(1) or "").strip()
    if not job:
        return "請指定要重跑的 job，例如 `重跑 status` / `重跑 日報` / `重跑 小主管`。", "rerun_no_job"
    ok, err = _spawn_commander_op(["rerun", job])
    if not ok:
        return f"⚠ Commander 重跑排程失敗: `{err}`", "rerun_spawn_err"
    return f"收到：Commander 開始重跑 `{job}`。完成後會回報結果。", "rerun_job"


def _h_commander_backfill(m, text):
    date_str = (m.group(1) or "").strip()
    args = ["backfill"]
    if date_str:
        args.extend(["--date", date_str])
    ok, err = _spawn_commander_op(args)
    if not ok:
        return f"⚠ Commander 補跑排程失敗: `{err}`", "backfill_spawn_err"
    label = date_str or "昨天"
    return f"收到：Commander 開始補跑 `{label}` 的 pending/day-end chain。完成後會回報結果。", "backfill"


# ----------------------------------------------------------------------
# Pattern registry — order matters: more specific first
# ----------------------------------------------------------------------

PATTERNS: list[tuple[re.Pattern, callable]] = []


def _register(pat: str, handler):
    PATTERNS.append((re.compile(pat, re.IGNORECASE | re.UNICODE), handler))


# Edge state ops (very specific — verb + edge + N)
_register(r"^\s*(?:approve|批准)\s*(?:edge)?\s*#?\s*(\d+)\s*$", _h_approve)
_register(r"^\s*(?:reject|拒絕|否決)\s*(?:edge)?\s*#?\s*(\d+)\s*$", _h_reject)

# Housekeeping — explicit slash-commands AND TC variants
_register(r"^\s*(?:/clear(?:\s*queue)?|清隊列|清\s*queue|清\s*inbox|清空\s*queue)\s*$", _h_clear_queue)
_register(r"^\s*(?:/compact|壓縮(?:\s*db)?|compact\s*db|VACUUM)\s*$", _h_compact_db)

# Resend — boss says "重發" / "再發一次" / "重新發" → re-queue last sent reply
_register(r"^\s*(?:重發|重新發|再發一次|再來一次|resend|reqsend)\s*$", _h_resend)

# Boss human-audit mobile HTML. Generates the latest file and asks cmd_handler
# to attach it in the PM via the internal BLACKSITE_ATTACH_FILE marker.
_register(
    r"^\s*(?:查核頁|人類查核|驗收面板|audit(?:\s*page)?|boss\s*audit|"
    r"pm\s*(?:audit|查核)|(?:Commander\s*)?(?:傳|發|pm)\s*(?:一份|最新)?\s*(?:查核頁|audit))\s*$",
    _h_boss_audit_html,
)

# Tier 3 strategist boss-trigger (CLAUDE.md §15 + CHIEF_STRATEGIST.md §9).
# Fires processors/chief_strategist.py --force in detached background;
# memo lands in brief queue with [STRATEGY] prefix 3-5 min later.
# Match before search/lookup so `策略長 上工` doesn't get parsed as "find 策略長".
_register(
    r"^\s*(?:策略長\s*上工|策略长\s*上工|策略長\s*上崗|策略長\s*上岗|"
    r"chief\s*strategist\s*(?:run|now|on\s*duty)|"
    r"strategist\s*(?:run|now|on\s*duty)|"
    r"director\s*of\s*intelligence\s*run)\s*[!.?？！。]*\s*$",
    _h_strategist_run,
)

# Commander maintenance ops. Keep above generic search/status patterns.
_register(
    "^\\s*(?:\\u88dc\\u8dd1|\\u91cd\\u8dd1).*(?:\\u6628\\u665a|\\u6628\\u5929|\\u6628\\u65e5|pending|\\u65e5\\u7d50|\\u6f0f\\u8dd1)(?:\\s*(\\d{4}-\\d{2}-\\d{2}))?\\s*$",
    _h_commander_backfill,
)
_register(
    "^\\s*(?:restart|\\u91cd\\u555f|\\u91cd\\u958b|\\u91cd\\u65b0\\u555f\\u52d5)\\s*(?:blacksite|daemon|commander|listener|bridge|\\u7cfb\\u7d71|\\u6392\\u7a0b)?\\s*$",
    _h_restart_daemon,
)
# 5/19 boss 抓到：原本 fallback `^.*(?:restart|重啟|重開)\s*$` 太寬鬆，
# 「再幫她重啟」「Mika 重啟」「好，重啟」這類自然句尾含「重啟」的訊息
# 全被當成命令吃掉、自動排程 daemon 重啟 + 回模板，bridge 完全沒看到內容。
# 真實使用情境裡，「想下 restart daemon 指令」幾乎都是 line 532 那種錨定的
# 純指令格式；任何夾雜其他文字的「重啟」都該走 bridge 讓 LLM 判讀，不是
# fast_path 直接觸發。fallback 移除。
_register(
    "^\\s*(?:rerun|run|\\u91cd\\u8dd1|\\u88dc\\u8dd1|\\u57f7\\u884c)\\s+(.+?)\\s*$",
    _h_commander_rerun,
)

# Funnel / operator / status — exact-ish phrases
_register(r"^\s*(?:funnel\s*queue|漏斗隊列|漏斗清單|funnel)\s*$", _h_funnel_queue)
_register(r"^\s*(?:operator(?:-?cluster)?|cluster|操作員|集群)\s+(.+?)\s*$", _h_operator_cluster)
# Status — broadened to catch boss's natural-language status pings.
# Covers: 狀態 / 現況 / 現在怎樣 / 現在呢 / 現在如何 / 怎樣 / 怎麼樣 / 咋樣 / 啥情況 / 狀況 /
#          有什麼新的 / 有什麼新訊號 / OK 嗎 / sup / what's up / status (already)
_register(r"^\s*(?:status|狀態|現況|現在(?:怎樣|呢|如何|咋樣)?|怎麼?樣|咋樣|啥情況|狀況(?:如何)?|有(?:啥|什麼)新(?:的|訊號|事)?|sup|what'?s\s*up|快照)\s*[?？!.。、]*\s*$", _h_status)

# Search/lookup card patterns
_register(r"^\s*(?:查|search|find|找|卡)\s+(.+?)\s*$", _h_search_card)
_register(r"^\s*(.+?)\s*的卡\s*$", _h_search_card)

# Help + ping (most generic — last)
_register(r"^\s*(?:help|\?|幫助|指令|說明|怎麼用)\s*$", _h_help)
_register(r"^\s*(?:ping|hi|hello|hey|嗨|你好|哈囉|哈嘍|在嗎|在不在|有人嗎)\s*[!.?？！。]*\s*$", _h_ping)


# ----------------------------------------------------------------------
# Main entry — called from cmd_handler.write_inbox
# ----------------------------------------------------------------------

def try_fast_path(text: str) -> tuple[str, str] | None:
    """Returns (reply_md, intent_label) on match, else None.
    Caller writes reply_md DIRECTLY to outbox/ skipping scheduled-task."""
    if not text:
        return None
    s = text.strip()
    if not s:
        return None
    for pat, handler in PATTERNS:
        m = pat.match(s)
        if m:
            try:
                return handler(m, s)
            except Exception as e:
                # Don't silently fail — surface the error to boss so we can fix
                return f"⚠ fast-path handler err: {type(e).__name__}: {str(e)[:200]}", "handler_err"
    return None


# ----------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    samples = [
        "你好", "Hi!", "ping",
        "status", "狀態",
        "查 examplefunnel", "卡 examplebrand", "examplefunnel 的卡",
        "operator examplebrand.com", "operator-cluster ExampleFunnelChat",
        "funnel queue", "漏斗隊列",
        "approve edge 3", "批准 edge 5", "拒絕 7",
        "help", "?",
        # 「策略長 上工」 family — pattern-match-only check (handler spawns
        # subprocess; skip in self-test to avoid spawning during dev). Verify
        # via dry pattern lookup.
        "下個 example_event 該推嗎",  # should NOT match (freeform)
        "examplebrand 跟誰有關係",  # should NOT match
    ]
    for s in samples:
        r = try_fast_path(s)
        if r:
            reply, intent = r
            print(f"✓ [{intent:<22}] {s!r:<40} → {reply[:70]!r}")
        else:
            print(f"✗ [FREEFORM            ] {s!r}")

    # Strategist trigger — pattern-only check (don't actually spawn)
    print("\n--- strategist trigger pattern (no spawn) ---")
    for s in ("策略長 上工", "chief strategist run", "strategist on duty",
              "Strategist Now", "策略长 上工", "策略長 上崗"):
        matched = any(p.match(s.strip()) for p, h in PATTERNS if h.__name__ == "_h_strategist_run")
        print(f"{'✓' if matched else '✗'} {s!r}")
