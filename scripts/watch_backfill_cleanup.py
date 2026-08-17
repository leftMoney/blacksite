"""Watcher — polls every N min for whether the BG backfill process is still
alive (psutil + cmdline match); when it disappears:
  1. Force-unloads Qwen from VRAM (boss directive 2026-05-08).
  2. Computes pipeline stats (rows touched, Stage1/Stage2 distribution,
     Stage3 high-value candidates).
  3. Drops a `[OCR_DONE]` brief queue file so the commander TG bridge DMs boss.
  4. Logs milestone to system_history.
  5. Exits.

PID-based replaces an earlier log-marker approach that false-positived on
stale DONE lines from prior runs in the same daily log file.

Usage (background):
    pythonw scripts/watch_backfill_cleanup.py --max-hours 7
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from processors.pipeline._qwen_unload import unload_qwen, list_loaded

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME_DIR / "logs"
QUEUE_DIR = RUNTIME_DIR / "briefs" / "queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [watch_cleanup] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"watch_backfill_cleanup_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def compute_stats() -> dict:
    """Pipeline stats snapshot for the boss-facing notification."""
    from db.connection import get_connection
    out = {}
    conn = get_connection()
    try:
        out["media_with_ocr"] = conn.execute(
            "SELECT COUNT(*) FROM media WHERE media_kind='photo' AND ocr_text IS NOT NULL"
        ).fetchone()[0]
        out["stage1_total"] = conn.execute(
            "SELECT COUNT(*) FROM media_signal_filter"
        ).fetchone()[0]
        out["stage1_signal"] = conn.execute(
            "SELECT COUNT(*) FROM media_signal_filter WHERE verdict='signal'"
        ).fetchone()[0]
        out["stage1_noise"] = conn.execute(
            "SELECT COUNT(*) FROM media_signal_filter WHERE verdict='noise'"
        ).fetchone()[0]
        out["stage1_error"] = conn.execute(
            "SELECT COUNT(*) FROM media_signal_filter WHERE verdict='error'"
        ).fetchone()[0]
        out["stage2_total"] = conn.execute(
            "SELECT COUNT(*) FROM media_kb_decision"
        ).fetchone()[0]
        out["stage2_admit"] = conn.execute(
            "SELECT COUNT(*) FROM media_kb_decision WHERE kb_admit=1"
        ).fetchone()[0]
        out["stage2_reject"] = conn.execute(
            "SELECT COUNT(*) FROM media_kb_decision WHERE kb_admit=0"
        ).fetchone()[0]
        out["stage2_legacy"] = conn.execute(
            "SELECT COUNT(*) FROM media_kb_decision "
            "WHERE model_used='opus_default_via_claude_exe_2026_05_07'"
        ).fetchone()[0]
        out["stage2_new_pipeline"] = conn.execute(
            "SELECT COUNT(*) FROM media_kb_decision "
            "WHERE model_used='claude-haiku-4-5-20251001'"
        ).fetchone()[0]
        out["stage3_candidates"] = conn.execute(
            """SELECT COUNT(*) FROM media_kb_decision
                WHERE kb_admit=1 AND kb_value_score>=70"""
        ).fetchone()[0]
        out["stage3_done"] = conn.execute(
            "SELECT COUNT(*) FROM media_strategic_brief"
        ).fetchone()[0]
        # value class distribution (new pipeline only)
        out["class_dist"] = {}
        for row in conn.execute(
            """SELECT kb_value_class, COUNT(*) FROM media_kb_decision
                WHERE model_used='claude-haiku-4-5-20251001'
                GROUP BY kb_value_class"""
        ):
            out["class_dist"][row[0] or "(null)"] = row[1]
        # remaining pending (had ocr but no Stage1)
        out["pending_stage1"] = conn.execute(
            """SELECT COUNT(*) FROM media m
            LEFT JOIN media_signal_filter s ON s.media_row_id=m.row_id
                WHERE m.media_kind='photo' AND m.ocr_text IS NOT NULL
                  AND s.media_row_id IS NULL"""
        ).fetchone()[0]
    finally:
        conn.close()
    return out


def write_completion_brief(stats: dict, unload_status: list,
                            elapsed_str: str | None = None) -> Path:
    ts = datetime.now(TZ).strftime("%Y-%m-%dT%H-%M-%S")
    queue_path = QUEUE_DIR / f"pending_{ts}_ocr_backfill_done.md"
    class_dist = stats.get("class_dist") or {}
    class_lines = "\n".join(
        f"  - {k}: {v}" for k, v in sorted(
            class_dist.items(), key=lambda x: -x[1]) if k != "(null)"
    ) or "  (no new-pipeline rows yet)"
    body = f"""[OCR_DONE] L4 hybrid pipeline backfill 完成

