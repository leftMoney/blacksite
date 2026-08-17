"""scripts/org.py — boss organization-activity dashboard (5/5 ship Phase C).

5 個問題收斂成一條：組織內部都在動（小主管評估、策略長指示、agent memory）但
boss 沒入口看 → 等於不存在。本 CLI 是**摘要 + 點開細節**入口（per CLAUDE.md
boss 5/5 directive: 「不要全部資訊都貼上來」）。

Subcommands:
  status                              # 一頁式：今日組織活動摘要（30 行內）
  meetings [--since 7d]               # 小主管 KPI eval + 策略長 memo 活動列表
  directives [--unprocessed]          # 策略長 directives 列表 + 處理狀態
  memory --new <window>               # 列本週 mtime 變動的 agent memory
  memory <agent_id>                   # 摘要該 agent (我的經驗 + Boss curated)，
                                      #   非 full dump（用 agents.py memory 才是 full）

Boss invocation:
  py scripts/org.py status
  py scripts/org.py meetings --since 7d
  py scripts/org.py directives
  py scripts/org.py memory --new 7d
  py scripts/org.py memory P03_Bigo
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TZ = timezone(timedelta(hours=7))
ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
KPI_DIR = RUNTIME_DIR / "agent_kpi"
INCIDENTS_DIR = RUNTIME_DIR / "agent_incidents"
MEMORY_DIR = RUNTIME_DIR / "agent_memory"
DIRECTIVES_DIR = RUNTIME_DIR / "strategy_directives"
MEMOS_DIR = RUNTIME_DIR / "strategy_memos"
DIGEST_DIR = RUNTIME_DIR / "strategist_digest"
INDEX_DB = RUNTIME_DIR / "index.db"


def now() -> datetime:
    return datetime.now(TZ)


def _today_iso() -> str:
    return now().date().isoformat()


def _parse_window(s: str) -> timedelta:
    m = re.match(r"^(\d+)([hdw])$", s.strip().lower())
    if not m:
        return timedelta(days=7)
    n, unit = int(m.group(1)), m.group(2)
    return {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


# ====================================================================
# Data extractors
# ====================================================================

def section_chief_activity(today_only: bool = True) -> dict:
    """How many agents evaluated today, by which chief."""
    today = _today_iso()
    chiefs: dict[str, int] = {}
    total = 0
    for yp in KPI_DIR.glob("*.yaml"):
        try:
            d = yaml.safe_load(yp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        ts = (d.get("last_evaluated_at") or "")[:10]
        if today_only and ts != today:
            continue
        chief = d.get("last_evaluated_by") or "SECTION_CHIEF"
        chiefs[chief] = chiefs.get(chief, 0) + 1
        total += 1
    return {"total_evaluated": total, "by_chief": chiefs, "date": today}


def incidents_today() -> dict:
    """Incidents opened today + state breakdown."""
    today = _today_iso()
    states: dict[str, int] = {}
    opened_today = 0
    all_incidents = []
    for ip in INCIDENTS_DIR.glob("INC-*.md"):
        try:
            text = ip.read_text(encoding="utf-8")
            fm = _parse_frontmatter(text)
        except Exception:
            continue
        all_incidents.append(fm)
        st = fm.get("state", "unknown")
        states[st] = states.get(st, 0) + 1
        if (fm.get("opened_at") or "")[:10] == today:
            opened_today += 1
    return {
        "opened_today": opened_today,
        "states": states,
        "total": len(all_incidents),
    }


def _parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except Exception:
        return {}


def _load_directive_yaml(path: Path) -> dict:
    """strategy_directives/*.yaml uses `---` separator: frontmatter first
    document (issued_by / expires_at) + body second document (directives:
    list). yaml.safe_load_all merges both into one dict for the caller."""
    text = path.read_text(encoding="utf-8")
    docs = list(yaml.safe_load_all(text))
    merged: dict = {}
    for d in docs:
        if isinstance(d, dict):
            merged.update(d)
    return merged


def strategist_status() -> dict:
    """Latest memo + latest directives + unprocessed-ish count."""
    memos = sorted(MEMOS_DIR.glob("*.md")) if MEMOS_DIR.exists() else []
    last_memo = memos[-1] if memos else None
    memo_info = None
    if last_memo:
        m_mtime = datetime.fromtimestamp(last_memo.stat().st_mtime, tz=TZ)
        first_line = last_memo.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0][:80]
        memo_info = {
            "name": last_memo.name,
            "mtime": m_mtime.isoformat(timespec="seconds"),
            "first_line": first_line,
        }

    directives = sorted(DIRECTIVES_DIR.glob("*.yaml")) if DIRECTIVES_DIR.exists() else []
    last_dir = directives[-1] if directives else None
    dir_info = None
    if last_dir:
        try:
            d_doc = _load_directive_yaml(last_dir)
            kinds = [it.get("kind") for it in (d_doc.get("directives") or []) if isinstance(it, dict)]
            dir_info = {
                "name": last_dir.name,
                "directive_count": len(kinds),
                "kinds": kinds,
                "issued_for": d_doc.get("issued_for", "?"),
                "expires_at": d_doc.get("expires_at", "?"),
            }
        except Exception as e:
            dir_info = {"name": last_dir.name, "error": str(e)[:80]}

    digests = sorted(DIGEST_DIR.glob("*.md")) if DIGEST_DIR.exists() else []
    return {"last_memo": memo_info, "last_directive_file": dir_info,
            "memo_count": len(memos), "directive_files": len(directives),
            "digest_count": len(digests)}


def memory_freshness(window: timedelta) -> list[dict]:
    """List agent memory files updated within window (mtime)."""
    cutoff = now() - window
    out = []
    if not MEMORY_DIR.exists():
        return out
    for mp in MEMORY_DIR.glob("*.md"):
        mtime = datetime.fromtimestamp(mp.stat().st_mtime, tz=TZ)
        if mtime < cutoff:
            continue
        try:
            text = mp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Extract token count from frontmatter / sections (rough)
        size_chars = len(text)
        # Count "我的經驗" + "Boss curated" non-empty lines as proxy for learnings
        learning_lines = _count_learning_lines(text)
        out.append({
            "agent_id": mp.stem,
            "mtime": mtime.isoformat(timespec="seconds"),
            "size_chars": size_chars,
            "learning_lines": learning_lines,
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


def _count_learning_lines(text: str) -> int:
    """Count non-empty bullet/dash lines in 我的經驗 + Boss curated sections."""
    count = 0
    in_section = False
    for line in text.split("\n"):
        if line.startswith("# 我的經驗") or line.startswith("# Boss curated"):
            in_section = True
            continue
        if line.startswith("# "):
            in_section = False
            continue
        if in_section and line.strip().startswith(("-", "*", "•")):
            count += 1
    return count


def memory_summary(agent_id: str) -> dict | None:
    """Read agent memory; return frontmatter + 我的經驗 + Boss curated sections only."""
    candidates = list(MEMORY_DIR.glob(f"{agent_id}*.md"))
    if not candidates:
        return None
    mp = candidates[0]
    text = mp.read_text(encoding="utf-8", errors="replace")
    fm = _parse_frontmatter(text)
    sections = _extract_sections(text, ["我的經驗", "Boss curated"])
    return {"agent_id": mp.stem, "path": str(mp.relative_to(ROOT)),
            "frontmatter": fm, "sections": sections}


def _extract_sections(text: str, headings: list[str]) -> dict[str, str]:
    out = {h: "" for h in headings}
    current = None
    buf: list[str] = []
    for line in text.split("\n"):
        m = re.match(r"^#\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                out[current] = "\n".join(buf).strip()
            heading = m.group(1).strip()
            current = heading if heading in headings else None
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def library_state() -> dict:
    """sqlite library snapshot."""
    if not INDEX_DB.exists():
        return {"err": "index.db missing"}
    out = {}
    db = sqlite3.connect(str(INDEX_DB))
    for tbl in ("kb_chunks", "kb_documents", "kb_leads", "boss_opinions"):
        try:
            n = db.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            out[tbl] = n
        except Exception:
            out[tbl] = "missing"
    db.close()
    return out


# ====================================================================
# Renderers
# ====================================================================

def cmd_status() -> None:
    print(f"=== 組織活動 status @ {now().isoformat(timespec='seconds')} ===\n")

    sc = section_chief_activity(today_only=True)
    inc = incidents_today()
    print("▌ 小主管（SECTION_CHIEF）")
    print(f"  - 今日 KPI eval: {sc['total_evaluated']} agents 評過", end="")
    if sc["by_chief"]:
        chiefs = ", ".join(f"{k}={v}" for k, v in sc["by_chief"].items())
        print(f"  ({chiefs})")
    else:
        print("  ⚠ 今日無評估")
    print(f"  - 今日新開 incident: {inc['opened_today']} 條 / 全部 {inc['total']} 條 "
          f"({', '.join(f'{k}={v}' for k,v in inc['states'].items())})")
    print("  - 細節: py scripts/org.py meetings --since 24h")
    print("")

    s = strategist_status()
    print("▌ 策略長（CHIEF_STRATEGIST）")
    if s["last_memo"]:
        print(f"  - 上次 memo: {s['last_memo']['name']} @ {s['last_memo']['mtime']}")
    else:
        print("  - ⚠ 沒有 memo 產出")
    if s["last_directive_file"]:
        d = s["last_directive_file"]
        if "error" in d:
            print(f"  - 最新 directives: {d['name']} (parse err: {d['error']})")
        else:
            kinds_summary = ", ".join(set(d.get("kinds") or [])) or "(空)"
            print(f"  - 最新 directives: {d['name']} → {d['directive_count']} 條 ({kinds_summary})")
    else:
        print("  - ⚠ 沒有 directives 產出")
    print(f"  - 累計：{s['memo_count']} memos / {s['directive_files']} directive files / {s['digest_count']} digests")
    print("  - 細節: py scripts/org.py directives  /  py scripts/org.py meetings --since 7d")
    print("")

    fresh = memory_freshness(timedelta(days=7))
    print("▌ Agent learnings (7d 內 memory mtime 變動)")
    if not fresh:
        print("  - ⚠ 過去 7d 沒有任何 agent memory 更新（learning_added 機制可能沒接上）")
    else:
        for f in fresh[:5]:
            print(f"  - {f['agent_id']}: mtime={f['mtime'][11:16]} learning_lines={f['learning_lines']}")
        if len(fresh) > 5:
            print(f"  - ... 共 {len(fresh)} 個 (細節: org.py memory --new 7d)")
    print("")

    lib = library_state()
    print("▌ 書庫 (library) 狀態")
    parts = []
    for k in ("kb_chunks", "kb_documents", "kb_leads", "boss_opinions"):
        parts.append(f"{k}={lib.get(k, '?')}")
    print(f"  - {' '.join(parts)}")
    if lib.get("kb_chunks") in (0, "missing") and lib.get("kb_documents") in (0, "missing"):
        print("  - 🔴 書庫未 ingest（library_ingest pipeline 未上線 — Phase A 待做）")
    print("")


def cmd_meetings(since: str) -> None:
    window = _parse_window(since)
    cutoff = now() - window
    print(f"=== meetings since {cutoff.isoformat(timespec='seconds')} ===\n")

    print("▌ Section Chief KPI eval activity")
    rows = []
    for yp in KPI_DIR.glob("*.yaml"):
        try:
            d = yaml.safe_load(yp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        ts_str = d.get("last_evaluated_at") or ""
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        if ts < cutoff:
            continue
        rows.append((ts_str, yp.stem, d.get("status", "?"),
                     d.get("last_evaluated_by", "SECTION_CHIEF")))
    rows.sort(reverse=True)
    if not rows:
        print("  (no eval in window)")
    else:
        for ts, agent, status, chief in rows[:30]:
            print(f"  [{ts[:16]}] {chief:18s} → {agent:25s} status={status}")
        if len(rows) > 30:
            print(f"  ... +{len(rows) - 30} more")
    print("")

    print("▌ Strategist memos in window")
    if MEMOS_DIR.exists():
        for mp in sorted(MEMOS_DIR.glob("*.md")):
            mtime = datetime.fromtimestamp(mp.stat().st_mtime, tz=TZ)
            if mtime < cutoff:
                continue
            print(f"  [{mtime.isoformat(timespec='seconds')[:16]}] {mp.name}")
    print("")

    print("▌ Strategist directives in window")
    if DIRECTIVES_DIR.exists():
        for dp in sorted(DIRECTIVES_DIR.glob("*.yaml")):
            mtime = datetime.fromtimestamp(dp.stat().st_mtime, tz=TZ)
            if mtime < cutoff:
                continue
            try:
                d_doc = _load_directive_yaml(dp)
                n = len(d_doc.get("directives") or [])
            except Exception:
                n = -1
            print(f"  [{mtime.isoformat(timespec='seconds')[:16]}] {dp.name} ({n} directives)")
    print("")


def cmd_directives(unprocessed_only: bool) -> None:
    print(f"=== strategist directives ===\n")
    if not DIRECTIVES_DIR.exists():
        print("(directives dir missing)")
        return
    today = _today_iso()
    for dp in sorted(DIRECTIVES_DIR.glob("*.yaml"), reverse=True)[:10]:
        try:
            d_doc = _load_directive_yaml(dp)
        except Exception as e:
            print(f"⚠ {dp.name}: parse err {e}")
            continue
        expires = d_doc.get("expires_at", "?")
        is_unprocessed = expires != "?" and expires[:10] >= today
        if unprocessed_only and not is_unprocessed:
            continue
        print(f"📄 {dp.name}  issued_for={d_doc.get('issued_for', '?')}  expires={expires[:10]}")
        for it in (d_doc.get("directives") or [])[:5]:
            kind = it.get("kind", "?")
            ag = it.get("agent_id") or it.get("target") or ""
            rationale = (it.get("rationale") or "")[:80].replace("\n", " ").strip()
            print(f"  - {kind:25s} {ag:18s}  {rationale}")
        rest = len(d_doc.get("directives") or []) - 5
        if rest > 0:
            print(f"  ... +{rest} more (cat {dp.relative_to(ROOT).as_posix()})")
        print("")


def cmd_memory_new(window_str: str) -> None:
    window = _parse_window(window_str)
    fresh = memory_freshness(window)
    print(f"=== agent memory mtime change in last {window_str} ({len(fresh)} agents) ===\n")
    if not fresh:
        print("(none)")
        print("\n⚠ note: mtime 變動不等於有新 learning；mtime 可能只是被 _llm_synth 重寫")
        print("    要確認真有新 learning，可以 diff git history 或加 system_history learning_added kind (Phase B)")
        return
    for f in fresh:
        print(f"  {f['agent_id']:25s} mtime={f['mtime'][:16]} chars={f['size_chars']:>5} "
              f"learning_lines={f['learning_lines']}")


def cmd_memory_summary(agent_id: str) -> None:
    res = memory_summary(agent_id)
    if not res:
        print(f"⚠ no memory file matching {agent_id!r} in {MEMORY_DIR}")
        return
    print(f"=== {res['agent_id']} ({res['path']}) ===\n")
    fm = res["frontmatter"]
    print(f"tier={fm.get('tier','?')} sub_class={fm.get('sub_class','?')} "
          f"managed_by={fm.get('managed_by','?')} last_updated={fm.get('last_updated','?')}\n")
    for h, body in res["sections"].items():
        print(f"## {h}")
        if not body.strip():
            print("  (empty — no learnings yet)")
        else:
            for line in body.split("\n"):
                if line.strip():
                    print(f"  {line}")
        print("")


# ====================================================================
# CLI
# ====================================================================

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="org.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="one-page org activity summary")

    p_meet = sub.add_parser("meetings", help="list section_chief evals + strategist memos")
    p_meet.add_argument("--since", default="24h", help="window: 24h, 7d, 2w (default 24h)")

    p_dir = sub.add_parser("directives", help="strategist directives")
    p_dir.add_argument("--unprocessed", action="store_true",
                       help="show only directives not yet expired")

    p_mem = sub.add_parser("memory", help="agent memory summary or freshness list")
    p_mem.add_argument("agent_id", nargs="?", default=None,
                       help="agent_id for summary (omit if --new)")
    p_mem.add_argument("--new", default=None, metavar="WINDOW",
                       help="list memories with mtime change within window (24h/7d/2w)")

    args = p.parse_args(argv)

    if args.cmd == "status":
        cmd_status()
    elif args.cmd == "meetings":
        cmd_meetings(args.since)
    elif args.cmd == "directives":
        cmd_directives(args.unprocessed)
    elif args.cmd == "memory":
        if args.new:
            cmd_memory_new(args.new)
        elif args.agent_id:
            cmd_memory_summary(args.agent_id)
        else:
            p.error("memory: provide <agent_id> or --new <window>")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
