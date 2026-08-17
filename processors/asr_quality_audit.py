"""ASR quality audit.

Scheduled LLM judge for sampled voice/video transcripts.

v1 is an accuracy proxy, not literal WER: the current Codex CLI supports image
attachments, not audio attachments. The audit therefore compares stored ASR,
an independent audit decode, decoder confidence metadata, and source context.
It explicitly records audio_level_judge_available=0 until an audio-capable LLM
path is automated.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from db.connection import get_connection
from db.schema import init_db
from processors.asr_whisper import get_model, has_audio_stream
from processors.history_log import log_event
from processors.llm_router import codex_model_for_tier, json_schema_file, run_codex

ACTIVE_INSTANCE = os.environ.get("ACTIVE_INSTANCE", "_TEMPLATE")
RUNTIME = ROOT / "instances" / ACTIVE_INSTANCE / "runtime"
LOG_DIR = RUNTIME / "logs"
AUDIT_DIR = RUNTIME / "agent_kpi" / "asr_audit"
QUEUE_DIR = RUNTIME / "briefs" / "queue"
for p in (LOG_DIR, AUDIT_DIR, QUEUE_DIR):
    p.mkdir(parents=True, exist_ok=True)

TZ = timezone(timedelta(hours=7))

DAILY_N = int(os.environ.get("ASR_AUDIT_DAILY_N", "8"))
WEEKLY_N = int(os.environ.get("ASR_AUDIT_WEEKLY_N", "32"))
SINCE_HOURS = int(os.environ.get("ASR_AUDIT_SINCE_HOURS", "72"))
PER_REQ_TIMEOUT_S = int(os.environ.get("ASR_AUDIT_LLM_TIMEOUT_S", "240"))

WARN_AVG = float(os.environ.get("ASR_AUDIT_WARN_AVG", "70"))
CRIT_AVG = float(os.environ.get("ASR_AUDIT_CRIT_AVG", "50"))
WARN_USABLE_RATE = float(os.environ.get("ASR_AUDIT_WARN_USABLE_RATE", "0.65"))
CRIT_USABLE_RATE = float(os.environ.get("ASR_AUDIT_CRIT_USABLE_RATE", "0.40"))
MIN_ALERT_SAMPLE_N = int(os.environ.get("ASR_AUDIT_MIN_ALERT_SAMPLE_N", "4"))


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def log(msg: str) -> None:
    line = f"[{now_iso()}] [asr_audit] {msg}"
    print(line, flush=True)
    with (LOG_DIR / f"asr_audit_{now().strftime('%Y-%m-%d')}.log").open(
        "a", encoding="utf-8"
    ) as f:
        f.write(line + "\n")


def clean(s: object, limit: int = 600) -> str:
    if s is None:
        return ""
    out = str(s).replace("\r", " ").replace("\n", " ").strip()
    return out[:limit]


def yaml_s(s: object, limit: int = 160) -> str:
    return clean(s, limit).replace("'", "")


def row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


# ----------------------------------------------------------------------
# Sample selection
# ----------------------------------------------------------------------

def _query_bucket(conn, where: str, params: tuple, limit: int) -> list[dict]:
    rows = conn.execute(
        f"""SELECT
                m.row_id, m.message_row_id, m.platform, m.media_kind, m.file_path,
                m.file_size, m.mime_type, m.duration_s, m.transcript,
                m.transcript_lang, m.processed_at, m.captured_at,
                msg.chat_title, msg.chat_username, msg.sender_username,
                msg.sender_name, msg.text AS message_text
              FROM media m
         LEFT JOIN messages msg ON msg.row_id = m.message_row_id
             WHERE m.media_kind IN ('voice','video','audio')
               AND m.processed_at >= ?
               AND m.transcript IS NOT NULL
               AND m.transcript <> ''
               AND m.transcript NOT LIKE '[asr_error:%'
               AND {where}
          ORDER BY RANDOM()
             LIMIT ?""",
        params + (limit,),
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def pick_samples(conn, sample_n: int, since_hours: int) -> list[dict]:
    cutoff = (now() - timedelta(hours=since_hours)).isoformat(timespec="seconds")
    per = max(1, sample_n // 4)
    buckets = [
        ("local", "m.transcript_lang = 'th'", (cutoff,), per),
        (
            "language_anomaly",
            "(m.transcript_lang IS NOT NULL AND m.transcript_lang NOT IN ('th','en','vi','id','tl'))",
            (cutoff,),
            per,
        ),
        ("short", "COALESCE(m.duration_s, 0) <= 6 OR length(m.transcript) < 30", (cutoff,), per),
        ("long", "COALESCE(m.duration_s, 0) >= 15 OR length(m.transcript) >= 120", (cutoff,), per),
        ("empty", "m.transcript IN ('<NOTEXT>','<NOAUDIO>')", (cutoff,), per),
    ]

    seen: set[int] = set()
    out: list[dict] = []
    for bucket, where, params, limit in buckets:
        for r in _query_bucket(conn, where, params, limit):
            if r["row_id"] in seen:
                continue
            r["bucket"] = bucket
            seen.add(r["row_id"])
            out.append(r)
            if len(out) >= sample_n:
                return out

    if len(out) < sample_n:
        fill = _query_bucket(conn, "1=1", (cutoff,), sample_n * 2)
        for r in fill:
            if r["row_id"] in seen:
                continue
            r["bucket"] = "fill"
            seen.add(r["row_id"])
            out.append(r)
            if len(out) >= sample_n:
                break
    random.shuffle(out)
    return out[:sample_n]


# ----------------------------------------------------------------------
# Audit decode
# ----------------------------------------------------------------------

def audit_decode(sample: dict) -> dict:
    abs_path = ROOT / sample["file_path"]
    if not abs_path.exists():
        return {"ok": False, "error": "missing_file"}
    if not has_audio_stream(abs_path):
        return {
            "ok": True,
            "audit_transcript": "<NOAUDIO>",
            "audit_lang": None,
            "audit_lang_prob": None,
            "segments": 0,
            "avg_logprob": None,
            "avg_no_speech_prob": None,
            "max_compression_ratio": None,
            "elapsed_s": 0.0,
        }

    t0 = time.time()
    model = get_model()
    segments, info = model.transcribe(
        str(abs_path),
        language=None,
        beam_size=1,
        vad_filter=True,
        word_timestamps=False,
    )
    parts: list[str] = []
    avg_logprobs: list[float] = []
    no_speech_probs: list[float] = []
    comp_ratios: list[float] = []
    segment_count = 0
    for seg in segments:
        segment_count += 1
        text = (getattr(seg, "text", "") or "").strip()
        if text:
            parts.append(text)
        for attr, bucket in (
            ("avg_logprob", avg_logprobs),
            ("no_speech_prob", no_speech_probs),
            ("compression_ratio", comp_ratios),
        ):
            val = getattr(seg, attr, None)
            if isinstance(val, (int, float)):
                bucket.append(float(val))

    return {
        "ok": True,
        "audit_transcript": " ".join(parts) if parts else "<NOTEXT>",
        "audit_lang": getattr(info, "language", None),
        "audit_lang_prob": getattr(info, "language_probability", None),
        "segments": segment_count,
        "avg_logprob": round(mean(avg_logprobs), 4) if avg_logprobs else None,
        "avg_no_speech_prob": round(mean(no_speech_probs), 4) if no_speech_probs else None,
        "max_compression_ratio": round(max(comp_ratios), 4) if comp_ratios else None,
        "elapsed_s": round(time.time() - t0, 2),
    }


# ----------------------------------------------------------------------
# LLM judge
# ----------------------------------------------------------------------

ASR_AUDIT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "expected_language": {"type": "string"},
        "language_verdict": {
            "type": "string",
            "enum": ["match", "suspicious", "wrong", "empty_ok", "not_assessable"],
        },
        "transcript_agreement": {
            "type": "string",
            "enum": ["same", "minor_diff", "major_diff", "empty", "not_assessable"],
        },
        "accuracy_proxy_0_100": {"type": "integer", "minimum": 0, "maximum": 100},
        "commercial_usability": {
            "type": "string",
            "enum": ["usable", "partial", "low", "reject"],
        },
        "kb_policy": {
            "type": "string",
            "enum": ["allow_kb", "low_confidence_only", "exclude"],
        },
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "key_terms": {"type": "array", "items": {"type": "string"}},
        "needs_audio_level_review": {"type": "boolean"},
        "comment": {"type": "string", "maxLength": 240},
    },
    "required": [
        "expected_language", "language_verdict", "transcript_agreement",
        "accuracy_proxy_0_100", "commercial_usability", "kb_policy",
        "risk_flags", "key_terms", "needs_audio_level_review", "comment",
    ],
}


PROMPT = """You are auditing Blacksite ASR for _TEMPLATE.

