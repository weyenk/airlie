-- Leases (2026-10-31): a claim counts an attempt; a running job whose heartbeat lapsed is requeued, or failed
-- once it has used its attempts. Apply after 001 with the owner role.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_jobs_running_heartbeat ON jobs (heartbeat_at) WHERE status = 'running';