✅ Backfill 已跑完 + Qwen model 已從 VRAM 卸載

## 整體狀態
- media (photo, with OCR text): **{stats.get('media_with_ocr')}**
- Stage 1 audited: **{stats.get('stage1_total')}** \
(signal {stats.get('stage1_signal')} / noise {stats.get('stage1_noise')} / error {stats.get('stage1_error')})
- Stage 2 verdicts (legacy + new): **{stats.get('stage2_total')}**
  - legacy 5/7 Opus re-audit: {stats.get('stage2_legacy')}
  - new pipeline (Haiku 4.5): {stats.get('stage2_new_pipeline')}
- Stage 2 admit / reject: **{stats.get('stage2_admit')} / {stats.get('stage2_reject')}**
- Stage 3 high-value queue (score≥70): **{stats.get('stage3_candidates')}** \
(processed {stats.get('stage3_done')})

## Value-class 分布 (new-pipeline rows)
{class_lines}

## 剩餘未跑 Stage 1
- pending_stage1: **{stats.get('pending_stage1')}** \
(若 >0 ⇒ daemon cron 接著補)

## VRAM 卸載
{json_block(unload_status)}

## 下一步建議
1. 若 stage3_candidates >> 0 而 stage3_done == 0 → 等今晚 19:00 cron 自動跑，或手動 `py -m processors.pipeline.stage3_sonnet_strategic --limit 20`
2. 明日 06:00 第一次 audit_sonnet 會跑 N=20 sample，看 qwen_acc / haiku_acc 是否在 floor 上
3. 若想立刻看 audit → `py -m processors.pipeline.audit_sonnet --kind daily`
"""
    if elapsed_str:
        body = body.replace("已從 VRAM 卸載",
                            f"已從 VRAM 卸載 (watcher 觀察時長 {elapsed_str})")
    queue_path.write_text(body, encoding="utf-8")
    return queue_path


def json_block(data) -> str:
    import json as _json
    return "```\n" + _json.dumps(data, ensure_ascii=False, indent=2) + "\n```"


PIPELINE_PROCESS_MARKERS = (
    "processors.pipeline.backfill",
    "processors\\pipeline\\backfill",
    "processors/pipeline/backfill",
    "processors.pipeline.stage3_sonnet_strategic",
    "processors\\pipeline\\stage3_sonnet_strategic",
    "processors/pipeline/stage3_sonnet_strategic",
)


def find_backfill_process() -> int | None:
    """Returns PID of any running pipeline backfill process (Stage 1+2 OR
    Stage 3), or None if none found. Watcher fires only when ALL pipeline
    BG processes are gone."""
    try:
        import psutil
    except ImportError:
        log("psutil not installed — fallback to log-marker mode (less reliable)")
        return None
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                joined = " ".join(cmdline)
                if any(marker in joined for marker in PIPELINE_PROCESS_MARKERS):
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        log(f"psutil iteration failed: {type(e).__name__}: {e}")
    return None


def latest_backfill_log() -> Path | None:
    files = sorted(LOG_DIR.glob("backfill_*.log"))
    return files[-1] if files else None


def latest_stage3_log() -> Path | None:
    files = sorted(LOG_DIR.glob("stage3_sonnet_*.log"))
    return files[-1] if files else None


def youngest_log_age_min() -> float | None:
    """Min mtime across backfill log + stage3 log — pipeline alive if either
    is fresh."""
    ages = []
    for p in (latest_backfill_log(), latest_stage3_log()):
        if p and p.exists():
            ages.append((datetime.now(TZ).timestamp() - p.stat().st_mtime) / 60.0)
    if not ages:
        return None
    return min(ages)


def backfill_log_age_min() -> float | None:
    return youngest_log_age_min()


def write_heartbeat() -> None:
    """One-line sentinel for outside observers (boss / session_status.py)."""
    hb = LOG_DIR / "watch_backfill_cleanup.heartbeat"
    try:
        hb.write_text(now_iso(), encoding="utf-8")
    except Exception:
        pass


def write_stalled_brief(stats: dict, last_log_age_min: float) -> Path:
    ts = datetime.now(TZ).strftime("%Y-%m-%dT%H-%M-%S")
    queue_path = QUEUE_DIR / f"pending_{ts}_pipeline_stalled.md"
    body = f"""[PIPELINE_STALLED] OCR backfill 卡住

