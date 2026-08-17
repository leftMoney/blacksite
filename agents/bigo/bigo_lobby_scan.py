"""
Blacksite — Bigo Live anonymous lobby scanner.

bigo.tv exposes language-tagged + category lobby feeds without login. Each
visible "room card" surfaces:
  - room_id (in /room/<id> link)
  - streamer display name + uid
  - viewer count (live)
  - room title (the streamer's stream-title at the moment)
  - thumbnail
  - language tag

This v1 captures lobby snapshots for KOL discovery + viewer-count
amplification signal. v1.5 (post P03 register) will add per-room
comment-stream capture (separate agent).

Output:
  instances/_TEMPLATE/runtime/raw/bigo/<YYYY-MM-DD>.jsonl
  one record per room snapshot per scan tick (multiple snapshots over time
  build a viewer-count-over-time curve at the rules-layer).

Usage:
  py agents/bigo/bigo_lobby_scan.py
  py agents/bigo/bigo_lobby_scan.py --target lang:th
  py agents/bigo/bigo_lobby_scan.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
POLICY_PATH = INSTANCE_DIR / "policy" / "bigo_lobby.yaml"
RAW_DIR = INSTANCE_DIR / "runtime" / "raw" / "bigo"
LOG_DIR = INSTANCE_DIR / "runtime" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

from agents._common.browser_viewport import mobile_viewport  # noqa: E402


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"bigo_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_policy() -> dict[str, Any]:
    with POLICY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def write_jsonl(record: dict) -> None:
    today = now_bkk().strftime("%Y-%m-%d")
    out_path = RAW_DIR / f"{today}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def target_url(target: dict) -> str:
    """v1.0 supported `kind=lang_feed/category` with a code/name; v1.0.1
    (Phase C 2026-04-30) simplified to direct `url:` field. Both shapes
    supported for backward compat."""
    if "url" in target:
        return target["url"]
    kind = target.get("kind")
    if kind == "lang_feed":
        return f"https://www.bigo.tv/show/{target['code']}"
    if kind == "category":
        return f"https://www.bigo.tv/category/{target['name']}"
    raise ValueError(f"unknown target shape {target}")


def target_label(target: dict) -> str:
    if "label" in target:
        return target["label"]
    kind = target.get("kind")
    if kind == "lang_feed":
        return f"lang:{target['code']}"
    if kind == "category":
        return f"cat:{target['name']}"
    return str(target)


async def scan_target(
    browser, target: dict, policy: dict, dry_run: bool
) -> dict:
    sleep_rng = policy["scan"]["inter_request_sleep_s"]
    ua = random.choice(policy["scan"]["user_agent_pool"])
    url = target_url(target)
    label = target_label(target)
    max_rooms = int(policy.get("per_target_max_rooms", 60))

    stats = {"target": label, "tier": target.get("tier"), "status": "init",
             "rooms_seen": 0, "rooms_new": 0}
    context = await browser.new_context(
        user_agent=ua,
        locale="th-TH",
        viewport=mobile_viewport(),
        is_mobile=True,
        has_touch=True,
    )
    # Mild headless-detection mitigation: hide navigator.webdriver before any
    # page script runs. Bigo's lobby JS gates room-card render on
    # bot-heuristics; Phase C 2026-04-30 confirmed real Chrome shows 65
    # rooms while headless Playwright shows 0 with same wait time.
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    page = await context.new_page()

    try:
        resp = await page.goto(url, wait_until="networkidle", timeout=25000)
        status = resp.status if resp else 0
        stats["status"] = f"http_{status}"
        if status not in (200, 302):
            log_line(f"[scan] {label:<14} HTTP {status} — skip")
            return stats

        # Wait for room cards. Bigo lobby renders via JS; per Phase C
        # 2026-04-30 inspection: 5s hydration → 65 room anchors visible.
        # 2s was too short.
        try:
            await page.wait_for_selector("a[href*='/'], img", timeout=8000)
            await page.wait_for_timeout(7000)  # extended hydration window
        except Exception:
            log_line(f"[scan] {label:<14} no room links rendered")
            return stats

        # Extract room cards. Bigo's exact DOM changes; we look for any anchor
        # whose href contains a numeric segment that looks like a room/uid path.
        rooms = await page.evaluate(
            """(maxRooms) => {
                const out = [];
                const seen = new Set();
                // Any anchor whose href looks like /<digits>/ or /<digits>?...
                const anchors = document.querySelectorAll("a[href]");
                for (const a of anchors) {
                    if (out.length >= maxRooms) break;
                    const href = a.getAttribute('href') || '';
                    // Bigo geo-prefixes paths with /th/, /id/, /vi/ etc. for
                    // non-TW visitors. Match optional 2-4 letter locale prefix.
                    const m = href.match(/^\\/(?:[a-z]{2,4}\\/)?(\\d{4,})(?:\\/|$|\\?)/);
                    if (!m) continue;
                    const roomId = m[1];
                    if (seen.has(roomId)) continue;
                    seen.add(roomId);
                    // Card content: title + streamer name + viewer count are usually
                    // siblings or children; capture the entire card text as a haystack.
                    let card = a;
                    for (let i = 0; i < 4 && card.parentElement; i++) {
                        if (card.parentElement.querySelectorAll('a').length > 1) break;
                        card = card.parentElement;
                    }
                    const text = (card.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 600);
                    // Try to find viewer count text — Bigo shows like "1.2K" or "523"
                    // TODO: set UI markers for your instance's language — add the
                    // target language's "viewers" word to this alternation.
                    const viewerMatch = text.match(/(\\d+(?:\\.\\d+)?[KMB]?)\\s*(?:viewers?|觀眾)?/i);
                    out.push({
                        room_id: roomId,
                        href: href,
                        text: text,
                        viewer_str: viewerMatch ? viewerMatch[1] : null,
                    });
                }
                return out;
            }""",
            max_rooms,
        )

        stats["rooms_seen"] = len(rooms)
        ts = now_bkk().isoformat(timespec="seconds")
        for r in rooms:
            stats["rooms_new"] += 1  # every snapshot is new (time-series)
            record = {
                "ts": ts,
                "platform": "bigo",
                "kind": "lobby_snapshot",
                "target": label,
                "tier": target.get("tier"),
                "room_id": r["room_id"],
                "url": f"https://www.bigo.tv{r['href']}" if r["href"].startswith("/") else r["href"],
                "card_text": r.get("text") or "",
                "viewer_str": r.get("viewer_str"),
            }
            if not dry_run:
                write_jsonl(record)

    except Exception as e:
        log_line(f"[scan] {label:<14} ERR {type(e).__name__}: {e}")
        stats["status"] = f"error:{type(e).__name__}"
    finally:
        await context.close()
        await asyncio.sleep(random.uniform(*sleep_rng))

    return stats


async def main_async(args) -> None:
    policy = load_policy()
    if not policy.get("scan", {}).get("enable", False):
        log_line("[bigo_lobby_scan] scan disabled in policy — exit")
        return

    targets = policy.get("targets") or []
    if args.target:
        # Filter by --target lang:th  or  --target cat:chat
        wanted = set(args.target)
        targets = [t for t in targets if target_label(t) in wanted]
        if not targets:
            log_line(f"[bigo_lobby_scan] no targets match {args.target}")
            return

    proxy_url = os.environ.get("BLACKSITE_TH_PROXY")
    proxy_cfg = {"server": proxy_url} if proxy_url else None

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] bigo_lobby_scan start "
        f"targets={len(targets)} proxy={'yes' if proxy_cfg else 'no (datacenter IP)'} "
        f"dry_run={args.dry_run}"
    )

    totals = {"rooms_seen": 0, "ok": 0, "err": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=proxy_cfg)
        for target in targets:
            stats = await scan_target(browser, target, policy, args.dry_run)
            log_line(
                f"[scan] {stats['target']:<14} {stats['status']:<14} "
                f"rooms={stats['rooms_seen']}"
            )
            totals["rooms_seen"] += stats["rooms_seen"]
            if stats["status"].startswith("http_2"):
                totals["ok"] += 1
            elif stats["status"].startswith("error"):
                totals["err"] += 1
        await browser.close()

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] bigo_lobby_scan done "
        f"ok={totals['ok']} err={totals['err']} rooms={totals['rooms_seen']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", nargs="*", help="restrict to specific target labels (e.g. lang:th cat:chat)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
