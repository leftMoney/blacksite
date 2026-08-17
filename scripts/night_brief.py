"""
Blacksite — Night brief (boss-facing summary of last N hours).

Reads SQLite index. Default window: 8 hours. Outputs Traditional Chinese
boss brief with platform breakdown, top engagement, new entities, media
inventory.

Usage:
  py scripts/night_brief.py                  # last 8h
  py scripts/night_brief.py --hours 12
  py scripts/night_brief.py --hours 24 --platform telegram
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

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from db.connection import get_connection  # noqa: E402

TZ = timezone(timedelta(hours=7))


def fmt_int(n) -> str:
    if n is None:
        return "-"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n/1000:.1f}K"
    return f"{n/1_000_000:.1f}M"


def fmt_size(n) -> str:
    if not n:
        return "0B"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f}KB"
    if n < 1024 * 1024 * 1024:
        return f"{n/1024/1024:.1f}MB"
    return f"{n/1024/1024/1024:.2f}GB"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=8)
    parser.add_argument("--platform", default=None)
    args = parser.parse_args()

    conn = get_connection()
    cutoff = (datetime.now(TZ) - timedelta(hours=args.hours)).isoformat(timespec="seconds")
    now_str = datetime.now(TZ).isoformat(timespec="seconds")

    print(f"\n=== Blacksite 夜班簡報 ===")
    print(f"窗口：過去 {args.hours} 小時（{cutoff} ~ {now_str}）")
    print(f"資料庫：{conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]} 訊息累計、"
          f"{conn.execute('SELECT COUNT(*) FROM entities').fetchone()[0]} 實體、"
          f"{conn.execute('SELECT COUNT(*) FROM media').fetchone()[0]} 媒體檔\n")

    # Per-platform message count + engagement
    print("【平台訊息量（窗口內新增）】")
    rows = conn.execute(
        """SELECT platform,
                  COUNT(*) AS n,
                  AVG(views) AS avg_views,
                  MAX(views) AS max_views,
                  SUM(COALESCE(forwards, 0)) AS total_fwd
             FROM messages
            WHERE ts >= ?
            GROUP BY platform
            ORDER BY n DESC""",
        (cutoff,),
    ).fetchall()
    print(f"  {'平台':<10} {'訊息':<8} {'平均views':<10} {'最高views':<10} {'fwd 總和':<10}")
    for r in rows:
        avg = f"{r['avg_views']:.0f}" if r["avg_views"] is not None else "-"
        mx = fmt_int(r["max_views"])
        fwd = fmt_int(r["total_fwd"])
        print(f"  {r['platform']:<10} {r['n']:<8} {avg:<10} {mx:<10} {fwd:<10}")
    if not rows:
        print("  (窗口內無新訊息)")

    # Top TG channels by message volume
    print("\n【TG 高活躍頻道（窗口內訊息數）】")
    rows = conn.execute(
        """SELECT chat_username, chat_title, COUNT(*) AS n
             FROM messages
            WHERE platform='telegram' AND ts >= ?
              AND chat_username IS NOT NULL
            GROUP BY chat_username
            ORDER BY n DESC
            LIMIT 10""",
        (cutoff,),
    ).fetchall()
    for r in rows:
        title = (r["chat_title"] or "")[:40]
        print(f"  {r['n']:>4}  @{r['chat_username']:<22} {title}")

    # Top engagement TG messages
    print("\n【TG 最高互動訊息（views + reactions）】")
    rows = conn.execute(
        """SELECT chat_username, sender_name, ts, views, reactions_total,
                  substr(text, 1, 80) AS preview, url
             FROM messages
            WHERE platform='telegram' AND ts >= ?
              AND (views IS NOT NULL OR reactions_total > 0)
            ORDER BY (COALESCE(views,0) + COALESCE(reactions_total,0)*10) DESC
            LIMIT 5""",
        (cutoff,),
    ).fetchall()
    for r in rows:
        v = fmt_int(r["views"])
        rx = fmt_int(r["reactions_total"])
        prev = (r["preview"] or "").replace("\n", " ")
        print(f"  👁{v:>6} ❤{rx:>4}  @{r['chat_username']}  <{r['sender_name'] or '?'}>")
        print(f"           {prev}")

    # New entities (first_seen in window)
    print("\n【窗口內新發現實體】")
    rows = conn.execute(
        """SELECT kind, platform, name, seen_count
             FROM entities
            WHERE first_seen_ts >= ?
            ORDER BY seen_count DESC
            LIMIT 15""",
        (cutoff,),
    ).fetchall()
    for r in rows:
        plat = r["platform"] or "*"
        print(f"  {r['kind']:<10} ({plat:<8}) {r['name']:<40} ×{r['seen_count']}")
    if not rows:
        print("  (無)")

    # Media inventory
    print("\n【媒體下載（窗口內）】")
    rows = conn.execute(
        """SELECT platform, media_kind, COUNT(*) AS n, SUM(file_size) AS sz
             FROM media
            WHERE captured_at >= ?
            GROUP BY platform, media_kind
            ORDER BY platform, sz DESC""",
        (cutoff,),
    ).fetchall()
    for r in rows:
        print(f"  {r['platform']:<10} {r['media_kind']:<10} {r['n']:>4} 檔  共 {fmt_size(r['sz'])}")
    if not rows:
        print("  (無)")

    # Forward chain origins (TG funnel signal)
    print("\n【TG forward 鏈來源 top 10（窗口內）】")
    rows = conn.execute(
        """SELECT fwd_from_chat_id, COUNT(*) AS n
             FROM messages
            WHERE platform='telegram' AND ts >= ?
              AND fwd_from_chat_id IS NOT NULL
            GROUP BY fwd_from_chat_id
            ORDER BY n DESC
            LIMIT 10""",
        (cutoff,),
    ).fetchall()
    for r in rows:
        print(f"  ×{r['n']:<5} chat_id={r['fwd_from_chat_id']}")

    # YouTube hits in window
    print("\n【YouTube 命中影片（窗口內，依 view_count 排序 top 5）】")
    rows = conn.execute(
        """SELECT chat_title, text, views, url
             FROM messages
            WHERE platform='youtube' AND ts >= ?
            ORDER BY views DESC
            LIMIT 5""",
        (cutoff,),
    ).fetchall()
    for r in rows:
        v = fmt_int(r["views"])
        title = (r["text"] or "")[:60]
        ch = (r["chat_title"] or "")[:25]
        print(f"  👁{v:>6}  [{ch}] {title}")
    if not rows:
        print("  (無)")

    # Brand mentions (count per brand entity in window)
    print("\n【brand seed 提及次數（窗口內）】")
    rows = conn.execute(
        """SELECT e.name, e.kind, COUNT(*) AS n
             FROM entities e
             JOIN messages_entities me ON me.entity_row_id = e.row_id
             JOIN messages m ON m.row_id = me.message_row_id
            WHERE m.ts >= ?
              AND e.name IN ('examplefunnel','examplebet','betbrand-b','slotbrand-a',
                             'examplebrand','betbrand-c','examplebrand-2',
                             'examplefunnel.com','examplebet.com','examplebrand.com')
            GROUP BY e.name
            ORDER BY n DESC""",
        (cutoff,),
    ).fetchall()
    for r in rows:
        print(f"  ×{r['n']:<5} {r['kind']:<10} {r['name']}")
    if not rows:
        print("  (無 brand 提及)")

    print("\n=== 結束 ===\n")
    conn.close()


if __name__ == "__main__":
    main()