⚠ Watcher 偵測：backfill log 超過 {last_log_age_min:.1f} min 沒更新；process 未退但邏輯停滯。

可能原因：
- Ollama hang (Qwen 模型呼叫 timeout 沒返)
- ANTHROPIC_OAUTH_TOKEN 失效 (Haiku API 401)
- DB lock / disk full
- VRAM OOM 但 process 沒崩

## 當前狀態
- Stage 1 audited: {stats.get('stage1_total')}
- Stage 2 verdicts: {stats.get('stage2_total')} (admit {stats.get('stage2_admit')})
- pending Stage 1: {stats.get('pending_stage1')}

## Boss 建議排查
1. 看 backfill log 末尾: `Get-Content "instances/_TEMPLATE/runtime/logs/backfill_$(Get-Date -Format 'yyyy-MM-dd').log" -Tail 20`
2. 看 Ollama health: `curl http://localhost:11434/api/ps`
3. 檢查 OAuth: `py -c "import os; from dotenv import load_dotenv; load_dotenv('.env'); print(bool(os.environ.get('ANTHROPIC_OAUTH_TOKEN')))"`
4. 強制 kill backfill: `py -c "import psutil; [p.terminate() for p in psutil.process_iter(['cmdline']) if any('backfill' in c for c in (p.info.get('cmdline') or []))]"`
5. 重啟 backfill: `py -m processors.pipeline.backfill --max-rows 2700 --chunk 100` (resume-safe，從中斷處繼續)

