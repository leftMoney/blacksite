"""scripts/compose_cards_loop.py — manual compose loop replacement for missing
Claude scheduled task `blacksite-cards-build`.

Boss 5/7 audit revealed: queue/pending_2026-05-02T13-13-29.json (202KB) sat
unprocessed for 5 days because the Claude scheduled task wasn't running. This
script bridges the gap — invokes _llm_synth.claude_run for each candidate
bundle, generates a 7-dim card per CHIEF_STRATEGIST.md schema, applies via
card_builder.py apply-card.

Usage:
  py scripts/compose_cards_loop.py                         # all queue files, all candidates
  py scripts/compose_cards_loop.py --limit 5               # first 5 candidates only
  py scripts/compose_cards_loop.py --queue pending_2026-05-07T14-59-04.json
  py scripts/compose_cards_loop.py --dry-run               # don't UPSERT, just print
"""

from __future__ import annotations

# 2026-05-14: old Claude Code scheduled task `blacksite-cards-build` is
# disabled and moved under .claude/scheduled-tasks/disabled_*. This daemon path
# is the active M4 compose route.

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from processors import llm_profiles  # noqa: E402

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
QUEUE_DIR = RUNTIME_DIR / "cards" / "queue"
BUILT_DIR = RUNTIME_DIR / "cards" / "built"
COMPOSED_DIR = RUNTIME_DIR / "cards" / "composed"
LOG_DIR = RUNTIME_DIR / "logs"
COMPOSED_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def _log(msg: str) -> None:
    line = f"[{now_iso()}] [compose_cards_loop] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"compose_cards_loop_{now().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


COMPOSE_PROMPT_TEMPLATE = """\
你是 Blacksite _TEMPLATE 策略長兼 KB card 編纂者。從以下 7 維度 evidence bundle
合成一張結構化情報卡 (KB card)，目的是為 the client brand 商業決策服務。

## the client brand 商業決策關注（決定 actionability + decision_tags）
1. 競對 funnel 結構（灰盤 operator graph）
2. folk-belief/lottery 玩家心理觸發點（KOL / 號碼 / dream）
3. 直播虛擬禮物洗錢監管熱區（Bigo / TikTok Live / 其他直播平台）
4. 運動 KOL × sportsbook 跨界
5. 在地支付行為（instant-payment rail / 主導 e-wallet / 超商通路）
6. Regulator weather（the sports regulator / 執政者立場 / Casino bill / 廣告禁令條文）

## decision_tags 受控值（必選 1+ 個）
- TA_acquisition          # 玩家獲取/folk-belief 心理
- funnel_competitor_intel # 競對 funnel 結構
- regulatory_weather      # 法規動向
- KOL_safety_audit        # KOL 風險
- payment_behavior        # 支付行為
- folk-belief_x_lottery_overlap# folk-belief × 彩券 cross-signal
- brand_seed_pulse        # 競對品牌 SEO 變動
- operator_graph          # 灰盤 operator 圖
- bot_pump_noise_filter   # 純 spam noise（低商業價值，標 noise filter 用）

## 輸出格式（嚴格 JSON，無 markdown 包裝）
```json
{{
  "entity_row_id": <int>,
  "title": "<string 60 字內 — 中文 OK，要凸顯該 entity 對 the client brand 的角色>",
  "body_md": "<繁體中文 markdown 200-400 字，含: ## 角色定位 / ## 商業含義 / ## 證據摘要 / ## 後續觀察>",
  "decision_tags": "<comma-separated controlled values from above>",
  "actionability_score": <float 0.0-1.0; the client brand 決策可行動性 — 0.9+ = 立即可動, 0.5-0.8 = 觀察級, <0.3 = 純 noise>,
  "risk_layer": "<regulatory|brand_safety|persona_burn|none>",
  "time_decay_class": "<perishable (24-72h) | seasonal (1-3 mo) | structural (multi-quarter)>"
}}
```

## Evidence bundle (7-dim)
```json
{evidence_json}
```

請只輸出 JSON，不要 markdown 包裝、不要其他文字。
"""


def compose_one_card(bundle: dict, dry_run: bool) -> dict | None:
    """Invoke LLM to compose one card from a candidate bundle."""
    try:
        from processors._llm_synth import claude_run
    except ImportError:
        _log("_llm_synth unavailable; cannot compose")
        return None

    eid = bundle.get("entity_row_id")
    name = (bundle.get("entity") or {}).get("name", "?")
    _log(f"composing card for entity_row_id={eid} name={name}")

    evidence_json = json.dumps(bundle, ensure_ascii=False, indent=2)
    if len(evidence_json) > 8000:
        evidence_json = evidence_json[:8000] + "\n... [truncated]"

    task = COMPOSE_PROMPT_TEMPLATE.format(evidence_json=evidence_json)

    try:
        # 5/18 security tightening: explicit allowed_tools="" — compose is
        # pure JSON-text generation. The evidence_json contains untrusted
        # KB-derived OCR/rationale; with no tools, an injection embedded in
        # that content cannot reach Bash/Write/Edit. Caller parses the
        # JSON output itself (json.loads at line 158).
        ok, out = claude_run(
            task=task,
            skill_prefix=False,
            extra_system="",
            allowed_tools="",
            permission_mode="default",
            agent_memory_id="CHIEF_STRATEGIST",
            timeout_s=300,
            max_retries=2,
        )
    except Exception as e:
        _log(f"  LLM call failed: {e}")
        return None

    if not ok or not out:
        _log(f"  LLM returned no output (ok={ok})")
        return None

    text = out.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        card = json.loads(text)
    except json.JSONDecodeError as e:
        _log(f"  JSON parse failed: {e}; raw output preview: {text[:300]}")
        return None

    if "entity_row_id" not in card and eid is not None:
        card["entity_row_id"] = eid

    return card


def apply_card(card: dict, dry_run: bool) -> bool:
    """Write composed card JSON to composed/ and invoke card_builder apply-card."""
    eid = card.get("entity_row_id")
    if eid is None:
        _log("  card missing entity_row_id; skip")
        return False

    composed_path = COMPOSED_DIR / f"composed_{eid}_{now().strftime('%Y%m%dT%H%M%S')}.json"
    composed_path.write_text(
        json.dumps(card, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _log(f"  wrote {composed_path.name}")

    if dry_run:
        _log(f"  [dry-run] would apply-card {composed_path.name}")
        return True

    # Audit label: the strategic-tier model id from YAML, suffixed with the
    # compose-loop pass identifier so DB rows are attributable to this code
    # path. No hardcoded model name.
    compose_label = f"{llm_profiles.tier_model('claude', 'strategic')}-via-compose-loop"

    try:
        no_window_kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        result = subprocess.run(
            [sys.executable, str(ROOT / "processors" / "card_builder.py"),
             "apply-card", str(composed_path),
             "--model-used", compose_label],
            capture_output=True, text=True, timeout=60,
            **no_window_kw,
        )
        if result.returncode == 0:
            _log(f"  apply-card OK: {result.stdout.strip()[:200]}")
            return True
        else:
            _log(f"  apply-card FAIL rc={result.returncode}: {result.stderr.strip()[:300]}")
            return False
    except Exception as e:
        _log(f"  apply-card exception: {e}")
        return False


def process_queue_file(queue_path: Path, limit: int | None, dry_run: bool) -> dict:
    _log(f"processing {queue_path.name}")
    data = json.loads(queue_path.read_text(encoding="utf-8"))
    candidates = data.get("candidates", [])
    if limit:
        candidates = candidates[:limit]
    _log(f"  {len(candidates)} candidates to compose")

    summary = {"queue": queue_path.name, "total": len(candidates),
               "composed": 0, "applied": 0, "failed": 0, "cards": []}

    for i, bundle in enumerate(candidates, 1):
        eid = bundle.get("entity_row_id")
        name = (bundle.get("entity") or {}).get("name", "?")
        _log(f"[{i}/{len(candidates)}] eid={eid} name={name}")

        card = compose_one_card(bundle, dry_run)
        if card is None:
            summary["failed"] += 1
            continue
        summary["composed"] += 1

        ok = apply_card(card, dry_run)
        if ok:
            summary["applied"] += 1
            summary["cards"].append({
                "entity_row_id": eid,
                "name": name,
                "title": card.get("title"),
                "decision_tags": card.get("decision_tags"),
                "actionability_score": card.get("actionability_score"),
                "risk_layer": card.get("risk_layer"),
            })
        else:
            summary["failed"] += 1

    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default=None,
                   help="specific queue filename (default: all pending)")
    p.add_argument("--limit", type=int, default=None,
                   help="limit candidates per queue file")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.queue:
        queue_files = [QUEUE_DIR / args.queue]
    else:
        queue_files = sorted(QUEUE_DIR.glob("pending_*.json"))

    if not queue_files:
        _log("no queue files to process")
        return

    overall = {"queue_files": len(queue_files), "summaries": []}
    for qp in queue_files:
        if not qp.exists():
            _log(f"queue file missing: {qp}")
            continue
        s = process_queue_file(qp, args.limit, args.dry_run)
        overall["summaries"].append(s)
        if not args.dry_run:
            done_path = qp.parent / qp.name.replace("pending_", "done_", 1)
            qp.rename(done_path)
            _log(f"renamed {qp.name} → {done_path.name}")

    print(json.dumps(overall, ensure_ascii=False, indent=2))

    if not args.dry_run:
        try:
            from processors.history_log import log_event
            total_applied = sum(s["applied"] for s in overall["summaries"])
            total_failed = sum(s["failed"] for s in overall["summaries"])
            log_event(
                actor="compose_cards_loop", kind="milestone", scope="library",
                title=f"Compose loop: {total_applied} cards applied / {total_failed} failed",
                body=json.dumps(overall, ensure_ascii=False),
            )
        except Exception as e:
            _log(f"log_event failed: {e}")


if __name__ == "__main__":
    main()
