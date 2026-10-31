# Bridge `llm` jobs for products: bare model, claim when idle, leases

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Job Bus `llm` kind fit for ECFF's Press Box desk: the product's persona is the only system text, the bridge claims only while airlie is idle, and a job can't hang in `running`.

**Architecture:** The bridge's `llm` kind posts to serve with a raw catalog model name (serve's `upstream()` then adds no role prompt) and sets its own generation settings from a new `bridge.llm` block. A `busy` probe (story lane locks + every engine's `/v1/status` `last_used_at`) gates each claim. The bridge heartbeats a running job; the hub requeues a job whose heartbeat lapsed, up to an attempt cap.

**Tech Stack:** Python 3.12 + uv, httpx, pytest (bridge); TypeScript + Express + PGlite/vitest (hub).

**Spec:** weyenk/ecff-website `docs/superpowers/specs/2026-10-31-press-box-on-airlie-hub-design.md` (branch `claude/press-box-on-airlie-hub`), section "airlie side". The `llm` payload contract does not change.

## Global Constraints

- The `llm` job contract is unchanged: `{prompt, persona, output: "chat.completions", caller}` in, `{output, content}` out; `model`/`thinking` in the payload still fail.
- The persona is the only system message the model sees: no airlie role prompt, no `client-bridge` rule.
- `max_tokens` must stay above `thinking_budget` when thinking is on (config validation).
- A story run is busy from start to finish, gaps included (build or plan lane lock held).
- The busy check lives in one module (`airlie/serve/bridge/busy.py`) so GPU sharing can replace it later.
- Test gate (AGENTS.md) plus `task hub:check` must pass.

## Review Focus

1. A serve `/v1/status` that is down or slow must not wedge the bridge: treat an unreachable engine as not busy (serve's own failure then fails the job visibly) and time the probe out in 3 s. Test in Task 2.
2. The bridge's own just-finished job bumps `last_used_at`; it must not read as "busy" forever. Test in Task 2.
3. A heartbeat that fails (network blip, 409 because the hub already requeued it) must not crash the job or the loop; it is logged. Test in Task 3.
4. A worker posting a result for a job the hub already requeued and another claim took must get the hub's existing 403/409, not overwrite. Test in Task 4.
5. A thinking reply that takes minutes must not hit the serve client's 60 s timeout. Test in Task 1.

---

### Task 1: The `llm` kind posts to a bare model with its own settings

**Files:**
- Modify: `src/airlie/config/schema.py` (BridgeSpec, ~297-322: add `LlmKindSpec` and `llm` field)
- Modify: `src/airlie/serve/bridge/kinds.py:118-184` (LlmKind body)
- Modify: `src/airlie/serve/bridge/airlie_client.py:58` (`chat_completions` timeout)
- Modify: `airlie.yaml` bridge comment block (~225-241), `docs/integrations/bridge-clients.md` (Kind `llm` section), `src/airlie/serve/bridge/README.md`
- Test: `tests/unit/test_bridge.py`

**Interfaces:**
- Produces: `class LlmKindSpec(Strict)` with `model: str = "generalist"`, `max_tokens: int = 16384`, `thinking: bool = True`, `thinking_budget: int | None = 8192`, `timeout_s: float = 900.0`; a model validator rejecting `thinking and thinking_budget and max_tokens <= thinking_budget`. `BridgeSpec.llm: LlmKindSpec = Field(default_factory=LlmKindSpec)`.
- Produces: `ServeClient.chat_completions(body: dict[str, Any], *, timeout_s: float | None = None) -> dict[str, Any]`.

- [ ] **Step 1: Update `test_llm_happy_path_posts_chat_completions` to the new body**

```python
assert serve_t.chat_posts == [
    {
        "model": "generalist",
        "messages": [
            {"role": "system", "content": "# Writer\n\nYou write concise summaries."},
            {"role": "user", "content": "summarise the week"},
        ],
        "max_tokens": 16384,
        "chat_template_kwargs": {"enable_thinking": True},
        "thinking_token_budget": 8192,
    }
]
```

Add `test_llm_uses_bridge_llm_settings` (bridge_cfg with `llm=LlmKindSpec(model="small", max_tokens=4096, thinking=False, thinking_budget=None)` → body has `model: "small"`, `max_tokens: 4096`, `chat_template_kwargs: {"enable_thinking": False}`, and no `thinking_token_budget` key), `test_llm_spec_rejects_max_tokens_under_budget` (`LlmKindSpec(max_tokens=100, thinking_budget=200)` raises `ValidationError`), and `test_llm_chat_timeout_is_the_spec_timeout` (FakeServe records `request.extensions["timeout"]["read"]` on the chat post; it equals 900.0).

Add a serve test in `tests/unit/test_serve_chat.py`: `test_raw_catalog_model_gets_only_the_clients_system_message` — POST `{"model": "generalist", "messages": [system persona, user]}`; the upstream body's messages are exactly those two (no role prompt, no client-bridge rule). If such a test already exists, cite it in the commit instead.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/unit/test_bridge.py -q -k llm`
Expected: FAIL (body still `model: "chat"`; `LlmKindSpec` missing)

- [ ] **Step 3: Implement**

In `LlmKind.run`, build the body from `self._bridge.llm`: `model`, the two messages, `max_tokens`, `chat_template_kwargs={"enable_thinking": spec.thinking}`, and `thinking_token_budget` only when `spec.thinking and spec.thinking_budget`. Call `self._serve.chat_completions(body, timeout_s=spec.timeout_s)`; `chat_completions` passes `timeout=httpx.Timeout(timeout_s, connect=10.0)` to `post` when given. Update the class docstring ("raw catalog model, not the chat role: the persona is the only system text"). Docs: the `llm` section of bridge-clients.md says the persona is the only system text and the reply is the model's answer without its reasoning; README and the airlie.yaml comment show `bridge.llm`.

- [ ] **Step 4: Run them and see them pass**

Run: `uv run pytest tests/unit/test_bridge.py -q && uv run pytest tests/unit -q -k "chat_completions or raw_catalog"`
Expected: PASS

- [ ] **Step 5: Commit**

`git commit -m "bridge: llm jobs go to a bare model with the bridge's own settings, so a product's persona is the only system text"`

---

### Task 2: The bridge claims only while airlie is idle

**Files:**
- Create: `src/airlie/serve/bridge/busy.py`
- Modify: `src/airlie/serve/bridge/runner.py` (`BridgeRunner.tick`, `process_job`, `run_bridge`)
- Modify: `src/airlie/config/schema.py` (BridgeSpec: `idle_s`)
- Test: `tests/unit/test_bridge_busy.py`, `tests/unit/test_bridge.py`

**Interfaces:**
- Consumes: `airlie.story.state.lock_held(stories_dir: Path, lane: Lane) -> bool`; lanes `("build", "plan")`; stories dir `cfg.observability.state_dir.expanduser() / "stories"`; engine roots `spec.base_url.removesuffix("/v1")` for every `cfg.engines` entry.
- Produces:
  - `BridgeSpec.idle_s: float = Field(default=120.0, ge=0.0, description="a model used this recently by anything but the bridge counts as busy")`
  - `async def busy_reason(cfg: AirlieConfig, *, http: httpx.AsyncClient, now: float, own_last_used: float) -> str | None` — `None` when idle, else one human line: `"story build lane is running"`, `"story plan lane is running"`, or `"model generalist used 14 s ago on engine main"`.
  - `BridgeRunner.last_job_finished: float = 0.0` (epoch seconds, set after each `process_job`), and `BridgeRunner.busy: Callable[[], Awaitable[str | None]] | None` (None = never busy; `run_bridge` wires the real probe).

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_bridge_busy.py`)

- `test_build_lock_held_is_busy`: hold `lane_lock(stories, "build")` in the test, `busy_reason` returns `"story build lane is running"` and makes no HTTP call.
- `test_plan_lock_held_is_busy`: same for plan.
- `test_recent_model_use_is_busy`: fake `/v1/status` with `generalist` `last_used_at = now - 14`; returns a line containing `"generalist"` and `"14 s"`.
- `test_old_model_use_is_idle`: `last_used_at = now - 121`; returns `None`.
- `test_own_job_does_not_count`: `last_used_at = now - 5`, `own_last_used = now - 4`; returns `None` (a use at or before the bridge's own last job, plus 2 s slack, is the bridge's).
- `test_unreachable_engine_is_idle`: transport raises `httpx.ConnectError`; returns `None`.
- `test_status_probe_times_out_in_3s`: the request's read timeout is 3.0.
- `test_unloaded_or_null_last_used_is_ignored`: `last_used_at: null` → `None`.

In `tests/unit/test_bridge.py`: `test_busy_bridge_does_not_claim` (runner with `busy` returning `"story build lane is running"`: `tick()` returns False, `hub_t.claim_calls == 0`) and `test_idle_bridge_claims` (busy returns None: one claim). `test_busy_reason_logged_once_per_change`: two busy ticks with the same reason log it once (caplog).

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest tests/unit/test_bridge_busy.py tests/unit/test_bridge.py -q`
Expected: FAIL (module missing)

- [ ] **Step 3: Implement `busy.py` and wire it**

`busy_reason` checks the two lane locks first (no network), then GETs `{root}/v1/status` for each distinct engine root with `timeout=3.0`, reads `model_manager.models[*]` with `loaded` true and numeric `last_used_at`, and reports the most recent use where `now - last_used_at < cfg.bridge.idle_s` and `last_used_at > own_last_used + 2.0`. Errors and non-200 answers count as idle for that engine. Module docstring: why a story's whole run counts (its gaps), and that this is the one place GPU sharing would change. `BridgeRunner.tick` calls `self.busy()` before `claim`; when busy, it logs the reason at info when it differs from the last one logged and returns False. `process_job` sets `last_job_finished = self.clock.wall()` after posting the result: add `wall() -> float` (epoch seconds) to `Clock` and `RealClock` (`time.time()`), and to `FakeClock` as `1_800_000_000.0 + self.now` (its `now` attribute is the monotonic counter, so the method is not named `now`). `run_bridge` builds the probe with its own `httpx.AsyncClient` and closes it in the `finally`.

- [ ] **Step 4: Run and see them pass**

Run: `uv run pytest tests/unit/test_bridge_busy.py tests/unit/test_bridge.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

`git commit -m "bridge: claim a hub job only while no story lane is held and no model was used by anything else in bridge.idle_s"`

---

### Task 3: The bridge heartbeats a running job

**Files:**
- Modify: `src/airlie/serve/bridge/job_bus.py` (add `heartbeat`)
- Modify: `src/airlie/serve/bridge/runner.py` (`process_job`)
- Modify: `src/airlie/config/schema.py` (BridgeSpec: `heartbeat_s`)
- Test: `tests/unit/test_bridge.py` (FakeHub learns `/heartbeat`)

**Interfaces:**
- Produces: `JobBusClient.heartbeat(job_id: str, worker_id: str) -> bool` (POST `/jobs/{id}/heartbeat` `{"worker_id"}`; True on 200, False on 409/403/404 with a warning log; raises `BridgeError` only on 401). `BridgeSpec.heartbeat_s: float = Field(default=60.0, gt=0.0)`.

- [ ] **Step 1: Write the failing tests**

- `test_running_job_sends_heartbeats`: a handler that waits on the fake clock for 150 s; with `heartbeat_s=60` FakeHub records 2 heartbeats for that job id, then the result.
- `test_heartbeat_failure_does_not_stop_the_job`: FakeHub answers heartbeat with 409; the job still posts its `done` result and the runner keeps looping.
- `test_heartbeat_stops_when_job_ends`: after the result, no further heartbeat is posted when the clock advances.

- [ ] **Step 2: Run and see them fail**

Run: `uv run pytest tests/unit/test_bridge.py -q -k heartbeat`
Expected: FAIL

- [ ] **Step 3: Implement**

`process_job` starts a task that loops `await clock.sleep(heartbeat_s); await hub.heartbeat(...)` (catching and logging any `BridgeError`/`httpx.HTTPError`), runs the handler, then cancels the task before posting the result.

- [ ] **Step 4: Run and see them pass**

Run: `uv run pytest tests/unit/test_bridge.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

`git commit -m "bridge: heartbeat a running hub job every bridge.heartbeat_s"`

---

### Task 4: The hub requeues a job whose heartbeat lapsed

**Files:**
- Create: `hub/migrations/002_job_attempts.sql`
- Modify: `hub/src/jobStore.ts` (`claimNextQueuedJob`, new `requeueLapsedJobs`, `mapJob`/`serializeJob` add `attempts`)
- Modify: `hub/src/types.ts` (`JobRow.attempts`, `HubConfig.leaseSeconds`, `HubConfig.maxJobAttempts`)
- Modify: `hub/src/config.ts` (`LEASE_SECONDS` default 300, `MAX_JOB_ATTEMPTS` default 3; both ≥ 1; test defaults the same)
- Modify: `hub/src/createApp.ts` (claim route calls `requeueLapsedJobs` first)
- Modify: `hub/tests/helpers.ts` (apply 002 after 001), `hub/README.md`, `docs/bridge-contract.md` (lease, attempts, the two env vars, apply 002 to production)
- Test: `hub/tests/contract.test.ts`

**Interfaces:**
- Produces: `requeueLapsedJobs(db: Queryable, leaseSeconds: number, maxAttempts: number): Promise<{ requeued: number; failed: number }>`; `JobRow.attempts: number` (incremented by each claim); the job JSON gains `"attempts"`.

`002_job_attempts.sql`:

```sql
-- Leases (2026-10-31): a claim counts an attempt; a running job whose heartbeat lapsed is requeued, or failed
-- once it has used its attempts. Apply after 001 with the owner role.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_jobs_running_heartbeat ON jobs (heartbeat_at) WHERE status = 'running';
```

- [ ] **Step 1: Write the failing tests** (contract.test.ts; age a heartbeat with `UPDATE jobs SET heartbeat_at = now() - interval '10 minutes'`)

- `requeues a running job whose heartbeat lapsed`: enqueue, claim (attempts 1), age it, claim again → the same job comes back with `attempts: 2`, `status: "running"`.
- `fails a lapsed job that used its attempts`: `maxJobAttempts: 1`; claim, age, claim → 204; the tenant GET shows `status: "failed"`, `error_message` contains `"heartbeat lapsed"`.
- `keeps a job with a fresh heartbeat`: claim, heartbeat, claim → 204 (nothing requeued).
- `a stale worker cannot post a result after requeue`: claim as `w1`, age, claim as `w2`, `w1` posts a result → 403.

- [ ] **Step 2: Run and see them fail**

Run: `task hub:check`
Expected: FAIL

- [ ] **Step 3: Implement**

`requeueLapsedJobs` runs two UPDATEs on `status='running' AND heartbeat_at < now() - make_interval(secs => $1)`: those with `attempts >= $2` → `failed`, `error_message = 'worker heartbeat lapsed after ' || attempts || ' attempts'`, `finished_at = now()`; the rest → `queued`, `worker_id = NULL`, `claimed_at = NULL`. Both bump `version` and `updated_at`. `claimNextQueuedJob` adds `attempts = j.attempts + 1`.

- [ ] **Step 4: Run and see them pass**

Run: `task hub:check`
Expected: PASS

- [ ] **Step 5: Commit**

`git commit -m "hub: a running job whose heartbeat lapsed goes back to queued, and fails after MAX_JOB_ATTEMPTS"`

---

### Task 5: Gate and PR

- [ ] Run the AGENTS.md test gate in order and `task hub:check`; all pass.
- [ ] Push the branch and open a draft PR on weyenk/airlie. The body lists Noah's production steps: apply `hub/migrations/002_job_attempts.sql` to the hub's Supabase; set hub env `ALLOWED_KINDS=studio,llm` and an `ecff` key in `TENANT_SERVICE_KEYS` (and `MAX_PAYLOAD_BYTES` if the ECFF plan finds it must rise); redeploy the hub; restart `task bridge`.