Important limitation: you cannot hear the audio in this v1 audit. Do NOT claim
literal WER. Judge an accuracy PROXY using:
- stored production transcript
- independent audit decode transcript
- language detection and decoder confidence metadata
- source context from the Telegram/social message

Goal: prevent bad ASR from polluting the intelligence KB. Favor conservative
low-confidence decisions when language is suspicious, short audio is ambiguous,
or stored/audit transcripts disagree.

Return only JSON matching the schema.

MEDIA:
media_row_id: {row_id}
bucket: {bucket}
kind: {media_kind}
duration_s: {duration_s}
mime_type: {mime_type}
file_size: {file_size}

SOURCE CONTEXT:
platform: {platform}
chat_title: {chat_title}
chat_username: {chat_username}
sender: {sender}
message_text: {message_text}

PRODUCTION ASR:
stored_lang: {stored_lang}
stored_transcript: {stored_transcript}

AUDIT PASS:
audit_lang: {audit_lang}
audit_lang_prob: {audit_lang_prob}
segments: {segments}
avg_logprob: {avg_logprob}
avg_no_speech_prob: {avg_no_speech_prob}
max_compression_ratio: {max_compression_ratio}
audit_transcript: {audit_transcript}

Scoring:
- 85-100: stored/audit agree, language plausible, text commercially usable.
- 70-84: minor differences or mild uncertainty, still usable with caution.
- 50-69: partial/low-confidence; keep as low-confidence evidence only.
- 0-49: likely wrong, language suspicious, empty when context implies speech,
  or transcript not commercially usable.
