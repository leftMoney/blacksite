"""
Blacksite — TikTok FYP feed harvester (logged-in, Phase A active).

Spawned by section_chief_orchestrate when target_kpi.is_verify_only=false.
Realises §15.7 boss directive 2026-05-11: shift P03 TikTok from passive
verify-only into local-language algorithm-shaping intelligence for folk-belief /
lottery / lucky_number / Friday ritual / prize reveal / card-collecting hooks.
Same scaffold supports P04 sports algo-shaping.

Algo-shape mechanism: watch-time-weighted dwell. Matching videos get long
dwell (rewatch occasionally); forbidden vertical gets sub-1s swipe. No like,
save, follow, comment, DM, duet, stitch, share, upload — see personas/warmup/
tiktok.md OPSEC §3 and CLAUDE.md §9.

Intel output: per-video metadata (caption, hashtags, author, sound) →
`runtime/raw/<agent_id>/<YYYY-MM-DD>.jsonl`. Section Chief promotes to KB
cards / library on next daily synthesis.

Usage:
  py agents/tiktok/feed_harvest.py --persona P03 --duration-min 8
  py agents/tiktok/feed_harvest.py --persona P03 --duration-min 2 --no-headless
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

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
RAW_DIR = RUNTIME / "raw"
LOG_DIR = RUNTIME / "logs"
SCREENSHOT_DIR = RUNTIME / "screenshots"
for d in (LOG_DIR, SCREENSHOT_DIR):
    d.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

# Persona → canonical agent_id (matches schedule yaml + KPI yaml filenames)
PERSONA_AGENT_ID = {
    "P03": "P03_TikTok",
    "P04": "P04_TikTok_sports",
}

# Phase B seed queries — one local keyword search at session start injects
# keyword signal into algo, drawing FYP toward persona's vertical. Sampled
# from policy/tiktok_hashtags.yaml `search_queries` + local_yolk hashtags.
# TODO: set search seeds for your instance (see instances/_TEMPLATE/policy/*.yaml)
PHASE_B_SEEDS = {
    "P03": [
        "example_keyword_1", "example_keyword_2", "example_keyword_3",
    ],
    "P04": [
        "example_sport_1", "example_sport_2", "example_sport_3",
    ],
}

# Per-persona vertical classifier. Match keys are local-script ONLY (no
# Latin keywords like "lucky"/"fortune"/"folk-belief") to avoid false positives
# from Western trends (5/20 'lucky girl 🍀' caught by 'lucky' keyword;
# 策略長 5/21 ruling: P03 yolk vertical is local-language folk-belief/lottery,
# Latin keyword match is structurally a false-positive vector).
# Override surface (future): instances/<inst>/policy/persona_follow_targets/
# <persona>.yaml `feed_harvest_classifier:` section.
# TODO: set search seeds for your instance (see instances/_TEMPLATE/policy/*.yaml)
# Match/forbidden keys should be the target language's native-script terms ONLY
# (no Latin keywords like "lucky"/"fortune") to avoid false positives from
# Western trends. 策略長 5/21 ruling: yolk vertical is local-language folk-belief/
# lottery; Latin keyword match is structurally a false-positive vector.
CLASSIFIER = {
    "P03": {
        # folk-belief / lottery / fortune / dream — yolk vertical (local script only)
        "match": [
            "example_keyword_1", "example_keyword_2", "example_keyword_3",
        ],
        # P04 territory + politics (cross-vertical contamination)
        "forbidden": [
            "example_sport_1", "example_sport_2", "example_politics_1",
        ],
    },
    "P04": {
        "match": [
            "example_sport_1", "example_sport_2", "example_sport_3",
        ],
        # P03 territory + politics
        "forbidden": [
            "example_keyword_1", "example_keyword_2", "example_politics_1",
        ],
    },
}


# Match additionally requires the caption is non-ASCII-dominant. This blocks
# English captions that happen to contain local chars in hashtags from
# polluting the match bucket (e.g. English meme post w/ a #local_keyword tag
# piggybacking on volume). 策略長 5/21: "match condition = local script present
# AND non-English-ASCII dominant".
_local_RE = None  # lazy compile

# TODO: set UI markers for your instance's language — set LOCAL_SCRIPT_RANGE
# to the target language's Unicode block as a (lo_char, hi_char) tuple, e.g.
# a U+0E00..U+0E7F block = (chr(0x0E00), chr(0x0E7F)). Default None matches nothing.
LOCAL_SCRIPT_RANGE = None  # (lo, hi) inclusive; None = no local-script detection


def _has_local_script(text: str) -> bool:
    """True if text contains any local-script Unicode codepoint in LOCAL_SCRIPT_RANGE."""
    if not text or LOCAL_SCRIPT_RANGE is None:
        return False
    lo, hi = LOCAL_SCRIPT_RANGE
    for ch in text:
        if lo <= ch <= hi:
            return True
    return False


def _is_non_ascii_dominant(text: str, threshold: float = 0.30) -> bool:
    """True if more than `threshold` of chars are non-ASCII. Set to 0.30
    rather than 0.50 because local captions often mix ASCII hashtags."""
    if not text:
        return False
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    return (non_ascii / len(text)) > threshold

# Dwell budget (seconds) per classification — algo-shape weight is watch-time.
DWELL_S = {
    "match":     (6.0, 12.0),   # long dwell = strong positive signal
    "forbidden": (0.4, 1.0),    # sub-1s swipe = strong negative signal
    "other":     (2.0, 4.5),    # neutral
}

# Selectors (data-e2e is TikTok's accessibility attribute; more stable than
# className but still subject to platform churn — keep fallbacks)
SEL_CONTAINER = '[data-e2e="recommend-list-item-container"]'
SEL_CAPTION = [
    '[data-e2e="video-desc"]',
    '[data-e2e="browse-video-desc"]',
]
SEL_AUTHOR = [
    '[data-e2e="video-author-uniqueid"]',
    '[data-e2e="browse-username"]',
]
SEL_MUSIC = [
    '[data-e2e="video-music"]',
    '[data-e2e="music-title"]',
]

LOGGED_IN_MARKERS = [
    '[data-e2e*="nav-profile" i]',
    '[data-e2e*="profile" i]',
    'button[aria-label*="profile" i]',
    'img[class*="avatar" i]',
]

CAPTCHA_SIGNALS = [
    'iframe[title*="captcha" i]',
    'iframe[src*="captcha" i]',
    '[class*="captcha" i]',
    # TODO: set UI markers for your instance's language — add the target
    # language's "verify you are human" wording to this alternation.
    'text=/verify.*human|please.*verify/i',
]

# Cap intel volume per run to keep raw JSONL bounded; mostly a safety net.
MAX_VIDEOS_PER_RUN = 80


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def log(aid: str, msg: str) -> None:
    line = f"[{now_iso()}] [tt_feed] [{aid}] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"tiktok_feed_{datetime.now(TZ).strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def hist(actor: str, kind: str, title: str, body: str = "",
         scope: str = "persona", refs: list | None = None) -> int:
    try:
        from processors.history_log import log_event
        return log_event(actor=actor, kind=kind, scope=scope,
                         title=title, body=body, refs=refs)
    except Exception as e:
        log(actor, f"history_log failed: {e}")
        return -1


def raw_path(agent_id: str) -> Path:
    d = RAW_DIR / agent_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{datetime.now(TZ).strftime('%Y-%m-%d')}.jsonl"


def emit(agent_id: str, persona: str, event: dict) -> None:
    out = {"ts": now_iso(), "persona": persona, "platform": "tiktok",
           "agent_id": agent_id, **event}
    with raw_path(agent_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False) + "\n")


def classify(persona: str, text: str) -> str:
    rules = CLASSIFIER.get(persona, {})
    lower = (text or "").lower()
    # Forbidden checked first — local script not required (any cross-vertical
    # signal in any language is still cross-vertical pollution to avoid).
    for kw in rules.get("forbidden", []):
        if kw.lower() in lower:
            return "forbidden"
    # Match requires (a) local keyword present AND (b) caption is local-script
    # AND non-ASCII-dominant. This blocks English captions that piggyback on
    # local hashtag volume from polluting match (5/20 'lucky girl 🍀' false
    # positive root cause).
    matched_kw = None
    for kw in rules.get("match", []):
        if kw.lower() in lower:
            matched_kw = kw
            break
    if matched_kw and _has_local_script(text) and _is_non_ascii_dominant(text):
        return "match"
    return "other"


async def _logged_in(page) -> tuple[bool, str | None]:
    for marker in LOGGED_IN_MARKERS:
        try:
            if await page.locator(marker).first.is_visible(timeout=3_000):
                return True, marker
        except Exception:
            continue
    return False, None


async def _captcha_visible(page) -> bool:
    for sel in CAPTCHA_SIGNALS:
        try:
            if await page.locator(sel).first.is_visible(timeout=1_000):
                return True
        except Exception:
            continue
    return False


async def _try_text(page, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() < 1:
                continue
            t = await loc.inner_text(timeout=1_500)
            if t and t.strip():
                return t.strip()
        except Exception:
            continue
    return ""


# Extract all rendered FYP items (current + adjacent buffered). Logged-in FYP
# DOM does NOT expose /@<handle>/video/<id> links on item containers (probe
# 2026-05-18) so we derive a signature_id from caption+author hash, and try
# to enrich video_id from __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON if present.
EXTRACT_ALL_JS = r"""() => {
    const out = { items: [], rehydration_ids: [] };
    const containers = Array.from(
        document.querySelectorAll('[data-e2e="recommend-list-item-container"]')
    );
    const vh = window.innerHeight;

    // First pass: collect candidate video ids from rehydration JSON (best-effort)
    // for later caption-match correlation.
    const rehItems = [];
    try {
        const reh = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
        if (reh) {
            const data = JSON.parse(reh.textContent);
            const stack = [data];
            let nodes = 0;
            const seen = new Set();
            while (stack.length && nodes < 80000) {
                const n = stack.pop(); nodes++;
                if (!n || typeof n !== 'object') continue;
                if (Array.isArray(n)) { for (const v of n) stack.push(v); continue; }
                const id = n.id || n.aweme_id;
                if (id && typeof id === 'string' && /^\d{15,20}$/.test(id)
                    && (n.desc !== undefined || n.description !== undefined)) {
                    if (!seen.has(id)) {
                        seen.add(id);
                        rehItems.push({
                            id,
                            desc: (n.desc || n.description || '').trim(),
                            authorHandle: n.author && (n.author.uniqueId || n.author.unique_id || n.author.username) || null,
                        });
                    }
                }
                for (const k of Object.keys(n)) stack.push(n[k]);
            }
        }
    } catch (e) { out.reh_error = e.message; }
    out.rehydration_ids = rehItems.map(x => x.id);

    for (const c of containers) {
        const r = c.getBoundingClientRect();
        const inViewport = (r.bottom > 0 && r.top < vh && r.height > 100);
        const grab = (sel) => {
            const el = c.querySelector(sel);
            return el ? (el.innerText || el.textContent || '').trim() : '';
        };
        const caption = grab('[data-e2e="video-desc"]');
        const music   = grab('[data-e2e="video-music"]');

        // Author handle: first /@<handle> anchor in container
        let author = '';
        for (const a of c.querySelectorAll('a[href^="/@"]')) {
            const href = a.getAttribute('href') || '';
            const m = href.match(/^\/@([A-Za-z0-9_.\-]+)/);
            if (m) { author = m[1]; break; }
        }

        // Hashtags via data-e2e=search-common-link OR href*="/tag/"
        const hashtags = [];
        const tagSeen = new Set();
        for (const a of c.querySelectorAll('a[data-e2e="search-common-link"], a[href*="/tag/"]')) {
            const t = (a.innerText || '').trim().replace(/^#/, '');
            if (t && !tagSeen.has(t)) { tagSeen.add(t); hashtags.push(t); }
        }

        // Cross-correlate caption to rehydration item for video_id
        let video_id = null;
        if (caption && rehItems.length > 0) {
            const key = caption.slice(0, 40);
            for (const it of rehItems) {
                if (it.desc && it.desc.slice(0, 40) === key) {
                    video_id = it.id; break;
                }
            }
        }

        const item = {
            scroll_index: c.getAttribute('data-scroll-index') || c.id || null,
            inViewport,
            bbox_top: Math.round(r.top),
            bbox_height: Math.round(r.height),
            video_id,
            author,
            caption,
            music,
            hashtags,
            like_count:    grab('[data-e2e="like-count"]'),
            comment_count: grab('[data-e2e="comment-count"]'),
            share_count:   grab('[data-e2e="share-count"]'),
            favorite_count: grab('[data-e2e="favorite-count"]'),
        };
        // Only emit if there's any content (skip empty placeholders)
        if (item.caption || item.author || item.hashtags.length > 0) {
            out.items.push(item);
        }
    }
    return out;
}"""


def _signature_id(item: dict) -> str:
    import hashlib
    key = (item.get("author", "") + "|" + (item.get("caption", "") or "")[:200]).encode("utf-8")
    return hashlib.md5(key).hexdigest()[:16]


SWIPE_NEXT_JS = r"""() => {
    // Find currently-most-visible article, then scrollIntoView on next one.
    // TikTok FYP uses an inner scroll container, not document body, so
    // wheel/keyboard events on the page level don't reliably advance it.
    const articles = Array.from(
        document.querySelectorAll('[data-e2e="recommend-list-item-container"]')
    );
    const vh = window.innerHeight;
    let currentIdx = -1, bestTopDist = Infinity;
    for (let i = 0; i < articles.length; i++) {
        const r = articles[i].getBoundingClientRect();
        if (r.height < 100) continue;
        const dist = Math.abs(r.top);  // current = top closest to 0
        if (r.bottom > 0 && r.top < vh && dist < bestTopDist) {
            bestTopDist = dist; currentIdx = i;
        }
    }
    if (currentIdx === -1 || currentIdx + 1 >= articles.length) {
        return { advanced: false, reason: 'no_next', currentIdx, total: articles.length };
    }
    articles[currentIdx + 1].scrollIntoView({ behavior: 'auto', block: 'start' });
    return { advanced: true, currentIdx, nextIdx: currentIdx + 1, total: articles.length };
}"""


async def _swipe_next(page) -> dict:
    """Advance to next FYP video. TikTok logged-in FYP uses inner scroll
    container — page-level wheel/keyboard don't route. scrollIntoView on the
    next article element is the reliable path."""
    try:
        return await page.evaluate(SWIPE_NEXT_JS) or {"advanced": False}
    except Exception as e:
        return {"advanced": False, "error": str(e)}


async def harvest(
    persona: str,
    duration_min: int,
    headless: bool,
) -> int:
    agent_id = PERSONA_AGENT_ID.get(persona)
    if not agent_id:
        print(f"unknown persona {persona!r}", file=sys.stderr)
        return 2

    log(agent_id, f"start duration_min={duration_min} headless={headless}")
    hist(agent_id, kind="milestone", scope="persona",
         title=f"{agent_id} feed_harvest start",
         body=f"persona={persona} duration_min={duration_min}")

    from agents._common.camoufox_session import launch_persona, storage_state_path

    state_path = storage_state_path(persona, "tiktok")
    if not state_path.exists():
        log(agent_id, f"no storage_state at {state_path}; abort")
        hist(agent_id, kind="warning", scope="persona",
             title=f"{agent_id} feed_harvest abort no storage_state",
             body=f"expected={state_path}")
        return 2

    deadline = datetime.now(TZ) + timedelta(minutes=duration_min)
    seen_ids: set[str] = set()
    stats = {"match": 0, "forbidden": 0, "other": 0, "no_id": 0}
    consecutive_empty = 0

    async with launch_persona(
        persona, "tiktok",
        headless=headless,
        use_storage_state=True,
    ) as (browser, context, page):
        try:
            await page.goto("https://www.tiktok.com/foryou",
                            wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            log(agent_id, f"goto /foryou failed: {e}")
            emit(agent_id, persona, {"event": "feed_harvest_abort",
                                     "reason": "goto_failed", "detail": str(e)})
            return 3

        # Lazy-render retry (TikTok FYP nav chrome can no-show on first paint)
        await page.wait_for_timeout(4_000)
        ok, marker = await _logged_in(page)
        if not ok:
            try:
                await page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                pass
            await page.wait_for_timeout(3_000)
            ok, marker = await _logged_in(page)
        if not ok:
            if await _captcha_visible(page):
                log(agent_id, "captcha gate — aborting harvest, notifying boss")
                try:
                    shot = SCREENSHOT_DIR / f"{agent_id}_{datetime.now(TZ).strftime('%Y%m%dT%H%M%S')}_captcha.png"
                    await page.screenshot(path=str(shot), full_page=False)
                except Exception:
                    shot = None
                emit(agent_id, persona, {"event": "feed_harvest_abort",
                                         "reason": "captcha_gate",
                                         "screenshot": shot.name if shot else None})
                from agents._common.warmup_session_base import queue_manual_login_alert
                queue_manual_login_alert(
                    aid=agent_id, persona=persona, platform="tiktok",
                    reason="captcha_gate_during_harvest",
                    action_hint="solve TikTok captcha in browser; storage_state will refresh on next verify",
                    screenshot_name=shot.name if shot else None,
                )
                return 3
            log(agent_id, "not logged in — aborting harvest")
            emit(agent_id, persona, {"event": "feed_harvest_abort",
                                     "reason": "not_logged_in"})
            return 3

        log(agent_id, f"logged in via {marker!r}, beginning FYP scroll")
        emit(agent_id, persona, {"event": "feed_harvest_start",
                                 "duration_min": duration_min,
                                 "marker": marker})

        # Phase B: single local keyword search at session start. Sends a strong
        # vertical signal to TikTok's algo before we begin watch-time logging.
        # Result page captured into raw JSONL for intel too (local keyword
        # surfacing what the engine returns for it).
        seeds = PHASE_B_SEEDS.get(persona) or []
        if seeds:
            from urllib.parse import quote
            seed = random.choice(seeds)
            search_url = f"https://www.tiktok.com/search?q={quote(seed)}"
            try:
                log(agent_id, f"Phase B: search seed = {seed!r}")
                await page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(5_000)
                # Capture the search-result top-N for intel (different DOM shape;
                # rely on rehydration parser from tiktok_listen if available)
                emit(agent_id, persona, {
                    "event": "phase_b_search",
                    "query": seed,
                    "url": search_url,
                })
                # Back to FYP for watch-time phase
                await page.goto("https://www.tiktok.com/foryou",
                                wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(4_000)
            except Exception as e:
                log(agent_id, f"Phase B search failed (non-fatal): {e}")
                emit(agent_id, persona, {"event": "phase_b_search_error",
                                         "query": seed, "error": str(e)})

        # Initial wait so FYP first items finish hydrating before we sample.
        # Probe 2026-05-18 showed FYP needs ~6-8s + networkidle settle for
        # all 9 buffered articles to populate caption / hashtags.
        await page.wait_for_timeout(4_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        await page.wait_for_timeout(2_000)

        videos_seen = 0
        while datetime.now(TZ) < deadline and videos_seen < MAX_VIDEOS_PER_RUN:
            # Captcha can appear mid-session (rate-limit gate)
            if await _captcha_visible(page):
                log(agent_id, "captcha appeared mid-session; stopping early")
                emit(agent_id, persona, {"event": "feed_harvest_captcha_midrun",
                                         "videos_seen": videos_seen})
                break

            try:
                payload = await page.evaluate(EXTRACT_ALL_JS)
            except Exception as e:
                log(agent_id, f"extract failed: {e}")
                payload = None

            items = (payload or {}).get("items") or []
            if not items:
                consecutive_empty += 1
                stats["no_id"] += 1
                if consecutive_empty >= 4:
                    log(agent_id, "4 consecutive extracts returned 0 items — DOM drift; stopping")
                    emit(agent_id, persona, {"event": "feed_harvest_dom_drift",
                                             "videos_seen": videos_seen})
                    break
                await _swipe_next(page)
                await page.wait_for_timeout(random.randint(1500, 2500))
                continue
            consecutive_empty = 0

            # Identify "current playing" item (in-viewport, ≥50% viewport height).
            # TikTok FYP places it at top of viewport with full vertical fill.
            vh_threshold = 0  # set below from a heuristic
            current_sig = None
            current_cls = None
            for it in items:
                if it.get("inViewport") and it.get("bbox_height", 0) >= 400:
                    if current_sig is None or abs(it["bbox_top"]) < 50:
                        current_sig = _signature_id(it)
                        current_cls = None  # filled when we emit below

            # Emit ALL newly-observed items for intel coverage; dwell budget
            # applies only to the current-playing one.
            emitted_this_tick = 0
            for it in items:
                sig = _signature_id(it)
                if sig in seen_ids:
                    continue
                seen_ids.add(sig)

                classify_text = " ".join([
                    it.get("caption") or "",
                    " ".join(it.get("hashtags") or []),
                    it.get("author") or "",
                    it.get("music") or "",
                ])
                cls = classify(persona, classify_text)
                stats[cls] += 1

                is_current = (sig == current_sig)
                if is_current:
                    current_cls = cls

                emit(agent_id, persona, {
                    "event": "fyp_video",
                    "signature_id": sig,
                    "video_id": it.get("video_id"),
                    "author": it.get("author"),
                    "caption": it.get("caption"),
                    "hashtags": it.get("hashtags") or [],
                    "music": it.get("music"),
                    "stats": {
                        "like_count":     it.get("like_count"),
                        "comment_count":  it.get("comment_count"),
                        "share_count":    it.get("share_count"),
                        "favorite_count": it.get("favorite_count"),
                    },
                    "classification": cls,
                    "is_current_playing": is_current,
                    "scroll_index": it.get("scroll_index"),
                })
                videos_seen += 1
                emitted_this_tick += 1

            # Dwell budget tied to current-playing item only (algo-shape signal).
            # Default to "other" dwell when no current detected.
            cls_for_dwell = current_cls or "other"
            dwell_lo, dwell_hi = DWELL_S[cls_for_dwell]
            dwell_s = random.uniform(dwell_lo, dwell_hi)
            await page.wait_for_timeout(int(dwell_s * 1000))

            swipe_result = await _swipe_next(page)
            if not swipe_result.get("advanced"):
                log(agent_id, f"swipe stalled: {swipe_result} — waiting for buffer fill")
                await page.wait_for_timeout(2_500)
            else:
                # Post-swipe hydration pause
                await page.wait_for_timeout(random.randint(1100, 1800))

        emit(agent_id, persona, {
            "event": "feed_harvest_end",
            "videos_seen": videos_seen,
            "classification_counts": stats,
            "duration_min_planned": duration_min,
            "ended_reason": ("deadline" if datetime.now(TZ) >= deadline
                             else "max_videos" if videos_seen >= MAX_VIDEOS_PER_RUN
                             else "early"),
        })

        log(agent_id, f"done videos={videos_seen} match={stats['match']} "
                      f"forbidden={stats['forbidden']} other={stats['other']} "
                      f"no_id={stats['no_id']}")
        hist(agent_id, kind="milestone", scope="persona",
             title=f"{agent_id} feed_harvest end",
             body=f"videos={videos_seen} classification={stats}")

        # Re-persist storage_state so any session-cookie refresh is captured
        try:
            await context.storage_state(path=str(state_path))
        except Exception as e:
            log(agent_id, f"storage_state save failed: {e}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", required=True, choices=["P03", "P04"])
    parser.add_argument("--duration-min", type=int, default=8)
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()
    return asyncio.run(harvest(
        persona=args.persona,
        duration_min=args.duration_min,
        headless=not args.no_headless,
    ))


if __name__ == "__main__":
    sys.exit(main())
