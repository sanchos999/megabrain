-- Repair installations where the legacy 003_m53e_project_identity.sql caused
-- 003_pgvector.sql to be skipped by the version-only migration ledger.
-- Idempotent for existing deployments and a no-op when pgvector is unavailable.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            event_id        TEXT NOT NULL,
            content_hash    TEXT NOT NULL,
            model           TEXT NOT NULL,
            model_version   TEXT NOT NULL,
            dimension       INTEGER NOT NULL,
            embedding       vector(1024) NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            indexed_at      TIMESTAMPTZ,
            project_id      TEXT,
            CONSTRAINT pk_memory_embeddings PRIMARY KEY (event_id, model_version),
            CONSTRAINT uq_memory_embeddings_content UNIQUE (event_id, content_hash, model_version)
        );
        ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS project_id TEXT;
        UPDATE memory_embeddings me
           SET project_id = e.project_id
          FROM events e
         WHERE e.event_id = me.event_id AND me.project_id IS NULL;
        CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
            ON memory_embeddings (model_version);
        CREATE INDEX IF NOT EXISTS idx_memory_embeddings_project
            ON memory_embeddings (project_id, model_version);
    END IF;
END
$$;
