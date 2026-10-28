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

Apply `migrations/001_jobs.sql` to the database once.

## Tests

```bash
task hub:gate    # from repo root
# or
cd hub && npm ci && npm run typecheck && npm test
```

Contract tests use in-process PGlite (no external Postgres required).

## Deploy

Point Vercel at the `hub/` directory; set the same env vars. Entry: `api/index.ts`.

See [`docs/bridge-contract.md`](../docs/bridge-contract.md) for the full contract.
