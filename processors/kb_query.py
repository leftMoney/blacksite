"""
M7 — KB retrieval helper (Claude-direct, no embeddings).

Given a question or filter spec, returns active cards as a packaged JSON
bundle for Claude to reason over. No embedding service, no vector store —
just SQL filters on cards × entities × funnel_edges, returning the structured
corpus for in-context reasoning.

Rationale (boss directive 2026-04-29):
  Boss is on Claude Max 5X (flat-rate token budget); his Gemini paid project
  has no free embedding tier. Sending raw cards to Claude on each query is
  cheaper than buying embedding API + maintaining a vector store. For ~200
  active cards × ~500 tokens = ~100K tokens per query — well within Claude's
  context window at low relative cost (already paid via subscription).

Usage:
  py processors/kb_query.py state                                         # quick stats
  py processors/kb_query.py cards --tags funnel_competitor_intel
  py processors/kb_query.py cards --tags operator_graph,brand_seed_pulse  # OR
  py processors/kb_query.py cards --decay perishable                       # only fresh
  py processors/kb_query.py cards --search examplefunnel                       # name/title/body
  py processors/kb_query.py cards --min-action 0.5
  py processors/kb_query.py cards --as-prompt > q.md                        # for direct paste
  py processors/kb_query.py funnel --min-push 3
  py processors/kb_query.py operator-cluster <entity_name>                 # cluster around entity

Output formats:
  Default: human-readable per-card sections
  --json:   machine-readable JSON (for further pipelining)
  --as-prompt: markdown styled as a Claude prompt context (drop into chat)
"""

from __future__ import annotations

import argparse
import json
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

TZ = timezone(timedelta(hours=7))


def _build_card_query(tags=None, decay=None, search=None,
                      min_action=None, limit=200) -> tuple[str, list]:
    where = ["c.state = 'active'", "e.state = 'active'"]
    params: list = []
    if tags:
        # OR semantics across tags
        ors = []
        for t in tags:
            ors.append("c.decision_tags LIKE ?")
            params.append(f"%{t}%")
        where.append("(" + " OR ".join(ors) + ")")
    if decay:
        where.append("c.time_decay_class = ?")
        params.append(decay)
    if search:
        where.append("(e.name LIKE ? OR c.title LIKE ? OR c.body_md LIKE ?)")
        s = f"%{search}%"
        params.extend([s, s, s])
    if min_action is not None:
        where.append("c.actionability_score >= ?")
        params.append(float(min_action))
    sql = f"""
        SELECT c.row_id AS card_id, c.entity_row_id, c.title, c.body_md,
               c.decision_tags, c.actionability_score, c.risk_layer,
               c.time_decay_class, c.last_built_at, c.raw_pointer_json,
               e.kind, e.name, e.platform, e.seen_count, e.last_seen_ts
          FROM cards c
          JOIN entities e ON e.row_id = c.entity_row_id
         WHERE {' AND '.join(where)}
         ORDER BY c.actionability_score DESC NULLS LAST, e.seen_count DESC
         LIMIT ?
    """
    params.append(int(limit))
    return sql, params


