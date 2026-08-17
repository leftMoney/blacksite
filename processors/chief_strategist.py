"""processors/chief_strategist.py — Tier 3 策略長 weekly synthesizer.

Per CLAUDE.md §15 Tier 3 + boss 5/2 directive.

Cron: Sunday 21:00 GMT+7 weekly via blacksite_daemon.
On-demand: boss DMs commander `「策略長 上工」` → main session shells out to
`py processors/chief_strategist.py --force`.

Flow:
  1. Read past 7d kb_cards / kb_leads / boss_opinions / system_history
  2. Read this week's Section Chief digest at runtime/strategist_digest/<YYYY-WW>.md
  3. Read open incidents in state=escalated_strategist
  4. Read past 30d strategy memos (self-coherence)
  5. Spawn LLM analyst via _llm_synth.claude_run with CHIEF_STRATEGIST.md skill prefix
  6. Output: strategy memo + directive yaml(s) + brief queue [STRATEGY] insert

Per CLAUDE.md §6.4: timestamps ISO 8601 with +07:00.
Per CLAUDE.md §13.6: log_event 'milestone' on completion; 'warning' if memo
generation failed.

CLI:
  py processors/chief_strategist.py            # weekly cron entry; idempotent for current week
  py processors/chief_strategist.py --force    # boss-trigger; allow same-week re-run
  py processors/chief_strategist.py --week 2026-W18   # backfill specific week
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
MEMO_DIR = RUNTIME_DIR / "strategy_memos"
DIGEST_DIR = RUNTIME_DIR / "strategist_digest"
DIRECTIVE_DIR = RUNTIME_DIR / "strategy_directives"
INCIDENTS_DIR = RUNTIME_DIR / "agent_incidents"
BRIEF_QUEUE = RUNTIME_DIR / "briefs" / "queue"
SKILL_PATH = ROOT / "personas" / "skills" / "CHIEF_STRATEGIST.md"

for d in (LOG_DIR, MEMO_DIR, DIGEST_DIR, DIRECTIVE_DIR, BRIEF_QUEUE):
    d.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def now_dt() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_iso()}] [chief_strategist] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"chief_strategist_{now_dt().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _hist(kind: str, title: str, body: str | None = None,
          refs: list | None = None, parent_id: int | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(
            actor="cron_chief_strategist", kind=kind, scope="strategist",
            title=title[:118], body=body, refs=refs, parent_id=parent_id,
        )
    except Exception as e:
        log(f"history_log fail: {type(e).__name__}: {e}")
        return -1


def iso_week(dt: datetime) -> str:
    """Return YYYY-Www (ISO week date)."""
    iy, iw, _ = dt.isocalendar()
    return f"{iy:04d}-W{iw:02d}"


def next_monday(dt: datetime) -> datetime:
    days_ahead = (7 - dt.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (dt + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Input gathering — produce a context bundle for the LLM analyst
# ---------------------------------------------------------------------------

def _gather_context(week: str, force: bool) -> dict:
    """Read all inputs the strategist needs. Returns paths + counts; LLM
    will use Read tool to load full content.

    Idempotency: if memo for this week already exists and not force, abort
    early. Boss-trigger uses --force to bypass.
    """
    memo_path = MEMO_DIR / f"{week}.md"
    if memo_path.exists() and not force:
        return {"already_exists": True, "memo_path": str(memo_path)}

    # 7-day window
    end = now_dt()
    start = end - timedelta(days=7)
    cutoff_iso = start.isoformat(timespec="seconds")

    from db.connection import get_connection
    conn = get_connection()
    try:
        cards_total = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE last_built_at >= ? AND state='active'",
            (cutoff_iso,),
        ).fetchone()[0]
        leads_total = conn.execute(
            "SELECT COUNT(*) FROM kb_leads WHERE emitted_at >= ?",
            (cutoff_iso,),
        ).fetchone()[0]
        leads_escalated = conn.execute(
            "SELECT COUNT(*) FROM kb_leads WHERE state IN ('escalated','conflict_flag') "
            "AND emitted_at >= ?", (cutoff_iso,),
        ).fetchone()[0]
        opinions_total = conn.execute(
            "SELECT COUNT(*) FROM boss_opinions WHERE source_ts >= ? AND status='active'",
            (cutoff_iso,),
        ).fetchone()[0]
        history_events = conn.execute(
            "SELECT COUNT(*) FROM system_history WHERE ts >= ? "
            "AND kind IN ('milestone','decision','warning','crash')",
            (cutoff_iso,),
        ).fetchone()[0]
    finally:
        conn.close()

    digest_path = DIGEST_DIR / f"{week}.md"
    digest_exists = digest_path.exists()

    # Open escalated incidents
    escalated_incidents: list[str] = []
    try:
        from processors.agent_incidents import list_incidents
        for inc in list_incidents(state="escalated_strategist"):
            escalated_incidents.append(
                inc["frontmatter"].get("incident_id") or inc["path"].stem
            )
    except Exception as e:
        log(f"failed to enumerate escalated incidents: {type(e).__name__}: {e}")

    # Past 4 weeks of memos for self-coherence
    past_memos = []
    for p in sorted(MEMO_DIR.glob("*.md"), reverse=True)[:4]:
        if p.stem == week:
            continue
        past_memos.append(p.relative_to(ROOT).as_posix())

    return {
        "already_exists": False,
        "week": week,
        "cutoff_iso": cutoff_iso,
        "cards_total": cards_total,
        "leads_total": leads_total,
        "leads_escalated": leads_escalated,
        "opinions_total": opinions_total,
        "history_events": history_events,
        "digest_path": str(digest_path) if digest_exists else None,
        "digest_exists": digest_exists,
        "escalated_incidents": escalated_incidents,
        "past_memos": past_memos,
        "memo_out": str(MEMO_DIR / f"{week}.md"),
        "directive_out": str(DIRECTIVE_DIR / f"{next_monday(now_dt()).strftime('%Y-%m-%d')}.yaml"),
        "brief_strategy_out": str(BRIEF_QUEUE / f"pending_STRATEGY_{week}.md"),
    }


# ---------------------------------------------------------------------------
# LLM invocation
# ---------------------------------------------------------------------------

def _build_prompt(ctx: dict) -> str:
    """Construct the task prompt. The CHIEF_STRATEGIST.md skill is loaded
    as system prompt prefix via _llm_synth.claude_run skill_prefix=True (we
    swap SKILL_PATH temporarily — see _spawn_strategist)."""
    week = ctx["week"]
    cutoff = ctx["cutoff_iso"]
    db_rel = (RUNTIME_DIR / "index.db").relative_to(ROOT).as_posix()
    memo_rel = Path(ctx["memo_out"]).relative_to(ROOT).as_posix()
    directive_rel = Path(ctx["directive_out"]).relative_to(ROOT).as_posix()
    brief_rel = Path(ctx["brief_strategy_out"]).relative_to(ROOT).as_posix()
    digest_rel = (
        Path(ctx["digest_path"]).relative_to(ROOT).as_posix()
        if ctx["digest_path"] else "(none — Section Chief digest absent this week)"
    )

    incident_list = "\n".join(f"  - runtime/agent_incidents/{i}.md" for i in ctx["escalated_incidents"])
    past_memo_list = "\n".join(f"  - {m}" for m in ctx["past_memos"]) or "  - (no past memos — first week)"

    return f"""你是 Blacksite _TEMPLATE 策略長 (Chief Strategist / Director of Intelligence)。
