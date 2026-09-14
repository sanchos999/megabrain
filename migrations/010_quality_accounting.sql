-- MegaBrain 0.1.2: honest token/cost accounting and quality escalation audit.
-- UNKNOWN is stored as NULL with an explicit status; zeros are never fabricated.
ALTER TABLE consolidation_runs ALTER COLUMN tokens_in DROP NOT NULL;
ALTER TABLE consolidation_runs ALTER COLUMN tokens_out DROP NOT NULL;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS tokens_in_status TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS tokens_out_status TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS tokens_in_estimated INTEGER;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS tokens_out_estimated INTEGER;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS cost_status TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS escalation_stage INTEGER NOT NULL DEFAULT 1;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS escalation_reason TEXT;
ALTER TABLE consolidation_runs ADD COLUMN IF NOT EXISTS wait_reason TEXT;
-- Historical zeros written without Router usage metadata are UNKNOWN, not 0.
UPDATE consolidation_runs
   SET tokens_in = NULL, tokens_in_status = 'UNKNOWN',
       tokens_out = NULL, tokens_out_status = 'UNKNOWN', cost_status = 'UNKNOWN'
 WHERE tokens_in = 0 AND tokens_out = 0 AND estimated_cost IS NULL
   AND status IN ('success', 'failed');
-- Index for the daily input-token guard (reported, else estimated).
CREATE INDEX IF NOT EXISTS idx_consolidation_runs_day_tokens
    ON consolidation_runs (started_at) WHERE status IN ('dispatching', 'success');
