-- Canonical memory retrieval: semantic index over derived memory_items.
-- Raw events remain the immutable evidence/fallback index.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        CREATE TABLE IF NOT EXISTS memory_item_embeddings (
            item_id        TEXT NOT NULL REFERENCES memory_items(item_id) ON DELETE CASCADE,
            content_hash   TEXT NOT NULL,
            model          TEXT NOT NULL,
            model_version  TEXT NOT NULL,
            dimension      INTEGER NOT NULL,
            embedding      vector(1024) NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            indexed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_memory_item_embeddings PRIMARY KEY (item_id, model_version),
            CONSTRAINT uq_memory_item_embeddings_content UNIQUE (item_id, content_hash, model_version)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_item_embeddings_model
            ON memory_item_embeddings (model_version);
        CREATE INDEX IF NOT EXISTS idx_memory_item_embeddings_hnsw
            ON memory_item_embeddings USING hnsw (embedding vector_cosine_ops)
            WITH (m=16, ef_construction=200);
    ELSE
        RAISE NOTICE 'pgvector extension not present; skipping memory_item_embeddings';
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_memory_items_fts
    ON memory_items USING gin (to_tsvector('simple', content::text));
