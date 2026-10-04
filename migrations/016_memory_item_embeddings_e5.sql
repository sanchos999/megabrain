-- Additive, reversible side index for experimental multilingual-e5-small.
-- BGE-M3 vectors remain untouched and continue to serve as the fallback.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        CREATE TABLE IF NOT EXISTS memory_item_embeddings_e5 (
            item_id        TEXT NOT NULL REFERENCES memory_items(item_id) ON DELETE CASCADE,
            content_hash   TEXT NOT NULL,
            model          TEXT NOT NULL,
            model_version  TEXT NOT NULL,
            dimension      INTEGER NOT NULL CHECK (dimension = 384),
            embedding      vector(384) NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            indexed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT pk_memory_item_embeddings_e5 PRIMARY KEY (item_id, model_version),
            CONSTRAINT uq_memory_item_embeddings_e5_content
                UNIQUE (item_id, content_hash, model_version)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_item_embeddings_e5_model
            ON memory_item_embeddings_e5 (model_version);
        CREATE INDEX IF NOT EXISTS idx_memory_item_embeddings_e5_hnsw
            ON memory_item_embeddings_e5 USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 120);
    END IF;
END
$$;
