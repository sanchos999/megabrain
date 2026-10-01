-- Exact revision boundaries for Context Capsule delta retrieval.
ALTER TABLE events ADD COLUMN IF NOT EXISTS project_revision BIGINT;
CREATE INDEX IF NOT EXISTS idx_events_project_revision
    ON events (project_id, project_revision)
    WHERE project_revision IS NOT NULL;

-- Existing imported rows did not carry the boundary. Reconstruct their stable
-- chronological order once; all new writes use the exact write-time value.
WITH ranked AS (
    SELECT event_id,
           row_number() OVER (
               PARTITION BY project_id
               ORDER BY created_at, observed_at, event_id
           ) + 1 AS revision
    FROM events
    WHERE project_id IS NOT NULL AND project_revision IS NULL
)
UPDATE events e
SET project_revision = ranked.revision
FROM ranked
WHERE e.event_id = ranked.event_id;