CHIEF_STRATEGIST.md skill 已注入 system prompt — 你的身份 + 職責 + 紀律全在那。

## 本週合成任務 — {week} GMT+7

合成過去 7 天 (cutoff: {cutoff}) 的 Blacksite _TEMPLATE 情報，產出策略 memo +
directive yaml + brief queue 推送，目標 boss 商業決策可用。

## 輸入資料 (用 Read / Bash sqlite3 自取)

1. **Section Chief 本週 digest**: `{digest_rel}` ({"有" if ctx["digest_exists"] else "無"})
2. **過去 7d KB cards** ({ctx["cards_total"]} 張，active state):
   ```
   sqlite3 {db_rel} "SELECT row_id, title, actionability_score, last_built_at FROM cards WHERE last_built_at >= '{cutoff}' AND state='active' ORDER BY actionability_score DESC NULLS LAST LIMIT 30"
   ```
3. **過去 7d kb_leads** ({ctx["leads_total"]} 條, {ctx["leads_escalated"]} escalated):
   ```
   sqlite3 {db_rel} "SELECT lead_id, type, target, state, resolution FROM kb_leads WHERE emitted_at >= '{cutoff}' ORDER BY emitted_at DESC LIMIT 50"
   sqlite3 {db_rel} "SELECT lead_id, type, target, suggested_action, evidence FROM kb_leads WHERE state IN ('escalated','conflict_flag') AND emitted_at >= '{cutoff}'"
   ```
