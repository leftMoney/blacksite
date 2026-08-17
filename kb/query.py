"""kb/query.py — unified read-only KB query helper for agents (boss 5/3 directive).

Replaces ad-hoc sqlite3 calls scattered across agents / chiefs / strategist with
a single CLI any tier can shell out to. Read-only by design (WAL connection
opened in deferred mode; no write SQL emitted by this module).

Subcommands:
  search  <text>                  — fuzzy text match across cards + messages
  cards   [--tier T] [--since X]  — list cards (active state by default)
  entity  <name>                  — 360-view: 24h count, related entities, cards, leads
  leads   [--state X] [--since X] — mirror of scripts/leads.py ls
  memo    [--week YYYY-WW]        — read strategy_memos
  funnel  [--kind K]              — funnel_edges browse
  state                           — KB scale snapshot (counts per table)

Output: tab-separated structured (TSV-friendly for grep). Use --json for
machine-readable. All timestamps display as ISO with +07:00 offset.

Per CLAUDE.md §6.4: timestamps ISO 8601 with +07:00.
Per CLAUDE.md §15: Tier 2/3 chiefs use this; Tier 1 mostly via Section Chief.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
MEMO_DIR = RUNTIME_DIR / "strategy_memos"


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_since(s: str | None) -> str | None:
    if not s:
        return None
    m = re.match(r"^(\d+)([hdwm])$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
            "w": timedelta(weeks=n),
            "m": timedelta(days=n * 30),
        }[unit]
        return (datetime.now(TZ) - delta).isoformat(timespec="seconds")
    return s


def _emit(rows: list[dict], use_json: bool, fields: list[str]) -> None:
    if use_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    if not rows:
        print("(no rows)")
        return
    print("\t".join(fields))
    for r in rows:
        cells = []
        for f in fields:
            v = r.get(f)
            if v is None:
                cells.append("")
            else:
                cells.append(str(v).replace("\t", " ").replace("\n", " ")[:200])
        print("\t".join(cells))
    print(f"\n  ({len(rows)} rows)")


# ---------------------------------------------------------------------------
# search — fuzzy text across cards + messages
# ---------------------------------------------------------------------------

def cmd_search(args) -> int:
    needle = args.text
    since = parse_since(args.since) if args.since else None
    conn = get_connection()
    try:
        results = []

        # Cards
        sql_cards = (
            "SELECT row_id, title, body_md, actionability_score, state, last_built_at, "
            "decision_tags, evidence_count "
            "FROM cards WHERE (title LIKE ? OR body_md LIKE ?) "
        )
        params: list = [f"%{needle}%", f"%{needle}%"]
        if since:
            sql_cards += "AND last_built_at >= ? "
            params.append(since)
        sql_cards += "ORDER BY actionability_score DESC NULLS LAST LIMIT ?"
        params.append(args.limit)
        for row in conn.execute(sql_cards, params).fetchall():
            d = dict(row)
            d["_source"] = "card"
            results.append(d)

        # Messages
        sql_msgs = (
            "SELECT row_id, ts, platform, persona, chat_username, sender_username, text "
            "FROM messages WHERE text LIKE ? "
        )
        mparams: list = [f"%{needle}%"]
        if args.platform:
            sql_msgs += "AND platform = ? "
            mparams.append(args.platform)
        if since:
            sql_msgs += "AND ts >= ? "
            mparams.append(since)
        sql_msgs += "ORDER BY ts DESC LIMIT ?"
        mparams.append(args.limit)
        for row in conn.execute(sql_msgs, mparams).fetchall():
            d = dict(row)
            d["_source"] = "message"
            results.append(d)

        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
            return 0
        cards = [r for r in results if r["_source"] == "card"]
        msgs = [r for r in results if r["_source"] == "message"]
        print(f"== cards ({len(cards)}) ==")
        for r in cards:
            print(f"  card #{r['row_id']:<6} act={r.get('actionability_score', '?')} "
                  f"state={r.get('state', '?')} {r.get('title', '')[:80]}")
        print()
        print(f"== messages ({len(msgs)}) ==")
        for r in msgs:
            print(f"  msg #{r['row_id']:<8} {r['ts'][:19]} [{r['platform']:<10}] "
                  f"@{r.get('chat_username') or '?'} {(r.get('text') or '')[:80]}")
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# cards — list active cards (filterable)
# ---------------------------------------------------------------------------

def cmd_cards(args) -> int:
    since = parse_since(args.since)
    conn = get_connection()
    try:
        sql = (
            "SELECT row_id, entity_row_id, card_kind, title, actionability_score, "
            "state, decision_tags, last_built_at, evidence_count "
            "FROM cards WHERE 1=1 "
        )
        params: list = []
        if args.state:
            sql += "AND state = ? "
            params.append(args.state)
        else:
            sql += "AND state = 'active' "
        if since:
            sql += "AND last_built_at >= ? "
            params.append(since)
        if args.tier:
            # tier filter via decision_tags or via entities join
            sql += ("AND row_id IN (SELECT c.row_id FROM cards c "
                    "LEFT JOIN entities e ON c.entity_row_id = e.row_id "
                    "WHERE e.tier = ?) ")
            params.append(args.tier)
        sql += "ORDER BY actionability_score DESC NULLS LAST, last_built_at DESC LIMIT ?"
        params.append(args.limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        _emit(rows, args.json,
              ["row_id", "card_kind", "title", "actionability_score",
               "state", "decision_tags", "last_built_at", "evidence_count"])
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# entity — 360-view (24h msg count, related entities, cards, leads)
# ---------------------------------------------------------------------------

def cmd_entity(args) -> int:
    name = args.name
    cutoff_24h = (datetime.now(TZ) - timedelta(hours=24)).isoformat(timespec="seconds")
    conn = get_connection()
    try:
        # Entity row
        ent = conn.execute(
            "SELECT * FROM entities WHERE name = ? OR aliases_json LIKE ? "
            "ORDER BY seen_count DESC LIMIT 1",
            (name, f"%{name}%"),
        ).fetchone()
        ent_dict = dict(ent) if ent else None

        # 24h message count by chat_username / sender_username / text mention
        msg_count_24h = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE ts >= ? "
            "AND (chat_username = ? OR sender_username = ? OR text LIKE ?)",
            (cutoff_24h, name, name, f"%{name}%"),
        ).fetchone()[0]

        # Cards mentioning entity (by entity_row_id or text)
        cards_sql = (
            "SELECT row_id, title, actionability_score, state, last_built_at "
            "FROM cards WHERE (title LIKE ? OR body_md LIKE ?) "
            "OR entity_row_id = ? ORDER BY last_built_at DESC LIMIT 10"
        )
        cards = [dict(r) for r in conn.execute(
            cards_sql,
            (f"%{name}%", f"%{name}%", ent_dict["row_id"] if ent_dict else -1)
        ).fetchall()]

        # Leads
        leads = [dict(r) for r in conn.execute(
            "SELECT lead_id, type, target, state, emitted_at FROM kb_leads "
            "WHERE target LIKE ? ORDER BY emitted_at DESC LIMIT 10",
            (f"%{name}%",),
        ).fetchall()]

        # Related entities — co-occurrence in messages_entities (best-effort)
        related = []
        if ent_dict:
            try:
                related = [dict(r) for r in conn.execute(
                    "SELECT e.name, e.kind, e.tier, COUNT(*) AS cooccur "
                    "FROM messages_entities me1 "
                    "JOIN messages_entities me2 ON me1.message_row_id = me2.message_row_id "
                    "JOIN entities e ON me2.entity_row_id = e.row_id "
                    "WHERE me1.entity_row_id = ? AND me2.entity_row_id != ? "
                    "GROUP BY me2.entity_row_id ORDER BY cooccur DESC LIMIT 10",
                    (ent_dict["row_id"], ent_dict["row_id"]),
                ).fetchall()]
            except Exception:
                related = []

        out = {
            "entity": ent_dict,
            "msg_count_24h": msg_count_24h,
            "cards": cards,
            "leads": leads,
            "related_entities": related,
        }
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
            return 0
        print(f"== entity: {name} ==")
        if ent_dict:
            print(f"  row_id={ent_dict['row_id']} kind={ent_dict['kind']} "
                  f"platform={ent_dict.get('platform','-')} tier={ent_dict.get('tier','-')} "
                  f"seen_count={ent_dict.get('seen_count',0)}")
            print(f"  state={ent_dict.get('state','-')} last_seen={ent_dict.get('last_seen_ts','-')}")
        else:
            print(f"  (no entity row matching {name!r})")
        print(f"\n24h messages mentioning: {msg_count_24h}")
        print(f"\n== related entities ({len(related)}) ==")
        for r in related:
            print(f"  {r['name']:<32} kind={r['kind']:<14} tier={r['tier'] or '-':<8} cooccur={r['cooccur']}")
        print(f"\n== cards ({len(cards)}) ==")
        for c in cards:
            print(f"  card #{c['row_id']:<6} act={c.get('actionability_score','?')} "
                  f"state={c['state']:<14} {c['title'][:80]}")
        print(f"\n== leads ({len(leads)}) ==")
        for l in leads:
            print(f"  {l['lead_id']:<22} {l['emitted_at'][:19]} [{l['type']:<22}] "
                  f"state={l['state']:<18} target={l.get('target','')[:40]}")
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# leads — mirror scripts/leads.py ls (subset)
# ---------------------------------------------------------------------------

def cmd_leads(args) -> int:
    since = parse_since(args.since) if args.since else None
    conn = get_connection()
    try:
        sql = "SELECT * FROM kb_leads WHERE 1=1 "
        params: list = []
        if args.state:
            sql += "AND state = ? "
            params.append(args.state)
        if since:
            sql += "AND emitted_at >= ? "
            params.append(since)
        sql += "ORDER BY emitted_at DESC LIMIT ?"
        params.append(args.limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        _emit(rows, args.json,
              ["lead_id", "emitted_at", "type", "state", "triage_lane",
               "target", "actionability", "auto_safe"])
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# memo — read strategy memos
# ---------------------------------------------------------------------------

def cmd_memo(args) -> int:
    if args.week:
        target = MEMO_DIR / f"{args.week}.md"
        if not target.exists():
            print(f"(memo not found: {target})")
            return 1
        print(target.read_text(encoding="utf-8"))
        return 0
    # Default: list available memos
    if not MEMO_DIR.exists():
        print("(no memos directory)")
        return 0
    memos = sorted(MEMO_DIR.glob("*.md"), reverse=True)[:args.limit]
    if not memos:
        print("(no memos)")
        return 0
    for m in memos:
        size = m.stat().st_size
        mtime = datetime.fromtimestamp(m.stat().st_mtime, TZ).isoformat(timespec="seconds")
        print(f"  {m.stem:<14} {size:>6}B  {mtime}")
    return 0


# ---------------------------------------------------------------------------
# funnel — funnel_edges browse
# ---------------------------------------------------------------------------

def cmd_funnel(args) -> int:
    conn = get_connection()
    try:
        sql = (
            "SELECT row_id, from_chat_username, from_platform, to_target_kind, "
            "to_target, edge_kind, push_count, distinct_senders, "
            "review_state, join_state, last_seen_ts "
            "FROM funnel_edges WHERE 1=1 "
        )
        params: list = []
        if args.kind:
            sql += "AND edge_kind = ? "
            params.append(args.kind)
        if args.review:
            sql += "AND review_state = ? "
            params.append(args.review)
        sql += "ORDER BY last_seen_ts DESC LIMIT ?"
        params.append(args.limit)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        _emit(rows, args.json,
              ["row_id", "from_chat_username", "from_platform", "edge_kind",
               "to_target_kind", "to_target", "push_count", "distinct_senders",
               "review_state", "join_state", "last_seen_ts"])
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# state — KB scale snapshot
# ---------------------------------------------------------------------------

def cmd_state(args) -> int:
    conn = get_connection()
    try:
        out = {"as_of": now_iso(), "tables": {}}
        cutoff_24h = (datetime.now(TZ) - timedelta(hours=24)).isoformat(timespec="seconds")
        for tbl, time_col in [
            ("messages", "ts"),
            ("entities", "last_seen_ts"),
            ("cards", "last_built_at"),
            ("kb_leads", "emitted_at"),
            ("funnel_edges", "last_seen_ts"),
            ("boss_opinions", "source_ts"),
            ("system_history", "ts"),
        ]:
            try:
                total = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                last_24h = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE {time_col} >= ?",
                    (cutoff_24h,),
                ).fetchone()[0]
                out["tables"][tbl] = {"total": total, "last_24h": last_24h}
            except Exception as e:
                out["tables"][tbl] = {"error": f"{type(e).__name__}: {e}"}

        # Card states
        try:
            states = conn.execute(
                "SELECT state, COUNT(*) AS n FROM cards GROUP BY state"
            ).fetchall()
            out["card_states"] = {r["state"]: r["n"] for r in states}
        except Exception:
            out["card_states"] = {}

        # Lead states
        try:
            states = conn.execute(
                "SELECT state, COUNT(*) AS n FROM kb_leads GROUP BY state"
            ).fetchall()
            out["lead_states"] = {r["state"]: r["n"] for r in states}
        except Exception:
            out["lead_states"] = {}

        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
            return 0
        print(f"KB state @ {out['as_of']}")
        print()
        for tbl, stat in out["tables"].items():
            if "error" in stat:
                print(f"  {tbl:<18} ERR {stat['error']}")
            else:
                print(f"  {tbl:<18} total={stat['total']:>8}  last_24h={stat['last_24h']:>6}")
        print()
        print(f"card states: {out['card_states']}")
        print(f"lead states: {out['lead_states']}")
        return 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Unified read-only KB query helper (boss 5/3 §15)")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd")

    p_s = sub.add_parser("search", help="fuzzy text search across cards + messages")
    p_s.add_argument("text")
    p_s.add_argument("--platform", default=None)
    p_s.add_argument("--since", default=None, help="duration like 24h / 7d / ISO")
    p_s.add_argument("--limit", type=int, default=20)
    p_s.set_defaults(func=cmd_search)

    p_c = sub.add_parser("cards", help="list cards (default state=active)")
    p_c.add_argument("--tier", default=None, help="filter by entity.tier (yolk/white/shell)")
    p_c.add_argument("--state", default=None)
    p_c.add_argument("--since", default=None)
    p_c.add_argument("--limit", type=int, default=30)
    p_c.set_defaults(func=cmd_cards)

    p_e = sub.add_parser("entity", help="360-view of one entity")
    p_e.add_argument("name")
    p_e.set_defaults(func=cmd_entity)

    p_l = sub.add_parser("leads", help="list kb_leads")
    p_l.add_argument("--state", default=None)
    p_l.add_argument("--since", default=None)
    p_l.add_argument("--limit", type=int, default=30)
    p_l.set_defaults(func=cmd_leads)

    p_m = sub.add_parser("memo", help="read strategy memos")
    p_m.add_argument("--week", default=None, help="ISO week e.g. 2026-W18")
    p_m.add_argument("--limit", type=int, default=8)
    p_m.set_defaults(func=cmd_memo)

    p_f = sub.add_parser("funnel", help="funnel_edges browse")
    p_f.add_argument("--kind", default=None)
    p_f.add_argument("--review", default=None)
    p_f.add_argument("--limit", type=int, default=30)
    p_f.set_defaults(func=cmd_funnel)

    p_st = sub.add_parser("state", help="KB scale snapshot")
    p_st.set_defaults(func=cmd_state)

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return 0
    # Inject default values args might not have
    for k in ("limit", "since"):
        if not hasattr(args, k):
            setattr(args, k, None)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
