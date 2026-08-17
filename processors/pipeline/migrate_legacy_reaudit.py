"""P8 — One-shot migration of legacy media_reaudit (3,509 rows from 5/7
Opus-default-via-claude.exe re-audit) into the new media_kb_decision table.

Per CLAUDE.md §2.1 migration directive (boss 5/8):
  Stage 2 (media_kb_decision) is a superset of media_reaudit; legacy rows
  flow in with model_used='opus_default_via_claude_exe_2026_05_07' (the
  audit was previously believed to be Sonnet, but Pro plan dashboard
  showed 0% Sonnet usage — actual model was Opus default via agent shell).

  These rows are NOT re-audited; they retain their 5/7 verdicts.

Idempotent: INSERT OR IGNORE so re-running is safe and existing post-5/8
pipeline rows (which are newer / better) are kept.

Usage:
    py -m processors.pipeline.migrate_legacy_reaudit          # dry-run by default
    py -m processors.pipeline.migrate_legacy_reaudit --commit
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from db.connection import get_connection

TZ = timezone(timedelta(hours=7))
LEGACY_MODEL_TAG = "opus_default_via_claude_exe_2026_05_07"


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def migrate(commit: bool) -> dict:
    conn = get_connection()
    src_total = conn.execute("SELECT COUNT(*) FROM media_reaudit").fetchone()[0]
    already = conn.execute(
        """SELECT COUNT(*) FROM media_reaudit r
           WHERE r.media_row_id IN (SELECT media_row_id FROM media_kb_decision)"""
    ).fetchone()[0]
    eligible = src_total - already

    if not commit:
        print(f"[dry-run] media_reaudit total: {src_total}")
        print(f"[dry-run] already present in media_kb_decision: {already} "
              f"(will be skipped via INSERT OR IGNORE)")
        print(f"[dry-run] would migrate: {eligible}")
        conn.close()
        return {"dry_run": True, "src_total": src_total,
                "would_migrate": eligible, "already_present": already}

    cur = conn.execute(
        """INSERT OR IGNORE INTO media_kb_decision
           (media_row_id, kb_admit, kb_value_class, kb_value_score,
            decision_tags, rationale, audit_score_0_100, audit_verdict,
            raw_response, model_used, processed_at)
           SELECT
              r.media_row_id,
              COALESCE(r.kb_admit, 0),
              r.kb_value_class,
              r.kb_value_score_0_100,
              r.decision_tags,
              r.kb_rationale,
              r.audit_score_0_100,
              r.audit_verdict,
              r.raw_response,
              ?,
              COALESCE(r.audited_at, ?)
             FROM media_reaudit r""",
        (LEGACY_MODEL_TAG, now_iso()),
    )
    inserted = cur.rowcount
    conn.commit()

    new_total = conn.execute("SELECT COUNT(*) FROM media_kb_decision").fetchone()[0]
    legacy_total = conn.execute(
        "SELECT COUNT(*) FROM media_kb_decision WHERE model_used=?",
        (LEGACY_MODEL_TAG,),
    ).fetchone()[0]
    conn.close()

    return {
        "src_total": src_total,
        "already_present": already,
        "eligible": eligible,
        "inserted": inserted,
        "new_kb_decision_total": new_total,
        "legacy_tagged_total": legacy_total,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true",
                        help="actually perform the migration (default: dry-run)")
    args = parser.parse_args()
    result = migrate(commit=args.commit)
    print("RESULT:")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