4. **過去 7d boss_opinions** ({ctx["opinions_total"]} 條):
   ```
   sqlite3 {db_rel} "SELECT opinion_id, kind, topic, content FROM boss_opinions WHERE source_ts >= '{cutoff}' AND status='active' ORDER BY source_ts DESC LIMIT 30"
   ```
5. **過去 7d system_history milestones / decisions / warnings**:
   ```
   sqlite3 {db_rel} "SELECT id, kind, scope, title, ts FROM system_history WHERE ts >= '{cutoff}' AND kind IN ('milestone','decision','warning','crash') ORDER BY ts DESC LIMIT 40"
   ```
6. **Open escalated_strategist incidents** ({len(ctx["escalated_incidents"])} 條):
{incident_list or "   - (none)"}
7. **過去 4 週 memos (自我一致性檢查)**:
{past_memo_list}

## 輸出 (3 個檔案，缺一不可)

### A. 策略 memo: `{memo_rel}`

按 CHIEF_STRATEGIST.md §3.2 格式 (frontmatter + §1-§10):
1. Title (≤60 字)
2. Executive Summary (≤150 字繁中，1 句含 1 intelligence implication)
3. Intelligence Posture (2-4 bullets: what is decision-ready vs weakly grounded)
4. Coverage Balance
5. Regulatory Weather (forecast 30d)
6. Evidence Quality & KB Groundedness
7. Cross-platform Anomalies
8. Directives to Section Chief (next week, numbered, 含 rationale + measurable success)
9. Boss Decision Items (specific date + intelligence resource/scope decision binary)
10. Self-eval — 上週 memo 預測落地了沒？
11. Predictive Lead Time (≥1 早期信號 public-news 還沒抓到)

### B. Directive yaml: `{directive_rel}`

按 CHIEF_STRATEGIST.md §3.3 格式。每條 directive 必含 `kind` + `rationale`，
其中 kind ∈ {{focus_topic, agent_kpi_adjust, agent_directive, open_incident,
investigation_request}}。directive_date={next_monday(now_dt()).strftime('%Y-%m-%d')}，
expires_at={(next_monday(now_dt()) + timedelta(days=7)).isoformat(timespec='seconds')} 顯式 `+07:00`。

### C. Brief queue 推送: `{brief_rel}`

**不要 copy 全文**。寫一份給 boss 看的 TG 短摘要，≤300 字，純文字（不用 markdown 表格、不用長 code block）。

格式（嚴格按這個，多一字扣分）：

```
[策略長] W{week} 週報

▶ 本週定論（1-3 句，是什麼情報可以做決策了）
▶ 商業行動（1-3 條，每條一句，具體）
▶ 特例洞察（只寫真的異常的，沒有就省略）
▶ 需要你決定（只列需要 boss 二選一的，沒有就省略）
▶ 已發出 N 條 directive 給小主管

---
完整卷宗: instances/_TEMPLATE/runtime/strategy_memos/{week}.md
```

boss 只讀這份，細節在 memo 檔。寫完後把這個短摘要存到 `{brief_rel}`。

## 紀律 (CHIEF_STRATEGIST.md §10)

