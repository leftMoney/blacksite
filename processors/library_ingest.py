"""processors/library_ingest.py — 5/5 Phase A: 書庫 (library) ingestion.

Boss 5/5 directive 第 1 條:「每天週報都沒有入庫的情報，沒有進書庫」.
Pre-fix state: kb_chunks=0, kb_documents=0 — confirmed never ingested
anything despite system having strategist memos, daily briefs, boss_opinions,
and resolved leads accumulating.

Sources ingested (this pass):
  1. strategy_memos/*.md             — Tier 3 weekly synthesis (decay=structural)
  2. briefs/sent/*.md                — Tier 2 daily output (decay=30d)
  3. boss_opinions table (active)    — boss directives / decisions (decay=structural)
  4. kb_leads (resolved_*)           — closed lead resolutions (decay=30d)

Idempotency: UNIQUE(source_kind, source_row_id) on kb_documents catches
re-runs; we use hash-derived stable int from natural ID so re-ingest of
the same memo / opinion / lead becomes a no-op.

Chunking strategy:
  - memos / briefs : split by `## ` markdown heading (one chunk per section)
  - opinions       : whole opinion as one chunk (typically short)
  - leads          : whole lead row as one chunk

Daemon cron: 20:00 daily (after 19:00 daily_brief composition; before
21:00 strategist Sun runs that may want fresh library state).

Per CLAUDE.md §6.4 timezone constitution: all timestamps GMT+7 ISO 8601.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
INDEX_DB = RUNTIME_DIR / "index.db"
LOG_DIR = RUNTIME_DIR / "logs"

MEMOS_DIR = RUNTIME_DIR / "strategy_memos"
BRIEFS_DIR = RUNTIME_DIR / "briefs" / "sent"

TZ = timezone(timedelta(hours=7))
SCHEMA_VERSION = 8


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def _log(msg: str) -> None:
    line = f"[{now_iso()}] [library_ingest] {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"library_ingest_{now().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _stable_int(natural_id: str) -> int:
    """Hash a natural string id to a stable 31-bit int for kb_documents
    source_row_id. UNIQUE(source_kind, source_row_id) ⇒ same natural id
    always maps to same row → re-ingest is a no-op."""
    return int.from_bytes(hashlib.md5(natural_id.encode("utf-8")).digest()[:4], "big") & 0x7FFFFFFF


def _blob_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ====================================================================
# Markdown helpers
# ====================================================================

def _strip_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_without_frontmatter)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    import yaml as _yaml
    try:
        fm = _yaml.safe_load(text[3:end]) or {}
    except Exception:
        fm = {}
    body = text[end + len("\n---"):].lstrip()
    return fm, body


def _split_by_h2(body: str) -> list[tuple[str, str]]:
    """Split markdown body by `## ` headings → list of (heading, content).
    Content before first `## ` becomes ('_preamble', text)."""
    out: list[tuple[str, str]] = []
    current_head = "_preamble"
    buf: list[str] = []
    for line in body.split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            content = "\n".join(buf).strip()
            if content:
                out.append((current_head, content))
            current_head = m.group(1).strip()
            buf = []
        else:
            buf.append(line)
    content = "\n".join(buf).strip()
    if content:
        out.append((current_head, content))
    return out


# ====================================================================
# DB helpers
# ====================================================================

def _conn() -> sqlite3.Connection:
    db = sqlite3.connect(str(INDEX_DB))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=10000")
    return db


def _ensure_doc(
    db: sqlite3.Connection,
    *,
    doc_id: str,
    source_kind: str,
    source_row_id: int,
    platform: str,
    persona: str | None,
    event_at: str,
    valid_from: str,
    valid_to: str | None,
    text: str,
    raw_path: str | None,
) -> bool:
    """Insert kb_documents row if not exists. Returns True if inserted."""
    cur = db.execute(
        "SELECT 1 FROM kb_documents WHERE source_kind=? AND source_row_id=?",
        (source_kind, source_row_id),
    )
    if cur.fetchone():
        return False
    obs = now_iso()
    db.execute(
        """
        INSERT INTO kb_documents (
            doc_id, source_kind, source_row_id, platform, persona,
            observed_at, event_at, valid_from, valid_to,
            source_blob_hash, raw_pointer_json,
            indexed_at, schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            doc_id, source_kind, source_row_id, platform, persona,
            obs, event_at, valid_from, valid_to,
            _blob_hash(text),
            json.dumps({"raw_path": raw_path}, ensure_ascii=False) if raw_path else None,
            obs, SCHEMA_VERSION,
        ),
    )
    return True


