# Airlie Bridge — Job Bus contract (Phase 0)

The Job Bus is the shared cloud queue between product clients (LHR, ECFF, …), channel adapters, and the outbound Mac `task bridge`. This document is the human-readable source of truth; the machine-readable OpenAPI lives at [`hub/openapi.yaml`](../hub/openapi.yaml).

**Product clients:** see [`docs/integrations/bridge-clients.md`](integrations/bridge-clients.md) for how any BFF connects (tenant key, `created_by`, payload rules).

## Base URL

Deploy the hub at `/api/v1` (example: `https://hub.example/api/v1`).

## Authentication

| Caller | Header | Notes |
|--------|--------|-------|
| Product BFF / channel enqueue | `Authorization: Bearer <tenant_service_key>` | Maps to `tenant_id` via server config |
| End-user ownership | `X-Created-By: <subject>` on read/list; `created_by` in POST body on enqueue | Hub never verifies JWT; BFF must |
| Mac bridge worker | `Authorization: Bearer <BRIDGE_WORKER_SECRET>` | Timing-safe compare; wrong length → **401** (never 500) |

Bearer tokens must not appear in logs.

### Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `DATABASE_URL` | yes | Supabase Postgres (owner/service role connection) |
| `BRIDGE_WORKER_SECRET` | yes | ≥256-bit shared secret for worker routes |
| `TENANT_SERVICE_KEYS` | yes | JSON map `{"lhr":"…","ecff":"…"}` |
| `ALLOWED_KINDS` | no | Comma-separated kind allowlist (default `studio`; production should set `studio,llm`) |
| `MAX_PAYLOAD_BYTES` | no | Enqueue payload cap (default `65536`) |
| `LEASE_SECONDS` | no | A running job whose heartbeat is older than this is requeued (default `300`, at least 1; the bridge heartbeats every 60 s) |
| `MAX_JOB_ATTEMPTS` | no | Claims a job may use before a lapsed lease fails it (default `3`, at least 1) |

Apply schema from [`hub/migrations/001_jobs.sql`](../hub/migrations/001_jobs.sql), then [`hub/migrations/002_job_attempts.sql`](../hub/migrations/002_job_attempts.sql) (adds `attempts`; **must be applied to production** before deploying the lease code). Tables have **RLS enabled with no policies**; only the owner connection used by the hub may access rows.

## Job model

State machine: `queued → running → done | failed | cancelled`.

| Field | Description |
|-------|-------------|
| `id` | UUID |
| `tenant_id` | Registered tenant (`lhr`, `ecff`, …) |
| `kind` | Work type (`studio`, …) — allowlisted at enqueue |
| `status` | Current state |
| `payload` | **Data only** — see security |
| `result` | Terminal JSON from worker |
| `error_message` | Human-readable failure |
| `created_by` | Subject for ownership SQL |
| `channel`, `channel_ref` | Optional routing metadata |
| `worker_id` | Set on claim |
| `attempts` | Claims so far; each claim adds one |
| `claimed_at`, `heartbeat_at`, `finished_at` | Timestamps |

### Job events

Append-only `job_events` rows with monotonic `seq` per job. Workers append progress; clients list events for jobs they own.

## HTTP API

### Client routes (tenant service key)

- `POST /jobs` — enqueue `{ kind, payload, created_by?, channel?, channel_ref? }` → **201**
- `GET /jobs` — list for `tenant_id` + `X-Created-By`
- `GET /jobs/{id}` — fetch one (404 if wrong owner)
- `GET /jobs/{id}/events` — list events (404 if wrong owner)

### Worker routes (worker secret)

- `POST /jobs/claim` — body `{ worker_id }`; **200** + one job or **204** empty (short poll, no long-poll)
- `POST /jobs/{id}/events` — `{ worker_id, kind, payload }`
- `POST /jobs/{id}/heartbeat` — `{ worker_id }`
- `POST /jobs/{id}/result` — `{ worker_id, status, result?, error_message? }`; idempotent replay when already terminal with same outcome

Claim uses `FOR UPDATE SKIP LOCKED` and sets `status=running` atomically.

## Security contract (launch-blocking)

1. **Payload is data:** reject `repo`, `test_cmd`, shell/path control fields; optional `repo_key` must be a short enum key (no `/`). Bridge resolves paths from config allowlists.
2. **Kind allowlist** at enqueue; payload size cap.
3. **Ownership SQL:** reads filter `tenant_id` + `created_by`; other users get **404** (IDOR-safe).
4. **Lease:** each claim first requeues running jobs whose heartbeat is older than `LEASE_SECONDS`, or fails them (`worker heartbeat lapsed after N attempts`) once `attempts >= MAX_JOB_ATTEMPTS`. A requeue clears `worker_id`, so the old worker's result gets **403**. Nothing sweeps leases between claims: a lapsed job stays `running` until a worker next claims (a bridge waiting for airlie to go idle does not claim).
5. **Worker binding:** events/heartbeat/result require matching `worker_id` and expected `status`.
6. **Worker secret:** timing-safe, length-safe **401**.
7. **RLS parity:** enabled, no policies; document owner bypass for ops.

## Versioning

Phase 0 exposes unversioned job shapes under `/api/v1`. Breaking changes require a new prefix or explicit version field on jobs.

## QA gate

Automated contract tests **T1–T9** live in [`hub/tests/contract.test.ts`](../hub/tests/contract.test.ts):

| Test | Property |
|------|----------|
| T1 | IDOR → 404 |
| T2 | Control-field reject |
| T3 | Claim → `running` |
| T4 | Worker binding |
| T5 | Terminal replay no-op |
| T6 | Secret length-safe 401 |
| T7 | Kind allowlist |
| T8 | Bad tenant key 401 |
| T9 | Monotonic event `seq` |

Run: `task hub:gate` from the repo root.
