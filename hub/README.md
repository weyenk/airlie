# airlie-hub

Dedicated Job Bus API for the Airlie Bridge platform (Phase 0).

## Local dev

```bash
cd hub
npm ci
export DATABASE_URL='postgresql://…'   # Supabase owner/service URL
export BRIDGE_WORKER_SECRET='…'        # ≥32 chars
export TENANT_SERVICE_KEYS='{"lhr":"dev-key"}'
npm run dev                            # listens on :8787
```

Apply `migrations/001_jobs.sql`, then `migrations/002_job_attempts.sql`, to the database (owner role). **002 must be applied to production before this hub version is deployed**: the claim path writes `jobs.attempts`.

Leases: every claim counts an attempt. On each claim the hub first sweeps `running` jobs whose `heartbeat_at` is older than `LEASE_SECONDS` (default 300): jobs with `attempts >= MAX_JOB_ATTEMPTS` (default 3) become `failed` (`worker heartbeat lapsed after N attempts`), the rest go back to `queued`. A stale worker's later result is rejected with 403. Both env vars must be at least 1.

## Tests

```bash
task hub:gate    # from repo root
# or
cd hub && npm ci && npm run typecheck && npm test
```

Contract tests use in-process PGlite (no external Postgres required).

## Deploy

Point Vercel at the `hub/` directory; set the same env vars. Entry: `api/index.ts`.

Production should set `ALLOWED_KINDS=studio,llm` so product BFFs can enqueue chat completions
(`docs/integrations/bridge-clients.md`). Without it, only `studio` is accepted.

See [`docs/bridge-contract.md`](../docs/bridge-contract.md) for the full contract.