def _ensure_chunk(
    db: sqlite3.Connection,
    *,
    chunk_id: str,
    doc_id: str,
    chunk_index: int,
    text: str,
    platform: str,
    persona: str | None,
    event_at: str,
    valid_from: str,
    valid_to: str | None,
    decay_class: str,
) -> bool:
    """Insert kb_chunks row if not exists. Returns True if inserted."""
    cur = db.execute(
        "SELECT 1 FROM kb_chunks WHERE chunk_id=?",
        (chunk_id,),
    )
    if cur.fetchone():
        return False
    obs = now_iso()
    db.execute(
        """
        INSERT INTO kb_chunks (
            chunk_id, doc_id, chunk_index, text, text_len,
            observed_at, event_at, valid_from, valid_to,
            platform, persona, decay_class, indexed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            chunk_id, doc_id, chunk_index, text, len(text),
            obs, event_at, valid_from, valid_to,
            platform, persona, decay_class, obs,
        ),
    )
    return True


# ====================================================================
# Per-source ingestion
# ====================================================================

def ingest_strategy_memos(db: sqlite3.Connection) -> dict:
    """Scan strategy_memos/*.md → kb_documents + kb_chunks (per ## section)."""
    if not MEMOS_DIR.exists():
        return {"docs_new": 0, "chunks_new": 0, "files_seen": 0}
    docs_new = chunks_new = 0
    files_seen = 0
    for mp in sorted(MEMOS_DIR.glob("*.md")):
        files_seen += 1
        try:
            text = mp.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            _log(f"read fail {mp.name}: {e}")
            continue
        fm, body = _strip_frontmatter(text)
        natural_id = f"strategy_memo:{mp.stem}"  # e.g. strategy_memo:2026-W18
        doc_id = natural_id
        srid = _stable_int(natural_id)
        # event_at = authored_at from frontmatter, fallback file mtime
        event_at = fm.get("authored_at") or datetime.fromtimestamp(
            mp.stat().st_mtime, tz=TZ
        ).isoformat(timespec="seconds")
        valid_from = event_at
        # Strategist memos are reference material — stay current until next memo
        valid_to = None

        # Use whole body for source_blob_hash (so any edit re-versions)
        if _ensure_doc(
            db,
            doc_id=doc_id,
            source_kind="strategy_memo",
            source_row_id=srid,
            platform="org",
            persona="CHIEF_STRATEGIST",
            event_at=event_at,
            valid_from=valid_from,
            valid_to=valid_to,
            text=body,
            raw_path=mp.relative_to(ROOT).as_posix(),
        ):
            docs_new += 1

        sections = _split_by_h2(body)
        for idx, (heading, content) in enumerate(sections):
            chunk_text = f"## {heading}\n\n{content}" if heading != "_preamble" else content
            cid = f"{doc_id}#{idx}"
            if _ensure_chunk(
                db,
                chunk_id=cid,
                doc_id=doc_id,
                chunk_index=idx,
                text=chunk_text,
                platform="org",
                persona="CHIEF_STRATEGIST",
                event_at=event_at,
                valid_from=valid_from,
                valid_to=valid_to,
                decay_class="structural",
            ):
                chunks_new += 1

    return {"docs_new": docs_new, "chunks_new": chunks_new, "files_seen": files_seen}


def ingest_daily_briefs(db: sqlite3.Connection) -> dict:
    """Scan briefs/sent/*.md → kb_documents + kb_chunks (per ## section)."""
    if not BRIEFS_DIR.exists():
        return {"docs_new": 0, "chunks_new": 0, "files_seen": 0}
    docs_new = chunks_new = 0
    files_seen = 0
    for bp in sorted(BRIEFS_DIR.glob("*.md")):
        files_seen += 1
        try:
            text = bp.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            _log(f"read fail {bp.name}: {e}")
            continue
        fm, body = _strip_frontmatter(text)
        natural_id = f"daily_brief:{bp.stem}"
        doc_id = natural_id
        srid = _stable_int(natural_id)
        event_at = fm.get("brief_date") or datetime.fromtimestamp(
            bp.stat().st_mtime, tz=TZ
        ).isoformat(timespec="seconds")
        # Normalize "YYYY-MM-DD" to ISO with offset
        if re.match(r"^\d{4}-\d{2}-\d{2}$", str(event_at).strip()):
            event_at = f"{event_at}T19:00:00+07:00"
        valid_from = event_at
        # Daily briefs decay 30d — historical context only, fresh ones supersede
        valid_to = None

        if _ensure_doc(
            db,
            doc_id=doc_id,
            source_kind="daily_brief",
            source_row_id=srid,
            platform="org",
            persona="SECTION_CHIEF",
            event_at=event_at,
            valid_from=valid_from,
            valid_to=valid_to,
            text=body,
            raw_path=bp.relative_to(ROOT).as_posix(),
        ):
            docs_new += 1

        sections = _split_by_h2(body)
        for idx, (heading, content) in enumerate(sections):
            chunk_text = f"## {heading}\n\n{content}" if heading != "_preamble" else content
            cid = f"{doc_id}#{idx}"
            if _ensure_chunk(
                db,
                chunk_id=cid,
                doc_id=doc_id,
                chunk_index=idx,
                text=chunk_text,
                platform="org",
                persona="SECTION_CHIEF",
                event_at=event_at,
                valid_from=valid_from,
                valid_to=valid_to,
                decay_class="30d",
            ):
                chunks_new += 1

    return {"docs_new": docs_new, "chunks_new": chunks_new, "files_seen": files_seen}


def ingest_boss_opinions(db: sqlite3.Connection) -> dict:
    """Read active boss_opinions → one doc + one chunk per opinion."""
    cur = db.execute(
        "SELECT opinion_id, source_ts, kind, content, context_summary, topic "
        "FROM boss_opinions WHERE status='active'"
    )
    docs_new = chunks_new = total = 0
    for row in cur.fetchall():
        total += 1
        opinion_id, source_ts, kind, content, ctx, topic = row
        natural_id = f"boss_opinion:{opinion_id}"
        doc_id = natural_id
        srid = _stable_int(natural_id)
        text = (
            f"[{kind}] {content}\n\n"
            f"context: {ctx or ''}\n"
            f"topic: {topic or ''}"
        )
        if _ensure_doc(
            db,
            doc_id=doc_id,
            source_kind="boss_opinion",
            source_row_id=srid,
            platform="org",
            persona="boss",
            event_at=source_ts,
            valid_from=source_ts,
            valid_to=None,
            text=text,
            raw_path=None,
        ):
            docs_new += 1
        cid = f"{doc_id}#0"
        if _ensure_chunk(
            db,
            chunk_id=cid,
            doc_id=doc_id,
            chunk_index=0,
            text=text,
            platform="org",
            persona="boss",
            event_at=source_ts,
            valid_from=source_ts,
            valid_to=None,
            decay_class="structural",
        ):
            chunks_new += 1
    return {"docs_new": docs_new, "chunks_new": chunks_new, "rows_seen": total}


def ingest_resolved_leads(db: sqlite3.Connection) -> dict:
    """Read kb_leads where state IN ('resolved_closed','resolved_escalate') →
    one doc + one chunk per resolved lead. Adds the 'lead conclusion' to library."""
    try:
        cur = db.execute(
            "SELECT lead_id, origin, target, state, suggested_action, "
            "       resolution_at, evidence, resolution "
            "FROM kb_leads "
            "WHERE state IN ('resolved_closed','resolved_escalate')"
        )
    except sqlite3.OperationalError as e:
        _log(f"kb_leads query skip: {e}")
        return {"docs_new": 0, "chunks_new": 0, "rows_seen": 0}
    docs_new = chunks_new = total = 0
    for row in cur.fetchall():
        total += 1
        lead_id, origin, target, state, suggested_action, resolved_at, evidence, resolution = row
        if not resolved_at:
            continue
        # resolved_at must have offset for kb_documents CHECK
        if not re.search(r"[+-]\d{2}:\d{2}$", str(resolved_at)):
            continue
        natural_id = f"lead_resolved:{lead_id}"
        doc_id = natural_id
        srid = _stable_int(natural_id)
        text = (
            f"[lead {state}] {target}\n\n"
            f"origin: {origin or '?'}\n"
            f"suggested_action: {suggested_action or ''}\n"
            f"resolution: {resolution or ''}\n"
            f"evidence: {(evidence or '')[:1000]}"
        )
        if _ensure_doc(
            db,
            doc_id=doc_id,
            source_kind="lead_resolved",
            source_row_id=srid,
            platform="org",
            persona=None,
            event_at=resolved_at,
            valid_from=resolved_at,
            valid_to=None,
            text=text,
            raw_path=None,
        ):
            docs_new += 1
        cid = f"{doc_id}#0"
        if _ensure_chunk(
            db,
            chunk_id=cid,
            doc_id=doc_id,
            chunk_index=0,
            text=text,
            platform="org",
            persona=None,
            event_at=resolved_at,
            valid_from=resolved_at,
            valid_to=None,
            decay_class="30d",
        ):
            chunks_new += 1
    return {"docs_new": docs_new, "chunks_new": chunks_new, "rows_seen": total}


# ====================================================================
# Entry
# ====================================================================

def run() -> dict:
    if not INDEX_DB.exists():
        _log(f"ABORT: index.db missing at {INDEX_DB}")
        return {"err": "no_db"}

    db = _conn()
    summary = {}
    try:
        for name, fn in (
            ("strategy_memos", ingest_strategy_memos),
            ("daily_briefs", ingest_daily_briefs),
            ("boss_opinions", ingest_boss_opinions),
            ("resolved_leads", ingest_resolved_leads),
        ):
            try:
                res = fn(db)
                db.commit()
                summary[name] = res
                _log(f"{name}: {res}")
            except Exception as e:
                db.rollback()
                _log(f"FAIL {name}: {type(e).__name__}: {e}")
                summary[name] = {"err": f"{type(e).__name__}: {e}"}
    finally:
        db.close()

    # Total counts
    total_docs = sum((r.get("docs_new", 0) for r in summary.values() if isinstance(r, dict)))
    total_chunks = sum((r.get("chunks_new", 0) for r in summary.values() if isinstance(r, dict)))
    _log(f"DONE total_docs_new={total_docs} total_chunks_new={total_chunks}")

    # Audit trail to system_history
    try:
        from processors.history_log import log_event
        log_event(
            actor="library_ingest",
            kind="metric",
            scope="library",
            title=f"ingest pass: +{total_docs} docs / +{total_chunks} chunks",
            body=json.dumps(summary, ensure_ascii=False, indent=2),
        )
    except Exception as e:
        _log(f"history_log fail (non-fatal): {type(e).__name__}: {e}")

    return summary


if __name__ == "__main__":
    res = run()
    if res.get("err"):
        sys.exit(1)
    sys.exit(0)
