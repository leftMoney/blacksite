"""
Blacksite — TikTok scanner (Playwright, anonymous read-only).

Mode: anonymous. FYP / algorithm-shaped feed deliberately NOT used (FYP
requires TH residential IP per INSTANCE.md §1, blocked at v1).
Focus: hashtag landing pages + keyword search + handle profile pages.

Endpoints (no login):
  hashtag:  https://www.tiktok.com/tag/<encoded-tag>      (no '#')
  search:   https://www.tiktok.com/search?q=<encoded-query>
  user:     https://www.tiktok.com/@<handle>

TikTok aggressively rate-limits scrape; per-run jitter is wider than other
listeners. Falls back from search → hashtag if rate-limited.

Output:
  instances/_TEMPLATE/runtime/raw/tiktok/<YYYY-MM-DD>.jsonl

Usage:
  py agents/tiktok/tiktok_listen.py
  py agents/tiktok/tiktok_listen.py --hashtags example_keyword_1 example_keyword_2
  py agents/tiktok/tiktok_listen.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
POLICY_PATH = INSTANCE_DIR / "policy" / "tiktok_hashtags.yaml"
RAW_DIR = INSTANCE_DIR / "runtime" / "raw" / "tiktok"
MEDIA_DIR = INSTANCE_DIR / "runtime" / "media" / "tiktok"
LOG_DIR = INSTANCE_DIR / "runtime" / "logs"
SEEN_PATH = INSTANCE_DIR / "runtime" / "tiktok_seen_videos.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

from agents._common.browser_viewport import MOBILE_USER_AGENT, mobile_viewport  # noqa: E402


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"tiktok_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def load_policy() -> dict[str, Any]:
    with POLICY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen() -> set[str]:
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set[str]) -> None:
    capped = sorted(seen)[-50000:]
    SEEN_PATH.write_text(json.dumps(capped), encoding="utf-8")


def write_jsonl(record: dict) -> None:
    today = now_bkk().strftime("%Y-%m-%d")
    out_path = RAW_DIR / f"{today}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def should_download_video(target: str, policy: dict) -> bool:
    """Per policy: download_video flag (global) OR target ∈ download_video_for_tags."""
    out = policy.get("output", {})
    if out.get("download_video"):
        return True
    whitelist = out.get("download_video_for_tags") or []
    return target in whitelist


def download_video(video_id: str, author: str | None, target: str) -> dict | None:
    """Synchronous yt-dlp call. Best-effort; returns media_files entry or None."""
    if not author:
        return None
    today = now_bkk().strftime("%Y-%m-%d")
    out_dir = MEDIA_DIR / today
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path_tmpl = str(out_dir / f"{video_id}.%(ext)s")
    url = f"https://www.tiktok.com/@{author}/video/{video_id}"
    try:
        import yt_dlp
    except ImportError:
        return None
    opts = {
        "outtmpl": out_path_tmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 1,
        "fragment_retries": 1,
        "socket_timeout": 30,
        "format": "best[height<=720]/best",   # cap at 720p
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        # Find the saved file
        ext = info.get("ext", "mp4")
        saved_path = out_dir / f"{video_id}.{ext}"
        if not saved_path.exists():
            cands = list(out_dir.glob(f"{video_id}.*"))
            saved_path = cands[0] if cands else None
        if not saved_path or not saved_path.exists():
            return {"media_kind": "video", "skipped": "yt_dlp_no_file"}
        size = saved_path.stat().st_size
        rel = str(saved_path.relative_to(ROOT)).replace("\\", "/")
        return {
            "media_kind": "video",
            "file_path": rel,
            "file_size": size,
            "mime_type": f"video/{ext}",
            "duration_s": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
            "tiktok_target": target,
        }
    except Exception as e:
        return {"media_kind": "video", "error": f"{type(e).__name__}: {str(e)[:120]}"}


# Extract video records from TikTok hashtag/search/profile pages.
# TikTok embeds initial state in __UNIVERSAL_DATA_FOR_REHYDRATION__ <script>
# (a JSON blob). Pulling video IDs + metadata from that is more reliable than
# DOM querying since the structure changes frequently.
EXTRACT_JS = r"""() => {
    const out = { videos: [], source: null };
    // Strategy: parse rehydration JSON, deep-search for video-like items
    // (objects with both `id` (numeric string) and `desc` and `author`).
    const script = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
    const seenIds = new Set();
    const pushItem = (it) => {
        const id = String(it.id || it.aweme_id || '');
        if (!id || !/^\d{15,20}$/.test(id)) return;
        if (seenIds.has(id)) return;
        seenIds.add(id);
        out.videos.push({
            id,
            desc: it.desc || it.description,
            createTime: it.createTime || it.create_time,
            author: it.author && (it.author.uniqueId || it.author.unique_id || it.author.username),
            authorId: it.author && (it.author.id || it.author.uid),
            stats: it.stats || it.statistics,
            video_url: it.video && (it.video.playAddr || it.video.play_addr),
            cover_url: it.video && (it.video.cover || it.video.cover_url),
            hashtags: Array.isArray(it.textExtra) ? it.textExtra.filter(x => x.hashtagName).map(x => x.hashtagName) : undefined,
            music_id: it.music && (it.music.id || it.music.mid),
            music_title: it.music && (it.music.title || it.music.musicName),
        });
    };
    if (script) {
        try {
            const data = JSON.parse(script.textContent);
            // Recursive walk: any object with id + desc + author looks like a video
            const stack = [data];
            let nodes = 0;
            while (stack.length && nodes < 100000) {
                const node = stack.pop();
                nodes++;
                if (!node || typeof node !== 'object') continue;
                if (Array.isArray(node)) {
                    for (const v of node) stack.push(v);
                    continue;
                }
                if (node.id && (node.desc !== undefined || node.description !== undefined) && node.author) {
                    pushItem(node);
                }
                for (const k of Object.keys(node)) stack.push(node[k]);
            }
            if (out.videos.length > 0) out.source = 'rehydration_deep';
        } catch (e) { out.error = e.message; }
    }
    // Fallback: regex over full HTML for /@<handle>/video/<id>
    if (out.videos.length === 0) {
        const html = document.documentElement.outerHTML;
        const re = /\/@([A-Za-z0-9_.-]+)\/video\/(\d{15,20})/g;
        let m;
        while ((m = re.exec(html)) !== null) {
            const id = m[2], handle = m[1];
            if (seenIds.has(id)) continue;
            seenIds.add(id);
            out.videos.push({ id, author: handle, _via: 'html_regex' });
        }
        if (out.videos.length > 0) out.source = 'html_regex_fallback';
    }
    return out;
}"""


async def scan_target(
    browser, kind: str, target: str, dry_run: bool, sleep_rng: list[int]
) -> dict:
    """kind: 'hashtag' | 'search' | 'user'."""
    if kind == "hashtag":
        url = f"https://www.tiktok.com/tag/{quote(target)}"
    elif kind == "search":
        url = f"https://www.tiktok.com/search?q={quote(target)}"
    elif kind == "user":
        url = f"https://www.tiktok.com/@{target}"
    else:
        raise ValueError(kind)

    stats = {"kind": kind, "target": target, "status": "init", "videos": 0, "new": 0}
    context = await browser.new_context(
        user_agent=MOBILE_USER_AGENT,
        locale="th-TH",
        viewport=mobile_viewport(),
        is_mobile=True,
        has_touch=True,
    )
    page = await context.new_page()
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        status = resp.status if resp else 0
        stats["status"] = f"http_{status}"
        if status != 200:
            return stats

        # Wait for the rehydration script to be injected
        try:
            await page.wait_for_selector(
                '#__UNIVERSAL_DATA_FOR_REHYDRATION__', timeout=10000
            )
        except Exception:
            pass
        # TikTok hydrates video items several seconds after script injection;
        # also scroll once to trigger lazy-load.
        await asyncio.sleep(4)
        try:
            await page.evaluate("window.scrollBy(0, 1500)")
        except Exception:
            pass
        await asyncio.sleep(2)

        data = await page.evaluate(EXTRACT_JS)
        videos = data.get("videos") or []

        # Python-side regex fallback over full page HTML if JS evaluate yielded
        # nothing. This is the most reliable path since `/@<handle>/video/<id>`
        # consistently appears in TikTok HTML even when rehydration JSON shape
        # changes.
        if not videos:
            html = await page.content()
            seen_ids = set()
            for m in re.finditer(
                r"/@([A-Za-z0-9_.\-]+)/video/(\d{15,20})", html
            ):
                vid_id, handle = m.group(2), m.group(1)
                if vid_id in seen_ids:
                    continue
                seen_ids.add(vid_id)
                videos.append({"id": vid_id, "author": handle, "_via": "py_regex"})
            if videos:
                data["source"] = "py_regex_fallback"

        stats["videos"] = len(videos)
        stats["source"] = data.get("source")

        seen = scan_target.seen_cache
        download_enabled = scan_target.download_enabled_for_target.get(target, False)
        downloaded_this_target = 0
        max_dl_per_target = scan_target.max_downloads_per_target

        for v in videos:
            vid = str(v.get("id"))
            if not vid or vid in seen:
                continue
            seen.add(vid)
            stats["new"] += 1
            media_files: list[dict] = []
            if (
                download_enabled
                and not dry_run
                and downloaded_this_target < max_dl_per_target
                and v.get("author")
            ):
                entry = download_video(vid, v.get("author"), target)
                if entry:
                    media_files.append(entry)
                    if entry.get("file_path"):
                        downloaded_this_target += 1
                        stats.setdefault("downloads", 0)
                        stats["downloads"] += 1
            record = {
                "ts": now_bkk().isoformat(timespec="seconds"),
                "platform": "tiktok",
                "kind": kind,
                "target": target,
                "video_id": vid,
                "author": v.get("author"),
                "author_id": v.get("authorId"),
                "desc": v.get("desc"),
                "create_time": v.get("createTime"),
                "stats": v.get("stats"),
                "hashtags": v.get("hashtags"),
                "music_id": v.get("music_id"),
                "music_title": v.get("music_title"),
                "cover_url": v.get("cover_url"),
                "video_url": v.get("video_url"),
                "source": data.get("source"),
                "media_files": media_files,
            }
            if not dry_run:
                write_jsonl(record)
    except Exception as e:
        stats["status"] = f"err:{type(e).__name__}"
        log_line(f"[scan] {kind}/{target} ERR: {type(e).__name__}: {str(e)[:120]}")
    finally:
        await context.close()
        await asyncio.sleep(random.uniform(*sleep_rng))
    return stats


# Class-attr style caches so scan_target can dedupe + know per-target download
# eligibility across calls in one run.
scan_target.seen_cache = set()
scan_target.download_enabled_for_target = {}
scan_target.max_downloads_per_target = 5   # cap downloads per scheduled run


async def main_async(args) -> None:
    policy = load_policy()
    if not policy.get("scan", {}).get("enable", False):
        log_line("[tiktok_listen] scan disabled — exit")
        return

    sleep_rng = policy["scan"]["inter_request_sleep_s"]

    targets: list[tuple[str, str]] = []
    if args.hashtags:
        targets.extend([("hashtag", h) for h in args.hashtags])
    elif args.search:
        targets.extend([("search", q) for q in args.search])
    elif args.users:
        targets.extend([("user", u) for u in args.users])
    else:
        # Default: all hashtags + a sample of search queries
        for cat in ("local_yolk", "local_white", "brand", "sports_kol"):
            for tag in policy["hashtags"].get(cat, []):
                targets.append(("hashtag", tag))
        for q in policy.get("search_queries", [])[:6]:
            targets.append(("search", q))

    seen = load_seen()
    scan_target.seen_cache = seen
    # Build per-target download eligibility from policy
    scan_target.download_enabled_for_target = {}
    for kind, t in targets:
        scan_target.download_enabled_for_target[t] = should_download_video(t, policy)
    proxy_url = os.environ.get("BLACKSITE_TH_PROXY")
    proxy_cfg = {"server": proxy_url} if proxy_url else None

    dl_targets = [t for t, e in scan_target.download_enabled_for_target.items() if e]
    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] tiktok_listen start "
        f"targets={len(targets)} seen={len(seen)} "
        f"proxy={'yes' if proxy_cfg else 'no'} dry_run={args.dry_run} "
        f"download_targets={dl_targets}"
    )

    totals = {"videos": 0, "new": 0, "ok": 0, "err": 0}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, proxy=proxy_cfg)
        for kind, target in targets:
            stats = await scan_target(browser, kind, target, args.dry_run, sleep_rng)
            log_line(
                f"[scan] {kind:<7} {target[:25]:<25} "
                f"{stats.get('status','?'):<22} "
                f"src={stats.get('source','?')!s:<18} "
                f"vids={stats['videos']:<3} new={stats['new']}"
            )
            totals["videos"] += stats["videos"]
            totals["new"] += stats["new"]
            if stats["status"] == "http_200":
                totals["ok"] += 1
            elif stats["status"].startswith("err"):
                totals["err"] += 1
        await browser.close()

    if not args.dry_run:
        save_seen(scan_target.seen_cache)

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] tiktok_listen done "
        f"ok={totals['ok']} err={totals['err']} videos={totals['videos']} new={totals['new']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hashtags", nargs="*")
    parser.add_argument("--search", nargs="*")
    parser.add_argument("--users", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