def fetch_cards(conn, **filters) -> list[dict]:
    sql, params = _build_card_query(**filters)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def fetch_funnel(conn, min_push=1, kind=None) -> list[dict]:
    where = ["1=1"]
    params: list = []
    if min_push:
        where.append("push_count >= ?")
        params.append(int(min_push))
    if kind:
        where.append("edge_kind = ?")
        params.append(kind)
    sql = f"""
        SELECT row_id, from_chat_username, to_target_kind, to_target,
               edge_kind, bait_intent, push_count, distinct_senders,
               avg_amplification, review_state, review_verdict,
               join_state, last_seen_ts
          FROM funnel_edges
         WHERE {' AND '.join(where)}
         ORDER BY push_count DESC, avg_amplification DESC
         LIMIT 100
    """
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch_operator_cluster(conn, anchor_name: str) -> dict:
    """Given an entity name, return the operator cluster: anchor + identifier-shared neighbors + raw co-occurrence."""
    anchor = conn.execute(
        "SELECT row_id, kind, name, platform, seen_count FROM entities WHERE name=?",
        (anchor_name,),
    ).fetchone()
    if not anchor:
        return {"error": f"entity '{anchor_name}' not found"}
    aid = anchor["row_id"]
    # Identifier-shared = co-occur via shared identifier entities (phone/wallet/promo/lineid)
    shared = conn.execute(
        """SELECT DISTINCT e2.kind, e2.name, e2.seen_count
             FROM messages_entities me1
             JOIN messages_entities me2 ON me2.message_row_id = me1.message_row_id
             JOIN entities e2 ON e2.row_id = me2.entity_row_id
             JOIN messages_entities me3 ON me3.entity_row_id = e2.row_id
             JOIN messages_entities me4 ON me4.message_row_id = me3.message_row_id
             JOIN entities ident ON ident.row_id = me4.entity_row_id
            WHERE me1.entity_row_id = ?
              AND ident.kind IN ('phone','lineid','promo','wallet')
              AND e2.row_id != ?
              AND e2.kind IN ('channel','user','domain','brand')
            ORDER BY e2.seen_count DESC LIMIT 20""",
        (aid, aid),
    ).fetchall()
    cooccur = conn.execute(
        """SELECT e2.kind, e2.name, e2.seen_count, COUNT(*) co_msgs
             FROM messages_entities me1
             JOIN messages_entities me2 ON me2.message_row_id = me1.message_row_id
             JOIN entities e2 ON e2.row_id = me2.entity_row_id
            WHERE me1.entity_row_id = ?
              AND e2.row_id != ?
            GROUP BY e2.row_id ORDER BY co_msgs DESC LIMIT 15""",
        (aid, aid),
    ).fetchall()
    return {
        "anchor": dict(anchor),
        "identifier_shared_neighbors": [dict(r) for r in shared],
        "top_cooccurring_entities": [dict(r) for r in cooccur],
    }


