-- M3->M4 production: pgvector embeddings in main megabrain DB.
-- Extension 'vector' must be installed by a superuser prior to this migration.
-- If the extension is unavailable (e.g. ephemeral test DBs where the schema is
-- dropped between runs), this migration is a no-op and is still recorded as
-- applied — the production deployment script verifies the extension exists.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            event_id        TEXT NOT NULL,
            content_hash    TEXT NOT NULL,           -- sha256 of embedded content
            model           TEXT NOT NULL,           -- e.g. bge-m3-int8-onnx
            model_version   TEXT NOT NULL,           -- e.g. xenova-bge-m3-onnx-int8-512
            dimension       INTEGER NOT NULL,
            embedding       vector(1024) NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            indexed_at      TIMESTAMPTZ,
            CONSTRAINT pk_memory_embeddings PRIMARY KEY (event_id, model_version),
            CONSTRAINT uq_memory_embeddings_content UNIQUE (event_id, content_hash, model_version)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
            ON memory_embeddings (model_version);
    ELSE
        RAISE NOTICE 'pgvector extension not present; skipping memory_embeddings (test/ephemeral DB)';
    END IF;
END
$$;
