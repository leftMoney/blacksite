-- ============================================================================
-- Blacksite KB Phase 0 migration — v7 → v8 (DRAFT, NON-DESTRUCTIVE)
-- ============================================================================
--
-- Purpose:
--   Phase 0 KB-readiness per kb/DESIGN.md §13 / §19. Adds the minimum schema
--   surface needed for the v0 chunk loader (kb/loader_v0.py) and §3 four-layer
--   abstraction (Document → Chunk → Entity → Relationship → Card) without
--   touching v7 ingestion. Cards already exist as v4. This migration covers
--   the missing two: kb_documents + kb_chunks, plus a relationships edge
--   table and a cross-instance-id column on entities (per §15-3 / §23.2
--   default — schema preserved early so PH/VN/ID instances can plug in).
--
-- Phase 0 scope discipline (per CLAUDE.md §6.4 + DESIGN §17):
--   • Additive only. No DROP. No ALTER on v7 columns. No data mutation.
--   • Every timestamp column = TEXT NOT NULL with explicit +HH:MM offset.
--   • CHECK constraints reject inserts that don't carry an offset suffix
--     (per §6.4 audit — engine self-rejects naive datetimes at write time).
--   • Manual review by boss before applying. NEVER auto-applied.
--
-- Change log:
--   v8.1  2026-05-02  initial draft — kb_documents, kb_chunks,
--                     kb_relationships, entities.cross_instance_id
--
-- Tables added (3): kb_documents, kb_chunks, kb_relationships
-- Columns added (1): entities.cross_instance_id
-- Indexes added (12)
--
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. kb_documents — canonical Document layer (one row per source observation
--    that can be sliced into chunks). Unifies messages + media + future
--    page-snapshots under a single doc_id namespace. v7 messages.row_id +
--    media.row_id are pointed at via source_kind/source_row_id, NOT FK,
--    so v7 tables are untouched.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kb_documents (
    doc_id              TEXT PRIMARY KEY,                    -- e.g. "msg:12345" / "media:678"
    source_kind         TEXT NOT NULL,                       -- message | media | page_snapshot
    source_row_id       INTEGER NOT NULL,                    -- v7 messages.row_id or media.row_id
    platform            TEXT NOT NULL,                       -- telegram | bigo | streamB | ...
    persona             TEXT,                                -- P01 / P02 / NULL (anonymous)
    -- §6.4 KB schema contract — three offset-aware timestamps
    observed_at         TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
                                                             -- when engine ingested this observation
    event_at            TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
                                                             -- when the underlying event occurred (publish time)
    valid_from          TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
                                                             -- start of fact validity window (defaults = event_at)
    valid_to            TEXT,                                -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
                                                             -- nullable — open-ended until decay rule fires
    -- provenance (per DESIGN §4.2)
    source_blob_hash    TEXT,                                -- SHA256 of raw blob (or message content_hash fallback)
    raw_pointer_json    TEXT,                                -- {raw_path, raw_offset} for §10.5 ledger
    -- ingestion bookkeeping (uses canonical pattern from CLAUDE.md §6.4(6))
    indexed_at          TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
    schema_version      INTEGER NOT NULL DEFAULT 8,
    -- §6.4(2) audit: reject naive timestamps at write time
    CHECK (observed_at GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (event_at    GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (valid_from  GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (valid_to IS NULL OR valid_to GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (indexed_at  GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    UNIQUE(source_kind, source_row_id)
);

CREATE INDEX IF NOT EXISTS idx_kb_documents_platform_observed ON kb_documents(platform, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_documents_event_at ON kb_documents(event_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_documents_persona ON kb_documents(persona, observed_at DESC) WHERE persona IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_kb_documents_source ON kb_documents(source_kind, source_row_id);

-- ----------------------------------------------------------------------------
-- 2. kb_chunks — semantic-chunk layer (one row per embeddable unit). For
--    Phase 0 a chunk is 1:1 with a document (single-message text); Phase 1
--    will introduce sub-message chunking + bge-m3 embeddings.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kb_chunks (
    chunk_id            TEXT PRIMARY KEY,                    -- e.g. "msg:12345#0"
    doc_id              TEXT NOT NULL,                       -- FK kb_documents.doc_id (logical, not enforced for cross-FK safety)
    chunk_index         INTEGER NOT NULL DEFAULT 0,          -- ordinal within doc (0 = whole-doc chunk)
    text                TEXT NOT NULL,                       -- the chunk content
    text_len            INTEGER NOT NULL,                    -- char count (cheap pre-embedding gate)
    -- §6.4 KB schema contract — inherited but stored for query speed
    observed_at         TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
    event_at            TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
    valid_from          TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
    valid_to            TEXT,                                -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
    -- platform context for fast filtering (denormalized from kb_documents)
    platform            TEXT NOT NULL,
    persona             TEXT,
    -- §6.1 signal score components (Phase 0 leaves NULL; Phase 1 value_gate fills)
    score_signals_json  TEXT,                                -- {entity_density, amplification, novelty, corroboration, intent_polarity, source_trust}
    signal_score        REAL,                                -- aggregated [0,1]
    decay_class         TEXT NOT NULL DEFAULT '14d',         -- structural | 30d | 14d | 7d (per §9.3)
    -- vector slot (Phase 1 fills, Qdrant point id mirror)
    embedding_model     TEXT,                                -- 'bge-m3' / NULL until embedded
    embedding_at        TEXT,                                -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
    -- bookkeeping
    indexed_at          TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
    -- §6.4(2) audit
    CHECK (observed_at  GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (event_at     GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (valid_from   GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (valid_to IS NULL OR valid_to GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (embedding_at IS NULL OR embedding_at GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (indexed_at   GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (decay_class IN ('structural', '30d', '14d', '7d')),
    UNIQUE(doc_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_platform_observed ON kb_chunks(platform, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_event_at ON kb_chunks(event_at DESC);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_unembedded ON kb_chunks(indexed_at) WHERE embedding_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_kb_chunks_signal_score ON kb_chunks(signal_score DESC) WHERE signal_score IS NOT NULL;
-- Phase-0 simplification: no valid_to expiry index. No decay-rotation logic
-- yet, so indexing valid_to costs writes for zero current reads. Phase 2
-- (cadence sweepers) adds it when entity_decay starts mutating valid_to.

-- ----------------------------------------------------------------------------
-- 3. kb_relationships — directed edge layer (entity → entity, chunk-anchored).
--    Phase 0 leaves this empty; Phase 1+ populates from rules-layer +
--    funnel_edges. Schema declared early so loader output can reference.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS kb_relationships (
    rel_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    src_entity_row_id   INTEGER NOT NULL,                    -- entities.row_id
    dst_entity_row_id   INTEGER NOT NULL,                    -- entities.row_id
    rel_kind            TEXT NOT NULL,                       -- mentions | promotes | shares_sender | same_operator | mentor | competitor | cutoff (per §18.1.2)
    weight              REAL NOT NULL DEFAULT 1.0,           -- 0..1 trust / confidence (per §18.1.2)
    observed_freq       INTEGER NOT NULL DEFAULT 1,          -- co-occurrence count
    inferred_only       INTEGER NOT NULL DEFAULT 0,          -- 0=direct evidence / 1=inferred-only
    evidence_chunk_ids_json TEXT,                            -- JSON list of kb_chunks.chunk_id
    -- §6.4 KB schema contract
    first_observed_at   TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
    last_observed_at    TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
    valid_from          TEXT NOT NULL,                       -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4)
    valid_to            TEXT,                                -- ISO 8601 with +HH:MM offset (per CLAUDE.md §6.4) — nullable
    -- §6.4(2) audit
    CHECK (first_observed_at GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (last_observed_at  GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (valid_from        GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    CHECK (valid_to IS NULL OR valid_to GLOB '*[+-][0-9][0-9]:[0-9][0-9]'),
    UNIQUE(src_entity_row_id, dst_entity_row_id, rel_kind),
    FOREIGN KEY (src_entity_row_id) REFERENCES entities(row_id) ON DELETE CASCADE,
    FOREIGN KEY (dst_entity_row_id) REFERENCES entities(row_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_kb_rel_src ON kb_relationships(src_entity_row_id, rel_kind);
CREATE INDEX IF NOT EXISTS idx_kb_rel_dst ON kb_relationships(dst_entity_row_id, rel_kind);
CREATE INDEX IF NOT EXISTS idx_kb_rel_kind_last ON kb_relationships(rel_kind, last_observed_at DESC);

-- ----------------------------------------------------------------------------
-- 4. entities.cross_instance_id — added per §15-3 / §23.2 default. NULL until
--    cross-instance KB share (DESIGN §10.3) goes live; harmless on _TEMPLATE.
-- ----------------------------------------------------------------------------
ALTER TABLE entities ADD COLUMN cross_instance_id TEXT;
CREATE INDEX IF NOT EXISTS idx_entities_xinst ON entities(cross_instance_id) WHERE cross_instance_id IS NOT NULL;

-- ----------------------------------------------------------------------------
-- 5. PRAGMA bump (only when migration is applied, not when this file is
--    parsed for review). The migrate runner sets this; not run by the file
--    itself so a sqlparse / sqlite3 .read inspection doesn't accidentally
--    flip the version.
-- ----------------------------------------------------------------------------
-- PRAGMA user_version = 8;     -- intentionally commented; migrate runner sets

-- ============================================================================
-- ROLLBACK (apply only if Phase 0 needs to be undone; verify no Phase 1
-- dependents first — Phase 1 ingestor.py + value_gate.py both write into
-- kb_chunks / kb_documents).
-- ============================================================================
-- DROP INDEX IF EXISTS idx_entities_xinst;
-- -- SQLite has no DROP COLUMN before 3.35; on older versions, leave the
-- -- column in place (harmless NULL) rather than table-rebuild.
-- -- For SQLite ≥ 3.35:
-- -- ALTER TABLE entities DROP COLUMN cross_instance_id;
--
-- DROP INDEX IF EXISTS idx_kb_rel_kind_last;
-- DROP INDEX IF EXISTS idx_kb_rel_dst;
-- DROP INDEX IF EXISTS idx_kb_rel_src;
-- DROP TABLE IF EXISTS kb_relationships;
--
-- DROP INDEX IF EXISTS idx_kb_chunks_signal_score;
-- DROP INDEX IF EXISTS idx_kb_chunks_unembedded;
-- DROP INDEX IF EXISTS idx_kb_chunks_event_at;
-- DROP INDEX IF EXISTS idx_kb_chunks_platform_observed;
-- DROP INDEX IF EXISTS idx_kb_chunks_doc;
-- DROP TABLE IF EXISTS kb_chunks;
--
-- DROP INDEX IF EXISTS idx_kb_documents_source;
-- DROP INDEX IF EXISTS idx_kb_documents_persona;
-- DROP INDEX IF EXISTS idx_kb_documents_event_at;
-- DROP INDEX IF EXISTS idx_kb_documents_platform_observed;
-- DROP TABLE IF EXISTS kb_documents;
--
-- PRAGMA user_version = 7;

-- TO APPLY: py db/migrate.py v8