- 別 recap daily briefs (boss 已看)
- 別 list digest 內容 (boss 想看自己讀)
- DO synthesize: 本週情報哪裡強、哪裡弱、哪裡偏、哪裡失衡
- DO predict: 接下來 30d 哪些假設要驗證、艦隊該補哪塊證據
- DO escalate: 解不了的 → boss decision items
- DO self-correct: 過往 prediction 落地沒？missed 就承認，refine model
- 詞彙內部精確 (lottery / gambling 及目標國當地語彙直書)
- 時間戳含目標國 GMT offset 顯式 (e.g. `+07:00`)
- 用 instance 主幣別，外幣含換算 `(USD $X @ instance 匯率)`

完成後 stdout print: `STRATEGY_DONE memo={memo_rel} directive={directive_rel} brief={brief_rel}`
"""


def _spawn_strategist(prompt: str, timeout_s: float = 600.0) -> tuple[bool, str]:
    """Spawn claude.exe with CHIEF_STRATEGIST.md skill as system prefix.

    _llm_synth.claude_run normally loads SECTION_CHIEF (post 5/2 reorg).
    For the strategist, we swap SKILL_PATH temporarily by reading the
    chief skill, then prepending it as extra_system on top of the default
    skill prefix turned off.
    """
    from processors._llm_synth import claude_run

    if not SKILL_PATH.exists():
        log(f"CHIEF_STRATEGIST skill missing at {SKILL_PATH}")
        return False, ""

    skill_text = SKILL_PATH.read_text(encoding="utf-8")
    return claude_run(
        prompt,
        skill_prefix=False,           # we feed our own skill via extra_system
        extra_system=skill_text,
        allowed_tools="Read,Write,Edit,Bash,Grep,Glob",
        permission_mode="acceptEdits",
        timeout_s=timeout_s,
        max_retries=2,
        agent_memory_id="CHIEF_STRATEGIST",  # §15.Y memory injection
    )


# ---------------------------------------------------------------------------
# Post-spawn validation + brief queue copy
# ---------------------------------------------------------------------------

def _validate_outputs(ctx: dict) -> tuple[bool, str]:
    memo = Path(ctx["memo_out"])
    directive = Path(ctx["directive_out"])
    brief = Path(ctx["brief_strategy_out"])
    missing = [p for p in (memo, directive) if not p.exists()]
    if missing:
        return False, f"missing outputs: {[str(p) for p in missing]}"
    if memo.stat().st_size < 500:
        return False, f"memo too short ({memo.stat().st_size}B); likely failed synthesis"
    # Auto-generate brief summary if strategist didn't write one
    if not brief.exists():
        try:
            week_str = Path(ctx["memo_out"]).stem  # e.g. "2026-W20"
            memo_text = memo.read_text(encoding="utf-8")
            # Extract exec summary + directives count as minimal fallback
            lines = memo_text.splitlines()
            summary_lines: list[str] = []
            in_exec = False
            directive_count = 0
            for ln in lines:
                if "Executive Summary" in ln or "## 2." in ln:
                    in_exec = True
                    continue
                if in_exec and ln.startswith("## "):
                    in_exec = False
                if in_exec and ln.strip():
                    summary_lines.append(ln.strip())
                if ln.strip().startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.")) and "## 8." not in ln:
                    if "Directives" not in ln:
                        pass
                # count directive items
            for ln in lines:
                if ln.strip() and ln.strip()[0].isdigit() and ". " in ln[:4]:
                    directive_count += 1
            summary = " ".join(summary_lines[:3])[:200]
            memo_rel_path = Path(ctx["memo_out"]).relative_to(ROOT).as_posix()
            fallback = (
                f"[策略長] {week_str} 週報\n\n"
                f"▶ 摘要：{summary or '(詳見 memo)'}\n\n"
                f"▶ 已發出 directive 若干條給小主管\n\n"
                f"---\n完整卷宗: {memo_rel_path}"
            )
            brief.write_text(fallback, encoding="utf-8")
            log(f"auto-generated brief summary: {brief.name}")
        except Exception as e:
            return False, f"brief queue write failed: {type(e).__name__}: {e}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(week: str | None = None, force: bool = False) -> int:
    target_week = week or iso_week(now_dt())
    log(f"chief strategist run: week={target_week} force={force}")
    ctx = _gather_context(target_week, force)
    if ctx.get("already_exists"):
        log(f"memo already exists: {ctx['memo_path']}; skip (use --force to override)")
        return 0
    log(f"context: cards={ctx['cards_total']} leads={ctx['leads_total']} "
        f"opinions={ctx['opinions_total']} escalated_inc={len(ctx['escalated_incidents'])}")

    prompt = _build_prompt(ctx)
    log("spawning strategist analyst (Claude with CHIEF_STRATEGIST.md skill)…")
    ok, stdout = _spawn_strategist(prompt)
    if not ok:
        log(f"strategist spawn FAILED; stdout_tail={stdout[-300:]!r}")
        _hist("warning",
              f"strategist memo FAILED for {target_week}",
              body=f"force={force}\nstdout_tail:\n{stdout[-1200:]}")
        return 1
    log(f"strategist analyst exit OK; stdout_head={stdout[:300]!r}")

    valid, reason = _validate_outputs(ctx)
    if not valid:
        log(f"validate FAIL: {reason}")
        _hist("warning",
              f"strategist memo VALIDATION FAIL for {target_week}: {reason}",
              body=f"ctx={json.dumps(ctx, ensure_ascii=False)}")
        return 2

    _hist("milestone",
          f"strategist memo shipped: {target_week}",
          body=f"memo={ctx['memo_out']}\ndirective={ctx['directive_out']}\nbrief={ctx['brief_strategy_out']}",
          refs=[
              Path(ctx["memo_out"]).relative_to(ROOT).as_posix(),
              Path(ctx["directive_out"]).relative_to(ROOT).as_posix(),
              Path(ctx["brief_strategy_out"]).relative_to(ROOT).as_posix(),
          ])

    # Phase B (5/5): organization audit trail. Strategist's weekly run = one
    # "meeting" + one log_event per directive issued. Surfaces in daily brief
    # 「🏛️ 組織狀態」 + scripts/org.py meetings/directives.
    directive_items: list[dict] = []
    try:
        from processors.history_log import log_event
        # Parse the just-written directive yaml to enumerate issued items
        directive_kinds: list[str] = []
        directive_count = 0
        try:
            import yaml as _yaml
            dir_path = Path(ctx["directive_out"])
            if dir_path.exists():
                docs = list(_yaml.safe_load_all(dir_path.read_text(encoding="utf-8")))
                merged: dict = {}
                for d in docs:
                    if isinstance(d, dict):
                        merged.update(d)
                directive_items = [it for it in (merged.get("directives") or []) if isinstance(it, dict)]
                directive_count = len(directive_items)
                directive_kinds = [it.get("kind", "?") for it in directive_items]
        except Exception as e:
            log(f"directive parse for log_event fail (non-fatal): {type(e).__name__}: {e}")

        meeting_id = log_event(
            actor="CHIEF_STRATEGIST",
            kind="meeting",
            scope="strategist",
            title=f"strategist weekly run {target_week}: {directive_count} directives issued",
            body=f"week={target_week}\nmemo={Path(ctx['memo_out']).name}\n"
                 f"directive_file={Path(ctx['directive_out']).name}\n"
                 f"directive_count={directive_count}\nkinds={directive_kinds}",
            refs=[Path(ctx["memo_out"]).relative_to(ROOT).as_posix(),
                  Path(ctx["directive_out"]).relative_to(ROOT).as_posix()],
        )
        # Per-directive audit (chained to the meeting via parent_id)
        for kind in directive_kinds:
            try:
                log_event(
                    actor="CHIEF_STRATEGIST",
                    kind="directive_issued",
                    scope="strategist",
                    title=f"directive {kind} ({target_week})",
                    parent_id=meeting_id if isinstance(meeting_id, int) and meeting_id > 0 else None,
                    refs=[Path(ctx["directive_out"]).relative_to(ROOT).as_posix()],
                )
            except Exception:
                pass
    except Exception as e:
        log(f"meeting log_event fail (non-fatal): {type(e).__name__}: {e}")

    # Phase B+ (5/5): append learnings to memory. Strategist gets one weekly
    # digest line; each directive that targets a specific agent_id propagates
    # a one-liner to that agent's memory (so the agent itself accrues
    # "策略長給我的指示" history). Skip directives without agent_id (focus_topic
    # / investigation_request — those are not per-agent).
    try:
        from agents._common.agent_memory import append_learning
        # 1. Strategist self-digest — what did THIS run accomplish
        try:
            kinds_summary = ", ".join(sorted(set(directive_kinds))) if directive_items else "(none)"
            append_learning(
                "CHIEF_STRATEGIST",
                f"weekly memo {target_week}: {len(directive_items)} directives issued ({kinds_summary})",
                category="weekly_memo",
                boss_curated=False,
            )
        except Exception:
            pass
        # 2. Per-agent directive propagation
        for it in directive_items:
            aid = it.get("agent_id")
            if not aid:
                continue
            kind = it.get("kind", "?")
            rationale = (it.get("rationale") or "").strip().replace("\n", " ")
            line = f"策略長 {target_week} directive [{kind}]: {rationale[:160]}"
            try:
                append_learning(aid, line, category="strategist_directive", boss_curated=False)
            except Exception:
                pass
    except Exception as e:
        log(f"learning append fail (non-fatal): {type(e).__name__}: {e}")

    try:
        from processors.org_task_audit_refresh import refresh_org_task_audit
        refresh_org_task_audit(f"chief_strategist:{target_week}")
    except Exception:
        pass

    log(f"OK · memo={Path(ctx['memo_out']).name} directive={Path(ctx['directive_out']).name} brief={Path(ctx['brief_strategy_out']).name}")
    return 0


def consult(question: str) -> int:
    """Ad-hoc strategist consultation — no memo / directive write, just reply.
    Spawn LLM with CHIEF_STRATEGIST.md skill, 1-shot Q&A. Output to stdout
    short enough for TG DM (≤500 chars). For commander→strategist relay (boss
    can directly ask strategist questions via commander)."""
    from pathlib import Path as _P
    log(f"consult: {question[:80]!r}")
    skill_path = _P(__file__).resolve().parents[1] / "personas" / "skills" / "CHIEF_STRATEGIST.md"
    skill_text = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""
    prompt = (
        f"# Ad-hoc 策略長 Consultation (no memo write)\n\n"
        f"Boss asks via commander relay:\n\n"
        f"\"\"\"\n{question}\n\"\"\"\n\n"
        f"## Constraints\n"
        f"- 繁體中文\n"
        f"- ≤500 字（boss 在 TG 看，螢幕小）\n"
        f"- 無 markdown 表格、無 code block\n"
        f"- 直接給答案；不要客套；不要結語\n"
        f"- 必要時用 Read/Bash/Grep 工具讀 runtime/strategy_memos/ /  runtime/cards / runtime/kb_leads via SQL grounding\n"
        f"- 資訊不足就老實說「不夠斷，建議 boss 說『策略長 上工』跑完整 weekly memo」\n"
        f"- 引用具體 entity / 數字 where possible\n\n"
        f"## Output\n"
        f"純答案，無 metadata，無 markdown header。\n"
    )
    from processors._llm_synth import claude_run, MODEL_FOR_PER_SIGNAL
    ok, stdout = claude_run(
        prompt,
        skill_prefix=False,
        extra_system=skill_text,
        allowed_tools="Read,Bash,Grep,Glob",
        permission_mode="default",
        model=MODEL_FOR_PER_SIGNAL,
        timeout_s=300.0,
        agent_memory_id="CHIEF_STRATEGIST",  # §15.Y memory injection
    )
    if not ok:
        log("consult FAILED")
        print("⚠ 策略長 接線員 timeout / 失敗，建議切回主 session 或說「策略長 上工」跑 full memo")
        return 1
    print(stdout.strip())
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--week", default=None,
                   help="ISO week e.g. 2026-W18; defaults to current week")
    p.add_argument("--force", action="store_true",
                   help="re-run even if this week's memo already exists (boss-trigger)")
    p.add_argument("--consult", default=None, metavar="QUESTION",
                   help="Ad-hoc 1-shot strategist consultation (no memo write); "
                        "for commander→strategist relay")
    args = p.parse_args()
    if args.consult:
        rc = consult(args.consult)
    else:
        rc = run(args.week, args.force)
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
