-- db/migrations/v9_lead_opinion_pipeline.sql
-- ============================================================================
-- v9 — boss_opinions (Phase 0) + kb_leads (P1-P4 lead-to-lifecycle pipeline)
-- ============================================================================
-- Applied: 2026-05-02 GMT+7 (after v8 KB Phase 0).
-- Both tables additive (CREATE IF NOT EXISTS), no v7/v8 mutation.
-- All timestamp columns ISO 8601 with explicit +HH:MM offset (CLAUDE.md §6.4).
-- Rollback section at end.
-- ============================================================================

BEGIN;

-- =============== boss_opinions ===============
-- Extracted from instances/<inst>/runtime/cmd/conversation.jsonl by
-- processors/commander_opinion_extractor.py (cron */15 min).
-- Allows any session to query: "boss 對 X 議題的歷史意見"
CREATE TABLE IF NOT EXISTS boss_opinions (
    opinion_id      TEXT PRIMARY KEY,                        -- 'O-2026-05-02-001'
    source_role     TEXT NOT NULL,                            -- always 'boss' for now (room for 'commander' future)
    source_ts       TEXT NOT NULL                             -- ISO 8601 +HH:MM
                    CHECK(source_ts GLOB '*-*-*T*+*:*'),
    source_offset   INTEGER,                                  -- line index in conversation.jsonl
    extracted_at    TEXT NOT NULL                             -- when LLM extractor ran
                    CHECK(extracted_at GLOB '*-*-*T*+*:*'),
    topic           TEXT,                                     -- 'kb_design' | 'persona_opsec' | etc
    kind            TEXT NOT NULL                             -- semantic category
                    CHECK(kind IN ('directive','preference','decision','question','concern','feedback')),
    content         TEXT NOT NULL,                            -- boss's actual statement (verbatim)
    context_summary TEXT,                                     -- 1-line surrounding context
    refs            TEXT,                                     -- JSON array of related history_id / paths
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','superseded','resolved','archived')),
    superseded_by   TEXT                                      -- opinion_id that overrides this one
);
CREATE INDEX IF NOT EXISTS idx_boss_opinions_topic    ON boss_opinions(topic);
CREATE INDEX IF NOT EXISTS idx_boss_opinions_kind     ON boss_opinions(kind);
CREATE INDEX IF NOT EXISTS idx_boss_opinions_ts       ON boss_opinions(source_ts DESC);
CREATE INDEX IF NOT EXISTS idx_boss_opinions_status   ON boss_opinions(status);

-- track extractor progress (last processed conversation.jsonl offset)
CREATE TABLE IF NOT EXISTS commander_extractor_state (
    file_path       TEXT PRIMARY KEY,                         -- absolute path to conversation.jsonl
    last_offset     INTEGER NOT NULL DEFAULT 0,               -- line count processed
    last_run_at     TEXT NOT NULL                             -- ISO 8601 +HH:MM
                    CHECK(last_run_at GLOB '*-*-*T*+*:*')
);

