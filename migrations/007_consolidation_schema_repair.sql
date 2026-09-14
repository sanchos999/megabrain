-- Repair 0.1.1 scheduler schema when migration 005 was applied from an older draft.
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS batch_id TEXT;
UPDATE consolidation_runs
   SET batch_id = 'legacy_' || md5(project_id || ':' || input_hash)
 WHERE batch_id IS NULL;
ALTER TABLE consolidation_runs ALTER COLUMN batch_id SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_consolidation_runs_project_batch
    ON consolidation_runs (project_id, batch_id);
