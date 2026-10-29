# Airlie Bridge (`task bridge`)

Outbound Mac worker for the [Job Bus](../../../docs/bridge-contract.md): HTTPS claim/result to the hub, loopback
`POST /v1/airlie/runs` on `task serve`. No inbound ports on the Mac.

## Mac Studio (models available)

1. Configure `bridge:` in `airlie.yaml` / `airlie.local.yaml` (`job_bus_url`, `allowed_repos`, `kinds`).
2. Export secrets (never commit them):
   - `BRIDGE_WORKER_SECRET` — same value as the hub deployment
   - `AIRLIE_SERVE_API_KEY` — if serve requires a bearer token
3. Start inference and serve:
   - `task up` then `task serve` (or your usual Studio serve command with `--api-key` when tunneled)
4. Run the bridge:
   - Foreground: `task bridge`
   - launchd: `task bridge:launchd:install` (log: `~/.local/state/airlie/bridge.log`)

The bridge short-polls `POST /jobs/claim` every `bridge.claim_poll_s` seconds (default 5). For `studio` jobs it
enqueues a run with `plan: null`, polls serve until terminal, and posts `{ ok, final_answer, branch, tests }` to
the hub (Mac `report` paths are not forwarded).

## Development / CI without models

Unit tests **T10–T12** in `tests/unit/test_bridge.py` fake the hub and serve HTTP and a clock — no GPU or `task
serve` required. Run:

```sh
uv run pytest tests/unit/test_bridge.py -q
```

Manual end-to-end (LHR UI → hub → bridge → serve) is Studio-only confidence testing; the security gate is
automated contract + bridge tests.
