-- Preserve event-order history independently from wall-clock valid time.
-- This enables exact as-of-project-revision retrieval when multiple events
-- share the same created_at timestamp; it does not alter valid_from/valid_to.
ALTER TABLE memory_items
    ADD COLUMN IF NOT EXISTS valid_from_revision BIGINT,
    ADD COLUMN IF NOT EXISTS valid_to_revision BIGINT;

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS project_revision_trusted BOOLEAN NOT NULL DEFAULT TRUE;

-- Migration 012 reconstructed the order of pre-existing events using a stable
-- timestamp/event-id sort. Exact timestamp ties from that imported legacy set
-- have no provable relative order; post-012 writes got their revision inline.
WITH ambiguous_events AS MATERIALIZED (
    SELECT e.event_id
    FROM events e
    JOIN (
        SELECT project_id, created_at, observed_at
        FROM events
        GROUP BY project_id, created_at, observed_at
        HAVING count(*) > 1
    ) tied USING (project_id, created_at, observed_at)
), revision_cutoff AS (
    SELECT applied_at FROM schema_migrations WHERE version = 12
)
UPDATE events e
SET project_revision_trusted = false
FROM ambiguous_events ambiguous
WHERE ambiguous.event_id = e.event_id
  AND e.project_revision IS NOT NULL
  AND e.observed_at < (SELECT applied_at FROM revision_cutoff)
  AND e.project_revision_trusted IS TRUE;

-- Backfill the earliest trustworthy availability boundary from immutable
-- provenance. Rows without a revision-bearing source remain NULL (unordered).
WITH source_revisions AS (
    SELECT mi.item_id, max(e.project_revision) AS revision
    FROM memory_items mi
    JOIN events e ON e.event_id = ANY(mi.source_event_ids)
                 AND e.project_id = mi.project_id
    WHERE e.project_revision_trusted IS TRUE
      AND NOT EXISTS (
          SELECT 1
          FROM unnest(mi.source_event_ids) AS source_id(event_id)
          LEFT JOIN events source_event ON source_event.event_id = source_id.event_id
          WHERE source_event.event_id IS NULL
             OR source_event.project_id <> mi.project_id
             OR source_event.project_revision_trusted IS NOT TRUE
      )
    GROUP BY mi.item_id
)
UPDATE memory_items mi
SET valid_from_revision = sr.revision
FROM source_revisions sr
WHERE mi.item_id = sr.item_id
  AND mi.valid_from_revision IS NULL;

-- A superseded item's revision interval ends when its first successor became
-- available. Ties remain distinguishable even where valid_to = valid_from.
WITH successor_revisions AS (
    SELECT supersedes_id, min(valid_from_revision) AS revision
    FROM memory_items
    WHERE supersedes_id IS NOT NULL AND valid_from_revision IS NOT NULL
    GROUP BY supersedes_id
)
UPDATE memory_items old
SET valid_to_revision = successor_revisions.revision
FROM successor_revisions
WHERE successor_revisions.supersedes_id = old.item_id
  AND old.valid_to_revision IS NULL;

-- Terminal task/status rows become inactive at the revision that asserted the
-- terminal state, just as their valid-time interval closes at valid_from.
UPDATE memory_items
SET valid_to_revision = valid_from_revision
WHERE valid_to IS NOT NULL
  AND valid_from_revision IS NOT NULL
  AND valid_to_revision IS NULL
  AND coalesce(content->>'status', '') IN
      ('DONE','CANCELLED','REVOKED','SUPERSEDED');

CREATE INDEX IF NOT EXISTS idx_memory_items_project_revision_valid
    ON memory_items (project_id, valid_from_revision, valid_to_revision);
