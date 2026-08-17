"""
Blacksite — Facebook Page og:* metadata extractor (anonymous, fully ToS-compliant).

Successor to fb_page_scan.py (mbasic-based, dead post 2026-04-30 mbasic
deprecation). Fetches each Page via FB's own link-preview crawler endpoint
using the `facebookexternalhit/1.1` User-Agent — this is the same UA FB uses
when third-party sites generate FB share previews, so it's the publicly
sanctioned path for reading og:* metadata. No login, no cookies, no scraping
of timeline content. ~500KB HTML response per Page.

What we get (per hourly snapshot, per Page):
  - og:title (full Page name in local + English)
  - og:description (Page description + likes total + "talking about" count
    + contact email + website)
  - og:image URL (cover photo / latest post media — hash-diff = activity signal)
  - og:type (Page category: video.other / website / public_profile / etc)

What we DON'T get (and never will via this path):
  - Individual post text / images / engagement
  - Comments
  - Timeline scrollback

Commercial value (per CLAUDE.md §1 north star):
  - yolk operator KOL (ExampleKOL1 / ExampleKOL2 / ExampleKOL3): likes-trend +
    og:image hash diff = post-frequency proxy + influence-growth strategist signal
  - folk-belief KOL: follower growth window for the client brand commercial
    timing decisions
  - sports KOL (ExampleAthlete / ExampleAthlete2 / ExampleLeague): og:image = match-day visual signal,
    "talking about" surge = viral event detection

Output: instances/_TEMPLATE/runtime/raw/facebook_og_meta/<YYYY-MM-DD>.jsonl

Usage:
  py agents/facebook/fb_og_meta_scan.py
  py agents/facebook/fb_og_meta_scan.py --pages https://www.facebook.com/foo
  py agents/facebook/fb_og_meta_scan.py --dry-run

Per CLAUDE.md §6.4 all timestamps GMT+7 ISO 8601 with offset.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
POLICY_PATH = INSTANCE_DIR / "policy" / "facebook_pages.yaml"
RAW_DIR = INSTANCE_DIR / "runtime" / "raw" / "facebook_og_meta"
LOG_DIR = INSTANCE_DIR / "runtime" / "logs"
LAST_SEEN_PATH = INSTANCE_DIR / "runtime" / "fb_og_meta_last_seen.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

# FB's own link-preview crawler UA — public, sanctioned path for og:* fetch.
# Don't randomize: FB explicitly recognizes this UA + serves rich og: HTML.
UA = ("facebookexternalhit/1.1 "
      "(+http://www.facebook.com/externalhit_uatext.php)")

# Per-request hard cap so a stalled connection can't wedge the hourly cron.
REQUEST_TIMEOUT_S = 20

# Inter-request gentle pacing; FB's preview endpoint is generous but staggering
# avoids any pattern-detection noise. Mirrors fb_page_scan inter_request_sleep_s.
INTER_REQUEST_SLEEP_RANGE = (3, 7)


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log_line(msg: str) -> None:
    line = f"[{now_bkk().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    log_path = LOG_DIR / f"facebook_og_meta_{now_bkk().strftime('%Y-%m-%d')}.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_policy() -> dict[str, Any]:
    with POLICY_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_last_seen() -> dict[str, dict]:
    """Per-Page last og:image hash + likes count for diff signal."""
    if LAST_SEEN_PATH.exists():
        try:
            return json.loads(LAST_SEEN_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_last_seen(state: dict[str, dict]) -> None:
    LAST_SEEN_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_jsonl(record: dict) -> None:
    today = now_bkk().strftime("%Y-%m-%d")
    out_path = RAW_DIR / f"{today}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def page_slug_from_url(url: str) -> str:
    p = urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    return parts[0] if parts else url


# og:* meta tag pattern. FB serves both `property="og:..."` and
# `name="og:..."` variants depending on path; match either.
OG_META_RE = re.compile(
    r'<meta\s+(?:property|name)="og:([a-z_:]+)"\s+content="([^"]*)"',
    re.IGNORECASE,
)

# likes count + "talking about" usually live inside og:description, formatted
# variably across locales. Numbers may be separated by , . space or NBSP.
LIKES_RE = re.compile(r'([\d,]+)\s*(?:個讚|likes?|個贊|points?|讚)', re.IGNORECASE)
TALKING_RE = re.compile(r'([\d,]+)\s*(?:人正在談論|talking about|位網友)', re.IGNORECASE)
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
WEB_RE = re.compile(r'(https?://[^\s"<>]+|www\.[^\s"<>]+)')


def parse_og(html_text: str) -> dict[str, str]:
    """Pull all og:* tags. Returns dict keyed by suffix (title/image/...)."""
    og: dict[str, str] = {}
    for m in OG_META_RE.finditer(html_text):
        key = m.group(1).lower()
        val = html.unescape(m.group(2))
        og[key] = val
    return og


def parse_description(desc: str) -> dict[str, Any]:
    """Extract structured fields from og:description string."""
    out: dict[str, Any] = {}
    if not desc:
        return out
    m = LIKES_RE.search(desc)
    if m:
        try:
            out["likes_total"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = TALKING_RE.search(desc)
    if m:
        try:
            out["talking_count"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    em = EMAIL_RE.search(desc)
    if em:
        out["contact_email"] = em.group(0)
    web = WEB_RE.search(desc)
    if web:
        out["website"] = web.group(1)
    return out


def og_image_hash(image_url: str) -> str | None:
    """SHA256 of og:image URL. URL changes when FB cycles cover/featured media."""
    if not image_url:
        return None
    return hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:16]


def fetch_og(url: str, retries: int = 3) -> tuple[int, str | None]:
    """Returns (http_status, html_or_None). Retries with backoff on transient err."""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
    }
    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_S,
                                allow_redirects=True)
            return resp.status_code, resp.text
        except requests.RequestException as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    log_line(f"  fetch FAIL after {retries} retries: {type(last_err).__name__}: {last_err}")
    return 0, None


def scan_page(page_cfg: dict, last_seen: dict[str, dict],
              dry_run: bool) -> dict:
    raw_url = page_cfg["url"]
    if not raw_url.startswith("https://"):
        return {"slug": raw_url, "status": "skip_non_url"}
    slug = page_slug_from_url(raw_url)
    stats: dict[str, Any] = {"slug": slug, "tier": page_cfg.get("tier"),
                             "status": "init"}

    status, body = fetch_og(raw_url)
    if status == 0 or body is None:
        stats["status"] = "fetch_error"
        return stats
    stats["status"] = f"http_{status}"
    if status != 200:
        log_line(f"  {slug:<32} HTTP {status} — skip")
        return stats

    og = parse_og(body)
    if not og.get("title"):
        # Some Pages return SPA shell even to the bot UA (rare; e.g. age-gated
        # Pages or restricted regions). Treat as soft-fail; record nothing.
        stats["status"] = "no_og_metadata"
        log_line(f"  {slug:<32} no og:* metadata in {len(body)//1024}KB body — skip")
        return stats

    desc_fields = parse_description(og.get("description", ""))
    img_hash = og_image_hash(og.get("image", ""))

    prev = last_seen.get(slug, {})
    img_changed = (img_hash is not None
                   and prev.get("og_image_hash") is not None
                   and img_hash != prev.get("og_image_hash"))
    likes_delta = None
    if "likes_total" in desc_fields and "likes_total" in prev:
        likes_delta = desc_fields["likes_total"] - prev["likes_total"]

    record = {
        "ts": now_bkk().isoformat(timespec="seconds"),
        "platform": "facebook_og",
        "page_slug": slug,
        "page_url": raw_url,
        "tier": page_cfg.get("tier"),
        "role": page_cfg.get("role"),
        "og_type": og.get("type"),
        "og_title": og.get("title"),
        "og_description": og.get("description"),
        "og_url": og.get("url"),
        "og_image_url": og.get("image"),
        "og_image_hash": img_hash,
        "og_locale": og.get("locale"),
        "likes_total": desc_fields.get("likes_total"),
        "talking_count": desc_fields.get("talking_count"),
        "contact_email": desc_fields.get("contact_email"),
        "website": desc_fields.get("website"),
        "diff_image_changed": img_changed,
        "diff_likes_delta": likes_delta,
        "raw_html_size_b": len(body),
    }

    if not dry_run:
        write_jsonl(record)
        last_seen[slug] = {
            "og_image_hash": img_hash,
            "likes_total": desc_fields.get("likes_total"),
            "captured_at": record["ts"],
        }

    stats.update({
        "status": "ok",
        "likes_total": desc_fields.get("likes_total"),
        "img_changed": img_changed,
        "likes_delta": likes_delta,
    })
    log_line(f"  {slug:<32} ok  likes={desc_fields.get('likes_total','-')}  "
             f"talking={desc_fields.get('talking_count','-')}  "
             f"img_changed={img_changed}  delta={likes_delta}")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", nargs="*", default=None,
                        help="restrict to specific Page URLs")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    policy = load_policy()
    if not policy.get("scan", {}).get("enable", False):
        log_line("[fb_og_meta_scan] scan disabled in policy — exit")
        return 0

    if args.pages:
        page_cfgs = [{"url": u, "tier": "manual", "role": None}
                     for u in args.pages]
    else:
        page_cfgs = [
            p for p in policy.get("kol_pages", [])
            if isinstance(p.get("url"), str) and p["url"].startswith("https://")
        ]

    last_seen = load_last_seen()
    log_line(f"[fb_og_meta_scan] start pages={len(page_cfgs)} "
             f"prior_state_keys={len(last_seen)} dry_run={args.dry_run}")

    counts = {"ok": 0, "fetch_error": 0, "no_og_metadata": 0, "other": 0}
    for cfg in page_cfgs:
        st = scan_page(cfg, last_seen, args.dry_run)
        s = st.get("status", "other")
        if s in counts:
            counts[s] += 1
        elif s.startswith("http_") and s != "http_200":
            counts["other"] += 1
        else:
            counts["other"] += 1
        time.sleep(random.uniform(*INTER_REQUEST_SLEEP_RANGE))

    if not args.dry_run:
        save_last_seen(last_seen)

    log_line(f"[fb_og_meta_scan] done ok={counts['ok']} "
             f"fetch_err={counts['fetch_error']} "
             f"no_meta={counts['no_og_metadata']} other={counts['other']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
