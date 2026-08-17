"""
Entity state machine + decay cron (M5).

Daily 02:00 Bangkok. For each entity, computes:
    age_days   = today - entity.last_seen_ts
    decay_cls  = entity's card.time_decay_class (default 'seasonal' if no card)

Applies state transition table per decay_cls:

    age_days < dormant_days[cls]                       → 'active'
    dormant_days <= age_days < superseded_days[cls]    → 'dormant'
    age_days >= superseded_days[cls]                   → 'superseded'

Mirrors entity.state to all entity_brief cards on that entity (so M4
prepare-queue's `state='active'` filter naturally excludes dormant ones).

Manual states `noise` and `contradicted` are NEVER auto-modified — they
encode human / future-agent decisions and the decay cron must respect them.

State machine summary:

  ┌────────────────────────────────────────────────────┐
  │  active  ←─ decay rules + new evidence ←─  dormant │
  │     │                                          │   │
  │     ↓ no activity ≥ superseded_days            ↓   │
  │  superseded ←──────────────────────────────────┘   │
  │                                                    │
  │  noise / contradicted ← human only ← (any state)   │
  └────────────────────────────────────────────────────┘

Decay-driven transitions: active ↔ dormant ↔ superseded.
Human-only transitions: anything → noise / contradicted (no auto-revert).
"""

from __future__ import annotations

import argparse
import os
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
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=7))

# Decay-class → state transition windows. Tuned so:
#   - perishable: short half-life (one-shot promos, expired invites)
#   - seasonal:   campaign-cycle relevant (1-3 month windows)
#   - structural: stable identities (operator brands, KOLs, regulators)
DECAY_RULES = {
    "perishable": {"dormant_days": 14, "superseded_days": 30},
    "seasonal":   {"dormant_days": 60, "superseded_days": 120},
    "structural": {"dormant_days": 180, "superseded_days": 365},
}
DEFAULT_CLASS = "seasonal"
PROTECTED_STATES = {"noise", "contradicted"}  # human-only — never auto-modified

# Card refresh-interval table (used by card_builder.card_is_fresh).
# Same decay_class drives both decay-to-dormant AND refresh frequency.
CARD_REFRESH_HOURS = {
    "perishable": 4,
    "seasonal":   12,
    "structural": 24,
}


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log(msg: str) -> None:
    line = f"[{now_bkk().isoformat(timespec='seconds')}] [decay] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"decay_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def compute_state(last_seen_ts: str | None, decay_class: str | None) -> str:
    if not last_seen_ts:
        return "active"
    try:
        last = datetime.fromisoformat(last_seen_ts)
    except Exception:
        return "active"
    age_days = (now_bkk() - last).days
    rules = DECAY_RULES.get(decay_class or DEFAULT_CLASS, DECAY_RULES[DEFAULT_CLASS])
    if age_days >= rules["superseded_days"]:
        return "superseded"
    if age_days >= rules["dormant_days"]:
        return "dormant"
    return "active"


def run(dry_run: bool = False) -> dict:
    init_db()
    conn = get_connection()
    rows = conn.execute(
        """SELECT e.row_id, e.state, e.last_seen_ts,
                  c.time_decay_class
             FROM entities e
             LEFT JOIN cards c
               ON c.entity_row_id = e.row_id
              AND c.card_kind = 'entity_brief' """
    ).fetchall()

    transitions: dict[str, int] = {}
    cards_synced = 0
    protected_skipped = 0

    for r in rows:
        if r["state"] in PROTECTED_STATES:
            protected_skipped += 1
            continue
        new_state = compute_state(r["last_seen_ts"], r["time_decay_class"])
        if new_state == r["state"]:
            continue
        decay_class = r["time_decay_class"] or DEFAULT_CLASS
        reason = f"auto-decay {r['state']}→{new_state} via {decay_class} rules"
        key = f"{r['state']} → {new_state}"
        transitions[key] = transitions.get(key, 0) + 1
        if dry_run:
            continue
        conn.execute(
            "UPDATE entities SET state=?, state_changed_at=?, state_reason=? WHERE row_id=?",
            (new_state, now_bkk().isoformat(timespec="seconds"), reason, r["row_id"]),
        )
        # Mirror to entity_brief cards UNLESS they're in `contradicted` state.
        cur = conn.execute(
            "UPDATE cards SET state=? WHERE entity_row_id=? AND state != 'contradicted'",
            (new_state, r["row_id"]),
        )
        cards_synced += cur.rowcount

    log(f"scanned={len(rows)} protected={protected_skipped} "
        f"transitions={transitions or '{}'} cards_synced={cards_synced} dry_run={dry_run}")

    # State distribution snapshot
    snapshot = {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM entities GROUP BY state ORDER BY 2 DESC"
    ).fetchall()}
    log(f"entity state distribution: {snapshot}")

    conn.close()
    return {"scanned": len(rows), "transitions": transitions,
            "cards_synced": cards_synced, "snapshot": snapshot}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="compute transitions but don't write")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
