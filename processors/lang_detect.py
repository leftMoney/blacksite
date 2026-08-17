"""
Lightweight script-ratio language detector.

Decision policy:
1. Script-block dominance (>= 25% of letters): th / lo / km / my / zh
2. Vietnamese diacritics (ăâđêôơư + tone marks) at >= 1 char/100 → vi
3. ID/MS/TL distinguished by stop-word lexicon (top ~30 words each)
4. Else: en (default for latin-only with no SEA marker)
5. Returns 'mixed' / 'short' for ambiguous or <8-letter input

NOT a replacement for fastText / langdetect — purpose is to give a stable
hint per message so downstream classifiers and Card synthesis can pick the
right model + the right pattern set. Cheap, no external dependency, runs
on JSONL stream at >100k msg/sec.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

# Unicode block ranges (start, end inclusive)
_BLOCKS = [
    ("th", 0x0E00, 0x0E7F),       # local
    ("lo", 0x0E80, 0x0EFF),       # Lao
    ("km", 0x1780, 0x17FF),       # Khmer
    ("my", 0x1000, 0x109F),       # Myanmar
    ("zh", 0x4E00, 0x9FFF),       # CJK Unified
    ("zh", 0x3400, 0x4DBF),       # CJK Ext A
    ("ja", 0x3040, 0x309F),       # Hiragana
    ("ja", 0x30A0, 0x30FF),       # Katakana
    ("ko", 0xAC00, 0xD7AF),       # Hangul Syllables
    ("ar", 0x0600, 0x06FF),       # Arabic
    ("ru", 0x0400, 0x04FF),       # Cyrillic
]

# Stop-word lexicons (lowercased, normalized). Conservative — only words that
# are unambiguously characteristic of that language. ID and MS share many
# words; the ones below skew Indonesian; MS gets tagged when no ID-only marker.
_STOPWORDS = {
    "id": {
        "yang", "tidak", "saya", "kamu", "akan", "sudah", "bisa", "untuk",
        "dengan", "pada", "dari", "ini", "itu", "tapi", "atau", "kalau",
        "kalo", "banget", "aja", "nih", "sih", "bro", "gue", "dia",
        "kita", "kami", "mereka", "anda", "udah", "udah", "lagi",
        "promo", "menang", "main", "daftar",
    },
    "ms": {
        "yang", "tidak", "saya", "anda", "akan", "sudah", "boleh", "untuk",
        "dengan", "pada", "dari", "ini", "itu", "tetapi", "atau", "kalau",
        "lah", "betul", "macam", "awak", "sangat", "tolong", "bahasa",
        "syarikat", "kerajaan", "perlu",
    },
    "vi": {
        "không", "tôi", "bạn", "đã", "có", "được", "với", "này",
        "của", "và", "là", "cho", "khi", "nhưng", "hoặc", "nếu",
        "rồi", "nhé", "thế", "vẫn", "đang", "sẽ", "cũng",
    },
    "tl": {
        "ang", "ng", "sa", "ay", "mga", "ako", "ikaw", "siya",
        "tayo", "kami", "kayo", "sila", "po", "ko", "mo", "niya",
        "natin", "namin", "ninyo", "nila", "naman", "lang", "kaya",
        "para", "kasi", "yung", "pero", "lahat",
    },
    "en": {
        "the", "and", "for", "you", "this", "that", "with", "have", "are",
        "but", "not", "your", "from", "they", "will", "can", "all",
        "would", "there", "what", "when", "their", "more", "about",
    },
}

# Vietnamese diacritic set (just used to bias toward 'vi' detection).
_VI_DIACRITICS = set("ăâđêôơưĂÂĐÊÔƠƯạảãáàặẳẵắằậẩẫấầọỏõóòợởỡớờụủũúùựửữứừịỉĩíìỵỷỹýỳ")

_TOKEN_RE = re.compile(r"[A-Za-zÀ-žĂăÂâĐđÊêÔôƠơƯư]+", re.UNICODE)
_WORD_RE = re.compile(r"\b[\wÀ-ÿĂăÂâĐđÊêÔôƠơƯư]+\b", re.UNICODE)


def _classify_char(ch: str) -> str | None:
    cp = ord(ch)
    for tag, lo, hi in _BLOCKS:
        if lo <= cp <= hi:
            return tag
    if "A" <= ch <= "Z" or "a" <= ch <= "z":
        return "lat"
    if ch in _VI_DIACRITICS:
        return "lat_vi"
    # latin-with-diacritic (any letter outside ASCII A-Za-z but still alphabetic)
    if ch.isalpha():
        return "lat"
    return None


def detect(text: str | None) -> str:
    if not text:
        return "und"
    counts: Counter[str] = Counter()
    for ch in text:
        tag = _classify_char(ch)
        if tag:
            counts[tag] += 1

    total = sum(counts.values())
    if total < 8:
        return "short"

    # 1. Script-block dominance
    for tag in ("th", "lo", "km", "my", "zh", "ja", "ko", "ar", "ru"):
        if counts.get(tag, 0) / total >= 0.25:
            return tag

    # 2. Vietnamese diacritics signal (any presence is strong)
    if counts.get("lat_vi", 0) >= 1:
        return "vi"

    # 3. Latin-script disambiguation via stop-word lexicons
    words = {w.lower() for w in _WORD_RE.findall(text)}
    if words:
        scores = {lang: len(words & sw) for lang, sw in _STOPWORDS.items()}
        best_lang, best_score = max(scores.items(), key=lambda kv: kv[1])
        if best_score >= 2:
            return best_lang

    # 4. Mixed local + Latin (e.g. brand seeds quoted in local context)
    th_share = counts.get("th", 0) / total
    lat_share = (counts.get("lat", 0) + counts.get("lat_vi", 0)) / total
    if th_share >= 0.10 and lat_share >= 0.10:
        return "mixed"

    # 5. Default: latin-undetermined → en (best guess for short latin text)
    if lat_share >= 0.5:
        return "en"

    return "und"


if __name__ == "__main__":
    # Demo samples exercise the latin-script disambiguation + diacritic + CJK
    # paths. For an instance whose target country uses a non-latin script
    # (e.g. th / lo / km), add a native-script sample here to exercise the
    # script-block-dominance path (it will return that language's 2-letter code).
    samples = [
        "deposit 100 get 200 today's hot promo only!! Line: @slotbrand-a",
        "Saya mau daftar promo gratis dong, kode apa yang dipakai?",
        "Cá độ bóng đá hôm nay tỷ lệ rất đẹp, đăng ký ngay miễn phí!",
        "Mga kapatid, may bagong promo dito sa GCash. Pakitulong naman po.",
        "Tonight's match odds — Manchester vs Arsenal. Sure win formula!",
        "register now to claim your free signup credit, no deposit needed",
        "今晚的比赛赔率很好，马上免费注册",
        "短",
    ]
    for s in samples:
        print(f"{detect(s):>6} | {s}")
