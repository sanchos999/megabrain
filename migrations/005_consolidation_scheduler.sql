-- MegaBrain 0.1.1: durable project-scoped consolidation scheduler.
CREATE TABLE IF NOT EXISTS consolidation_projects (
    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
    first_dirty_at TIMESTAMPTZ NOT NULL,
    last_dirty_at TIMESTAMPTZ NOT NULL,
    pending_event_count INTEGER NOT NULL DEFAULT 0,
    last_consolidated_event_id TEXT,
    next_eligible_at TIMESTAMPTZ NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error_at TIMESTAMPTZ,
    last_error TEXT,
    last_success_at TIMESTAMPTZ,
    budget_paused BOOLEAN NOT NULL DEFAULT FALSE,
    paused BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_consolidation_projects_ready
    ON consolidation_projects (next_eligible_at, first_dirty_at)
    WHERE pending_event_count > 0 AND paused = false AND budget_paused = false;

CREATE TABLE IF NOT EXISTS consolidation_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    batch_id TEXT NOT NULL,
    from_event_id TEXT NOT NULL,
    to_event_id TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    input_hash TEXT NOT NULL,
    model_requested TEXT NOT NULL,
    model_selected TEXT,
    provider TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    estimated_cost DOUBLE PRECISION,
    derived_items_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_class TEXT,
    UNIQUE (project_id, batch_id),
    UNIQUE (project_id, input_hash)
);
CREATE TABLE IF NOT EXISTS consolidation_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
