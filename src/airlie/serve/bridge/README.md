# Airlie Bridge (`task bridge`)

Outbound Mac worker for the [Job Bus](../../../docs/bridge-contract.md): HTTPS claim/result to the hub, loopback
`POST /v1/airlie/runs` on `task serve`. No inbound ports on the Mac.

## Mac Studio (models available)

1. Configure `bridge:` in `airlie.yaml` / `airlie.local.yaml` (`job_bus_url`, `allowed_repos`, `kinds`).
2. Secrets (never commit them):
   - Put secrets once in `~/.config/airlie/bridge.env` (`chmod 600`); `task serve` and `task bridge` both read them (no `--api-key` every launch):
     - `BRIDGE_WORKER_SECRET=…`
     - `AIRLIE_SERVE_API_KEY=…` (loopback serve bearer; same value for serve + bridge)
   - Or export those for a one-off session
   - Do **not** put these in `~/.config/airlie/.env` (that file is for ATLASSIAN_* / story)
3. Start inference and serve:
   - `task up` then `task serve` (or your usual Studio serve command with `--api-key` when tunneled)
4. Run the bridge:
   - Foreground: `task bridge`
   - launchd: `task bridge:launchd:install` (log: `~/.local/state/airlie/bridge.log`)

The bridge short-polls `POST /jobs/claim` every `bridge.claim_poll_s` seconds (default 5). For `studio` jobs it
enqueues a run with `plan: null`, polls serve until terminal, and posts `{ ok, final_answer, branch, tests }` to
the hub (Mac `report` paths are not forwarded). For `llm` jobs it POSTs `/v1/chat/completions` with inline
`persona` (system) + `prompt` (user) and posts `{ output, content }` — persona markdown is owned by the product,
never read from Studio disk. The model is a raw catalog model (no chat role prompt, so the persona is the only
system text); `bridge.llm` sets it (`model: generalist`, `max_tokens`, `thinking`, `thinking_budget`, `timeout_s`).

```yaml
bridge:
  idle_s: 120          # a model used this recently by anything but the bridge counts as busy
  heartbeat_s: 60      # heartbeat a running job so the hub keeps its lease (hub LEASE_SECONDS, default 300)
  llm:
    model: generalist
    max_tokens: 16384  # caps thinking plus answer; must exceed thinking_budget
    thinking: true
    thinking_budget: 8192
    timeout_s: 900     # serve waits up to 900 s for a non-streamed reply too
```

**Busy gate.** The bridge claims a job only while airlie is idle. Before each claim it waits while a story lane
(`build` or `plan`) is held, while a model has a request in flight (`active_requests` on the engine's
`/v1/status`, which airlie's engine patch adds), or while a model was used by anything but the bridge in the last
`bridge.idle_s` seconds. Each wait logs one `not claiming: <reason>` line to `~/.local/state/airlie/bridge.log`,
and `idle again, claiming` when it ends. A serve outage or a handler error fails that job; the bridge keeps going.

Set hub production `ALLOWED_KINDS=studio,llm` and Mac `bridge.kinds: [studio, llm]` (see
`docs/integrations/bridge-clients.md`).

## Development / CI without models

Unit tests **T10–T12** (and `llm` cases) in `tests/unit/test_bridge.py` fake the hub and serve HTTP and a clock —
no GPU or `task serve` required. Run:

```sh
uv run pytest tests/unit/test_bridge.py -q
```

Manual end-to-end (LHR UI → hub → bridge → serve) is Studio-only confidence testing; the security gate is
automated contract + bridge tests.
