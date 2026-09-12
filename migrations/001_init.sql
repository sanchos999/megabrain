-- MegaBrain M1 schema. Source of truth: PostgreSQL.
-- All derived layers (RAM/Redis/FTS/vector/graph) are rebuildable from events.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ EVENTS (immutable raw log) ============
CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,          -- client-supplied or generated; never changes
    schema_version      INTEGER NOT NULL DEFAULT 1,
    source              TEXT NOT NULL,             -- e.g. "hermes", "model-router", "ide-agent"
    source_instance     TEXT,
    request_id          TEXT,
    turn_id             TEXT,
    sequence            BIGINT,
    session_id          TEXT,
    project_id          TEXT,
    event_type          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload             JSONB,                     -- small payload inline; large -> blob_ref
    payload_hash        TEXT,                      -- sha256 of canonical payload (excl. blob content)
    payload_size        BIGINT,
    blob_ref            TEXT,                      -- sha256 in blob store when payload offloaded
    blob_size           BIGINT,
    blob_mime           TEXT,
    model               TEXT,
    route               TEXT,
    provider            TEXT,
    parent_event_id     TEXT,
    correlation_id      TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_events_project_created ON events (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_session_created ON events (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_project_type   ON events (project_id, event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_turn           ON events (turn_id) WHERE turn_id IS NOT NULL;

-- ============ PROJECTS ============
CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ACTIVE'
                 CHECK (status IN ('ACTIVE','PAUSED','COMPLETED','ARCHIVED')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    revision     BIGINT NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_projects_status_updated ON projects (status, updated_at DESC);

-- ============ SESSION -> PROJECT MAPPING ============
CREATE TABLE IF NOT EXISTS session_project_map (
    session_id   TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(project_id),
    mapped_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ DERIVED STRUCTURED MEMORY (versioned, temporal) ============
-- kinds: PROJECT_STATE, DECISION, CONSTRAINT, TASK (+ schema-only future kinds)
CREATE TABLE IF NOT EXISTS memory_items (
    item_id           TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(project_id),
    kind              TEXT NOT NULL,
    -- temporal semantics
    valid_from        TIMESTAMPTZ NOT NULL,
    valid_to          TIMESTAMPTZ,                -- NULL = currently valid
    supersedes_id     TEXT REFERENCES memory_items(item_id),
    -- provenance
    source_event_ids  TEXT[] NOT NULL,
    confidence        REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
    extractor_type    TEXT NOT NULL DEFAULT 'DETERMINISTIC'
                      CHECK (extractor_type IN ('EXPLICIT','DETERMINISTIC','MANUAL','LLM')),
    extractor_version TEXT NOT NULL DEFAULT 'megabrain-m1',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- content (kind-specific JSON)
    content           JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mem_project_kind_valid
    ON memory_items (project_id, kind, valid_to NULLS FIRST, valid_from DESC);
CREATE INDEX IF NOT EXISTS idx_mem_supersedes ON memory_items (supersedes_id);

-- ============ PROJECT STATE REVISIONS (full versioned state snapshots) ============
CREATE TABLE IF NOT EXISTS project_state_revisions (
    project_id       TEXT NOT NULL REFERENCES projects(project_id),
    revision         BIGINT NOT NULL,
    state            JSONB NOT NULL,
    source_event_ids TEXT[] NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, revision)
);

-- ============ INGESTION CURSORS (for future importers M2) ============
CREATE TABLE IF NOT EXISTS ingestion_cursors (
    source      TEXT PRIMARY KEY,
    cursor_id   TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
