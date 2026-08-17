"""Generic public target scanner for policy-backed anonymous agents."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import yaml

try:
    import requests
except ImportError:  # pragma: no cover - runtime fallback
    requests = None

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
INSTANCE_DIR = ROOT / "instances" / ACTIVE_INSTANCE
RUNTIME = INSTANCE_DIR / "runtime"
RAW_DIR = RUNTIME / "raw"
LOG_DIR = RUNTIME / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def out_paths(agent_id: str, raw_subdir: str | None) -> list[Path]:
    today = now().strftime("%Y-%m-%d")
    paths = [RAW_DIR / agent_id / f"{today}.jsonl"]
    if raw_subdir and raw_subdir != agent_id:
        paths.append(RAW_DIR / raw_subdir / f"{today}.jsonl")
    return paths


def emit(record: dict, agent_id: str, raw_subdir: str | None) -> None:
    for path in out_paths(agent_id, raw_subdir):
        append_jsonl(path, record)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_d = dict(attrs)
        href = attrs_d.get("href")
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        text = re.sub(r"\s+", " ", html.unescape(" ".join(self._text))).strip()
        self.links.append({"href": self._href, "text": text})
        self._href = None
        self._text = []


def fetch(url: str, timeout: int = 20) -> tuple[int, str]:
    if requests is None:
        from urllib.request import Request, urlopen

        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - policy targets only
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    return int(resp.status_code), resp.text


def load_policy(filename: str) -> dict:
    path = INSTANCE_DIR / "policy" / filename
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def scan_target(
    *,
    target: dict,
    agent_id: str,
    platform: str,
    raw_subdir: str | None,
    work_order_id: str | None,
    task_focus: str | None,
    limit: int,
) -> int:
    name = target.get("name") or target.get("label") or target.get("url")
    url = target["url"]
    try:
        status, body = fetch(url)
    except Exception as exc:
        emit(
            {
                "ts": now_iso(),
                "event": "collector_status",
                "kind": "collector_status",
                "platform": platform,
                "agent_id": agent_id,
                "work_order_id": work_order_id,
                "task_focus": task_focus,
                "target": name,
                "url": url,
                "status": f"fetch_error:{type(exc).__name__}",
            },
            agent_id,
            raw_subdir,
        )
        return 0

    parser = LinkParser()
    parser.feed(body[:2_000_000])
    seen: set[str] = set()
    emitted = 0
    for link in parser.links:
        text = link.get("text") or ""
        href = link.get("href") or ""
        if len(text) < 5 or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        full_url = urljoin(url, href)
        key = hashlib.sha1(f"{name}|{full_url}|{text}".encode("utf-8")).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        emit(
            {
                "ts": now_iso(),
                "event": "policy_target_item",
                "kind": "policy_target_item",
                "platform": platform,
                "agent_id": agent_id,
                "work_order_id": work_order_id,
                "task_focus": task_focus,
                "target": name,
                "target_type": target.get("type"),
                "source_url": url,
                "item_id": key,
                "title": text[:500],
                "url": full_url,
                "http_status": status,
            },
            agent_id,
            raw_subdir,
        )
        emitted += 1
        if emitted >= limit:
            break

    if emitted == 0:
        emit(
            {
                "ts": now_iso(),
                "event": "collector_status",
                "kind": "collector_status",
                "platform": platform,
                "agent_id": agent_id,
                "work_order_id": work_order_id,
                "task_focus": task_focus,
                "target": name,
                "url": url,
                "status": f"http_{status}_zero_items",
            },
            agent_id,
            raw_subdir,
        )
    return emitted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--raw-subdir")
    parser.add_argument("--work-order-id")
    parser.add_argument("--task-focus")
    parser.add_argument("--limit-per-target", type=int, default=12)
    parser.add_argument("--ignore-disabled", action="store_true")
    parser.add_argument("--target", nargs="*")
    args = parser.parse_args()

    policy = load_policy(args.policy)
    if not args.ignore_disabled and not policy.get("scan", {}).get("enable", True):
        emit(
            {
                "ts": now_iso(),
                "event": "collector_status",
                "kind": "collector_status",
                "platform": args.platform,
                "agent_id": args.agent_id,
                "work_order_id": args.work_order_id,
                "task_focus": args.task_focus,
                "status": "policy_disabled",
                "policy": args.policy,
            },
            args.agent_id,
            args.raw_subdir,
        )
        return 0

    targets = list(policy.get("targets") or [])
    if args.target:
        wanted = set(args.target)
        targets = [
            t for t in targets
            if (t.get("name") in wanted or t.get("label") in wanted or t.get("url") in wanted)
        ]

    total = 0
    for target in targets:
        if not target.get("url"):
            continue
        total += scan_target(
            target=target,
            agent_id=args.agent_id,
            platform=args.platform,
            raw_subdir=args.raw_subdir,
            work_order_id=args.work_order_id,
            task_focus=args.task_focus,
            limit=args.limit_per_target,
        )
    print(json.dumps({"agent_id": args.agent_id, "targets": len(targets), "items": total}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
