"""
SQLite schema for Blacksite cross-platform index.

Design principles:
- JSONL stays source of truth (this DB is a queryable projection).
- Schema versioned via PRAGMA user_version; migrations append-only.
- Common columns across platforms (platform, ts, persona, external_id) so
  cross-platform queries Just Work; platform-specific data in `raw_json`.
- Entity table dedupes mentions of channels/handles/brands across platforms.
- Media table records every binary file Blacksite has on disk + its source
  message (FK to messages.row_id).
"""

from __future__ import annotations

from db.connection import get_connection

CURRENT_VERSION = 11

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS messages (
    row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,           -- telegram | localforum | x | tiktok | reddit | youtube
    external_id     TEXT NOT NULL,           -- platform-native id (msg_id / topic_id / video_id ...)
    persona         TEXT,                    -- P01 / P02 / null (anonymous-fetched)
    ts              TEXT NOT NULL,           -- ISO 8601 with offset (instance timezone)
    chat_external_id   TEXT,                 -- channel/sub/board id
    chat_username      TEXT,
    chat_title         TEXT,
    sender_external_id TEXT,
    sender_username    TEXT,
    sender_name        TEXT,
    text            TEXT,
    url             TEXT,
    -- engagement signals (NULL when platform doesn't expose)
    views           INTEGER,
    reactions_total INTEGER,
    forwards        INTEGER,
    replies         INTEGER,
    score           INTEGER,                  -- reddit upvote / localforum vote
    -- relations
    fwd_from_chat_id   TEXT,
    fwd_from_user_id   TEXT,
    reply_to_external  TEXT,
    -- bookkeeping
    edit_ts         TEXT,                    -- last edit timestamp if known
    raw_json        TEXT NOT NULL,           -- full original JSONL record
    raw_path        TEXT NOT NULL,           -- which file this came from
    raw_offset      INTEGER NOT NULL,        -- byte offset in raw file
    indexed_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+07:00','now','+7 hours')),
    UNIQUE(platform, external_id, persona)
);

CREATE INDEX IF NOT EXISTS idx_messages_platform_ts ON messages(platform, ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(platform, chat_external_id);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(platform, sender_external_id);
CREATE INDEX IF NOT EXISTS idx_messages_persona_ts ON messages(persona, ts DESC) WHERE persona IS NOT NULL;

-- Cross-platform entity registry: every @handle / channel / domain / brand
-- name we have evidence for. Mentions referenced from messages_entities.
CREATE TABLE IF NOT EXISTS entities (
    row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,           -- channel | user | brand | domain | hashtag | wallet
    platform        TEXT,                    -- platform-bound or null for cross-platform (brand/domain)
    name            TEXT NOT NULL,           -- canonical name (e.g. "examplefunnel", "examplebet.com")
    aliases_json    TEXT,                    -- ["examplefunnel", ...]
    first_seen_ts   TEXT,
    last_seen_ts    TEXT,
    seen_count      INTEGER NOT NULL DEFAULT 1,
    tier            TEXT,                    -- yolk | white | shell (when classified)
    role            TEXT,                    -- funnel | brand_public | operator_private | folk-belief_kol | sports_kol
    risk_flags_json TEXT,                    -- ["police_adjacent", ...]
    notes           TEXT,
    UNIQUE(kind, platform, name)
);

CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_role ON entities(role);

-- Many-to-many: each message can mention many entities
CREATE TABLE IF NOT EXISTS messages_entities (
    message_row_id  INTEGER NOT NULL,
    entity_row_id   INTEGER NOT NULL,
    mention_kind    TEXT NOT NULL,           -- author | forward_origin | text_mention | url
    PRIMARY KEY (message_row_id, entity_row_id, mention_kind),
    FOREIGN KEY (message_row_id) REFERENCES messages(row_id) ON DELETE CASCADE,
    FOREIGN KEY (entity_row_id) REFERENCES entities(row_id) ON DELETE CASCADE
);

-- Every binary file Blacksite has on disk
CREATE TABLE IF NOT EXISTS media (
    row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_row_id  INTEGER,                 -- nullable: media may pre-exist its parent message
    platform        TEXT NOT NULL,
    media_kind      TEXT NOT NULL,           -- photo | voice | document | video | sticker | thumbnail
    file_path       TEXT NOT NULL,           -- relative to project root, forward slashes
    file_size       INTEGER,
    mime_type       TEXT,
    duration_s      REAL,                    -- for voice/video
    width           INTEGER,
    height          INTEGER,
    sha256          TEXT,
    -- AI processing status (filled by L4 workers later)
    transcript      TEXT,                    -- whisper output
    transcript_lang TEXT,
    ocr_text        TEXT,                    -- OCR output
    vl_caption      TEXT,                    -- Qwen-VL caption
    processed_at    TEXT,
    -- bookkeeping
    captured_at     TEXT NOT NULL,
    raw_json        TEXT,
    UNIQUE(platform, file_path),
    FOREIGN KEY (message_row_id) REFERENCES messages(row_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_media_msg ON media(message_row_id);
CREATE INDEX IF NOT EXISTS idx_media_unprocessed ON media(processed_at) WHERE processed_at IS NULL;

-- Indexer run history (so we know where the cursor is + can re-run safely)
CREATE TABLE IF NOT EXISTS ingestion_runs (
    row_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    raw_path        TEXT NOT NULL,
    last_offset     INTEGER NOT NULL,
    last_indexed_at TEXT NOT NULL,
    rows_added      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(platform, raw_path)
);
"""


# V2: rules-layer classification + content_hash dedupe (M1).
# Adds intent / topic / tone / lang_detected / content_hash / amplification_count
# to messages. Identifier entities (phone / lineid / promo / wallet / qr) reuse
# the existing entities table (kind column is open-ended TEXT).
SCHEMA_V2_MIGRATIONS = [
    "ALTER TABLE messages ADD COLUMN content_hash TEXT",
    "ALTER TABLE messages ADD COLUMN intent TEXT",
    "ALTER TABLE messages ADD COLUMN topic TEXT",
    "ALTER TABLE messages ADD COLUMN tone TEXT",
    "ALTER TABLE messages ADD COLUMN lang_detected TEXT",
    "ALTER TABLE messages ADD COLUMN amplification_count INTEGER",
    "ALTER TABLE messages ADD COLUMN processed_at_rules TEXT",
    "CREATE INDEX IF NOT EXISTS idx_messages_content_hash ON messages(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_messages_unprocessed ON messages(processed_at_rules) WHERE processed_at_rules IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_messages_intent ON messages(intent) WHERE intent IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic) WHERE topic IS NOT NULL",
]

# V3: rules-layer 2nd stage on OCR'd media (M2). When ocr_gemini fills
# media.ocr_text, processors/run.py picks up media rows with processed_at_rules
# IS NULL and extracts identifier entities the same way it does for messages.
SCHEMA_V3_MIGRATIONS = [
    "ALTER TABLE media ADD COLUMN processed_at_rules TEXT",
    "CREATE INDEX IF NOT EXISTS idx_media_unprocessed_rules ON media(processed_at_rules) WHERE processed_at_rules IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_media_ocr_done ON media(ocr_text) WHERE ocr_text IS NOT NULL",
]

# V4: card synthesis layer (M4). One row per (entity, card_kind). Built every
# 4h by processors/card_builder.py — Haiku 4.5 / Gemini-2.5-Flash transforms
# atom-level evidence into ≤500-token decision-grade cards. State machine
# lives here too (active|dormant|contradicted|superseded) for M5 decay logic.
SCHEMA_V4_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS cards (
        row_id              INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_row_id       INTEGER,
        card_kind           TEXT NOT NULL,
        title               TEXT NOT NULL,
        body_md             TEXT NOT NULL,
        decision_tags       TEXT,
        actionability_score REAL,
        risk_layer          TEXT,
        time_decay_class    TEXT,
        state               TEXT NOT NULL DEFAULT 'active',
        evidence_count      INTEGER NOT NULL DEFAULT 0,
        first_built_at      TEXT NOT NULL,
        last_built_at       TEXT NOT NULL,
        last_seen_at        TEXT NOT NULL,
        raw_pointer_json    TEXT,
        model_used          TEXT NOT NULL,
        UNIQUE(entity_row_id, card_kind),
        FOREIGN KEY (entity_row_id) REFERENCES entities(row_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cards_kind ON cards(card_kind)",
    "CREATE INDEX IF NOT EXISTS idx_cards_state ON cards(state, last_built_at)",
    "CREATE INDEX IF NOT EXISTS idx_cards_decision_tags ON cards(decision_tags) WHERE decision_tags IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_cards_actionability ON cards(actionability_score DESC) WHERE state='active'",
]

# V6: entity state machine + decay (M5). Adds state vocab to entities so M4
# can skip dormant/superseded ones (saves card-rebuild token spend) and the
# daily decay cron can age them out per decay_class rules.
SCHEMA_V6_MIGRATIONS = [
    "ALTER TABLE entities ADD COLUMN state TEXT NOT NULL DEFAULT 'active'",
    "ALTER TABLE entities ADD COLUMN state_changed_at TEXT",
    "ALTER TABLE entities ADD COLUMN state_reason TEXT",
    "CREATE INDEX IF NOT EXISTS idx_entities_state ON entities(state) WHERE state != 'active'",
    "CREATE INDEX IF NOT EXISTS idx_entities_active_last ON entities(last_seen_ts DESC) WHERE state='active'",
]

# V5: TG funnel-edges (M4.5b). One row per (from_chat, target_kind, target)
# tuple — directed edges from observed chats to invite/channel/bot targets.
# review_state + join_state state-machines drive M4.5c (AI review) and M4.5d
# (auto-join). UPSERT pattern preserves review/join state across rebuilds.
SCHEMA_V5_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS funnel_edges (
        row_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        from_chat_id      TEXT NOT NULL,
        from_chat_username TEXT,
        from_platform     TEXT NOT NULL DEFAULT 'telegram',
        to_target_kind    TEXT NOT NULL,    -- tg_invite | tg_channel_ref | tg_bot_deeplink
        to_target         TEXT NOT NULL,
        edge_kind         TEXT NOT NULL DEFAULT 'casual_mention',  -- funnel_push | casual_mention
        bait_intent       TEXT,
        push_count        INTEGER NOT NULL DEFAULT 1,
        distinct_senders  INTEGER NOT NULL DEFAULT 1,
        avg_amplification REAL,
        sample_msg_row_id INTEGER,
        first_seen_ts     TEXT NOT NULL,
        last_seen_ts      TEXT NOT NULL,
        -- AI review (M4.5c)
        review_state      TEXT NOT NULL DEFAULT 'pending',
        review_verdict    TEXT,
        review_reason     TEXT,
        review_at         TEXT,
        review_model      TEXT,
        -- Auto-join (M4.5d)
        join_state        TEXT NOT NULL DEFAULT 'not_attempted',
        join_persona      TEXT,
        join_at           TEXT,
        join_error        TEXT,
        UNIQUE(from_chat_id, to_target_kind, to_target),
        FOREIGN KEY (sample_msg_row_id) REFERENCES messages(row_id) ON DELETE SET NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_funnel_edges_review ON funnel_edges(review_state) WHERE review_state IN ('pending','uncertain')",
    "CREATE INDEX IF NOT EXISTS idx_funnel_edges_join_queued ON funnel_edges(join_state) WHERE join_state='queued'",
    "CREATE INDEX IF NOT EXISTS idx_funnel_edges_kind ON funnel_edges(edge_kind, last_seen_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_funnel_edges_target ON funnel_edges(to_target_kind, to_target)",
]


# V7: system_history (M-history). Append-only event log for engine /
# daemon / boss / cron actions. Replaces ad-hoc CHECKPOINT.md narrative
# bloat — CHECKPOINT stays "thin current state" (per CLAUDE.md §13.2),
# history goes here. Multi-writer safe via SQLite WAL; query via
# scripts/history.py + processors.history_log.query().
SCHEMA_V7_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS system_history (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           TEXT NOT NULL,           -- ISO 8601 +07:00 (per §6.4)
        session_id   TEXT,                    -- 'main_<pid>_<startTs>' / 'cron' / 'bridge'
        actor        TEXT NOT NULL,           -- main | commander_bridge | cron_<job> | boss | p01 | engine
        kind         TEXT NOT NULL,           -- decision | milestone | config_change | crash |
                                              -- warning | directive | metric | trigger_fired |
                                              -- checkpoint_update
        scope        TEXT,                    -- bigo | p03 | daemon | gpu | kb | fb (facet)
        title        TEXT NOT NULL,           -- one-line headline (≤120 char)
        body         TEXT,                    -- markdown OK; long form
        refs         TEXT,                    -- JSON: file paths / row_ids / URLs / commits
        parent_id    INTEGER,                 -- chain: crash → fix → verify
        FOREIGN KEY (parent_id) REFERENCES system_history(id) ON DELETE SET NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_hist_ts ON system_history(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hist_scope ON system_history(scope, ts DESC) WHERE scope IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_hist_kind ON system_history(kind, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_hist_session ON system_history(session_id, ts DESC) WHERE session_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_hist_actor ON system_history(actor, ts DESC)",
]


# V8: 3-stage hybrid OCR pipeline + Sonnet audit (CLAUDE.md §2.1, boss
# 2026-05-08). Each stage gets its own table; rows in higher stages exist
# only when lower-stage verdict was a signal (cascade gating). Schema is
# superset of legacy media_reaudit so the historical 3,509 rows migrate
# into media_kb_decision with model_used='opus_default_via_claude_exe_2026_05_07'.
SCHEMA_V8_MIGRATIONS = [
    # Stage 1 — Qwen2.5-VL 7B local noise filter (~75% rejected here).
    """CREATE TABLE IF NOT EXISTS media_signal_filter (
        media_row_id     INTEGER PRIMARY KEY,
        verdict          TEXT NOT NULL,         -- signal | noise | error
        qwen_tags        TEXT,                  -- JSON array of basic tags
        confidence       REAL,                  -- 0.0-1.0 if model emits it
        raw_response     TEXT,                  -- full Qwen JSON / text reply
        model_used       TEXT NOT NULL,         -- qwen2.5vl:7b-q4_k_m | qwen2.5vl:32b-iq4_xs ...
        prompt_hash      TEXT,                  -- sha256[:12] of prompt template
        duration_ms      INTEGER,
        processed_at     TEXT NOT NULL,         -- ISO 8601 +07:00
        FOREIGN KEY (media_row_id) REFERENCES media(row_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_msf_verdict ON media_signal_filter(verdict, processed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_msf_signal ON media_signal_filter(verdict) WHERE verdict='signal'",

    # Stage 2 — Haiku 4.5 OAuth Bearer structured precision (~25% of input).
    # Superset schema: also accommodates the 3,509 historical media_reaudit
    # rows (P8 migration). Newer fields (stage1_*, prompt_hash, tokens,
    # duration_ms) are NULL for historical rows.
    """CREATE TABLE IF NOT EXISTS media_kb_decision (
        media_row_id          INTEGER PRIMARY KEY,
        -- core decision
        kb_admit              INTEGER NOT NULL,  -- 0 | 1
        kb_value_class        TEXT,
        kb_value_score        INTEGER,           -- 0-100
        decision_tags         TEXT,              -- JSON array
        rationale             TEXT,
        -- Stage 1 evidence chain (NULL for historical migrated rows)
        stage1_verdict        TEXT,
        stage1_confidence     REAL,
        -- legacy re-audit fields (only populated for historical migrated rows)
        audit_score_0_100     INTEGER,
        audit_verdict         TEXT,
        -- common
        raw_response          TEXT,
        model_used            TEXT NOT NULL,     -- claude-haiku-4-5-20251001 |
                                                 -- opus_default_via_claude_exe_2026_05_07 (legacy)
        prompt_hash           TEXT,
        duration_ms           INTEGER,
        input_tokens          INTEGER,
        output_tokens         INTEGER,
        processed_at          TEXT NOT NULL,
        FOREIGN KEY (media_row_id) REFERENCES media(row_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mkd_admit ON media_kb_decision(kb_admit, kb_value_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mkd_score ON media_kb_decision(kb_value_score DESC) WHERE kb_admit=1",
    "CREATE INDEX IF NOT EXISTS idx_mkd_processed ON media_kb_decision(processed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mkd_class ON media_kb_decision(kb_value_class) WHERE kb_value_class IS NOT NULL",

    # Stage 3 — Sonnet daily strategic interpretation (~5% of input,
    # kb_value_score >= 70). NOT 1:1 with media — Sonnet may re-evaluate
    # cross-case patterns over time, so AUTOINCREMENT row_id.
    """CREATE TABLE IF NOT EXISTS media_strategic_brief (
        row_id                INTEGER PRIMARY KEY AUTOINCREMENT,
        media_row_id          INTEGER NOT NULL,
        commercial_action     TEXT,              -- §1 north star: actionable the client brand move
        cross_case_pattern    TEXT,              -- pattern across multiple cases
        confidence            REAL,
        raw_response          TEXT,              -- Sonnet natural-language output
        related_media_ids     TEXT,              -- JSON array of cross-referenced media.row_id
        model_used            TEXT NOT NULL,
        prompt_hash           TEXT,
        duration_ms           INTEGER,
        processed_at          TEXT NOT NULL,
        FOREIGN KEY (media_row_id) REFERENCES media(row_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_msb_media ON media_strategic_brief(media_row_id, processed_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_msb_processed ON media_strategic_brief(processed_at DESC)",

    # Audit — Sonnet daily 06:00 (N=20) + weekly Mon 07:00 (N=100).
    # Drives the auto-improvement loop (improvement_proposed -> proposal md path).
    """CREATE TABLE IF NOT EXISTS pipeline_audit (
        row_id                INTEGER PRIMARY KEY AUTOINCREMENT,
        audit_kind            TEXT NOT NULL,     -- daily | weekly
        sample_size           INTEGER NOT NULL,
        sample_mix_json       TEXT,              -- bucket distribution
        sampled_media_ids     TEXT,              -- JSON array
        qwen_acc              REAL,
        haiku_acc             REAL,
        qwen_disagreements    INTEGER,
        haiku_disagreements   INTEGER,
        failure_modes_json    TEXT,
        alert_level           TEXT NOT NULL DEFAULT 'none',  -- none | warning | critical
        improvement_proposed  TEXT,              -- path to proposal md if generated
        fix_validated_at      TEXT,              -- set after 3 days post-fix improvement
        audit_model           TEXT NOT NULL,     -- sonnet (via claude.exe)
        audited_at            TEXT NOT NULL,
        notes                 TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pa_kind_date ON pipeline_audit(audit_kind, audited_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_pa_alert ON pipeline_audit(alert_level, audited_at DESC) WHERE alert_level IN ('warning','critical')",
]


# V9: KB promotion bookkeeping — track which admit rows have been promoted
# into cards table by promote_to_kb.py. Idempotent re-runs skip already-
# promoted rows. Boss directive 2026-05-08: 「admit row → KB 卡片」.
SCHEMA_V9_MIGRATIONS = [
    "ALTER TABLE media_kb_decision ADD COLUMN promoted_at TEXT",
    "ALTER TABLE media_kb_decision ADD COLUMN promoted_card_row_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_mkd_unpromoted ON media_kb_decision(processed_at) WHERE kb_admit=1 AND promoted_at IS NULL",
]


# V10: ASR accuracy-proxy audit. Scheduled LLM judge for sampled voice/video
# transcripts. v1 is reference-free: it compares stored transcript, audit-pass
# transcript, language/confidence metadata, and source context; it does NOT
# claim literal WER until an audio-capable LLM path is automated.
SCHEMA_V10_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS asr_audit (
        row_id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        audit_kind                  TEXT NOT NULL,      -- daily | weekly
        sample_size                 INTEGER NOT NULL,
        sample_mix_json             TEXT,
        sampled_media_ids           TEXT,
        avg_accuracy_proxy          REAL,
        usable_rate                 REAL,
        language_suspicious_count   INTEGER,
        low_confidence_count        INTEGER,
        alert_level                 TEXT NOT NULL DEFAULT 'none',
        audit_model                 TEXT NOT NULL,
        audio_level_judge_available INTEGER NOT NULL DEFAULT 0,
        results_json                TEXT,
        audited_at                  TEXT NOT NULL,
        notes                       TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_asr_audit_kind_date ON asr_audit(audit_kind, audited_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_asr_audit_alert ON asr_audit(alert_level, audited_at DESC) WHERE alert_level IN ('warning','critical')",
]


# V11: ASR transcript quality gate. ASR audit can mark transcripts as
# usable / low_confidence / exclude so future KB or rules consumers only use
# audio text that passed an explicit quality gate.
SCHEMA_V11_MIGRATIONS = [
    "ALTER TABLE media ADD COLUMN transcript_lang_prob REAL",
    "ALTER TABLE media ADD COLUMN transcript_quality TEXT DEFAULT 'unknown'",
    "ALTER TABLE media ADD COLUMN transcript_quality_at TEXT",
    "ALTER TABLE media ADD COLUMN transcript_quality_note TEXT",
    "CREATE INDEX IF NOT EXISTS idx_media_transcript_quality ON media(transcript_quality, processed_at DESC)",
]


def init_db() -> None:
    conn = get_connection()
    try:
        cur = conn.execute("PRAGMA user_version")
        version = cur.fetchone()[0]
        if version < 1:
            conn.executescript(SCHEMA_V1)
            version = 1
            conn.execute("PRAGMA user_version = 1")
        if version < 2:
            for stmt in SCHEMA_V2_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    # ALTER TABLE is not idempotent in SQLite; tolerate "duplicate column"
                    # so reruns after partial migrations do not crash.
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("PRAGMA user_version = 2")
            version = 2
        if version < 3:
            for stmt in SCHEMA_V3_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("PRAGMA user_version = 3")
            version = 3
        if version < 4:
            for stmt in SCHEMA_V4_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("PRAGMA user_version = 4")
            version = 4
        if version < 5:
            for stmt in SCHEMA_V5_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("PRAGMA user_version = 5")
            version = 5
        if version < 6:
            for stmt in SCHEMA_V6_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("PRAGMA user_version = 6")
            version = 6
        if version < 7:
            for stmt in SCHEMA_V7_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("PRAGMA user_version = 7")
            version = 7
        if version < 8:
            for stmt in SCHEMA_V8_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("PRAGMA user_version = 8")
            version = 8
        if version < 9:
            for stmt in SCHEMA_V9_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("PRAGMA user_version = 9")
            version = 9
        if version < 10:
            for stmt in SCHEMA_V10_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("PRAGMA user_version = 10")
            version = 10
        if version < 11:
            for stmt in SCHEMA_V11_MIGRATIONS:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            conn.execute("PRAGMA user_version = 11")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at db version {CURRENT_VERSION}")
