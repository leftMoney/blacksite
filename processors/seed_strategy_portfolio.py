"""Chief Strategist portfolio view for Seed Intelligence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
SEED_DIR = RUNTIME / "seed_intelligence"
CURRENT_JSON = SEED_DIR / "current.json"
AUDIT_JSON = SEED_DIR / "audit" / "current.json"
PORTFOLIO_JSON = SEED_DIR / "strategy_portfolio.json"
PORTFOLIO_MD = SEED_DIR / "strategy_portfolio.md"

# Example vertical → portfolio map. TODO: replace verticals + commercial_job with your
# instance's domain rings and the concrete commercial advantage each one serves (§1).
TARGETS = {
    "vertical_a": {"target_watchlist": 8, "commercial_job": "<core-domain acquisition signal for the client brand>"},
    "vertical_b": {"target_watchlist": 8, "commercial_job": "<adjacent-ecosystem / KOL activation signal>"},
    "vertical_c": {"target_watchlist": 5, "commercial_job": "<periphery / cultural-context signal>"},
}


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def portfolio() -> dict:
    seed = load_json(CURRENT_JSON, {"candidates": [], "watchlist": [], "actions": [], "summary": {}})
    audit = load_json(AUDIT_JSON, {"verdict": "unknown", "findings": [], "summary": {}})
    candidates = seed.get("candidates", []) if isinstance(seed, dict) else []
    watchlist = seed.get("watchlist", []) if isinstance(seed, dict) else []
    actions = seed.get("actions", []) if isinstance(seed, dict) else []

    by_vertical = defaultdict(list)
    for item in watchlist:
        by_vertical[str(item.get("vertical") or "unknown")].append(item)

    lanes = []
    for vertical, target in TARGETS.items():
        items = by_vertical.get(vertical, [])
        pending = [
            a for a in actions
            if any(w.get("seed_id") == a.get("seed_id") for w in items)
            and a.get("status") == "pending_boss_approval"
        ]
        gap = max(0, int(target["target_watchlist"]) - len(items))
        lanes.append({
            "vertical": vertical,
            "commercial_job": target["commercial_job"],
            "target_watchlist": target["target_watchlist"],
            "watchlist_count": len(items),
            "pending_boss_approval": len(pending),
            "gap": gap,
            "status": "underfilled" if gap else "filled",
            "top_seeds": sorted(items, key=lambda x: -int(x.get("seed_score") or 0))[:8],
        })

    status_counts = Counter(str(c.get("status") or "unknown") for c in candidates)
    directives = []
    for lane in lanes:
        if lane["gap"]:
            directives.append({
                "kind": "seed_portfolio_gap",
                "vertical": lane["vertical"],
                "ask": f"Increase verified seed discovery by {lane['gap']} watchlist slots.",
                "owner": "SECTION_CHIEF",
            })
    if audit.get("verdict") in {"critical", "warning"}:
        directives.append({
            "kind": "seed_quality_repair",
            "ask": "Resolve Seed Audit findings before approving more follows.",
            "owner": "SECTION_CHIEF.seed_audit",
        })

    payload = {
        "generated_at": now_iso(),
        "instance": ACTIVE_INSTANCE,
        "actor": "CHIEF_STRATEGIST.seed_portfolio",
        "audit_verdict": audit.get("verdict"),
        "candidate_status_counts": dict(status_counts),
        "lanes": lanes,
        "directives": directives,
    }
    write_json(PORTFOLIO_JSON, payload)
    write_report(payload)
    try:
        from processors.history_log import log_event

        log_event(
            actor="CHIEF_STRATEGIST",
            kind="metric",
            scope="seed_portfolio",
            title="seed portfolio review",
            body=json.dumps({
                "audit_verdict": payload["audit_verdict"],
                "directives": len(directives),
                "lanes": {x["vertical"]: x["watchlist_count"] for x in lanes},
            }, ensure_ascii=False),
            refs=[PORTFOLIO_JSON.relative_to(ROOT).as_posix(), PORTFOLIO_MD.relative_to(ROOT).as_posix()],
        )
    except Exception:
        pass
    return payload


def write_report(payload: dict) -> None:
    lines = [
        f"# Seed Portfolio Review - {payload['generated_at']}",
        "",
        f"audit_verdict: {payload.get('audit_verdict')}",
        f"candidate_status_counts: {json.dumps(payload.get('candidate_status_counts'), ensure_ascii=False)}",
        "",
        "## Lanes",
        "",
    ]
    for lane in payload["lanes"]:
        lines.append(
            f"- {lane['vertical']}: {lane['watchlist_count']}/{lane['target_watchlist']} "
            f"status={lane['status']} pending_boss={lane['pending_boss_approval']} "
            f"job={lane['commercial_job']}"
        )
    lines.extend(["", "## Directives", ""])
    if not payload["directives"]:
        lines.append("- none")
    for directive in payload["directives"]:
        lines.append(f"- {directive['kind']} {directive.get('vertical', '-')}: {directive['ask']} -> {directive['owner']}")
    PORTFOLIO_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()
    payload = portfolio()
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps({"audit_verdict": payload["audit_verdict"], "directives": len(payload["directives"]), "path": str(PORTFOLIO_JSON)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