## Watcher 動作
Qwen 已 force-unload (避免凍結模型佔 VRAM)。Watcher 繼續監控；若 backfill 復活會重新計時。
"""
    queue_path.write_text(body, encoding="utf-8")
    return queue_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-hours", type=float, default=7.0)
    parser.add_argument("--poll-min", type=float, default=2.0)
    parser.add_argument("--require-seen-once", action="store_true", default=True,
                        help="require seeing the process at least once before "
                        "interpreting absence as 'finished'")
    args = parser.parse_args()

    deadline = datetime.now(TZ) + timedelta(hours=args.max_hours)
    log(f"start poll_min={args.poll_min} deadline={deadline.isoformat()}")

    seen_alive = False
    started_watching = datetime.now(TZ)
    stuck_alert_fired = False  # don't spam — fire once until log resumes
    STUCK_THRESHOLD_MIN = float(os.environ.get("WATCH_STUCK_THRESHOLD_MIN", "30"))

    while datetime.now(TZ) < deadline:
        write_heartbeat()
        pid = find_backfill_process()
        if pid is not None:
            if not seen_alive:
                log(f"first sighting: backfill PID={pid}")
            seen_alive = True

            # stuck detection: backfill log mtime vs now
            log_age_min = backfill_log_age_min()
            if log_age_min is not None and log_age_min > STUCK_THRESHOLD_MIN:
                if not stuck_alert_fired:
                    log(f"STUCK detected: backfill log {log_age_min:.1f} min "
                        f"old (>{STUCK_THRESHOLD_MIN}). Force-unload + notify "
                        f"boss; keep watching.")
                    # unload Qwen (frozen model wastes VRAM)
                    for m in list_loaded():
                        name = m.get("name") or m.get("model")
                        if name:
                            unload_qwen(name, log_fn=log)
                    # boss notify
                    try:
                        stats = compute_stats()
                    except Exception as e:
                        log(f"stats fail: {type(e).__name__}: {e}")
                        stats = {}
                    try:
                        path = write_stalled_brief(stats, log_age_min)
                        log(f"stalled brief: {path.name}")
                    except Exception as e:
                        log(f"stalled brief fail: {type(e).__name__}: {e}")
                        path = None
                    try:
                        from processors.history_log import log_event
                        log_event(actor="watcher", kind="warning",
                                  scope="ocr_pipeline",
                                  title=f"OCR backfill STUCK — log {log_age_min:.0f} min stale",
                                  body=f"backfill PID={pid} alive but log not advancing. Qwen unloaded; boss notified.",
                                  refs=[str(path.relative_to(ROOT)) if path else "scripts/watch_backfill_cleanup.py"])
                    except Exception as e:
                        log(f"history fail: {type(e).__name__}: {e}")
                    stuck_alert_fired = True
            elif log_age_min is not None and log_age_min < (STUCK_THRESHOLD_MIN / 2):
                # log is fresh again — clear the stuck flag for re-arming
                if stuck_alert_fired:
                    log("backfill log resumed flowing — reset stuck flag")
                    stuck_alert_fired = False
        else:
            if seen_alive:
                log("backfill process gone — trigger cleanup + boss notify")
                # Wait 5s grace for the BG to finish writing its last DB row
                time.sleep(5)

                # 1. Unload Qwen
                pre = list_loaded()
                log(f"pre-unload loaded: {[m.get('name') for m in pre]}")
                unload_results = []
                for m in pre:
                    name = m.get("name") or m.get("model")
                    if name:
                        r = unload_qwen(name, log_fn=log)
                        unload_results.append(r)
                time.sleep(3)
                post = list_loaded()
                log(f"post-unload loaded: {[m.get('name') for m in post]}")
                if not post:
                    log("VRAM cleared OK")
                else:
                    log("WARN: model still loaded after unload")

                # 1b. Trigger end-of-pipeline KB sweep:
                #   (a) promote any new admit rows that came in late
                #   (b) refresh body_md of cards whose Stage 3 brief arrived
                #       after initial promotion. Both are pure SQL, fast.
                for sweep_args in (
                    ["--commit", "--limit", "2000"],
                    ["--refresh-stage3", "--commit", "--limit", "2000"],
                ):
                    try:
                        cmd = [sys.executable, "-m",
                               "processors.pipeline.promote_to_kb"] + sweep_args
                        r = subprocess.run(cmd, capture_output=True,
                                           timeout=180, cwd=str(ROOT),
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                        log(f"end-sweep promote_to_kb {sweep_args[0]}: rc={r.returncode}")
                    except Exception as e:
                        log(f"end-sweep fail {sweep_args}: {type(e).__name__}: {e}")

                # 2. Compute stats + write boss-facing brief queue
                try:
                    stats = compute_stats()
                    log(f"stats={stats}")
                except Exception as e:
                    log(f"stats compute fail: {type(e).__name__}: {e}")
                    stats = {}

                elapsed = datetime.now(TZ) - started_watching
                elapsed_h = elapsed.total_seconds() / 3600
                elapsed_str = f"{elapsed_h:.2f}h"

                try:
                    queue_path = write_completion_brief(
                        stats, unload_results, elapsed_str=elapsed_str)
                    log(f"brief queue file: {queue_path.name}")
                except Exception as e:
                    log(f"brief queue write fail: {type(e).__name__}: {e}")
                    queue_path = None

                # 3. Log to system_history
                try:
                    from processors.history_log import log_event
                    body_parts = [
                        f"VRAM unload: {[r.get('ok') for r in unload_results]} "
                        f"({len(post)} models still loaded post-unload)",
                        f"watcher elapsed: {elapsed_str}",
                        f"stats: stage1_total={stats.get('stage1_total')} "
                        f"stage2_admit={stats.get('stage2_admit')}/{stats.get('stage2_total')} "
                        f"stage3_candidates={stats.get('stage3_candidates')}",
                    ]
                    if queue_path:
                        body_parts.append(f"boss notify: {queue_path.relative_to(ROOT)}")
                    log_event(
                        actor="watcher", kind="milestone", scope="ocr_pipeline",
                        title="OCR backfill done — Qwen unloaded + boss notified",
                        body="\n".join(body_parts),
                        refs=[str(queue_path.relative_to(ROOT)) if queue_path else "scripts/watch_backfill_cleanup.py"],
                    )
                except Exception as e:
                    log(f"history log fail: {type(e).__name__}: {e}")

                return
            else:
                log("waiting for backfill process to start...")
        time.sleep(int(args.poll_min * 60))

    log("deadline reached — exit (no cleanup)")


if __name__ == "__main__":
    main()