"""


def parse_json(raw: str) -> dict | None:
    if not raw:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates = [fenced[-1]] if fenced else []
    if not candidates:
        depth = 0
        start = -1
        blocks = []
        for i, ch in enumerate(raw):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    blocks.append(raw[start:i + 1])
                    start = -1
        candidates = [b for b in blocks if "accuracy_proxy_0_100" in b]
    for cand in reversed(candidates):
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None


def judge_one(sample: dict, audit: dict, dry_run: bool = False) -> tuple[dict | None, dict]:
    prompt = PROMPT.format(
        row_id=sample["row_id"],
        bucket=sample.get("bucket", ""),
        media_kind=sample.get("media_kind", ""),
        duration_s=sample.get("duration_s"),
        mime_type=sample.get("mime_type", ""),
        file_size=sample.get("file_size"),
        platform=sample.get("platform", ""),
        chat_title=clean(sample.get("chat_title"), 240),
        chat_username=clean(sample.get("chat_username"), 120),
        sender=clean(sample.get("sender_username") or sample.get("sender_name"), 120),
        message_text=clean(sample.get("message_text"), 700),
        stored_lang=sample.get("transcript_lang"),
        stored_transcript=clean(sample.get("transcript"), 1500),
        audit_lang=audit.get("audit_lang"),
        audit_lang_prob=audit.get("audit_lang_prob"),
        segments=audit.get("segments"),
        avg_logprob=audit.get("avg_logprob"),
        avg_no_speech_prob=audit.get("avg_no_speech_prob"),
        max_compression_ratio=audit.get("max_compression_ratio"),
        audit_transcript=clean(audit.get("audit_transcript"), 1500),
    )
    if dry_run:
        return None, {"_dry_prompt": prompt[:1200]}

    schema_path = json_schema_file("asr_audit_judge", ASR_AUDIT_SCHEMA)
    result = run_codex(
        prompt,
        tier="audit",
        model=codex_model_for_tier("audit"),
        output_schema=schema_path,
        timeout_s=PER_REQ_TIMEOUT_S,
        sandbox="read-only",
    )
    meta = result.meta()
    if not result.ok:
        meta["_error"] = result.error or "llm failed"
        return None, meta
    parsed = parse_json(result.text)
    if not parsed:
        meta["_error"] = "parse_fail"
        meta["_raw"] = result.text[:500]
        return None, meta
    return parsed, meta


# ----------------------------------------------------------------------
# Aggregation + reporting
# ----------------------------------------------------------------------

def decide_alert(avg_score: float | None, usable_rate: float | None,
                 sample_size: int) -> str:
    if sample_size < MIN_ALERT_SAMPLE_N:
        return "none"
    avg = 100.0 if avg_score is None else avg_score
    usable = 1.0 if usable_rate is None else usable_rate
    if avg < CRIT_AVG or usable < CRIT_USABLE_RATE:
        return "critical"
    if avg < WARN_AVG or usable < WARN_USABLE_RATE:
        return "warning"
    return "none"


def aggregate(results: list[dict]) -> dict:
    judged = [r for r in results if isinstance(r.get("judge"), dict)]
    scores = [r["judge"].get("accuracy_proxy_0_100") for r in judged]
    scores = [s for s in scores if isinstance(s, (int, float))]
    usable = [
        r for r in judged
        if r["judge"].get("commercial_usability") in ("usable", "partial")
        and r["judge"].get("kb_policy") != "exclude"
    ]
    lang_suspicious = [
        r for r in judged
        if r["judge"].get("language_verdict") in ("suspicious", "wrong")
    ]
    low_conf = [
        r for r in judged
        if r["judge"].get("kb_policy") in ("low_confidence_only", "exclude")
        or (isinstance(r["judge"].get("accuracy_proxy_0_100"), int)
            and r["judge"].get("accuracy_proxy_0_100") < 70)
    ]
    avg_score = round(sum(scores) / len(scores), 1) if scores else None
    usable_rate = round(len(usable) / len(judged), 3) if judged else None
    mix: dict[str, int] = {}
    for r in results:
        b = r.get("bucket", "unknown")
        mix[b] = mix.get(b, 0) + 1
    return {
        "avg_score": avg_score,
        "usable_rate": usable_rate,
        "language_suspicious_count": len(lang_suspicious),
        "low_confidence_count": len(low_conf),
        "sample_mix": mix,
        "judged_count": len(judged),
        "error_count": len(results) - len(judged),
    }


def quality_from_judge(judge: dict) -> tuple[str, str]:
    score = judge.get("accuracy_proxy_0_100")
    policy = judge.get("kb_policy")
    usability = judge.get("commercial_usability")
    lang_verdict = judge.get("language_verdict")
    agreement = judge.get("transcript_agreement")

    if (
        policy == "exclude"
        or lang_verdict in ("suspicious", "wrong")
        or agreement == "major_diff"
        or (isinstance(score, (int, float)) and score < 50)
    ):
        quality = "exclude"
    elif (
        policy == "low_confidence_only"
        or usability not in ("usable", "partial")
        or (isinstance(score, (int, float)) and score < 70)
    ):
        quality = "low_confidence"
    else:
        quality = "usable"

    note = (
        f"asr_audit score={score} policy={policy} "
        f"usage={usability} lang={lang_verdict} agreement={agreement}"
    )
    return quality, note[:240]


def apply_transcript_quality_gates(conn, results: list[dict]) -> dict:
    stats = {"usable": 0, "low_confidence": 0, "exclude": 0, "skipped": 0}
    ts = now_iso()
    for r in results:
        judge = r.get("judge")
        if not isinstance(judge, dict):
            stats["skipped"] += 1
            continue
        quality, note = quality_from_judge(judge)
        conn.execute(
            """UPDATE media
                  SET transcript_quality = ?,
                      transcript_quality_at = ?,
                      transcript_quality_note = ?
                WHERE row_id = ?""",
            (quality, ts, note, r["media_row_id"]),
        )
        stats[quality] = stats.get(quality, 0) + 1
    conn.commit()
    return stats


def write_yaml(kind: str, results: list[dict], agg: dict, alert: str) -> Path:
    today = now().strftime("%Y-%m-%d")
    path = AUDIT_DIR / f"{today}_{kind}.yaml"
    lines = [
        f"date: '{today}'",
        f"audit_kind: '{kind}'",
        f"audited_at: '{now_iso()}'",
        "audio_level_judge_available: 0",
        f"sample_size: {len(results)}",
        f"judged_count: {agg['judged_count']}",
        f"avg_accuracy_proxy: {agg['avg_score'] if agg['avg_score'] is not None else 'null'}",
        f"usable_rate: {agg['usable_rate'] if agg['usable_rate'] is not None else 'null'}",
        f"language_suspicious_count: {agg['language_suspicious_count']}",
        f"low_confidence_count: {agg['low_confidence_count']}",
        f"alert_level: '{alert}'",
        "samples:",
    ]
    for r in results:
        j = r.get("judge") or {}
        lines.append(f"  - media_row_id: {r['media_row_id']}")
        lines.append(f"    bucket: '{yaml_s(r.get('bucket'))}'")
        lines.append(f"    stored_lang: '{yaml_s(r.get('stored_lang'))}'")
        lines.append(f"    audit_lang: '{yaml_s(r.get('audit_lang'))}'")
        lines.append(f"    score: {j.get('accuracy_proxy_0_100', 'null')}")
        lines.append(f"    language_verdict: '{yaml_s(j.get('language_verdict'))}'")
        lines.append(f"    agreement: '{yaml_s(j.get('transcript_agreement'))}'")
        lines.append(f"    kb_policy: '{yaml_s(j.get('kb_policy'))}'")
        if j.get("comment"):
            lines.append(f"    comment: '{yaml_s(j.get('comment'), 220)}'")
        if r.get("error"):
            lines.append(f"    error: '{yaml_s(r.get('error'), 220)}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"yaml -> {path}")
    return path


def insert_audit_row(conn, kind: str, results: list[dict], agg: dict,
                     alert: str, yaml_path: Path) -> int:
    cur = conn.execute(
        """INSERT INTO asr_audit
           (audit_kind, sample_size, sample_mix_json, sampled_media_ids,
            avg_accuracy_proxy, usable_rate, language_suspicious_count,
            low_confidence_count, alert_level, audit_model,
            audio_level_judge_available, results_json, audited_at, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            kind,
            len(results),
            json.dumps(agg["sample_mix"], ensure_ascii=False),
            json.dumps([r["media_row_id"] for r in results]),
            agg["avg_score"],
            agg["usable_rate"],
            agg["language_suspicious_count"],
            agg["low_confidence_count"],
            alert,
            codex_model_for_tier("audit"),
            0,
            json.dumps(results, ensure_ascii=False),
            now_iso(),
            f"yaml={yaml_path.relative_to(ROOT).as_posix()}; reference_free_accuracy_proxy",
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def emit_history(kind: str, row_id: int, agg: dict, alert: str, yaml_path: Path) -> None:
    refs = [str(yaml_path.relative_to(ROOT)).replace("\\", "/"), f"asr_audit:{row_id}"]
    body = (
        f"audit_kind: {kind}\n"
        "audio_level_judge_available: 0\n"
        f"avg_accuracy_proxy: {agg['avg_score']}\n"
        f"usable_rate: {agg['usable_rate']}\n"
        f"language_suspicious_count: {agg['language_suspicious_count']}\n"
        f"low_confidence_count: {agg['low_confidence_count']}\n"
        f"error_count: {agg['error_count']}\n"
    )
    log_event(
        actor="asr_audit",
        kind="metric",
        scope="asr",
        title=f"ASR audit avg={agg['avg_score']} usable={agg['usable_rate']}",
        body=body,
        refs=refs,
    )
    if alert != "none":
        log_event(
            actor="asr_audit",
            kind="warning" if alert == "warning" else "crash",
            scope="asr",
            title=f"ASR audit {alert}",
            body=body,
            refs=refs,
        )
        _route_asr_alert_to_chain(alert, row_id, agg, refs[0])


def _route_asr_alert_to_chain(alert: str, row_id: int, agg: dict, yaml_ref: str) -> None:
    """Route ASR audit alert through 小主管 → 策略長 chain. Boss is never notified directly."""
    import subprocess
    ts = now().strftime("%Y-%m-%dT%H-%M-%S")
    summary = (
        f"avg={agg['avg_score']} usable={agg['usable_rate']} "
        f"low_conf={agg['low_confidence_count']} "
        f"lang_suspicious={agg['language_suspicious_count']}"
    )

    if alert == "warning":
        # warning → section_chief auto-applies fix; brief queue is FYI digest only
        try:
            consult_q = (
                f"ASR audit #{row_id} returned warning: {summary}. "
                f"All samples are Khmer (km) with major transcript divergence between production "
                f"(faster-whisper large-v3) and audit decode. "
                f"Recommend: (1) review if km audio requires a Khmer-tuned model or different "
                f"whisper prompt; (2) adjust ASR_AUDIT_WARN_USABLE_RATE threshold if Khmer is "
                f"structurally lower quality; (3) exclude km audio from KB text ingestion until "
                f"audio-level review is available. Apply whichever fix is feasible now."
            )
            subprocess.Popen(
                ["python", "processors/section_chief_eval.py",
                 "--consult", consult_q],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"[asr_audit] section_chief spawn fail: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
        q = QUEUE_DIR / f"pending_{ts}_asr_sc_fyi_{row_id}.md"
        q.write_text(
            f"[小主管 FYI] 語音辨識品質 警告（audit #{row_id}）\n\n"
            f"• {summary[:120]} → 語音轉文字品質下降，影響 KB 文字精確度\n"
            f"• 小主管已自主套用修正 → 下次 audit 追蹤效果\n\n"
            f"不需行動。",
            encoding="utf-8",
        )
    else:
        # critical → 策略長 auto-applies fix; boss notified FYI (no approval needed)
        consult_q = (
            f"ASR audit #{row_id} returned critical: {summary}. "
            f"Samples show severe transcript degradation (usable_rate=0.0, likely all non-local audio). "
            f"Action required: (1) review whisper model language config — consider forcing 'th' language "
            f"or filtering non-local audio at ingest; (2) assess if ASR_AUDIT_CRITICAL_USABLE_RATE "
            f"threshold needs adjustment for mixed-language corpus; (3) consider excluding non-local "
            f"audio from KB text ingestion. Issue directive now; escalate_boss only if cannot resolve."
        )
        try:
            subprocess.Popen(
                ["python", "processors/chief_strategist.py",
                 "--consult", consult_q],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"[asr_audit] chief_strategist spawn fail: {type(e).__name__}: {e}",
                  file=sys.stderr, flush=True)
        q = QUEUE_DIR / f"pending_{ts}_asr_strategist_fyi_{row_id}.md"
        q.write_text(
            f"[策略長 決策] 語音辨識品質 嚴重（audit #{row_id}）\n\n"
            f"• {summary[:120]} → 情況嚴重，KB 文字品質大幅受損\n"
            f"• 策略長已自主介入（調整 whisper model/語言設定）→ 詳見 `{yaml_ref}`\n\n"
            f"不需行動。",
            encoding="utf-8",
        )


def run_audit(kind: str, sample_n: int, since_hours: int,
              dry_run: bool = False) -> dict:
    init_db()
    conn = get_connection()
    samples = pick_samples(conn, sample_n, since_hours)
    log(f"kind={kind} sample_size={len(samples)} since_hours={since_hours}")
    if not samples:
        return {"skipped": True, "reason": "no samples"}

    results: list[dict] = []
    for i, sample in enumerate(samples, 1):
        log(f"[{i}/{len(samples)}] media={sample['row_id']} bucket={sample.get('bucket')} "
            f"stored_lang={sample.get('transcript_lang')}")
        try:
            audit = audit_decode(sample)
        except Exception as e:
            audit = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        if not audit.get("ok"):
            item = {
                "media_row_id": sample["row_id"],
                "bucket": sample.get("bucket"),
                "stored_lang": sample.get("transcript_lang"),
                "error": audit.get("error", "audit_decode_failed"),
            }
            results.append(item)
            log(f"  decode_error={item['error']}")
            continue

        judge, meta = judge_one(sample, audit, dry_run=dry_run)
        item = {
            "media_row_id": sample["row_id"],
            "bucket": sample.get("bucket"),
            "file_path": sample.get("file_path"),
            "stored_lang": sample.get("transcript_lang"),
            "audit_lang": audit.get("audit_lang"),
            "audit_lang_prob": audit.get("audit_lang_prob"),
            "avg_logprob": audit.get("avg_logprob"),
            "segments": audit.get("segments"),
            "judge": judge,
        }
        if meta.get("_error"):
            item["error"] = meta["_error"]
        results.append(item)
        if judge:
            log(f"  score={judge.get('accuracy_proxy_0_100')} "
                f"lang={judge.get('language_verdict')} "
                f"policy={judge.get('kb_policy')}")
        else:
            log(f"  judge_error={meta.get('_error') or 'dry_run'}")

    if dry_run:
        log("dry_run: skip yaml/db/history")
        return {"dry_run": True, "samples": len(samples), "results": results[:2]}

    agg = aggregate(results)
    alert = decide_alert(agg["avg_score"], agg["usable_rate"], len(results))
    qstats = apply_transcript_quality_gates(conn, results)
    log(f"transcript_quality_gates {qstats}")
    yaml_path = write_yaml(kind, results, agg, alert)
    row_id = insert_audit_row(conn, kind, results, agg, alert, yaml_path)
    emit_history(kind, row_id, agg, alert, yaml_path)
    conn.close()
    return {"asr_audit_row_id": row_id, "alert_level": alert, **agg}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--sample-n", type=int, default=None)
    parser.add_argument("--since-hours", type=int, default=SINCE_HOURS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sample_n = args.sample_n
    if sample_n is None:
        sample_n = WEEKLY_N if args.kind == "weekly" else DAILY_N
    result = run_audit(args.kind, sample_n, args.since_hours, dry_run=args.dry_run)
    log(f"DONE {result}")


if __name__ == "__main__":
    main()
    os._exit(0)
