-- Fast exact lookup for explicit current memory identifiers. Semantic and
-- historical queries continue to use the hybrid FTS + vector path.
CREATE INDEX IF NOT EXISTS idx_memory_items_item_key_current
    ON memory_items (lower(content->>'item_key'))
    WHERE valid_to IS NULL AND confidence >= 0.9
      AND extractor_type = 'EXPLICIT';
