-- M2: durable history import. Sources are immutable; import is idempotent
-- via source_records ledger (unique source identity).

-- ============ IMPORT BATCHES ============
CREATE TABLE IF NOT EXISTS import_batches (
    import_batch_id   TEXT PRIMARY KEY,
    source_system     TEXT NOT NULL,
    mode              TEXT NOT NULL,                  -- import / dry-run / resume
    status            TEXT NOT NULL DEFAULT 'PLANNED'
                      CHECK (status IN ('PLANNED','RUNNING','COMPLETED','PARTIAL','FAILED')),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    stats             JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ============ SOURCE RECORD MAP (idempotency ledger) ============
CREATE TABLE IF NOT EXISTS source_records (
    source_system        TEXT NOT NULL,
    source_instance      TEXT NOT NULL DEFAULT 'default',
    source_record_id     TEXT NOT NULL,
    megabrain_event_id   TEXT,
    source_session_id    TEXT,
    source_hash          TEXT,
    match_type           TEXT NOT NULL DEFAULT 'IMPORTED'
                         CHECK (match_type IN ('IMPORTED','DUPLICATE_SOURCE','MATCHED_TO_HERMES','HONCHO_ONLY','AMBIGUOUS')),
    import_batch_id      TEXT REFERENCES import_batches(import_batch_id),
    ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, source_instance, source_record_id)
);
CREATE INDEX IF NOT EXISTS idx_source_records_session ON source_records (source_system, source_session_id);
CREATE INDEX IF NOT EXISTS idx_source_records_hash    ON source_records (source_hash);

-- ============ IMPORTED SESSIONS ============
CREATE TABLE IF NOT EXISTS import_sessions (
    megabrain_session_id  TEXT NOT NULL,
    source_system         TEXT NOT NULL,
    source_session_id     TEXT NOT NULL,
    started_at            TIMESTAMPTZ,
    ended_at              TIMESTAMPTZ,
    project_id            TEXT,
    metadata              JSONB NOT NULL DEFAULT '{}'::jsonb,
    event_count           BIGINT NOT NULL DEFAULT 0,
    import_batch_id       TEXT REFERENCES import_batches(import_batch_id),
    ingested_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, source_session_id)
);

-- ============ IMPORT CHECKPOINTS (durable, in PG) ============
CREATE TABLE IF NOT EXISTS import_checkpoints (
    source_system     TEXT NOT NULL,
    scope             TEXT NOT NULL DEFAULT 'default',
    cursor_id         TEXT NOT NULL,
    processed         BIGINT NOT NULL DEFAULT 0,
    inserted          BIGINT NOT NULL DEFAULT 0,
    duplicates        BIGINT NOT NULL DEFAULT 0,
    failed            BIGINT NOT NULL DEFAULT 0,
    last_success_at   TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, scope)
);

-- ============ IMPORT ERRORS (dead-letter, recoverable) ============
CREATE TABLE IF NOT EXISTS import_errors (
    id               BIGSERIAL PRIMARY KEY,
    batch_id         TEXT NOT NULL,
    source_system    TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    error_class      TEXT NOT NULL,
    error_code       TEXT,
    attempts         INTEGER NOT NULL DEFAULT 1,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_import_errors_batch ON import_errors (batch_id, source_system);

-- ============ EVENTS: import provenance columns ============
-- source_created_at: original event time in source (temporal truth).
-- created_at keeps MegaBrain semantic role (same value for imports).
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_created_at TIMESTAMPTZ;
ALTER TABLE events ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMPTZ;
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_content_hash TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS import_batch_id TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS importer_version TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS original_sequence BIGINT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_session_id TEXT;
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_parent_id TEXT;
CREATE INDEX IF NOT EXISTS idx_events_source_record ON events (source, source_session_id, original_sequence)
    WHERE original_sequence IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_created_asc ON events (created_at ASC);

-- ============ IMPORTED HISTORY PROJECT BUCKET ============
INSERT INTO projects (project_id, name, status)
VALUES ('imported_history_unassigned', 'Импортированная история (UNASSIGNED)', 'ACTIVE')
ON CONFLICT (project_id) DO NOTHING;

-- ============ FTS (derived, rebuildable; exact filters use plain indexes) ============
ALTER TABLE events ADD COLUMN IF NOT EXISTS fts tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', coalesce(payload->>'text','') || ' ' || coalesce(payload->>'content',''))) STORED;
CREATE INDEX IF NOT EXISTS idx_events_fts ON events USING GIN (fts);