-- =============== kb_leads (P1-P4) ===============
-- Intel leads emitted by daily_brief LLM analyst as JSON sidecar.
-- Lifecycle: pending → triaged → executing → executed → resolved_{closed,escalate,...}
CREATE TABLE IF NOT EXISTS kb_leads (
    lead_id           TEXT PRIMARY KEY,                       -- 'L-2026-05-02-001'
    origin            TEXT NOT NULL,                          -- 'brief_2026-05-02' | 'cron_observation_<id>' | etc
    origin_ref        TEXT,                                   -- path to source brief or history_id
    emitted_at        TEXT NOT NULL                           -- ISO 8601 +HH:MM
                      CHECK(emitted_at GLOB '*-*-*T*+*:*'),

    -- analyst-emitted classification
    type              TEXT NOT NULL,                          -- 'sql_sample' | 'whois_lookup' | 'tier_upgrade' | 'code_fix_regex' | 'cross_platform_verify' | 'agent_strategy_change' | 'observation_cron' | 'card_builder_check'
    target            TEXT,                                   -- structured: 'chat_username=example-user-07' | 'entity:examplebet' | 'agent:bigo' | 'domain:examplebrand.me'
    suggested_action  TEXT NOT NULL,                          -- human/LLM-readable instruction
    confidence        REAL NOT NULL DEFAULT 0.5
                      CHECK(confidence >= 0 AND confidence <= 1),
    actionability     REAL NOT NULL DEFAULT 0.5
                      CHECK(actionability >= 0 AND actionability <= 1),
    reversibility     TEXT
                      CHECK(reversibility IS NULL OR reversibility IN ('safe','reversible','medium','destructive')),
    auto_safe         INTEGER NOT NULL DEFAULT 0
                      CHECK(auto_safe IN (0,1)),

    -- triage outcome (Stage 2)
    triage_lane       TEXT
                      CHECK(triage_lane IS NULL OR triage_lane IN ('AUTO_SAFE_EXEC','AUTO_SCHEDULE','SUBAGENT_DISPATCH','BOSS_ESCALATE','CLOSE_AS_NOISE')),
    triaged_at        TEXT
                      CHECK(triaged_at IS NULL OR triaged_at GLOB '*-*-*T*+*:*'),

    -- lifecycle state
    state             TEXT NOT NULL DEFAULT 'pending'
                      CHECK(state IN ('pending','triaged','executing','executed','escalated','resolved_closed','resolved_escalate','re_queued','conflict_flag')),
    evidence          TEXT,                                   -- JSON: execution results (sql_rows, whois data, etc)
    resolution        TEXT,                                   -- one-line resolution summary
    resolution_at     TEXT
                      CHECK(resolution_at IS NULL OR resolution_at GLOB '*-*-*T*+*:*'),
    re_queued_until   TEXT
                      CHECK(re_queued_until IS NULL OR re_queued_until GLOB '*-*-*T*+*:*'),

    -- chaining
    parent_lead_id    TEXT,                                   -- follow-up from a prior lead
    refs              TEXT                                    -- JSON array
);
CREATE INDEX IF NOT EXISTS idx_kb_leads_state         ON kb_leads(state);
CREATE INDEX IF NOT EXISTS idx_kb_leads_target        ON kb_leads(target);
CREATE INDEX IF NOT EXISTS idx_kb_leads_type          ON kb_leads(type);
CREATE INDEX IF NOT EXISTS idx_kb_leads_emitted       ON kb_leads(emitted_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_leads_lane          ON kb_leads(triage_lane);
CREATE INDEX IF NOT EXISTS idx_kb_leads_re_queued     ON kb_leads(re_queued_until)
    WHERE re_queued_until IS NOT NULL;

COMMIT;

-- ============================================================================
-- ROLLBACK (manual; do not auto-run)
-- ============================================================================
-- BEGIN;
-- DROP INDEX IF EXISTS idx_kb_leads_re_queued;
-- DROP INDEX IF EXISTS idx_kb_leads_lane;
-- DROP INDEX IF EXISTS idx_kb_leads_emitted;
-- DROP INDEX IF EXISTS idx_kb_leads_type;
-- DROP INDEX IF EXISTS idx_kb_leads_target;
-- DROP INDEX IF EXISTS idx_kb_leads_state;
-- DROP TABLE IF EXISTS kb_leads;
-- DROP INDEX IF EXISTS idx_boss_opinions_status;
-- DROP INDEX IF EXISTS idx_boss_opinions_ts;
-- DROP INDEX IF EXISTS idx_boss_opinions_kind;
-- DROP INDEX IF EXISTS idx_boss_opinions_topic;
-- DROP TABLE IF EXISTS boss_opinions;
-- DROP TABLE IF EXISTS commander_extractor_state;
-- COMMIT;

-- TO APPLY (run from the repo root): py -c "import sqlite3; conn=sqlite3.connect(r'instances\_TEMPLATE\runtime\index.db'); conn.executescript(open(r'db\migrations\v9_lead_opinion_pipeline.sql', encoding='utf-8').read()); conn.commit(); conn.close()"
