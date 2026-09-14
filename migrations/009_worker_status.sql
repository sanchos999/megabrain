-- MegaBrain 0.1.2 worker operational state.
CREATE TABLE IF NOT EXISTS worker_status (
    component TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    version TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_success_at TIMESTAMPTZ,
    last_error_at TIMESTAMPTZ,
    last_error_class TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    processed_items BIGINT NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'STARTING',
    detail TEXT
);
