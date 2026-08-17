"""
Blacksite — JSONL → SQLite indexer (incremental).

Per platform, walks every raw JSONL file under runtime/raw/, resumes from
ingestion_runs.last_offset, projects each row into messages + entities + media
tables. Idempotent (UNIQUE constraints catch dupes).

Run by daemon every 15 minutes; also runnable manually:
  py scripts/index_jsonl.py
  py scripts/index_jsonl.py --platform telegram
  py scripts/index_jsonl.py --rebuild   # truncate offsets, re-index everything
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

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
RAW_ROOT = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "raw"
LOG_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

# Mention extraction
URL_RE = re.compile(r"https?://[^\s\)]+", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{4,32})")
HASHTAG_RE = re.compile(r"#([\w฀-๿]{2,64})")


def now_bkk() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"index_jsonl_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


# ------------------------------------------------------------------
# Per-platform normalizers: raw JSONL row → messages-row dict
# ------------------------------------------------------------------

def norm_telegram(row: dict) -> dict:
    """tg_listen JSONL → messages row."""
    return {
        "platform": "telegram",
        "external_id": str(row.get("msg_id")),
        "persona": row.get("persona"),
        "ts": row.get("ts"),
        "chat_external_id": _str_or_none(row.get("chat_id")),
        "chat_username": row.get("chat_username"),
        "chat_title": row.get("chat_title"),
        "sender_external_id": _str_or_none(row.get("sender_id")),
        "sender_username": row.get("sender_username"),
        "sender_name": row.get("sender_name"),
        "text": row.get("text") or "",
        "url": _tg_url(row),
        "views": row.get("views"),
        "reactions_total": row.get("reactions_total"),
        "forwards": row.get("forwards"),
        "replies": row.get("replies"),
        "score": None,
        "fwd_from_chat_id": _str_or_none(row.get("fwd_from_chat_id")),
        "fwd_from_user_id": _str_or_none(row.get("fwd_from_user_id")),
        "reply_to_external": _str_or_none(row.get("reply_to_msg_id")),
        "edit_ts": row.get("edit_ts"),
    }


def _tg_url(row: dict) -> str | None:
    chat = row.get("chat_username")
    msg_id = row.get("msg_id")
    if chat and msg_id:
        return f"https://t.me/{chat}/{msg_id}"
    return None


def norm_pantip(row: dict) -> dict | None:
    if "topic_id" not in row:
        return None
    return {
        "platform": "pantip",
        "external_id": str(row["topic_id"]),
        "persona": None,
        "ts": row.get("ts"),
        "chat_external_id": row.get("tag") or row.get("room"),
        "chat_username": row.get("tag") or row.get("room"),
        "chat_title": None,
        "sender_external_id": None,
        "sender_username": None,
        "sender_name": None,
        "text": (row.get("title") or "") + ("\n\n" + (row.get("body") or "") if row.get("body") else ""),
        "url": row.get("url"),
        "views": row.get("views"),
        "reactions_total": None,
        "forwards": None,
        "replies": row.get("comments_count"),
        "score": row.get("vote_score"),
        "fwd_from_chat_id": None,
        "fwd_from_user_id": None,
        "reply_to_external": None,
        "edit_ts": None,
    }


def norm_x(row: dict) -> dict | None:
    if row.get("kind") != "profile_metadata" or not row.get("handle"):
        return None
    return {
        "platform": "x",
        "external_id": row["handle"],
        "persona": None,
        "ts": row.get("ts"),
        "chat_external_id": row["handle"],
        "chat_username": row["handle"],
        "chat_title": row.get("og_title"),
        "sender_external_id": row["handle"],
        "sender_username": row["handle"],
        "sender_name": row.get("og_title"),
        "text": (row.get("bio") or "") + ((" | " + row["website"]) if row.get("website") else ""),
        "url": row.get("url"),
        "views": None,
        "reactions_total": None,
        "forwards": None,
        "replies": None,
        "score": row.get("followers_count"),
        "fwd_from_chat_id": None,
        "fwd_from_user_id": None,
        "reply_to_external": None,
        "edit_ts": None,
    }


def norm_tiktok(row: dict) -> dict | None:
    if not row.get("video_id"):
        return None
    stats = row.get("stats") or {}
    return {
        "platform": "tiktok",
        "external_id": str(row["video_id"]),
        "persona": None,
        "ts": row.get("ts"),
        "chat_external_id": row.get("target"),
        "chat_username": row.get("target"),
        "chat_title": None,
        "sender_external_id": _str_or_none(row.get("author_id")),
        "sender_username": row.get("author"),
        "sender_name": row.get("author"),
        "text": row.get("desc") or "",
        "url": f"https://www.tiktok.com/@{row.get('author','')}/video/{row['video_id']}" if row.get("author") else None,
        "views": stats.get("playCount") or stats.get("play_count"),
        "reactions_total": stats.get("diggCount") or stats.get("digg_count"),
        "forwards": stats.get("shareCount") or stats.get("share_count"),
        "replies": stats.get("commentCount") or stats.get("comment_count"),
        "score": None,
        "fwd_from_chat_id": None,
        "fwd_from_user_id": None,
        "reply_to_external": None,
        "edit_ts": None,
    }


def norm_reddit(row: dict) -> dict | None:
    if not row.get("post_id"):
        return None
    return {
        "platform": "reddit",
        "external_id": row["post_id"],
        "persona": None,
        "ts": row.get("ts"),
        "chat_external_id": row.get("sub"),
        "chat_username": row.get("sub"),
        "chat_title": None,
        "sender_external_id": None,
        "sender_username": row.get("author"),
        "sender_name": row.get("author"),
        "text": ((row.get("title") or "") + "\n\n" + (row.get("selftext") or "")).strip(),
        "url": row.get("permalink") or row.get("url"),
        "views": None,
        "reactions_total": None,
        "forwards": None,
        "replies": row.get("num_comments"),
        "score": row.get("score"),
        "fwd_from_chat_id": None,
        "fwd_from_user_id": None,
        "reply_to_external": None,
        "edit_ts": None,
    }


def norm_youtube(row: dict) -> dict | None:
    if not row.get("id"):
        return None
    return {
        "platform": "youtube",
        "external_id": row["id"],
        "persona": None,
        "ts": row.get("ts"),
        "chat_external_id": row.get("channel_id"),
        "chat_username": row.get("uploader") or row.get("channel"),
        "chat_title": row.get("channel"),
        "sender_external_id": row.get("channel_id"),
        "sender_username": row.get("uploader") or row.get("channel"),
        "sender_name": row.get("channel") or row.get("uploader"),
        "text": row.get("title") or "",
        "url": row.get("url"),
        "views": row.get("view_count"),
        "reactions_total": None,
        "forwards": None,
        "replies": None,
        "score": None,
        "fwd_from_chat_id": None,
        "fwd_from_user_id": None,
        "reply_to_external": None,
        "edit_ts": None,
    }


def _str_or_none(v) -> str | None:
    return str(v) if v is not None else None


# ------------------------------------------------------------------
# M9 v1.0 normalizers — Facebook (mbasic) / Bigo / TrueID + 4 OTT broadcasters
# ------------------------------------------------------------------

def norm_facebook(row: dict) -> dict | None:
    if not row.get("post_id"):
        return None
    # page_slug (og/page agents) or fallback chain (feed_harvest may omit all)
    slug = (row.get("page_slug") or row.get("page_name") or
            row.get("page_id") or row.get("persona_id") or "feed")
    slug = str(slug)
    persona = row.get("persona_id") or row.get("persona") or None
    return {
        "platform": "facebook",
        "external_id": f"{slug}:{row['post_id']}",
        "persona": persona,
        "ts": row.get("ts"),
        "chat_external_id": slug,
        "chat_username": slug,
        "chat_title": slug,
        "sender_external_id": slug,
        "sender_username": slug,
        "sender_name": slug,
        "text": row.get("text") or "",
        "url": row.get("permalink") or row.get("page_url"),
        "views": None, "reactions_total": None, "forwards": None,
        "replies": None, "score": None,
        "fwd_from_chat_id": None, "fwd_from_user_id": None,
        "reply_to_external": None, "edit_ts": None,
    }


def norm_nimo(row: dict) -> dict | None:
    """Same lobby-snapshot pattern as Bigo. Each (room_id, scan_tick) is a
    distinct row — over time builds a viewer-count time series at L4."""
    if not row.get("room_id") or not row.get("ts"):
        return None
    snap_key = f"{row['room_id']}@{row['ts'][:16]}"
    return {
        "platform": "nimo",
        "external_id": snap_key,
        "persona": None,
        "ts": row.get("ts"),
        "chat_external_id": row.get("room_id"),
        "chat_username": row.get("room_id"),
        "chat_title": row.get("target"),
        "sender_external_id": row.get("room_id"),
        "sender_username": None,
        "sender_name": None,
        "text": row.get("card_text") or "",
        "url": row.get("url"),
        "views": None, "reactions_total": None, "forwards": None,
        "replies": None, "score": None,
        "fwd_from_chat_id": None, "fwd_from_user_id": None,
        "reply_to_external": None, "edit_ts": None,
    }


def norm_bigo(row: dict) -> dict | None:
    """Lobby snapshot: one row per (room_id, scan_tick). external_id encodes
    both so multiple snapshots over time produce distinct rows (the messages
    UNIQUE constraint is platform+external_id+persona)."""
    if not row.get("room_id") or not row.get("ts"):
        return None
    # Snapshot key = room_id:ts (minute-precision — sufficient for time-series)
    snap_key = f"{row['room_id']}@{row['ts'][:16]}"
    return {
        "platform": "bigo",
        "external_id": snap_key,
        "persona": None,
        "ts": row.get("ts"),
        "chat_external_id": row.get("room_id"),
        "chat_username": row.get("room_id"),
        "chat_title": row.get("target"),
        "sender_external_id": row.get("room_id"),
        "sender_username": None,
        "sender_name": None,
        "text": row.get("card_text") or "",
        "url": row.get("url"),
        "views": None, "reactions_total": None, "forwards": None,
        "replies": None,
        "score": None,  # viewer_str is text — could be parsed to int in v1.5
        "fwd_from_chat_id": None, "fwd_from_user_id": None,
        "reply_to_external": None, "edit_ts": None,
    }


def norm_trueid(row: dict) -> dict | None:
    if not row.get("article_id"):
        return None
    return {
        "platform": "trueid",
        "external_id": f"{row.get('feed','')}:{row['article_id']}",
        "persona": None,
        "ts": row.get("ts"),
        "chat_external_id": row.get("feed"),
        "chat_username": row.get("feed"),
        "chat_title": row.get("feed"),
        "sender_external_id": None, "sender_username": None, "sender_name": None,
        "text": row.get("title") or "",
        "url": row.get("url"),
        "views": None, "reactions_total": None, "forwards": None,
        "replies": None, "score": None,
        "fwd_from_chat_id": None, "fwd_from_user_id": None,
        "reply_to_external": None, "edit_ts": None,
    }


def _norm_ott_feed(platform: str, row: dict) -> dict | None:
    """Shared normalizer for the 4 OTT-broadcaster cohort (oneD / CH3 Plus /
    AIS Play / NOICE) that all use _common/web_feed_scanner.py output shape."""
    if not row.get("item_id"):
        return None
    return {
        "platform": platform,
        "external_id": f"{row.get('feed','')}:{row['item_id']}",
        "persona": None,
        "ts": row.get("ts"),
        "chat_external_id": row.get("feed"),
        "chat_username": row.get("feed"),
        "chat_title": row.get("feed"),
        "sender_external_id": None, "sender_username": None, "sender_name": None,
        "text": row.get("title") or "",
        "url": row.get("url"),
        "views": None, "reactions_total": None, "forwards": None,
        "replies": None, "score": None,
        "fwd_from_chat_id": None, "fwd_from_user_id": None,
        "reply_to_external": None, "edit_ts": None,
    }


def norm_oned(row: dict) -> dict | None:
    return _norm_ott_feed("oned", row)


def norm_ch3plus(row: dict) -> dict | None:
    return _norm_ott_feed("ch3plus", row)


def norm_aisplay(row: dict) -> dict | None:
    return _norm_ott_feed("aisplay", row)


def norm_noice(row: dict) -> dict | None:
    return _norm_ott_feed("noice", row)


# ------------------------------------------------------------------
# Indexer
# ------------------------------------------------------------------

PLATFORM_DIRS = {
    "telegram": [RAW_ROOT / "P01", RAW_ROOT / "P02"],
    "pantip":   [RAW_ROOT / "pantip"],
    "x":        [RAW_ROOT / "x"],
    "tiktok":   [RAW_ROOT / "tiktok"],
    "reddit":   [RAW_ROOT / "reddit"],
    "youtube":  [RAW_ROOT / "youtube" / "searches", RAW_ROOT / "youtube" / "channels"],
    "facebook": [RAW_ROOT / "facebook"],
    "bigo":     [RAW_ROOT / "bigo"],
    "nimo":     [RAW_ROOT / "nimo"],
    "trueid":   [RAW_ROOT / "trueid"],
    "oned":     [RAW_ROOT / "oned"],
    "ch3plus":  [RAW_ROOT / "ch3plus"],
    "aisplay":  [RAW_ROOT / "aisplay"],
    "noice":    [RAW_ROOT / "noice"],
}

NORMALIZERS = {
    "telegram": norm_telegram,
    "pantip":   norm_pantip,
    "x":        norm_x,
    "tiktok":   norm_tiktok,
    "reddit":   norm_reddit,
    "youtube":  norm_youtube,
    "facebook": norm_facebook,
    "bigo":     norm_bigo,
    "nimo":     norm_nimo,
    "trueid":   norm_trueid,
    "oned":     norm_oned,
    "ch3plus":  norm_ch3plus,
    "aisplay":  norm_aisplay,
    "noice":    norm_noice,
}


def upsert_entity(conn, kind: str, platform: str | None, name: str, ts: str) -> int:
    """Upsert an entity, return its row_id."""
    cur = conn.execute(
        "SELECT row_id, seen_count FROM entities WHERE kind=? AND platform IS ? AND name=?",
        (kind, platform, name),
    )
    r = cur.fetchone()
    if r:
        conn.execute(
            "UPDATE entities SET last_seen_ts=?, seen_count=? WHERE row_id=?",
            (ts, r["seen_count"] + 1, r["row_id"]),
        )
        return r["row_id"]
    cur = conn.execute(
        "INSERT INTO entities (kind, platform, name, first_seen_ts, last_seen_ts, seen_count) "
        "VALUES (?,?,?,?,?,1)",
        (kind, platform, name, ts, ts),
    )
    return cur.lastrowid


def index_row(conn, platform: str, raw_path: str, offset: int, raw: dict, msg: dict) -> int:
    """Insert one message row + its entity links + any media listed in raw."""
    if not msg.get("external_id") or not msg.get("ts"):
        return 0
    cur = conn.execute(
        """INSERT OR IGNORE INTO messages (
            platform, external_id, persona, ts,
            chat_external_id, chat_username, chat_title,
            sender_external_id, sender_username, sender_name,
            text, url,
            views, reactions_total, forwards, replies, score,
            fwd_from_chat_id, fwd_from_user_id, reply_to_external,
            edit_ts, raw_json, raw_path, raw_offset
        ) VALUES (?,?,?,?, ?,?,?, ?,?,?, ?,?, ?,?,?,?,?, ?,?,?, ?,?,?,?)""",
        (
            msg["platform"], msg["external_id"], msg.get("persona"), msg["ts"],
            msg.get("chat_external_id"), msg.get("chat_username"), msg.get("chat_title"),
            msg.get("sender_external_id"), msg.get("sender_username"), msg.get("sender_name"),
            msg.get("text"), msg.get("url"),
            msg.get("views"), msg.get("reactions_total"), msg.get("forwards"),
            msg.get("replies"), msg.get("score"),
            msg.get("fwd_from_chat_id"), msg.get("fwd_from_user_id"), msg.get("reply_to_external"),
            msg.get("edit_ts"),
            json.dumps(raw, ensure_ascii=False), raw_path, offset,
        ),
    )
    if cur.rowcount == 0:
        return 0
    msg_row_id = cur.lastrowid
    ts = msg["ts"]

    # Author entity
    if msg.get("sender_username") or msg.get("sender_external_id"):
        ename = msg.get("sender_username") or msg.get("sender_external_id")
        eid = upsert_entity(conn, "user", platform, ename, ts)
        conn.execute(
            "INSERT OR IGNORE INTO messages_entities VALUES (?,?,?)",
            (msg_row_id, eid, "author"),
        )
    # Channel entity
    if msg.get("chat_username") or msg.get("chat_external_id"):
        ename = msg.get("chat_username") or msg.get("chat_external_id")
        eid = upsert_entity(conn, "channel", platform, ename, ts)
        conn.execute(
            "INSERT OR IGNORE INTO messages_entities VALUES (?,?,?)",
            (msg_row_id, eid, "author"),
        )
    # Forward origin entity (TG)
    if msg.get("fwd_from_chat_id"):
        eid = upsert_entity(conn, "channel", platform, msg["fwd_from_chat_id"], ts)
        conn.execute(
            "INSERT OR IGNORE INTO messages_entities VALUES (?,?,?)",
            (msg_row_id, eid, "forward_origin"),
        )

    # Text-mined mentions / hashtags / urls
    text = msg.get("text") or ""
    for m in MENTION_RE.findall(text):
        eid = upsert_entity(conn, "user", platform, m, ts)
        conn.execute(
            "INSERT OR IGNORE INTO messages_entities VALUES (?,?,?)",
            (msg_row_id, eid, "text_mention"),
        )
    for h in HASHTAG_RE.findall(text):
        eid = upsert_entity(conn, "hashtag", None, h, ts)
        conn.execute(
            "INSERT OR IGNORE INTO messages_entities VALUES (?,?,?)",
            (msg_row_id, eid, "text_mention"),
        )
    for u in URL_RE.findall(text):
        # Strip path/query, keep host
        host = re.sub(r"^https?://", "", u).split("/")[0].split("?")[0]
        if host:
            eid = upsert_entity(conn, "domain", None, host.lower(), ts)
            conn.execute(
                "INSERT OR IGNORE INTO messages_entities VALUES (?,?,?)",
                (msg_row_id, eid, "url"),
            )

    # Media: TG listener writes media_files list on upgrade; older rows with
    # only media_kind get a stub row so we can later backfill once downloader
    # is wired.
    media_files = raw.get("media_files") or []
    for mf in media_files:
        if not mf.get("file_path"):
            continue
        conn.execute(
            """INSERT OR IGNORE INTO media (
                message_row_id, platform, media_kind, file_path,
                file_size, mime_type, duration_s, width, height, sha256,
                captured_at, raw_json
            ) VALUES (?,?,?,?, ?,?,?,?,?,?, ?,?)""",
            (
                msg_row_id, platform, mf.get("media_kind", "unknown"), mf["file_path"],
                mf.get("file_size"), mf.get("mime_type"), mf.get("duration_s"),
                mf.get("width"), mf.get("height"), mf.get("sha256"),
                ts, json.dumps(mf, ensure_ascii=False),
            ),
        )
    return 1


def index_file(conn, platform: str, path: Path) -> tuple[int, int]:
    """Returns (rows_added, new_offset)."""
    rel = str(path.relative_to(ROOT)).replace("\\", "/")
    cur = conn.execute(
        "SELECT last_offset FROM ingestion_runs WHERE platform=? AND raw_path=?",
        (platform, rel),
    )
    r = cur.fetchone()
    last_offset = r["last_offset"] if r else 0

    if not path.exists():
        return 0, last_offset

    file_size = path.stat().st_size
    if last_offset >= file_size:
        return 0, last_offset

    norm = NORMALIZERS[platform]
    rows_added = 0
    new_offset = last_offset

    with path.open("rb") as f:
        f.seek(last_offset)
        while True:
            line_start = f.tell()
            line = f.readline()
            if not line:
                break
            new_offset = f.tell()
            try:
                raw_text = line.decode("utf-8")
                raw = json.loads(raw_text)
            except Exception:
                continue
            try:
                msg = norm(raw)
            except Exception as e:
                log_line(f"[norm-err] {platform} {rel}@{line_start}: {type(e).__name__}: {e}")
                continue
            if msg is None:
                continue
            try:
                rows_added += index_row(conn, platform, rel, line_start, raw, msg)
            except Exception as e:
                log_line(f"[idx-err] {platform} {rel}@{line_start}: {type(e).__name__}: {e}")

    conn.execute(
        """INSERT INTO ingestion_runs (platform, raw_path, last_offset, last_indexed_at, rows_added)
           VALUES (?,?,?,?,?)
           ON CONFLICT(platform, raw_path) DO UPDATE SET
             last_offset=excluded.last_offset,
             last_indexed_at=excluded.last_indexed_at,
             rows_added=ingestion_runs.rows_added + excluded.rows_added""",
        (platform, rel, new_offset, now_bkk(), rows_added),
    )
    return rows_added, new_offset


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    init_db()
    conn = get_connection()

    if args.rebuild:
        log_line("[rebuild] truncating ingestion_runs + clearing tables")
        conn.execute("DELETE FROM messages_entities")
        conn.execute("DELETE FROM media")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM entities")
        conn.execute("DELETE FROM ingestion_runs")

    platforms = [args.platform] if args.platform else list(PLATFORM_DIRS)
    log_line(f"[{now_bkk()}] indexer start platforms={platforms}")

    totals = {"rows": 0, "files": 0}
    for p in platforms:
        for d in PLATFORM_DIRS[p]:
            if not d.exists():
                continue
            for f in sorted(d.rglob("*.jsonl")):
                rows, _ = index_file(conn, p, f)
                if rows:
                    log_line(f"[idx] {p:<10} {f.name:<25} +{rows}")
                totals["rows"] += rows
                totals["files"] += 1

    conn.close()
    log_line(f"[{now_bkk()}] indexer done files={totals['files']} new_rows={totals['rows']}")


if __name__ == "__main__":
    main()
