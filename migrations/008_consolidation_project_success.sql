-- Repair scheduler audit fields for databases upgraded from early 0.1.1 drafts.
ALTER TABLE consolidation_projects ADD COLUMN IF NOT EXISTS last_success_at TIMESTAMPTZ;
