-- M5.3E project identity and scoped lineage metadata.
ALTER TABLE session_project_map ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE session_project_map ADD COLUMN IF NOT EXISTS source_instance TEXT;
ALTER TABLE session_project_map ADD COLUMN IF NOT EXISTS channel TEXT;
ALTER TABLE session_project_map ADD COLUMN IF NOT EXISTS parent_session_id TEXT;
ALTER TABLE session_project_map ADD COLUMN IF NOT EXISTS conversation_id TEXT;
ALTER TABLE session_project_map ADD COLUMN IF NOT EXISTS mapping_reason TEXT;
ALTER TABLE session_project_map ADD COLUMN IF NOT EXISTS confidence REAL;

CREATE TABLE IF NOT EXISTS profile_project_map (
    source TEXT NOT NULL,
    profile TEXT NOT NULL,
    channel TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    mapping_reason TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, profile, channel)
);

CREATE TABLE IF NOT EXISTS project_mapping_conflicts (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT,
    existing_project_id TEXT,
    proposed_project_id TEXT,
    source TEXT,
    profile TEXT,
    channel TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
