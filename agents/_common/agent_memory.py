"""agents/_common/agent_memory.py — self-iterating agent memory layer.

Per CLAUDE.md §15 + boss 5/3 directive: every Tier 1/2/3 agent gets a
markdown memory file with frontmatter that the engine reads on every init,
appends learnings to over time, and LRU-evicts when token cap reached.

Memory ≠ skill. Skill (FIELD_AGENT.md / SECTION_CHIEF.md / CHIEF_STRATEGIST.md)
is loaded separately as system_prompt_prefix and IS the agent's identity +
SOP. Memory is the agent's accumulated experience: what worked / what didn't
/ boss-curated lessons. Separate budgets:
  - Tier 1 Field Agent:    6,000 tokens
  - Tier 2 Section Chief:  12,000 tokens
  - Tier 3 Chief Strategist: 25,000 tokens

File format: markdown with YAML frontmatter at
`instances/<active>/runtime/agent_memory/<agent_id>.md`.

Token estimator: rough ~2 chars/token (Chinese-heavy text). Use
`len(text) // 2`. Boss-locked simple metric, no tokenizer dep.

Sections in markdown body (parsed by `## ` headers):
  # 我是誰         — fixed identity (≤100 chars)
  # 我在做什麼     — current job (≤200 chars)
  # KPI 目標       — auto-sync from KPI yaml
  # 我的能力 / 工具 — tool list
  # 我的經驗       — append-only learnings (LRU-evictable)
  # Boss curated   — never evicted

LRU eviction: chronological order within `# 我的經驗` section. Each learning
prefixed with ISO timestamp. Oldest non-Boss-curated entry truncated first.

Per CLAUDE.md §6.4: timestamps ISO 8601 with +07:00.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
MEMORY_DIR = ROOT / "instances" / ACTIVE_INSTANCE / "runtime" / "agent_memory"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)

# 5/18 security: cross-cycle injection gate (CLAUDE.md §15.Y boss
# 5/3 + 5/18 follow-up). All LLM-generated learnings queue here
# instead of writing live memory; boss approves via CLI before they
# enter the agent's read-back surface. Without this gate, a single
# prompt-injection in Strategist week-N propagates to Strategist
# week-N+1 via its own memory file.
PENDING_DIR = MEMORY_DIR / "_pending"
PENDING_DIR.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

TIER_BUDGETS = {1: 6000, 2: 12000, 3: 25000}
DEFAULT_TIER = 1

_FM_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_SECTION_RE = re.compile(r"^# (.+?)$", re.MULTILINE)
_LEARNING_PREFIX_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})\]")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _path_for(agent_id: str) -> Path:
    return MEMORY_DIR / f"{agent_id}.md"


def estimate_tokens(text: str) -> int:
    """Rough estimator: 2 chars/token (Chinese-heavy fallback)."""
    return max(1, len(text) // 2)


def get_budget(agent_id: str) -> int:
    """Return token budget from frontmatter; fallback to tier default
    inferred from filename / agent_id (SECTION_CHIEF / CHIEF_STRATEGIST
    pattern)."""
    p = _path_for(agent_id)
    if p.exists():
        fm, _ = _parse(p.read_text(encoding="utf-8"))
        if fm and isinstance(fm.get("token_budget"), int):
            return fm["token_budget"]
        tier = fm.get("tier") if fm else None
        if isinstance(tier, int) and tier in TIER_BUDGETS:
            return TIER_BUDGETS[tier]
    if agent_id.upper().startswith("CHIEF_STRATEGIST"):
        return TIER_BUDGETS[3]
    if agent_id.upper().startswith("SECTION_CHIEF"):
        return TIER_BUDGETS[2]
    return TIER_BUDGETS[DEFAULT_TIER]


def _parse(raw: str) -> tuple[dict, str]:
    m = _FM_RE.match(raw)
    if not m:
        return {}, raw
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, m.group(2)


def _serialize(fm: dict, body: str) -> str:
    fm_str = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{fm_str}---\n{body}"


def load(agent_id: str) -> str:
    """Read memory md verbatim (frontmatter + body). Returns empty string
    if file missing — caller MAY treat as cold-start signal."""
    p = _path_for(agent_id)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Split body into [(header, content), ...] list. Header includes
    leading '# '. content excludes the header line + trailing newline."""
    parts: list[tuple[str, str]] = []
    cur_header = ""
    cur_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("# "):
            if cur_header or cur_lines:
                parts.append((cur_header, "\n".join(cur_lines).strip()))
            cur_header = line
            cur_lines = []
        else:
            cur_lines.append(line)
    if cur_header or cur_lines:
        parts.append((cur_header, "\n".join(cur_lines).strip()))
    return parts


