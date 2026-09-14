-- MegaBrain 0.1.1: durable consolidation dispatch rate guards.
ALTER TABLE consolidation_projects ADD COLUMN IF NOT EXISTS rate_paused BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE consolidation_projects ADD COLUMN IF NOT EXISTS last_block_reason TEXT;
ALTER TABLE consolidation_projects ADD COLUMN IF NOT EXISTS next_eligible_at TIMESTAMPTZ;
UPDATE consolidation_projects SET next_eligible_at=COALESCE(next_eligible_at, now()) WHERE next_eligible_at IS NULL;
ALTER TABLE consolidation_projects ALTER COLUMN next_eligible_at SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_consolidation_runs_dispatch_window
    ON consolidation_runs (started_at, status);
