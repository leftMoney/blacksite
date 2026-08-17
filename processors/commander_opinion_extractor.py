"""processors/commander_opinion_extractor.py — extract boss opinions from
commander's conversation.jsonl into boss_opinions SQLite table.

Cron */15 min via daemon. Tracks last processed file offset in
commander_extractor_state. For each new boss-role turn, calls
_llm_synth.claude_run (Haiku 4.5) with classification prompt. JSON line
inserted into boss_opinions.

Sidecar layer per CLAUDE.md §13.6 + boss directive 5/2 PM: commander cannot
modify code (M7.1 sandbox), so opinion extraction lives outside commander's
process. Any session can query boss directives history via
scripts/commander_history.py.

Backfill on first run: processes ALL existing boss turns (one-shot
classification cost ~$0.005-0.01 for typical 50-turn conversation).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from processors._llm_synth import claude_run, MODEL_HAIKU_4_5  # noqa: E402
from db.connection import get_connection  # noqa: E402

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
CONVERSATION_PATH = RUNTIME / "cmd" / "conversation.jsonl"
LOG_DIR = RUNTIME / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [opinion_extractor] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"opinion_extractor_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


CLASSIFY_PROMPT = """You classify a single chat turn from boss to commander (Blacksite intelligence framework, instance _TEMPLATE).

Boss's current statement (verbatim):
\"\"\"
{text}
\"\"\"

Recent context (last 3 turns before this one):
{context}

Output ONE LINE valid JSON, no markdown, no commentary. Schema:
{{
  "topic": "<short snake_case key, e.g. kb_design, persona_opsec, fb_strategy, daemon_health, brief_format, multi_agent_dev, lead_pipeline, opinion_extraction, folk-belief_research, bigo_strategy, llm_synth, infra, etc>",
  "kind": "directive|preference|decision|question|concern|feedback",
  "content": "<boss's text condensed to <=200 chars, preserve intent and key tokens>",
  "context_summary": "<1-line: what boss is responding to / asking>"
}}

Kinds:
- directive: tells engine to do specific work
- preference: states a desired way of doing things
- decision: locks a choice on an open question
- question: asks engine for information / status
- concern: flags risk / failure / worry
- feedback: reacts to prior engine output (positive / negative / corrective)

Pick exactly one kind. Pick the most specific topic. JSON only.
"""


def get_offset(conn) -> int:
    row = conn.execute(
        "SELECT last_offset FROM commander_extractor_state WHERE file_path=?",
        (str(CONVERSATION_PATH),),
    ).fetchone()
    return int(row[0]) if row else 0


def set_offset(conn, offset: int) -> None:
    conn.execute(
        "INSERT INTO commander_extractor_state(file_path, last_offset, last_run_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(file_path) DO UPDATE SET last_offset=excluded.last_offset, "
        "last_run_at=excluded.last_run_at",
        (str(CONVERSATION_PATH), offset, now_iso()),
    )


def load_turns() -> list[dict]:
    if not CONVERSATION_PATH.exists():
        return []
    out = []
    with CONVERSATION_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"role": "?", "text": "(parse fail)", "ts": now_iso()})
    return out


def format_context(turns: list[dict], i: int, k: int = 3) -> str:
    start = max(0, i - k)
    parts = []
    for t in turns[start:i]:
        role = t.get("role", "?")
        text = (t.get("text") or "")[:240]
        parts.append(f"[{role}] {text}")
    return "\n".join(parts) if parts else "(no prior turns)"


def gen_opinion_id(date_str: str, n: int) -> str:
    return f"O-{date_str}-{n:03d}"


def next_opinion_n(conn, date_str: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM boss_opinions WHERE opinion_id LIKE ?",
        (f"O-{date_str}-%",),
    ).fetchone()
    return (row[0] if row else 0) + 1


def classify(text: str, context: str) -> dict | None:
    prompt = CLASSIFY_PROMPT.format(text=text[:2000], context=context[:1500])
    ok, out = claude_run(
        prompt,
        skill_prefix=False,
        allowed_tools="",
        permission_mode="default",
        model=MODEL_HAIKU_4_5,
        timeout_s=30.0,
        max_retries=1,
    )
    if not ok or not out:
        return None
    s = out.find("{")
    e = out.rfind("}")
    if s < 0 or e < 0:
        return None
    try:
        return json.loads(out[s:e + 1])
    except json.JSONDecodeError:
        log(f"classify JSON parse fail: {out[:200]!r}")
        return None


def main() -> int:
    conn = get_connection()
    try:
        offset = get_offset(conn)
        all_turns = load_turns()
        total = len(all_turns)
        log(f"file_rows={total} processed_offset={offset}")
        if total <= offset:
            log("no new turns")
            return 0

        last_processed = total  # default: all processed
        new_count = 0
        for i in range(offset, total):
            turn = all_turns[i]
            if turn.get("role") != "boss":
                continue
            text = (turn.get("text") or "").strip()
            if len(text) < 2:
                continue
            ts = turn.get("ts") or now_iso()
            ctx = format_context(all_turns, i, k=3)
            cls = classify(text, ctx)
            if cls is None:
                log(f"classify fail at offset {i}; retry next run")
                last_processed = i  # rewind to this turn for retry
                break
            date_str = ts[:10]
            n = next_opinion_n(conn, date_str)
            oid = gen_opinion_id(date_str, n)
            try:
                conn.execute(
                    "INSERT INTO boss_opinions("
                    "  opinion_id, source_role, source_ts, source_offset,"
                    "  extracted_at, topic, kind, content, context_summary,"
                    "  refs, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                    (
                        oid, "boss", ts, i, now_iso(),
                        cls.get("topic", "uncategorized"),
                        cls.get("kind", "feedback"),
                        cls.get("content", text[:200]),
                        cls.get("context_summary", ""),
                        json.dumps(cls.get("refs", [])),
                    ),
                )
                conn.commit()  # commit per turn so partial progress sticks
                new_count += 1
                log(f"+ {oid} kind={cls.get('kind')} topic={cls.get('topic')}")
            except Exception as e:
                log(f"INSERT fail at offset {i}: {type(e).__name__}: {e}")
                last_processed = i
                break
        set_offset(conn, last_processed)
        conn.commit()
        log(f"done: +{new_count} opinions, offset {offset} → {last_processed}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
