-- Airlie Bridge Job Bus (Phase 0). Apply with owner/service role (bypasses RLS).
-- RLS is enabled with no policies: only the owner connection may read/write.
-- IDs are assigned by the hub (crypto.randomUUID) so this file works on PGlite and Supabase alike.

CREATE TABLE IF NOT EXISTS jobs (
  id UUID PRIMARY KEY,
  version INTEGER NOT NULL DEFAULT 1,
  tenant_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  result JSONB,
  error_message TEXT,
  created_by TEXT NOT NULL,
  channel TEXT,
  channel_ref TEXT,
  worker_id TEXT,
  claimed_at TIMESTAMPTZ,
  heartbeat_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jobs_tenant_created_by ON jobs (tenant_id, created_by, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_queued_claim ON jobs (created_at ASC) WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS job_events (
  id UUID PRIMARY KEY,
  job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (job_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events (job_id, seq ASC);

ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_events ENABLE ROW LEVEL SECURITY;
