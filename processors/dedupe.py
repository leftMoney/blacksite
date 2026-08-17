"""
Content-hash dedupe + amplification metric.

Goal: collapse copy-pasted shill text shared across many channels/accounts into
a single content cluster, while preserving the amplification count as the
signal of how widely the shill spread. Distinguishes:
  - One msg with forwards=5000 (TG schema field; one origin pumped 5K times)
  - 50 msgs with same content_hash (50 separate channels posted the same text)

The first is `forwards`; the second is `amplification_count`. Both matter.

Hash recipe:
    1. unicode NFKC normalize
    2. lowercase
    3. replace URL paths/queries with bare host (so different shorturls to the
       same site collapse)
    4. collapse whitespace to single space
    5. strip leading/trailing punctuation (but keep emoji — emoji-only msgs
       like 👀🙈 are themselves a content type and must dedupe)
    6. SHA-256 (truncate first 16 hex chars for index efficiency; collision
       probability negligible at our msg volume)

Amplification window: 7 days. amplification_count = number of distinct
(platform, persona) tuples that posted the same content_hash inside the
window. Stored back to messages.amplification_count.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_URL_RE = re.compile(r"https?://([^\s/?#]+)[^\s]*", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_TRIM_PUNCT = ".,;:!?。、！？；：()[]{}「」『』\"'`~"


def normalize_for_hash(text: str | None) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _URL_RE.sub(r"https://\1", s)
    s = s.lower()
    s = _WS_RE.sub(" ", s).strip()
    s = s.strip(_TRIM_PUNCT)
    return s


def content_hash(text: str | None) -> str | None:
    """16-hex-char prefix of SHA-256(normalized text). None if normalized < 2 codepoints.

    Threshold 2 (not 4) preserves emoji-only msgs like '👀🙈' which are the
    canonical bot-pump signal — collapsing those into a single hash is the
    whole point.
    """
    norm = normalize_for_hash(text)
    if len(norm) < 2:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------
# Amplification recompute (called from processors.run)
# ----------------------------------------------------------------------

def recompute_amplification(conn, window_days: int = 7) -> int:
    """
    For every msg whose content_hash is set and ts within last `window_days`,
    set amplification_count = total messages sharing the same hash in window.

    Raw count is the most interpretable signal:
        amp=1   unique
        amp=2-5 light reuse (could be casual quote-and-react)
        amp=6+  bot-pump or cross-channel paid spread (drill down via
                messages_entities + group-by chat_external_id to disambiguate)

    Returns number of distinct content_hash buckets updated (not rows touched).
    """
    cur = conn.execute(
        """
        SELECT content_hash, COUNT(*) AS amp
          FROM messages
         WHERE content_hash IS NOT NULL
           AND ts >= datetime('now', ?)
         GROUP BY content_hash
        """,
        (f"-{window_days} days",),
    )
    buckets = cur.fetchall()
    for row in buckets:
        conn.execute(
            "UPDATE messages SET amplification_count = ? WHERE content_hash = ? AND ts >= datetime('now', ?)",
            (row["amp"], row["content_hash"], f"-{window_days} days"),
        )
    return len(buckets)


if __name__ == "__main__":
    samples = [
        "deposit 100 get 200 https://slotbrand-a.com/promo register now",
        "deposit 100 get 200  https://slotbrand-a.com/promo?ref=abc register now",   # variant URL
        "deposit 100 get 200 https://slotbrand-a.com/different-path register now!!",  # different path
        "👀🙈",
        "👀🙈",        # exact dup
        "👀  🙈",       # whitespace variant
    ]
    for s in samples:
        print(f"{content_hash(s)} | {s!r}")