def state_summary(conn) -> dict:
    out = {}
    out["entities_by_state"] = {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM entities GROUP BY state").fetchall()}
    out["cards_by_state"] = {r[0]: r[1] for r in conn.execute(
        "SELECT state, COUNT(*) FROM cards GROUP BY state").fetchall()}
    out["funnel_by_review"] = {r[0]: r[1] for r in conn.execute(
        "SELECT review_state, COUNT(*) FROM funnel_edges GROUP BY review_state").fetchall()}
    out["funnel_by_join"] = {r[0]: r[1] for r in conn.execute(
        "SELECT join_state, COUNT(*) FROM funnel_edges GROUP BY join_state").fetchall()}
    out["messages_total"] = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    out["entities_total"] = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    out["active_cards"] = conn.execute(
        "SELECT COUNT(*) FROM cards WHERE state='active'").fetchone()[0]
    return out


# ----------------------------------------------------------------------
# Renderers
# ----------------------------------------------------------------------

def render_cards_human(cards: list[dict]) -> str:
    if not cards:
        return "(no cards matched)\n"
    lines = []
    for c in cards:
        lines.append("=" * 72)
        lines.append(f"#{c['card_id']} eid={c['entity_row_id']} action={c.get('actionability_score')} "
                     f"decay={c.get('time_decay_class')} risk={c.get('risk_layer')}")
        lines.append(f"TITLE: {c['title']}")
        lines.append(f"TAGS:  {c.get('decision_tags','')}")
        lines.append(f"ENT:   {c['kind']:<8} {c['name']:<40} (seen={c['seen_count']}, last={c['last_seen_ts'][:16] if c['last_seen_ts'] else '?'})")
        lines.append("-" * 72)
        lines.append(c["body_md"])
        lines.append("")
    return "\n".join(lines)


def render_cards_as_prompt(cards: list[dict], question: str | None = None) -> str:
    """Markdown styled as Claude prompt context. Drop directly into Claude chat."""
    parts = []
    if question:
        parts.append(f"# Boss question\n{question}\n")
    parts.append(f"# KB cards (n={len(cards)}, sorted by actionability)\n")
    for c in cards:
        parts.append(f"## {c['title']}")
        parts.append(f"- entity: {c['kind']} `{c['name']}` (seen {c['seen_count']}; last "
                     f"{c['last_seen_ts'][:10] if c['last_seen_ts'] else '?'})")
        parts.append(f"- tags: `{c.get('decision_tags','')}`  "
                     f"action={c.get('actionability_score')}  "
                     f"decay={c.get('time_decay_class')}  "
                     f"risk={c.get('risk_layer')}")
        parts.append("")
        parts.append(c["body_md"])
        parts.append("")
    if question:
        parts.append("---")
        parts.append("# Task")
        parts.append("Answer the boss question above using ONLY the cards as evidence. "
                     "Cite specific cards by title. State what's unknown / requires fresh "
                     "research (§8 Chrome protocol). Format: 1) one-line verdict, "
                     "2) 3-5 supporting bullets, 3) caveats.")
    return "\n".join(parts)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)

    p_state = sub.add_parser("state", help="quick state distribution snapshot")

    p_cards = sub.add_parser("cards", help="fetch active cards with filters")
    p_cards.add_argument("--tags", help="csv subset of decision_tags (OR match)")
    p_cards.add_argument("--decay", choices=("perishable", "seasonal", "structural"))
    p_cards.add_argument("--search", help="substring match in name/title/body")
    p_cards.add_argument("--min-action", type=float, help="filter actionability_score >= X")
    p_cards.add_argument("--limit", type=int, default=50)
    p_cards.add_argument("--json", action="store_true", help="emit JSON")
    p_cards.add_argument("--as-prompt", action="store_true",
                         help="emit as Claude-prompt-ready markdown")
    p_cards.add_argument("--question", help="boss question to embed in --as-prompt output")

    p_funnel = sub.add_parser("funnel", help="fetch funnel_edges with filters")
    p_funnel.add_argument("--min-push", type=int, default=1)
    p_funnel.add_argument("--kind", choices=("funnel_push", "casual_mention"))
    p_funnel.add_argument("--json", action="store_true")

    p_op = sub.add_parser("operator-cluster", help="explore operator graph around an entity")
    p_op.add_argument("anchor", help="entity name to anchor on")
    p_op.add_argument("--json", action="store_true")

    args = parser.parse_args()

    init_db()
    conn = get_connection()

    if args.mode == "state":
        s = state_summary(conn)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return

    if args.mode == "cards":
        tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
        cards = fetch_cards(conn, tags=tags, decay=args.decay, search=args.search,
                            min_action=args.min_action, limit=args.limit)
        if args.json:
            print(json.dumps(cards, ensure_ascii=False, indent=2))
        elif args.as_prompt:
            print(render_cards_as_prompt(cards, args.question))
        else:
            print(render_cards_human(cards))
        return

    if args.mode == "funnel":
        edges = fetch_funnel(conn, min_push=args.min_push, kind=args.kind)
        if args.json:
            print(json.dumps(edges, ensure_ascii=False, indent=2))
        else:
            for e in edges:
                print(f"#{e['row_id']:<3} {e['edge_kind']:<14} push={e['push_count']:<3} "
                      f"review={e['review_state']:<10} join={e['join_state']:<25} "
                      f"{e['from_chat_username']} → {e['to_target_kind']:<16} {e['to_target']}")
        return

    if args.mode == "operator-cluster":
        c = fetch_operator_cluster(conn, args.anchor)
        if args.json:
            print(json.dumps(c, ensure_ascii=False, indent=2))
        else:
            if "error" in c:
                print(c["error"])
                return
            print(f"=== Operator cluster around: {c['anchor']['name']} ({c['anchor']['kind']}, seen={c['anchor']['seen_count']}) ===")
            print()
            print("Identifier-shared neighbors (operator-graph collapse via shared phone/wallet/promo/lineid):")
            for n in c["identifier_shared_neighbors"]:
                print(f"  {n['kind']:<8} {n['name']:<40} seen={n['seen_count']}")
            print()
            print("Top co-occurring entities:")
            for n in c["top_cooccurring_entities"]:
                print(f"  {n['kind']:<8} {n['name']:<40} co_msgs={n['co_msgs']} seen={n['seen_count']}")
        return


if __name__ == "__main__":
    main()
