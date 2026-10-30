# Connecting a product to the Airlie Job Bus

This guide is for **any** service that wants Mac-side airlie compute (studio, later kinds)
without embedding airlie. The live hub is the shared queue; your product is a **client**.

## Roles (do not collapse these)

| Role | Runs where | Holds | Does |
|------|------------|-------|------|
| **Hub** (airlie-hub) | Vercel + Supabase | `DATABASE_URL`, `TENANT_SERVICE_KEYS`, `BRIDGE_WORKER_SECRET` | Queue, claim, results |
| **Mac bridge** | Studio / Mac | `BRIDGE_WORKER_SECRET`, `AIRLIE_SERVE_API_KEY` in `~/.config/airlie/bridge.env` | Outbound claim → `task serve` |
| **Product BFF** | Your API (e.g. LHR office Express) | **Your** row in `TENANT_SERVICE_KEYS` as `AIRLIE_HUB_TENANT_KEY` | Verify user → enqueue/list |
| **Browser / Telegram / mobile UI** | User device | **No** hub secrets | Talk only to your BFF or a channel adapter |

Tenant service keys **never** ship in SPAs, mobile apps, or chat bots’ client code.
Telegram inbound uses a **Mac-side channel adapter** (Phase 2), which is another BFF-like
caller—not the phone.

## One-time platform setup (hub operator)

1. Register a `tenant_id` (short slug: `lhr`, `ecff`, …).
2. Add it to hub env `TENANT_SERVICE_KEYS` JSON, e.g. `{"lhr":"<random ≥32 chars>","ecff":"…"}`.
3. Ensure Mac `airlie.local.yaml` `bridge.allowed_repos` includes any `repo_key` values you will send.
4. Redeploy hub after key changes; restart `task bridge` after repo allowlist changes.

## Product checklist

1. **BFF env (server only)**
   - `AIRLIE_HUB_URL` — e.g. `https://airlie-hub.vercel.app/api/v1`
   - `AIRLIE_HUB_TENANT_KEY` — the value for your `tenant_id` from `TENANT_SERVICE_KEYS`
2. **Verify the end user** with your existing auth (Supabase JWT, session cookie, …).
3. **Enqueue** (server → hub):

```http
POST {AIRLIE_HUB_URL}/jobs
Authorization: Bearer {AIRLIE_HUB_TENANT_KEY}
Content-Type: application/json

{
  "kind": "studio",
  "created_by": "<stable user subject>",
  "channel": "web",
  "payload": {
    "goal": "user-visible task text",
    "repo_key": "lhr"
  }
}
```

4. **List / get** (server → hub): same Bearer; always send `X-Created-By: <same subject>`.
   Wrong owner → hub **404** (not 403).
5. **Expose only your BFF** to the browser, e.g. `POST /api/bridge/jobs` that sets `created_by`
   from the verified session—never from a client-supplied spoofable field alone.
6. **Poll** terminal states: `queued` → `running` → `done` | `failed` | `cancelled`.
   For `studio`, show `result.final_answer` or `error_message`.
7. **Optional later:** domain result writers (persist `result` into your Postgres). Bridge never
   gets your `DATABASE_URL`.

## Payload rules (security)

Allowed examples: `goal`, `repo_key` (short lowercase key, no `/`).
Rejected by hub: `repo`, `test_cmd`, `cwd`, shell/path control fields.
The Mac resolves `repo_key` via `bridge.allowed_repos` only.

## Minimal curl smoke (no UI)

```bash
# Replace KEY and URL. Empty goal fails on the Mac before LLM (safe during benchmarks).
curl -sS -X POST "$AIRLIE_HUB_URL/jobs" \
  -H "authorization: Bearer $AIRLIE_HUB_TENANT_KEY" \
  -H "content-type: application/json" \
  -d '{"kind":"studio","created_by":"docs-smoke","channel":"web","payload":{"goal":"","repo_key":"lhr"}}'
```

With `task bridge` running, status becomes `failed` with
`studio payload requires a non-empty goal`. That proves bus + claim without GPU load.

## What this guide is not

- Not Telegram/Slack setup (see platform Phase 2).
- Not moving product cron jobs onto the Mac (separate migration).
- Not replacing your product database with the hub.

Machine contract: [`docs/bridge-contract.md`](../bridge-contract.md), OpenAPI [`hub/openapi.yaml`](../../hub/openapi.yaml).