def _join_sections(sections: list[tuple[str, str]]) -> str:
    out: list[str] = []
    for header, content in sections:
        if header:
            out.append(header)
        if content:
            out.append("")
            out.append(content)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _write_pending(
    agent_id: str,
    text: str,
    category: str,
    boss_curated: bool,
) -> str:
    """Drop one learning into PENDING_DIR/<ts>_<agent_id>.md awaiting boss
    approval. Returns the pending entry id (filename stem)."""
    ts = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
    # Make pending id unique even within the same second (multi-writer).
    pid = f"{ts}_{agent_id}_{abs(hash(text)) % 10_000_000:07d}"
    fm = {
        "pending_id": pid,
        "agent_id": agent_id,
        "category": category,
        "boss_curated_target": boss_curated,
        "queued_at": now_iso(),
    }
    body = f"# pending_learning\n\n{text.strip()}\n"
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    (PENDING_DIR / f"{pid}.md").write_text(_serialize(fm, body), encoding="utf-8")
    # Surface in system_history so boss sees it accumulating.
    try:
        from processors.history_log import log_event
        log_event(
            actor=agent_id, kind="warning", scope="agent_memory",
            title=f"{agent_id} learning queued (boss approval pending)",
            body=f"pending_id={pid}\ncategory={category}\n"
                 f"boss_curated_target={boss_curated}\n"
                 f"text={text.strip()[:500]}",
        )
    except Exception:
        pass
    return pid


