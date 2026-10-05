-- Fast scoped lookup from retrieved immutable episodes to explicit memories.
CREATE INDEX IF NOT EXISTS idx_memory_items_source_events_gin
    ON memory_items USING gin (source_event_ids);
