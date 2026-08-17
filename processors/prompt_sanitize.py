"""Sanitize untrusted text before injecting it into LLM prompts.

Threat model: foreign-platform-collected content (OCR text, forum posts,
TG/Discord messages, image alt-text) can carry prompt-injection payloads
that try to subvert downstream LLM judgment or coax tool use. This module
provides a single defensive entry point that:

  1. Strips markdown code fences (``` ... ```) that can pose as system blocks
  2. Detects and tags injection markers (`ignore previous instructions`,
     `system:`, `<system>...</system>`, `[INST]`, `### override`, etc.)
  3. Caps content length defensively (truncated content marked as such)
  4. Returns sanitized text + a flag indicating whether anything was caught

Use at the LAST possible step before f-string-substituting untrusted text
into a prompt template. Callers MUST treat the returned text as
display-safe-only — never re-evaluate as control flow / executable.

This is a defense-in-depth layer. The LLM's own resistance to injection
is still the primary defense; this layer makes injection markers visible
and harder to chain across stages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Patterns considered injection-likely. Case-insensitive. The fence
# detection handles the most common payload-hiding trick (multi-line
# code block masquerading as a system-prompt continuation).
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("ignore_previous", re.compile(r"\bignore\s+(?:all\s+)?(?:previous|above|prior|earlier)\s+(?:instructions?|prompts?|messages?|rules?)\b", re.IGNORECASE)),
    ("disregard_previous", re.compile(r"\bdisregard\s+(?:all\s+)?(?:previous|above|prior|earlier)\b", re.IGNORECASE)),
    ("forget_previous", re.compile(r"\bforget\s+(?:everything|all|previous|above)\b", re.IGNORECASE)),
    ("system_role_open", re.compile(r"(?:^|\n)\s*(?:#{1,4}\s*)?(?:system|assistant|user)\s*[:>]\s*", re.IGNORECASE)),
    ("system_tag", re.compile(r"<\s*/?\s*(?:system|sys|assistant|user|instruction)\s*>", re.IGNORECASE)),
    ("inst_brackets", re.compile(r"\[\s*(?:INST|/INST|SYSTEM|END_SYSTEM|HUMAN|AI)\s*\]", re.IGNORECASE)),
    ("override_directive", re.compile(r"(?:^|\n)\s*#{2,}\s*(?:override|new\s+instructions?|new\s+task|updated?\s+rules?)\b", re.IGNORECASE)),
    ("exfiltrate_prompt", re.compile(r"\b(?:repeat|reveal|print|output|show|display)\s+(?:the\s+|your\s+)?(?:system\s+prompt|instructions?|skill|persona|preamble)\b", re.IGNORECASE)),
    ("role_assign", re.compile(r"\byou\s+are\s+now\s+(?:a|an|the)\b", re.IGNORECASE)),
    ("jailbreak_dan", re.compile(r"\b(?:DAN|do\s+anything\s+now|developer\s+mode)\b", re.IGNORECASE)),
)

# Max characters allowed for any single untrusted field before truncation.
# 2000 chars covers >99% of OCR and short post payloads while bounding the
# fan-out a single injection can have.
DEFAULT_MAX_CHARS = 2000


@dataclass
class SanitizeResult:
    text: str
    truncated: bool = False
    fence_count: int = 0
    flagged: list[str] = None      # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.flagged is None:
            self.flagged = []

    @property
    def dirty(self) -> bool:
        """True iff any defensive measure triggered (caller may want to
        log / down-weight kb_value_score for such content)."""
        return self.truncated or self.fence_count > 0 or bool(self.flagged)

    def summary(self) -> str:
        """Short human-readable label for logs / audit rows."""
        if not self.dirty:
            return "clean"
        bits = []
        if self.truncated:
            bits.append("truncated")
        if self.fence_count:
            bits.append(f"fences={self.fence_count}")
        if self.flagged:
            bits.append("flags=" + ",".join(self.flagged))
        return ";".join(bits)


def _strip_fences(text: str) -> tuple[str, int]:
    """Replace ``` code blocks with a flat marker. Fence body is dropped
    entirely (not even sampled) because a sampled snippet would still
    leak the payload into the downstream prompt — defeating the point
    of stripping. The audit summary will show `fences=N` so observers
    know that something was attempted; the original raw text lives in
    `media.ocr_text` for forensic recovery."""
    count = 0
    def _repl(m):
        nonlocal count
        count += 1
        return "[CODE_FENCE_REMOVED]"
    out = re.sub(r"```(?:[a-zA-Z0-9_+-]*\n)?(.*?)```", _repl, text, flags=re.DOTALL)
    return out, count


def _detect_markers(text: str) -> list[str]:
    flagged = []
    for name, pat in _INJECTION_PATTERNS:
        if pat.search(text):
            flagged.append(name)
    return flagged


def sanitize_untrusted(
    text: str | None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    label: str = "untrusted",
) -> SanitizeResult:
    """Sanitize one untrusted text field. Idempotent. Safe on None / empty.

    Returns SanitizeResult; the `.text` is what callers should substitute
    into the prompt. `.dirty` flag tells the caller whether the source
    field tripped any defensive marker (useful for audit logging or
    automatic kb_value_score reduction).

    Order of operations:
      1. Coerce None → "" so f-strings can't NoneType-explode
      2. Strip code fences → flatten to markers
      3. Detect injection markers (NOT removed; replaced inline so the
         LLM can SEE that an injection attempt was made — this is more
         defensive than silent removal because the LLM is then explicitly
         informed that downstream content is hostile)
      4. Truncate to max_chars (kept tail dropped — most prompt-injection
         payloads are near top of payload, tail loss is acceptable)
    """
    if text is None:
        return SanitizeResult(text="")
    s = str(text)
    if not s:
        return SanitizeResult(text="")

    s, fence_count = _strip_fences(s)
    flagged = _detect_markers(s)
    if flagged:
        # Replace each detected pattern with a visible marker so the LLM
        # sees that an injection attempt was made. Do not silently remove.
        for name, pat in _INJECTION_PATTERNS:
            if name in flagged:
                s = pat.sub(f"[INJECTION_MARKER_BLOCKED:{name}]", s)

    truncated = False
    if len(s) > max_chars:
        s = s[:max_chars] + f"\n... [TRUNCATED at {max_chars} chars for {label}]"
        truncated = True

    return SanitizeResult(
        text=s,
        truncated=truncated,
        fence_count=fence_count,
        flagged=flagged,
    )


def sanitize_many(
    fields: Iterable[tuple[str, str | None]],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[dict[str, str], dict[str, str]]:
    """Sanitize a batch of (label, text) pairs. Returns:
      - clean_dict: {label: sanitized_text}
      - audit_dict: {label: summary_string}  (only entries that were dirty)
    Convenient for prompt builders that f-string-substitute several fields.
    """
    out_clean: dict[str, str] = {}
    out_audit: dict[str, str] = {}
    for label, raw in fields:
        res = sanitize_untrusted(raw, max_chars=max_chars, label=label)
        out_clean[label] = res.text
        if res.dirty:
            out_audit[label] = res.summary()
    return out_clean, out_audit