def list_pending(agent_id: str | None = None) -> list[dict]:
    """List pending learnings; optionally filter to one agent_id."""
    out = []
    if not PENDING_DIR.exists():
        return out
    for p in sorted(PENDING_DIR.glob("*.md")):
        try:
            fm, body = _parse(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if agent_id and fm.get("agent_id") != agent_id:
            continue
        # Strip the leading "# pending_learning\n\n" header
        body_lines = body.splitlines()
        if body_lines and body_lines[0].startswith("# "):
            text = "\n".join(body_lines[2:]).strip()
        else:
            text = body.strip()
        out.append({
            "pending_id": p.stem,
            "agent_id": fm.get("agent_id"),
            "category": fm.get("category"),
            "boss_curated_target": fm.get("boss_curated_target", False),
            "queued_at": fm.get("queued_at"),
            "text": text,
        })
    return out


def approve_pending(pending_id: str) -> bool:
    """Boss approval: merge one pending learning into the live memory file.
    Returns True on success."""
    p = PENDING_DIR / f"{pending_id}.md"
    if not p.exists():
        return False
    fm, body = _parse(p.read_text(encoding="utf-8"))
    body_lines = body.splitlines()
    if body_lines and body_lines[0].startswith("# "):
        text = "\n".join(body_lines[2:]).strip()
    else:
        text = body.strip()
    ok = append_learning(
        agent_id=fm["agent_id"],
        text=text,
        category=fm.get("category", "ops"),
        boss_curated=fm.get("boss_curated_target", False),
        boss_approved=True,
    )
    if ok:
        p.unlink(missing_ok=True)
        try:
            from processors.history_log import log_event
            log_event(
                actor="main", kind="config_change", scope="agent_memory",
                title=f"boss approved {pending_id}",
                body=f"agent_id={fm['agent_id']}\ncategory={fm.get('category')}\n"
                     f"text={text[:500]}",
            )
        except Exception:
            pass
    return ok


def reject_pending(pending_id: str, reason: str = "") -> bool:
    """Boss rejection: delete the pending entry; log to system_history."""
    p = PENDING_DIR / f"{pending_id}.md"
    if not p.exists():
        return False
    fm, _ = _parse(p.read_text(encoding="utf-8"))
    p.unlink(missing_ok=True)
    try:
        from processors.history_log import log_event
        log_event(
            actor="main", kind="config_change", scope="agent_memory",
            title=f"boss rejected {pending_id}",
            body=f"agent_id={fm.get('agent_id')}\nreason={reason[:300]}",
        )
    except Exception:
        pass
    return True


def append_learning(
    agent_id: str,
    text: str,
    category: str = "ops",
    boss_curated: bool = False,
    *,
    boss_approved: bool = False,
) -> bool:
    """Append a learning to `# 我的經驗` (or `# Boss curated` if boss_curated=True).
    Auto-creates file if missing using minimal stub. Returns True on success.

    5/18 security gate (CLAUDE.md §15.Y addendum): UNLESS the caller
    explicitly sets `boss_approved=True` OR `boss_curated=True`, the
    learning is QUEUED to PENDING_DIR rather than written live. Boss
    must approve via CLI (`py -m agents._common.agent_memory approve
    <pending_id>`) before it enters the agent's read-back surface.

    This breaks the Strategist-A → memory → Strategist-B amplification
    chain (prompt injection upstream cannot self-propagate across runs).
    Callers that legitimately need immediate write (e.g. boss-curated
    notes from main session, manual debugging) pass `boss_approved=True`
    explicitly.
    """
    # Pending gate: anything not explicitly boss-approved queues for review.
    if not (boss_approved or boss_curated):
        pid = _write_pending(agent_id, text, category, boss_curated=False)
        # Return True so callers' control flow stays normal; visibility is
        # through the pending queue + system_history warning row.
        return True

    p = _path_for(agent_id)
    if not p.exists():
        # Minimal stub — caller should normally pre-create via stub generator
        body = (
            "# 我是誰\n\n(unknown)\n\n"
            "# 我在做什麼\n\n(unknown)\n\n"
            "# 我的經驗\n\n"
            "# Boss curated\n\n"
        )
        fm = {
            "agent_id": agent_id,
            "tier": DEFAULT_TIER,
            "token_budget": TIER_BUDGETS[DEFAULT_TIER],
            "last_updated": now_iso(),
            "last_compacted": None,
            "managed_by": None,
        }
        p.write_text(_serialize(fm, body), encoding="utf-8")

    raw = p.read_text(encoding="utf-8")
    fm, body = _parse(raw)
    sections = _split_sections(body)

    target_header = "# Boss curated" if boss_curated else "# 我的經驗"
    line = f"- [{now_iso()}] [{category}] {text.strip()}"

    found = False
    for i, (header, content) in enumerate(sections):
        if header.strip() == target_header:
            new_content = (content + "\n" + line).strip() if content else line
            sections[i] = (header, new_content)
            found = True
            break
    if not found:
        sections.append((target_header, line))

    fm["last_updated"] = now_iso()
    p.write_text(_serialize(fm, _join_sections(sections)), encoding="utf-8")

    # Phase B (5/5): organization audit trail. Surfaces in scripts/org.py
    # memory --new + daily brief 「🏛️ 組織狀態」. Best-effort: if history_log
    # write fails, learning is still saved on disk.
    try:
        from processors.history_log import log_event
        log_event(
            actor=agent_id,
            kind="learning_added",
            scope="agent_memory",
            title=f"{agent_id} +1 {'boss-curated' if boss_curated else category}",
            body=f"agent_id={agent_id}\ncategory={category}\n"
                 f"boss_curated={boss_curated}\ntext={text.strip()[:500]}",
        )
    except Exception:
        pass

    return True


def compact(agent_id: str, target_tokens: int | None = None) -> dict:
    """LRU-evict oldest entries from `# 我的經驗` until full text fits budget.
    `# Boss curated` entries NEVER evicted. Returns summary dict."""
    p = _path_for(agent_id)
    if not p.exists():
        return {"agent_id": agent_id, "skipped": "file_missing"}

    raw = p.read_text(encoding="utf-8")
    fm, body = _parse(raw)
    budget = target_tokens if target_tokens is not None else get_budget(agent_id)

    initial_tokens = estimate_tokens(raw)
    if initial_tokens <= budget:
        return {
            "agent_id": agent_id,
            "tokens": initial_tokens,
            "budget": budget,
            "evicted": 0,
            "skipped": "under_budget",
        }

    sections = _split_sections(body)
    # Find learnings section
    learnings_idx = None
    for i, (h, _) in enumerate(sections):
        if h.strip() == "# 我的經驗":
            learnings_idx = i
            break
    if learnings_idx is None:
        return {
            "agent_id": agent_id,
            "tokens": initial_tokens,
            "budget": budget,
            "evicted": 0,
            "skipped": "no_learnings_section",
        }

    header, content = sections[learnings_idx]
    lines = [ln for ln in content.splitlines() if ln.strip()]
    # Sort by leading timestamp prefix (oldest first); lines without prefix
    # treated as oldest (no timestamp = pre-existing structure).
    def _ts_key(ln: str) -> str:
        m = _LEARNING_PREFIX_RE.match(ln)
        return m.group(1) if m else "0"

    lines_sorted = sorted(lines, key=_ts_key)

    evicted = 0
    while lines_sorted:
        sections[learnings_idx] = (header, "\n".join(lines_sorted).strip())
        candidate = _serialize(fm, _join_sections(sections))
        if estimate_tokens(candidate) <= budget:
            break
        # Evict oldest
        lines_sorted.pop(0)
        evicted += 1

    fm["last_compacted"] = now_iso()
    fm["last_updated"] = now_iso()
    sections[learnings_idx] = (header, "\n".join(lines_sorted).strip())
    final_text = _serialize(fm, _join_sections(sections))
    p.write_text(final_text, encoding="utf-8")
    return {
        "agent_id": agent_id,
        "tokens_before": initial_tokens,
        "tokens_after": estimate_tokens(final_text),
        "budget": budget,
        "evicted": evicted,
    }


def inject_into_extra_system(agent_id: str, base_extra_system: str = "") -> str:
    """Helper for LLM agents: prepend memory banner + memory text to
    base_extra_system. Compacts in-place if memory exceeds budget before
    injection so the prompt itself stays bounded."""
    budget = get_budget(agent_id)
    p = _path_for(agent_id)
    if p.exists():
        # Compact if needed (idempotent-no-op when under budget)
        compact(agent_id, target_tokens=budget)
    memory_text = load(agent_id)
    if not memory_text:
        return base_extra_system
    banner = (
        f"## 我的記憶 (auto-loaded from agent_memory/{agent_id}.md)\n\n"
        f"{memory_text}\n\n---\n\n"
    )
    if base_extra_system:
        return banner + base_extra_system
    return banner


def list_memory_files() -> list[str]:
    """Enumerate agent_ids that have memory files."""
    return sorted(p.stem for p in MEMORY_DIR.glob("*.md"))


# ---------------------------------------------------------------------------
# Frontmatter helpers (used by stub generator + chief lifecycle)
# ---------------------------------------------------------------------------

def get_frontmatter(agent_id: str) -> dict:
    p = _path_for(agent_id)
    if not p.exists():
        return {}
    fm, _ = _parse(p.read_text(encoding="utf-8"))
    return fm or {}


def update_frontmatter(agent_id: str, **kwargs) -> bool:
    """Merge kwargs into frontmatter; rewrite file. Returns True on success."""
    p = _path_for(agent_id)
    if not p.exists():
        return False
    fm, body = _parse(p.read_text(encoding="utf-8"))
    fm.update(kwargs)
    fm["last_updated"] = now_iso()
    p.write_text(_serialize(fm, body), encoding="utf-8")
    return True


def write_stub(
    agent_id: str,
    *,
    tier: int,
    sub_class: str | None = None,
    identity: str = "",
    job: str = "",
    kpi_summary: str = "",
    capabilities: str = "",
    managed_by: str | None = None,
    scope_tags: list | None = None,
    overwrite: bool = False,
) -> bool:
    """Create a memory stub. Skip if exists unless overwrite=True."""
    p = _path_for(agent_id)
    if p.exists() and not overwrite:
        return False
    budget = TIER_BUDGETS.get(tier, TIER_BUDGETS[DEFAULT_TIER])
    fm = {
        "agent_id": agent_id,
        "tier": tier,
        "sub_class": sub_class,
        "token_budget": budget,
        "last_updated": now_iso(),
        "last_compacted": None,
        "managed_by": managed_by,
        "scope_tags": scope_tags or [],
    }
    body = (
        f"# 我是誰\n\n{identity}\n\n"
        f"# 我在做什麼\n\n{job}\n\n"
        f"# KPI 目標\n\n{kpi_summary}\n\n"
        f"# 我的能力 / 工具\n\n{capabilities}\n\n"
        f"# 我的經驗\n\n"
        f"# Boss curated\n\n"
    )
    p.write_text(_serialize(fm, body), encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    pp = argparse.ArgumentParser(description="agent_memory CLI")
    sub = pp.add_subparsers(dest="cmd")

    p_load = sub.add_parser("load")
    p_load.add_argument("agent_id")

    p_app = sub.add_parser("append")
    p_app.add_argument("agent_id")
    p_app.add_argument("text")
    p_app.add_argument("--category", default="ops")
    p_app.add_argument("--boss-curated", action="store_true")
    p_app.add_argument("--boss-approved", action="store_true",
                       help="skip pending queue; write live immediately")

    p_com = sub.add_parser("compact")
    p_com.add_argument("agent_id")
    p_com.add_argument("--target", type=int, default=None)

    p_bud = sub.add_parser("budget")
    p_bud.add_argument("agent_id")

    sub.add_parser("ls")

    p_pend = sub.add_parser("pending", help="list pending learnings")
    p_pend.add_argument("agent_id", nargs="?",
                        help="optional: filter to one agent")
    p_pend.add_argument("--full", action="store_true",
                        help="show full text (not truncated)")

    p_appr = sub.add_parser("approve",
                            help="merge a pending learning into live memory")
    p_appr.add_argument("pending_id")

    p_rej = sub.add_parser("reject", help="discard a pending learning")
    p_rej.add_argument("pending_id")
    p_rej.add_argument("--reason", default="")

    p_all = sub.add_parser("approve-all",
                           help="approve every pending entry for one agent (or all)")
    p_all.add_argument("agent_id", nargs="?")

    args = pp.parse_args()
    if args.cmd == "load":
        print(load(args.agent_id))
    elif args.cmd == "append":
        ok = append_learning(args.agent_id, args.text, args.category,
                             boss_curated=args.boss_curated,
                             boss_approved=args.boss_approved)
        if args.boss_approved or args.boss_curated:
            print(f"appended_live={ok}")
        else:
            print(f"queued_pending={ok} (use `pending` to list, `approve <id>` to merge)")
    elif args.cmd == "compact":
        print(compact(args.agent_id, args.target))
    elif args.cmd == "budget":
        print(get_budget(args.agent_id))
    elif args.cmd == "pending":
        items = list_pending(args.agent_id)
        if not items:
            print("(none pending)")
        else:
            for it in items:
                preview = it["text"] if args.full else it["text"][:180]
                if not args.full and len(it["text"]) > 180:
                    preview += "..."
                target = "→ Boss curated" if it["boss_curated_target"] else "→ 我的經驗"
                print(f"[{it['pending_id']}] {it['agent_id']} {target} "
                      f"category={it['category']} queued={it['queued_at']}")
                print(f"   {preview}")
                print()
            print(f"({len(items)} pending)")
    elif args.cmd == "approve":
        ok = approve_pending(args.pending_id)
        print(f"approved={ok}")
    elif args.cmd == "reject":
        ok = reject_pending(args.pending_id, reason=args.reason)
        print(f"rejected={ok}")
    elif args.cmd == "approve-all":
        items = list_pending(args.agent_id)
        n_ok = 0
        for it in items:
            if approve_pending(it["pending_id"]):
                n_ok += 1
        print(f"approved {n_ok}/{len(items)}")
    elif args.cmd == "ls":
        for aid in list_memory_files():
            print(aid)
    else:
        pp.print_help()
