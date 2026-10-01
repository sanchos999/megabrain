-- Denormalize project scope on raw embeddings so filtered HNSW scans do not
-- need to discover the project only after joining every candidate to events.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
              WHERE table_name = 'memory_embeddings') THEN
        ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS project_id TEXT;
        UPDATE memory_embeddings me
        SET project_id = e.project_id
        FROM events e
        WHERE e.event_id = me.event_id AND me.project_id IS NULL;
        CREATE INDEX IF NOT EXISTS idx_memory_embeddings_project
            ON memory_embeddings (project_id, model_version);
    END IF;
END
$$;
