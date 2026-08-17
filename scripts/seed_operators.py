"""
Blacksite — operator entity seed.

Reads instances/<active>/data/operators.yaml and:
  1. Upserts operator metadata into entities (kind=brand) + their mirror domains
     (kind=domain), populating aliases_json / role / tier / risk_flags_json /
     notes. Existing rows preserve seen_count / first_seen_ts / last_seen_ts.
  2. Emits processors/rules/brands.yaml — identifier extractor patterns auto-
     generated from each operator's text_aliases. Loaded by rules_layer
     alongside identifiers.yaml; matched plain-text mentions become
     kind=brand entities canonicalized via force:<name> normalize.

Idempotent — running twice is a no-op (unless operators.yaml changed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
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
OPERATORS_YAML = ROOT / "instances" / ACTIVE_INSTANCE / "data" / "operators.yaml"
BRANDS_YAML = ROOT / "processors" / "rules" / "brands.yaml"

TZ = timezone(timedelta(hours=7))


def now_bkk() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def upsert_brand_entity(
    conn,
    canonical: str,
    domains: list[str],
    role: str | None,
    tier: str | None,
    risk_flags: list[str] | None,
    notes: str | None,
) -> tuple[int, str]:
    """Upsert kind=brand entity. Returns (row_id, status) where status is
    'inserted' | 'updated' | 'unchanged'."""
    aliases_json = json.dumps(domains, ensure_ascii=False) if domains else None
    risk_flags_json = json.dumps(risk_flags, ensure_ascii=False) if risk_flags else None

    cur = conn.execute(
        "SELECT row_id, aliases_json, role, tier, risk_flags_json, notes "
        "FROM entities WHERE kind='brand' AND platform IS NULL AND name=?",
        (canonical,),
    )
    r = cur.fetchone()
    if r:
        # Preserve observation columns (seen_count, first/last_seen_ts);
        # update only the seed-managed metadata columns.
        if (r["aliases_json"] == aliases_json
                and r["role"] == role
                and r["tier"] == tier
                and r["risk_flags_json"] == risk_flags_json
                and r["notes"] == notes):
            return r["row_id"], "unchanged"
        conn.execute(
            "UPDATE entities SET aliases_json=?, role=?, tier=?, "
            "risk_flags_json=?, notes=? WHERE row_id=?",
            (aliases_json, role, tier, risk_flags_json, notes, r["row_id"]),
        )
        return r["row_id"], "updated"

    cur = conn.execute(
        "INSERT INTO entities (kind, platform, name, aliases_json, role, tier, "
        "risk_flags_json, notes, seen_count) VALUES ('brand', NULL, ?, ?, ?, ?, ?, ?, 0)",
        (canonical, aliases_json, role, tier, risk_flags_json, notes),
    )
    return cur.lastrowid, "inserted"


def upsert_domain_entity(
    conn,
    domain: str,
    canonical: str,
    role: str | None,
    tier: str | None,
    risk_flags: list[str] | None,
    notes: str | None,
) -> tuple[int, str]:
    """Upsert kind=domain entity (lowercased). aliases_json points back to
    the canonical brand name for graph collapse."""
    name = domain.lower()
    aliases_json = json.dumps([canonical], ensure_ascii=False)
    risk_flags_json = json.dumps(risk_flags, ensure_ascii=False) if risk_flags else None

    cur = conn.execute(
        "SELECT row_id, aliases_json, role, tier, risk_flags_json, notes "
        "FROM entities WHERE kind='domain' AND platform IS NULL AND name=?",
        (name,),
    )
    r = cur.fetchone()
    if r:
        if (r["aliases_json"] == aliases_json
                and r["role"] == role
                and r["tier"] == tier
                and r["risk_flags_json"] == risk_flags_json
                and r["notes"] == notes):
            return r["row_id"], "unchanged"
        conn.execute(
            "UPDATE entities SET aliases_json=?, role=?, tier=?, "
            "risk_flags_json=?, notes=? WHERE row_id=?",
            (aliases_json, role, tier, risk_flags_json, notes, r["row_id"]),
        )
        return r["row_id"], "updated"

    cur = conn.execute(
        "INSERT INTO entities (kind, platform, name, aliases_json, role, tier, "
        "risk_flags_json, notes, seen_count) VALUES ('domain', NULL, ?, ?, ?, ?, ?, ?, 0)",
        (name, aliases_json, role, tier, risk_flags_json, notes),
    )
    return cur.lastrowid, "inserted"


def _yaml_sq(s: str) -> str:
    """YAML single-quoted string: doubles internal single quotes per spec."""
    return "'" + s.replace("'", "''") + "'"


def emit_brands_yaml(operators: list[dict], src_path: Path, out_path: Path) -> int:
    """Generate brands.yaml — identifier extractor patterns for plain-text
    brand mentions. Each operator's text_aliases regex(es) become entries
    that capture and force-normalize to the canonical name."""
    lines = [
        "# GENERATED by scripts/seed_operators.py — DO NOT EDIT BY HAND",
        f"# Source: {src_path.relative_to(ROOT).as_posix()}",
        f"# Regenerated: {now_bkk()}",
        "#",
        "# Each pattern captures any surface form of an operator's name and",
        "# force-normalizes to the canonical brand entity. Loaded by",
        "# processors/rules_layer.py alongside identifiers.yaml.",
        "",
        "brand:",
    ]
    pattern_count = 0
    for op in operators:
        canonical = op["canonical"]
        for pat in op.get("text_aliases") or []:
            lines.append(f"  # {canonical}")
            lines.append(f"  - regex: {_yaml_sq(pat)}")
            lines.append(f"    kind: brand")
            lines.append(f"    normalize: [{_yaml_sq('force:' + canonical)}]")
            pattern_count += 1
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pattern_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change without touching DB or files")
    ap.add_argument("--no-patterns", action="store_true",
                    help="Skip regenerating processors/rules/brands.yaml")
    args = ap.parse_args()

    if not OPERATORS_YAML.exists():
        print(f"FATAL: {OPERATORS_YAML} not found")
        sys.exit(1)

    with OPERATORS_YAML.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    operators = data.get("operators", [])
    print(f"loaded {len(operators)} operator entries from {OPERATORS_YAML.relative_to(ROOT)}")

    if args.dry_run:
        for op in operators:
            print(f"  - {op['canonical']:20s} tier={op.get('tier')} role={op.get('role')} "
                  f"domains={len(op.get('domains') or [])} aliases={len(op.get('text_aliases') or [])}")
        if not args.no_patterns:
            print(f"\nwould regenerate {BRANDS_YAML.relative_to(ROOT)}")
        return

    init_db()
    conn = get_connection()
    try:
        stats = {"brand_inserted": 0, "brand_updated": 0, "brand_unchanged": 0,
                 "domain_inserted": 0, "domain_updated": 0, "domain_unchanged": 0}
        for op in operators:
            canonical = op["canonical"]
            domains = op.get("domains") or []
            role = op.get("role")
            tier = op.get("tier")
            risk_flags = op.get("risk_flags") or []
            notes = (op.get("notes") or "").strip() or None

            _, brand_status = upsert_brand_entity(
                conn, canonical, domains, role, tier, risk_flags, notes
            )
            stats[f"brand_{brand_status}"] += 1

            for domain in domains:
                _, domain_status = upsert_domain_entity(
                    conn, domain, canonical, role, tier, risk_flags, notes
                )
                stats[f"domain_{domain_status}"] += 1
        conn.commit()
    finally:
        conn.close()

    print(f"brand entities  : +{stats['brand_inserted']} new / "
          f"~{stats['brand_updated']} updated / ={stats['brand_unchanged']} unchanged")
    print(f"domain entities : +{stats['domain_inserted']} new / "
          f"~{stats['domain_updated']} updated / ={stats['domain_unchanged']} unchanged")

    if not args.no_patterns:
        n = emit_brands_yaml(operators, OPERATORS_YAML, BRANDS_YAML)
        print(f"wrote {n} brand patterns to {BRANDS_YAML.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
