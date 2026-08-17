"""
Blacksite — Reddit scanner (PRAW read-only).

Mode: PRAW read_only (no user auth required, just client credentials).
Polls /new posts of configured subreddits + executes search queries on r/all;
writes matching posts (and optionally top-level comments) to JSONL.

Requires:
  REDDIT_CLIENT_ID     — from reddit.com/prefs/apps "create another app" → script
  REDDIT_CLIENT_SECRET — same
Both set in .env. Without them, listener exits cleanly with status message.

Output:
  instances/_TEMPLATE/runtime/raw/reddit/<YYYY-MM-DD>.jsonl

Usage:
  py agents/reddit/reddit_listen.py
  py agents/reddit/reddit_listen.py --subs example_subreddit_1 example_subreddit_2
  py agents/reddit/reddit_listen.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
POLICY_PATH = INSTANCE_DIR / "policy" / "reddit_subs.yaml"
RAW_DIR = INSTANCE_DIR / "runtime" / "raw" / "reddit"
LOG_DIR = INSTANCE_DIR / "runtime" / "logs"
SEEN_PATH = INSTANCE_DIR / "runtime" / "reddit_seen_posts.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))


def now_bkk() -> datetime:
    return datetime.now(TZ)


def log_line(msg: str) -> None:
    print(msg, flush=True)
    log_path = LOG_DIR / f"reddit_{now_bkk().strftime('%Y-%m-%d')}.log"
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


def matches_filter(text: str, keywords: list[str]) -> list[str]:
    if not text:
        return []
    t = text.lower()
    return [kw for kw in keywords if kw.lower() in t]


def serialize_post(submission, sub_name: str, tier: str, hits: list[str], policy: dict):
    record = {
        "ts": now_bkk().isoformat(timespec="seconds"),
        "platform": "reddit",
        "kind": "post",
        "sub": sub_name,
        "tier": tier,
        "post_id": submission.id,
        "title": submission.title,
        "selftext": submission.selftext or "",
        "author": str(submission.author) if submission.author else "[deleted]",
        "posted_at": submission.created_utc,
        "score": submission.score,
        "upvote_ratio": submission.upvote_ratio,
        "num_comments": submission.num_comments,
        "url": submission.url,
        "permalink": f"https://www.reddit.com{submission.permalink}",
        "flair": submission.link_flair_text,
        "filter_hits": hits,
    }
    if policy["output"].get("capture_top_level_comments"):
        comments = []
        try:
            submission.comments.replace_more(limit=0)
            max_cmt = policy["scan"].get("per_run_max_comments_per_post", 200)
            for c in submission.comments[:max_cmt]:
                comments.append({
                    "comment_id": c.id,
                    "parent_id": c.parent_id,
                    "author": str(c.author) if c.author else "[deleted]",
                    "body": c.body,
                    "posted_at": c.created_utc,
                    "score": c.score,
                })
        except Exception as e:
            log_line(f"[comments] {submission.id} err: {type(e).__name__}: {e}")
        record["comments"] = comments
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subs", nargs="*")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        log_line(
            "[reddit_listen] REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET missing in "
            ".env — exit. Boss: create script-type app at reddit.com/prefs/apps "
            "(see CHECKPOINT V6)."
        )
        return

    try:
        import praw
    except ImportError:
        log_line("[reddit_listen] praw not installed — `py -m pip install praw`")
        return

    policy = load_policy()
    if not policy.get("scan", {}).get("enable", False):
        log_line("[reddit_listen] scan disabled — exit")
        return

    seen = load_seen()
    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=policy["scan"]["praw_user_agent"],
    )
    reddit.read_only = True

    # Build sub list from policy
    if args.subs:
        sub_list = [(s, "white") for s in args.subs]
    else:
        sub_list = []
        for cat_name, cat in policy["subreddits"].items():
            tier = cat["tier"]
            for s in cat["subs"]:
                sub_list.append((s, tier))

    fk = policy["filter_keywords"]
    max_posts = policy["scan"]["per_run_max_posts_per_sub"]

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] reddit_listen start "
        f"subs={len(sub_list)} seen={len(seen)} dry_run={args.dry_run}"
    )

    totals = {"posts": 0, "kept": 0, "errs": 0}

    # Pass 1: /new from each sub
    for sub_name, tier in sub_list:
        try:
            subreddit = reddit.subreddit(sub_name)
            kept = 0
            for sub in subreddit.new(limit=max_posts):
                if sub.id in seen:
                    continue
                seen.add(sub.id)
                totals["posts"] += 1
                blob = (sub.title or "") + " " + (sub.selftext or "")
                hits = matches_filter(blob, fk)
                if not hits:
                    continue
                record = serialize_post(sub, sub_name, tier, hits, policy)
                if not args.dry_run:
                    write_jsonl(record)
                kept += 1
                totals["kept"] += 1
            log_line(f"[/new] r/{sub_name:<22} {tier:<6} kept={kept}")
        except Exception as e:
            totals["errs"] += 1
            log_line(f"[/new] r/{sub_name} ERR: {type(e).__name__}: {e}")

    # Pass 2: search queries on r/all
    sq = policy.get("search_queries", {})
    all_queries = []
    for qs in sq.values():
        all_queries.extend(qs)
    log_line(f"[search] running {len(all_queries)} queries on r/all (week)")
    try:
        all_sub = reddit.subreddit("all")
        for q in all_queries:
            kept_q = 0
            try:
                for sub in all_sub.search(q, sort="new", time_filter="week", limit=25):
                    if sub.id in seen:
                        continue
                    seen.add(sub.id)
                    totals["posts"] += 1
                    blob = (sub.title or "") + " " + (sub.selftext or "")
                    hits = matches_filter(blob, fk)
                    if not hits:
                        hits = [f"search_only:{q}"]
                    record = serialize_post(
                        sub, sub.subreddit.display_name, "search", hits, policy
                    )
                    if not args.dry_run:
                        write_jsonl(record)
                    kept_q += 1
                    totals["kept"] += 1
                log_line(f"[search] {q[:40]:<40} kept={kept_q}")
            except Exception as e:
                totals["errs"] += 1
                log_line(f"[search] {q!r} ERR: {type(e).__name__}: {e}")
    except Exception as e:
        log_line(f"[search] outer ERR: {type(e).__name__}: {e}")

    if not args.dry_run:
        save_seen(seen)

    log_line(
        f"[{now_bkk().isoformat(timespec='seconds')}] reddit_listen done "
        f"posts_seen={totals['posts']} kept={totals['kept']} errs={totals['errs']}"
    )


if __name__ == "__main__":
    main()
