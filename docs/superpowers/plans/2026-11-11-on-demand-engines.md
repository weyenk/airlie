# On-demand engines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every airlie engine starts when a request needs it and stops when idle, downloading first with visible progress when its model is missing; Gemma serves chat at 262144; the GPU limit is set and kept across reboots; the doctor reports the pinned floor and everything resident.

**Architecture:** A `Manager` in a new `airlie.manager` package owns resources (engines now, chat VMs in a later plan). Each resource has a footprint in GB and a lifecycle (`pinned` or `on_demand`). Callers `hold` a resource: the manager starts it if needed, makes room by stopping idle on-demand resources (least recently used first), or waits with a reason. Serve owns the manager. In-process callers (the chat route, serve's jobs) use it directly; out-of-process callers (the launchd story tick, `task run`) take an HTTP lease from serve, or start engines themselves when serve is down. Downloads go through `huggingface_hub.snapshot_download` with a progress hook.

**Tech Stack:** Python 3.12, FastAPI + httpx, asyncio, `huggingface_hub` (new direct dependency), mlx-vlm 0.7.12 servers via `engine/process.py`, launchd.

**Spec:** [`docs/superpowers/specs/2026-11-11-airlie-web-chat-agent-design.md`](../specs/2026-11-11-airlie-web-chat-agent-design.md), phases 0–2: "One Gemma engine at 262144", "The model manager", "Downloads with progress", "Status for the UI", "Memory and the doctor".

## Global Constraints

- Every task passes the gate from the repository root, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`.
- Unit tests over 500 ms or on the real clock go in `tests/unit/quarantine.toml`. Manager tests use `airlie.serve.bridge.clock.FakeClock` (`monotonic`, `wall`, `async sleep`) and fake resources, never real engines.
- Tests follow `skills/qa/*.md` (the layer guidelines):
  - Unit (`tests/unit`): one behaviour per test, named for the scenario and outcome, arranged Arrange-Act-Assert, no loops or conditionals in the body; fakes over mocks, doubles only at seams (clock, network, process, disk); no real clock, network or engine.
  - A test that drives a FastAPI app through `TestClient` goes in `tests/functional`: real routes and middleware, doubles only beyond the module boundary, one request per test, and for each new route the happy path, 401, 422 or 400, 404 and a dependency failure, asserting the body and not just the status.
  - Fakes are rebuilt per test (fixtures), never shared; every new test is seen red before the code is written.
  - New modules that hold logic are enrolled in `[tool.airlie.mutation].enrolled` once `task mutate:changed` is clean for them; changes to an already enrolled module (`config/schema.py`, `engine/http_engine.py`) need tests that catch every new mutant.
  - Web tests (vitest) follow `component.md`: query by role or text, mock at the network boundary, cover loading, error and empty states, never sleep.
  - E2E is only for critical journeys: this plan adds none (engine status is covered by component tests and the live checks).
- Dependencies through uv only. No Docker.
- Unload means stopping the process (`EngineProcess.stop`: SIGTERM, then SIGKILL after 15 s), never mlx-vlm's in-process swap; serve always sends the engine's served model name.
- The manager never stops a resource that has a holder.
- Defaults: `main`, `decision` and `gemma` pinned; `reviewer` and `small` on demand; `idle_unload_s: 600`.
- Gemma: `engines.gemma.max_kv_size: 262144`, `roles.chat.max_context: 262144`, QAT 8-bit weights (on this branch).
- `memory.wired_limit_mb` stays 126976; the doctor keeps `os_reserve_gb` 12.
- Status copy:
  - "Starting {display}…"
  - "Downloading {display}: {done} of {total} GB, {eta} left"
  - "Waiting for memory: {holder list}"
  - "{display} failed: {reason}"

  `{display}` is the engine name.

## Review Focus

1. **Two chats (or a chat and a story layer) asking for the same stopped engine at once.** Expected: one start, both wait for it. Test in Task 4 (`test_concurrent_holds_share_one_start`).
2. **An engine that dies while held** (crash, OOM kill). Expected: the holder's request fails with a clear error naming the engine and its log, the state is `failed`, and the next hold starts it again. Test in Task 4 (`test_a_dead_engine_is_failed_and_the_next_hold_restarts_it`).
3. **serve restarting while engines run.** Expected: running engines are adopted, not restarted, and pinned ones are not started twice. Test in Task 5 (`test_running_engines_are_adopted`).
4. **The launchd story tick running a long layer while serve's reaper ticks.** Expected: the lease keeps the reviewer up for the whole layer. Test in Task 7 (`test_a_renewed_lease_keeps_the_engine_past_idle`).
5. **A download interrupted** (network drop, serve restart mid-download). Expected: the next attempt resumes from the cache, and the UI shows the failure with a retry, not a frozen bar. Test in Task 3 (`test_a_failed_download_is_reported_and_can_retry`).

---

### Task 1: Gemma at 262144 and the MTP check

**Files:**
- Modify: `airlie.yaml` (`engines.gemma.max_kv_size: 262144`), `config/roles.yaml` (`chat.max_context: 262144`), `tests/unit/test_doctor.py`, `tests/unit/test_config.py`
- Create: `docs/bench/2026-11-12-gemma-qat-mtp.md` (results of the live check)

- [ ] **Step 1: Write the failing test** in `tests/unit/test_config.py`:

```python
def test_gemma_serves_chat_at_262k() -> None:
    cfg = load_config(ROOT / "airlie.yaml", NO_LOCAL)
    assert cfg.engines["gemma"].max_kv_size == 262144
    assert cfg.roles["chat"].max_context == 262144
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/unit/test_config.py -k 262k -v` → FAIL (131072).

- [ ] **Step 3: Change the two values.** Update `test_committed_studio_profiles_project_from_the_coders_corrected_kv_constant` in `tests/unit/test_doctor.py`:
  - The studio projection becomes `(111.0, [])`: gemma's one sequence is now 262.144k × 22 MB/1k at 8-bit, 3.0 GB.
  - Its comment and the `projection_detail` assertion read "gemma at 262144".

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_doctor.py -q` → PASS.

- [ ] **Step 5: Live MTP check** (needs the Studio; Noah's go to restart the gemma engine). Pull and restart:
  1. `task models:pull -- gemma4`
  2. `uv run airlie engine restart gemma`

  Send three requests through `POST http://127.0.0.1:8084/v1/chat/completions`, each with `max_tokens` 512:
  - (a) a 2k-token prompt;
  - (b) a 140k-token prompt (a long repository dump such as `git ls-files | xargs cat`, cut to size);
  - (c) the same 140k prompt with the engine restarted without `--draft-model` (temporarily remove `mtp` from `gemma4` in `airlie.local.yaml`).

  Record for each request: decode tok/s, and draft acceptance if `GET /v1/metrics` reports it.
  - **Keep MTP** when (b) completes without error and decodes at least as fast as (c).
  - **Otherwise** set `mtp: null` for `gemma4` in `config/models.yaml`, with a comment naming the bench file.

  Write the numbers and the decision to `docs/bench/2026-11-12-gemma-qat-mtp.md`.

- [ ] **Step 6: Commit**

```bash
git add airlie.yaml config/roles.yaml config/models.yaml tests/unit docs/bench/2026-11-12-gemma-qat-mtp.md
git commit -m "gemma: chat at 262144 on one engine; the QAT MTP pairing checked at short and long context"
```

### Task 2: The GPU limit, kept across reboots

**Files:**
- Create: `docs/integrations/gpu-limit.plist` (a LaunchDaemon template), `tests/unit/test_gpu_limit.py`
- Modify: `Taskfile.yml` (task `gpu:limit`), `src/airlie/cli.py` (`airlie host wired-limit`), `src/airlie/doctor.py:484-494` (the fix text), `README.md:383`, `tests/unit/test_doctor.py` (imports the helpers)
- Create (also): `tests/support/doctor.py`

**Interfaces:**
- Produces: `airlie host wired-limit` prints `memory.wired_limit_mb` from the resolved config, and nothing else; `task gpu:limit` uses it.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_gpu_limit.py`, in the style of `tests/unit/test_story_launchd.py` (plistlib and the Taskfile read through `yaml.safe_load`; never run launchctl or sudo). `taskfile()`, `cfg_with_limit()` and the CLI runner are small helpers local to this file; `FakeProbe` and `by_name` are imported from `tests/support/doctor.py` (move them there from `tests/unit/test_doctor.py`, so one test file never imports another):

```python
def test_the_daemon_sets_the_sysctl_at_boot() -> None:
    plist = plistlib.loads((ROOT / "docs/integrations/gpu-limit.plist").read_bytes())
    assert plist["Label"] == "dev.airlie.gpu-limit" and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == ["/usr/sbin/sysctl", "iogpu.wired_limit_mb=__MB__"]

def test_the_task_sets_it_now_and_installs_the_daemon() -> None:
    cmds = "\n".join(taskfile()["tasks"]["gpu:limit"]["cmds"])
    assert "airlie host wired-limit" in cmds and "sudo sysctl iogpu.wired_limit_mb=" in cmds
    assert "/Library/LaunchDaemons/dev.airlie.gpu-limit.plist" in cmds and "launchctl bootstrap system" in cmds

def test_host_wired_limit_prints_the_configured_value(cli_runner) -> None:
    assert cli_runner.invoke(app, ["host", "wired-limit"]).stdout.strip() == "126976"

def test_the_doctor_points_at_the_task() -> None:
    row = by_name(run_checks(cfg_with_limit(126976), FakeProbe(wired_mb=0)))["iogpu.wired_limit_mb"]
    assert row.status == "warn" and "task gpu:limit" in row.detail
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_gpu_limit.py -v` → FAIL.

- [ ] **Step 3: Implement.** The `gpu:limit` task:
  1. `MB=$({{.RUN}} airlie host wired-limit)`
  2. `sudo sysctl iogpu.wired_limit_mb=$MB`
  3. `sed "s|__MB__|$MB|" docs/integrations/gpu-limit.plist | sudo tee /Library/LaunchDaemons/dev.airlie.gpu-limit.plist >/dev/null`
  4. `sudo chown root:wheel` the plist, then `plutil -lint` it.
  5. `sudo launchctl bootout system/dev.airlie.gpu-limit || true`, then `sudo launchctl bootstrap system /Library/LaunchDaemons/dev.airlie.gpu-limit.plist`.

  Other changes:
  - The doctor's fix text becomes `run: task gpu:limit (sets it now and at every boot)`.
  - README line 383 says the same, instead of the stale `106496`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/test_gpu_limit.py tests/unit/test_doctor.py -q` → PASS.

- [ ] **Step 5: Live:** Noah runs `! task gpu:limit` (it asks for his password). Then `sysctl iogpu.wired_limit_mb` → `126976`, and `task doctor` shows the row ok.

- [ ] **Step 6: Commit**

```bash
git add docs/integrations/gpu-limit.plist Taskfile.yml src/airlie/cli.py src/airlie/doctor.py README.md tests/unit/test_gpu_limit.py tests/unit/test_doctor.py tests/support/doctor.py
git commit -m "host: task gpu:limit sets iogpu.wired_limit_mb now and at boot from memory.wired_limit_mb"
```

### Task 3: The downloader with progress

**Files:**
- Create: `src/airlie/models_download.py`, `tests/unit/test_models_download.py`
- Modify: `src/airlie/models_cmd.py` (pull through the downloader), `src/airlie/cli.py` (`models pull` prints progress), `pyproject.toml`, `uv.lock`, `tests/unit/test_models_cmd.py`

**Interfaces:**
- Produces:
  - `DownloadProgress` (frozen dataclass): `done_bytes: int`, `total_bytes: int`, `rate_bps: float`, `eta_s: float | None`.
  - `DownloadError(Exception)`.
  - `Downloader(*, fetch: Callable[..., str] = huggingface_hub.snapshot_download, disk_free: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free, cache_dir: Path | None = None, clock: Callable[[], float] = time.monotonic)`.
  - `async Downloader.ensure(repo: str, revision: str, on_progress: Callable[[DownloadProgress], None] = lambda p: None) -> Path`, which:
    - calls `fetch(repo, revision=…, dry_run=True)` first and sums `file_size` over entries with `will_download`;
    - raises `DownloadError("needs {n:.1f} GB, {f:.1f} GB free")` when free space is under 1.05 × that;
    - runs the real fetch in a thread with a `tqdm_class` that forwards byte updates to `on_progress` on the event loop, at most once a second plus a final call;
    - shares one in-flight download per `repo@revision` between callers;
    - returns the snapshot dir.
  - `pull(cfg, only=(), downloader=None, on_progress=None)` in `models_cmd.py` keeps its return shape `list[tuple[PullPlan, int]]` (0 or 1 per plan) and uses the downloader instead of `hf download`.

- [ ] **Step 1: Add the dependency**

Run: `uv add "huggingface_hub>=2.2"` → `uv sync --frozen` → exit 0.

- [ ] **Step 2: Write the failing tests** in `tests/unit/test_models_download.py` with a fake `fetch` (a dry-run list of fake `DryRunFileInfo`-like objects, and a real call that drives the given `tqdm_class` through `update(n)` calls), `FakeClock` passed as `clock` (no real time), and `tmp_path` for `cache_dir`:

```python
async def test_progress_reports_bytes_rate_and_eta(tmp_path) -> None: ...
async def test_two_callers_share_one_download(tmp_path) -> None: ...      # fetch called once (real), both get the path
async def test_not_enough_disk_is_refused_before_downloading(tmp_path) -> None: ...  # DownloadError, real fetch never called
async def test_progress_is_forwarded_at_most_once_a_second(tmp_path) -> None: ...    # 50 updates inside one fake second → one call plus the final one
async def test_a_failed_download_raises_download_error(tmp_path) -> None: ...        # the real fetch raises → DownloadError naming the repo
async def test_a_download_can_be_retried_after_it_failed(tmp_path) -> None: ...      # after the failure the next ensure calls fetch again and returns the path
async def test_a_cached_snapshot_downloads_nothing(tmp_path) -> None: ...  # dry run all is_cached → no progress beyond one final 100%
```

In `tests/unit/test_models_cmd.py`, two tests with a fake downloader: `test_pull_downloads_the_body_and_the_mtp_head_of_each_planned_model` and `test_a_download_error_gives_status_1_for_that_plan`.

- [ ] **Step 3: Run them to see them fail**

Run: `uv run pytest tests/unit/test_models_download.py tests/unit/test_models_cmd.py -v` → FAIL.

- [ ] **Step 4: Implement** as above. `airlie models pull` prints one line per model with the progress refreshed in place (`\r{name}: 7.2 of 19.4 GB, 3 min left`).

- [ ] **Step 5: Run the tests, then enrol the module**

Run: the Step 3 command → PASS. Run `task mutate:changed` and kill every survivor in `models_download.py` with a test (or list it in `equivalent` with a reason); then add `src/airlie/models_download.py` to `[tool.airlie.mutation].enrolled` in `pyproject.toml`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/airlie/models_download.py src/airlie/models_cmd.py src/airlie/cli.py tests/unit/test_models_download.py tests/unit/test_models_cmd.py
git commit -m "models: download pinned snapshots through huggingface_hub with progress, a disk check and shared downloads"
```

### Task 4: The manager

**Files:**
- Create: `src/airlie/manager/__init__.py`, `src/airlie/manager/core.py`, `tests/unit/test_manager.py`
- Modify: `src/airlie/config/schema.py` (`EngineSpec.lifecycle`, `EngineSpec.idle_unload_s`, `ManagerSpec`, `AirlieConfig.manager`), `airlie.yaml` (`reviewer` and `small`: `lifecycle: on_demand`), `tests/unit/test_config.py`, `pyproject.toml` (enrol `src/airlie/manager/core.py`)

**Interfaces:**
- Produces:
  - Config:
    - `EngineSpec.lifecycle: Literal["pinned", "on_demand"] = "pinned"` and `EngineSpec.idle_unload_s: float = 600.0`.
    - `ManagerSpec(Strict)`: `margin_gb: float = 2.0`, `start_timeout_s: float = 900.0`, `wait_timeout_s: float = 1800.0`, `reap_every_s: float = 30.0`, `lease_ttl_s: float = 120.0`.
    - `AirlieConfig.manager: ManagerSpec`.
  - `State = Literal["stopped", "downloading", "starting", "ready", "waiting", "stopping", "failed"]`.
  - `Status` (frozen dataclass): `name: str`, `kind: Literal["engine", "vm"]`, `state: State`, `detail: str = ""`, `progress: DownloadProgress | None = None`, `holders: tuple[str, ...] = ()`, `since: float = 0.0`.
  - `Resource(Protocol)`:
    - attributes `name: str`, `kind`, `lifecycle`, `idle_unload_s: float`, `footprint_gb: float`;
    - `async running() -> bool`;
    - `async start(report: Callable[[State, str, DownloadProgress | None], None]) -> None` (raises `EngineError` on failure);
    - `async stop() -> None`.
  - `Manager(resources: Sequence[Resource], *, budget_gb: float, clock: Clock, on_change: Callable[[Status], None] = lambda s: None, wait_timeout_s: float = 1800.0)` with:
    - `hold(name: str, owner: str) -> AsyncContextManager[None]`;
    - `async adopt() -> None` (marks running resources `ready`);
    - `async start_pinned() -> None`;
    - `async reap() -> list[str]` (stops on-demand resources with no holders idle for at least their `idle_unload_s`, and returns their names);
    - `async run(every_s: float) -> None` (reap forever);
    - `status() -> list[Status]`;
    - `touch(name: str) -> None` (marks it used now).
- Algorithm (`hold`), the part the tests and signatures leave open:

```text
under the resource's lock:
  if ready and still running → add holder, yield
  if ready but not running → state failed("exited"), fall through to start
  while used_gb(ready + starting, excluding name) + footprint > budget_gb:
      victim = idle (no holders) on_demand ready resource with the oldest last_used
      if victim: stop it (state stopping → stopped), continue
      state waiting(detail = "waiting for memory: " + holders of the others); await the shared condition
      (notified on every release and stop) or raise EngineError after wait_timeout_s
  state starting; await resource.start(report) → ready; or failed(reason) and raise
add holder; yield; finally remove holder, set last_used = clock.monotonic(), notify the condition
```

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_manager.py` with a `FakeResource` (records starts and stops, an `asyncio.Event` to hold a start open, a flag to die) and `FakeClock`:

```python
async def test_concurrent_holds_share_one_start() -> None: ...         # two holds on a stopped resource → start called once, both enter
async def test_an_idle_on_demand_resource_is_stopped_after_its_idle_time() -> None: ...
async def test_a_pinned_resource_is_never_reaped() -> None: ...
async def test_a_held_resource_is_not_reaped() -> None: ...
async def test_a_held_resource_is_not_evicted_to_make_room() -> None: ...   # the only candidate is held → the new hold waits
async def test_room_is_made_by_stopping_the_least_recently_used_idle() -> None: ...  # budget 30, a=10 (used t=1), b=10 (used t=2), c=15 → a stopped
async def test_waits_with_a_reason_then_starts_when_a_holder_releases() -> None: ...  # status shows "waiting for memory: chat:1"
async def test_waiting_gives_up_after_wait_timeout() -> None: ...      # EngineError naming the resource
async def test_a_dead_engine_is_failed_and_the_next_hold_restarts_it() -> None: ...
async def test_a_start_that_raises_leaves_the_resource_failed_and_raises_to_the_holder() -> None: ...
async def test_releasing_a_hold_notifies_a_waiter_and_records_last_used() -> None: ...
async def test_adopt_marks_running_resources_ready_without_starting() -> None: ...
async def test_status_changes_are_reported() -> None: ...              # on_change sees stopped→starting→ready
```

In `tests/unit/test_config.py` (`config/schema.py` is enrolled in mutation, so every new field needs a test that pins it):
  - `test_the_committed_lifecycles`: `reviewer` and `small` are `on_demand`; `main`, `decision` and `gemma` are `pinned`.
  - `test_manager_spec_defaults`: each `ManagerSpec` default and `EngineSpec.idle_unload_s` (600.0).
  - `test_manager_spec_rejects_unknown_keys_and_a_bad_lifecycle`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_manager.py tests/unit/test_config.py -v` → FAIL.

- [ ] **Step 3: Implement** as above. `task up` and `airlie engine up` with no name start only engines with `autostart` and `lifecycle == "pinned"` (`cli.py:239`); update `tests/unit/test_cli_engine.py`'s expectations.

- [ ] **Step 4: Run the tests, then enrol the module**

Run: `uv run pytest tests/unit/test_manager.py tests/unit/test_config.py tests/unit/test_cli_engine.py -q` → PASS. Run `task mutate:changed`; kill every survivor in `core.py` and the new `schema.py` lines with a test; then add `src/airlie/manager/core.py` to `[tool.airlie.mutation].enrolled`.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/manager src/airlie/config/schema.py airlie.yaml src/airlie/cli.py pyproject.toml tests/unit
git commit -m "manager: hold, start once, make room by stopping idle on-demand resources, wait with a reason, reap"
```

### Task 5: Engines as resources, and the doctor's two numbers

**Files:**
- Create: `src/airlie/manager/engines.py`, `tests/unit/test_manager_engines.py`, `tests/support/process.py`
- Modify: `src/airlie/doctor.py` (`engine_footprint_gb`, the floor row), `tests/unit/test_doctor.py`, `tests/unit/test_process.py` (imports the helpers)

**Interfaces:**
- Consumes: `EngineProcess` (`start(timeout_s)`, `stop()`, `status()`, its `prober.health_ok()`), `Downloader.ensure`, `RealProbe.model_snapshot_dir`.
- Produces:
  - `engine_footprint_gb(cfg, name) -> float` in `doctor.py`: that engine's weights and MTP head, its KV (the lane KV for the lane engine, else its `other_engines_kv` sequence), and its prefix cache. `project_memory_gb` becomes the sum over resident engines plus `os_reserve_gb`, with the same numbers as today.
  - `pinned_floor_gb(cfg) -> float`: the same sum over pinned engines only.
  - `EngineResource(cfg, name, *, process: EngineProcess, downloader: Downloader, on_disk: Callable[[str, str], bool])`:
    - `footprint_gb = engine_footprint_gb(cfg, name)`.
    - `start` downloads the body and MTP head when they are not on disk (reporting `downloading`), reports `starting`, then `process.start(timeout_s=cfg.manager.start_timeout_s)`.
    - `running` is true when the prober's health is ok on the engine's port, so an engine started by hand counts.
  - `engine_resources(cfg, *, state_dir, downloader) -> list[EngineResource]` for every `mlx_vlm` engine.
  - Doctor rows:
    - `engine:memory_floor[{profile}]` fails when the pinned floor is over the limit.
    - The existing `engine:{e}:memory_projection[{profile}]` becomes a warn, not a fail, when only everything-resident is over, with detail "… the manager will queue or stop idle engines".

- [ ] **Step 1: Write the failing tests**
  - `tests/unit/test_manager_engines.py`, with `FakeRunner` and `FakeProber` (imported from `tests/support/process.py`; move them there from `tests/unit/test_process.py`) and a fake downloader, `tmp_path` for the state dir, no real process:
    - `test_a_missing_model_downloads_before_start`: reports `downloading` then `starting`; spawn comes after the downloader.
    - `test_running_engines_are_adopted`: the prober is healthy and no pidfile exists, so `running()` is true and `adopt` does not spawn.
    - `test_a_start_failure_reports_the_log_tail`: the process dies, so the `EngineError` text includes `tail_log(20)`.
  - `tests/unit/test_doctor.py`:
    - For the committed studio, `pinned_floor_gb` is the projection minus reviewer and small. With the numbers on this branch that is `round(…, 1) == 79.6`: 111.0, minus the reviewer's 25.1 (19.4 weights + 2.2 KV + 3.5 cache), minus small's 6.3 (3 + 2.3 + 1).
    - The floor row is ok.
    - A fixture where only everything-resident is over the limit gives a projection row of `warn`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_manager_engines.py tests/unit/test_doctor.py -v` → FAIL.

- [ ] **Step 3: Implement** as above.

- [ ] **Step 4: Run the tests**

Run: the Step 2 command → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/manager/engines.py src/airlie/doctor.py tests/unit/test_manager_engines.py tests/unit/test_doctor.py tests/unit/test_process.py tests/support/process.py
git commit -m "manager: engines as resources (download, start, adopt); doctor: the pinned floor and everything resident"
```

### Task 6: serve owns the manager; chat waits instead of failing

**Files:**
- Create: `src/airlie/manager/api.py`, `tests/functional/test_manager_serve.py`
- Modify: `src/airlie/serve/app.py` (`create_app(..., manager: Manager | None = None)`, startup and shutdown, `/v1/chat/completions`), `tests/support/serve.py`

**Interfaces:**
- Consumes: `Manager`, `engine_resources`, `Feed.publish`.
- Produces:
  - `create_app(..., manager=None)` builds one from `engine_resources(cfg, …)` when it is not given. The budget is `memory.wired_limit_mb / 1024 - memory.os_reserve_gb - manager.margin_gb`.
  - Startup schedules `adopt()`, `start_pinned()` and `run(cfg.manager.reap_every_s)` as background tasks, so the HTTP server is up at once.
  - Shutdown cancels the loop and leaves engines running.
  - Every status change calls `feed.publish("engines", name)` (progress is throttled to once a second by the downloader).
  - Routes:
    - `GET /v1/airlie/engines` → `[{"name", "kind", "state", "detail", "progress": {"done_bytes", "total_bytes", "rate_bps", "eta_s"} | null, "holders": [...], "lifecycle"}]`.
    - `POST /v1/airlie/engines/{name}/leases` with body `{"owner": str}` → `{"lease": str, "ttl_s": float}`. It returns once the engine is ready and holds it until the lease is deleted or expires.
    - `PUT /v1/airlie/engines/{name}/leases/{lease}` → 204. It renews the lease and touches the engine.
    - `DELETE /v1/airlie/engines/{name}/leases/{lease}` → 204.
  - `/v1/chat/completions` holds the role's engine (`owner = f"chat:{uuid}"`) around the upstream call.
    - For a stream, it releases in the `StreamingResponse`'s `finally`.
    - A failed hold returns 503 `{"error": "<engine> failed: <reason>", "engine": name}`, never a bare 500.

- [ ] **Step 1: Write the failing tests** in `tests/functional/test_manager_serve.py` (through the real routes with `TestClient`, one request per test), with a manager over fake resources and a `FakeClock` passed to `create_app`, `FakeUpstream` for the engine HTTP, a deterministic test API key, and fakes rebuilt per test by fixtures:
  - `test_chat_to_a_stopped_engine_starts_it_and_answers`.
  - `test_a_stream_holds_the_engine_until_it_ends`: during the stream, `status()` shows one holder; afterwards, none.
  - `test_a_failed_start_is_a_503_naming_the_engine`.
  - `test_the_engines_route_lists_state_and_progress`.
  - `test_leases_hold_renew_and_release`.
  - `test_an_expired_lease_is_released`: advance the fake clock past `lease_ttl_s` with no renewal, so the holder is gone.
  - `test_status_changes_publish_an_engines_hint`: the feed sees `("engines", name)`.
  - Required by the functional guideline for every new route (assert the body, not just the status):
    - `test_the_engines_route_without_a_key_is_401`, and the same for the lease routes.
    - `test_a_lease_request_without_an_owner_is_422`.
    - `test_a_lease_on_an_unknown_engine_is_404`.
    - `test_renewing_or_deleting_an_unknown_lease_is_404`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/functional/test_manager_serve.py -v` → FAIL.

- [ ] **Step 3: Implement** as above. Leases are holds owned by `lease:{id}`, with an expiry checked in `reap`.

- [ ] **Step 4: Run the tests and the serve suites**

Run: `uv run pytest tests/functional/test_manager_serve.py tests/functional -q -k "serve or chat or manager"` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/manager/api.py src/airlie/serve/app.py tests/functional/test_manager_serve.py tests/support/serve.py
git commit -m "serve: owns the manager; chat waits for its engine; /v1/airlie/engines status and leases"
```

### Task 7: Every engine path goes through the manager

**Files:**
- Create: `src/airlie/manager/client.py`, `tests/unit/test_manager_client.py`, `tests/functional/test_manager_lease.py`
- Modify: `src/airlie/cli.py:326-360` (`_http_engine`), `src/airlie/serve/app.py:385` (`_default_factory`), `pyproject.toml` (enrol `src/airlie/manager/client.py` once `task mutate:changed` is clean for it)

**Interfaces:**
- Consumes: Task 6's lease routes; `Manager.hold`; `EngineProcess`.
- Produces:
  - `Holder(Protocol)` with `hold(engine: str, owner: str) -> AsyncContextManager[None]`.
  - Three implementations:
    - `InProcessHolder(manager)`.
    - `LeaseHolder(serve_url: str, api_key: str, client: httpx.AsyncClient, clock)`. It takes a lease, renews it every `ttl_s / 3` while held, and deletes it on exit.
    - `LocalHolder(cfg, state_dir)`. When serve is not reachable it starts the engine with `EngineProcess.start` if it is down, and never stops it (serve's reaper does that later).
  - `holder_for(cfg, *, manager: Manager | None) -> Holder` picks, in this order:
    1. `InProcessHolder` when a manager is given (inside serve).
    2. `LeaseHolder` when `GET {serve}/health` answers.
    3. `LocalHolder` otherwise.
  - `ManagedEngine(inner: Engine, engine_name: str, holder: Holder)` implements the `Engine` protocol (`airlie/engine/protocol.py:21`). `generate` and `yes_no` run inside `holder.hold(engine_name, owner=f"role:{req.role}")`; the other methods delegate.
  - `_http_engine` wraps each per-engine `HTTPEngine` in `ManagedEngine` before `RoutedEngine` combines them.

- [ ] **Step 1: Write the failing tests.**
  - In `tests/functional/test_manager_lease.py` (a `LeaseHolder` against serve's real lease routes through `TestClient`, a fake manager, `FakeClock`):
    - `test_a_renewed_lease_keeps_the_engine_past_idle`: while a hold lasts 3 × `lease_ttl_s`, the renewals keep the holder.
    - `test_leaving_the_hold_deletes_the_lease`.
  - In `tests/unit/test_manager_client.py` (fakes only; `httpx.MockTransport` for serve; `FakeRunner` and `FakeProber` for `LocalHolder`):
    - `test_generate_runs_inside_a_hold`: `ManagedEngine` over `engine/fake.py`'s fake engine records hold enter and exit around `generate`.
    - `test_yes_no_runs_inside_a_hold`, and `test_other_methods_do_not_hold`.
    - `test_holder_for_uses_the_in_process_manager_when_given`, `test_holder_for_uses_serve_when_its_health_answers`, `test_holder_for_falls_back_to_local_when_serve_is_down`.
    - `test_local_holder_starts_a_down_engine_and_leaves_it`.
    - `test_the_hold_is_released_when_the_call_raises`.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_manager_client.py -v` → FAIL.

- [ ] **Step 3: Implement** as above. The serve URL and key come from the same place `airlie mcp` reads them (`AIRLIE_URL`, default `http://127.0.0.1:8090`, and the serve API key store).

- [ ] **Step 4: Run the tests and the contract suite**

Run: `uv run pytest tests/unit/test_manager_client.py tests/functional/test_manager_lease.py tests/unit/engine_contract.py -q` → PASS, then the full gate.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/manager/client.py src/airlie/cli.py src/airlie/serve/app.py pyproject.toml tests/unit/test_manager_client.py tests/functional/test_manager_lease.py
git commit -m "engines: every role call holds its engine: in serve directly, from the tick by lease, else starts it locally"
```

### Task 8: Engine status in the web app

**Files:**
- Create: `web/src/api/engines.ts`, `web/src/stores/engines.ts`, `web/src/components/EngineStatusLine.vue`, tests beside each
- Modify: `web/src/screens/chat/StatusStrip.vue` (shows the chat engine's line when it is not `ready`), `web/src/screens/AdminScreen.vue` (an "Engines" table)

**Interfaces:**
- Consumes: `GET /v1/airlie/engines`; the feed's `engines` hints (`web/src/feed.ts` `Change`).
- Produces:
  - `useEngines()`: a Pinia store with `list: EngineStatus[]` and `byName(name)`. It refetches on an `engines` hint.
  - `EngineStatusLine` takes the prop `status: EngineStatus` and renders the Global Constraints copy, with a progress bar while `downloading`.

- [ ] **Step 1: Write the failing tests** (vitest, `component.md` rules: mock `fetch` at the network boundary and reset it after each test, query by role or text, no sleeps, names that state what the user sees):
  - The store refetches on a hint, and keeps the last list with an error when the refetch fails.
  - The line renders "Downloading reviewer: 7.2 of 19.4 GB, 3 min left" for a `downloading` status with those bytes and `eta_s` of 180, "Waiting for memory: chat:1" for `waiting`, and nothing for `ready`.
  - The Admin table lists each engine with its state and lifecycle; shows a loading state before the first answer, an empty state for no engines, and an error with a retry when the fetch fails.
  - The progress bar is `role="progressbar"` with `aria-valuenow`, and the test queries it by role.

- [ ] **Step 2: Run them to see them fail**

Run: `cd web && npx vitest run src/stores/engines.test.ts src/components/EngineStatusLine.test.ts` → FAIL.

- [ ] **Step 3: Implement** as above.

- [ ] **Step 4: Run the tests and the gate**

Run: the Step 2 command → PASS; `task web:gate` → exit 0.

- [ ] **Step 5: Commit**

```bash
git add web/src
git commit -m "web: engine status in the chat strip and an engines table in Admin"
```

### Task 9: Docs and the live checks

**Files:**
- Modify: `README.md` (engines start on demand; `task up` starts the pinned ones; lifecycle and `idle_unload_s` in `airlie.yaml`; `task gpu:limit`; downloads happen on first use with progress), `AGENTS.md` (one line under Configuration on `lifecycle`)

- [ ] **Step 1: Write the docs** as listed.
- [ ] **Step 2: Run the full gate** (the six commands in Global Constraints) → all exit 0; `task test:quarantine` → exit 0 (no new quarantine entries expected); `task mutate:changed` → no survivor in the enrolled new modules.
- [ ] **Step 3: Live checks on the Studio** (with Noah's go to restart serve; kill the old serve on :8090 and check the listener PID after the restart):
  1. Stop the reviewer (`uv run airlie engine down reviewer`), then start a story review or send `POST /v1/chat/completions` with `model: "reviewer"`.
     - Expect: the Admin table shows `starting`, then `ready`; the request answers.
     - Ten minutes after it finishes: `stopped`.
  2. With a temporary HF cache (`HF_HOME=$(mktemp -d)`), run `uv run airlie models pull controller_base`.
     - Expect: the progress line, then the snapshot.
  3. Restart serve while `main` and `gemma` run.
     - Expect: both are adopted (no new pids in `~/.local/state/airlie/engines/*/pid`).
  4. `task doctor`
     - Expect: `iogpu.wired_limit_mb` is ok, the floor row is ok, and every projection row is ok.
- [ ] **Step 4: Commit**

```bash
git add README.md AGENTS.md
git commit -m "docs: engines on demand, the pinned set, task gpu:limit, downloads on first use"
```

### Task 10: Write plan 3 — turn route, plain chat, superpowers

**Files:**
- Create: `docs/superpowers/plans/2026-11-1x-turn-route-and-plain-chat.md`

- [ ] **Step 1:** Invoke the `superpowers:writing-plans` skill.
- [ ] **Step 2:** Read the spec sections "Two kinds of chat", "Superpowers in chat", "Thread, transport, and the redesign" and "Order of work" (phase 3), and the code as it stands after Tasks 1-9. The plan builds on these interfaces from this plan: `Manager.hold(name, owner)` and the `Holder` protocol (`InProcessHolder` inside serve; the chat route holds its engine as `chat:{id}`), `Status` and the `engines` feed hints (the thread's status line), and `Resource`.
- [ ] **Step 3:** Cover `POST /v1/airlie/chat/turn` with the UI message stream and a server-side tool loop, plain chat with web, `skill` and `todo` tools, and the superpowers bootstrap and mapping. Include the PWA dependency note: PWA-b's push for chat replies needs the turn route, so a finished turn calls `Notifier.notify` (`docs/superpowers/plans/2026-11-11-pwa.md` Task 5).
- [ ] **Step 4:** The plan follows `skills/qa/*.md` (routes get functional tests with 401, 422, 404 and dependency-failure cases; fakes, no real engine; new logic modules enrolled in mutation).
- [ ] **Step 5:** Commit the plan.
- [ ] **Step 6:** Noah reviews it before it is executed.

### Task 11: Write plan 4 — repository chats in VMs

**Files:**
- Create: `docs/superpowers/plans/2026-11-1x-repository-chats-in-vms.md`

- [ ] **Step 1:** Invoke the `superpowers:writing-plans` skill.
- [ ] **Step 2:** Read the spec sections "Repository chats: a VM per session", "Superpowers in chat" and "Order of work" (phase 4), and the code as it stands after Tasks 1-9 and plan 3. A VM is another `Resource` with `kind == "vm"`: it implements `running`, `start`, `stop`, `footprint_gb` and the `State` reports of this plan's `Manager`, counts in the same memory budget, is held through `Holder` like an engine, and is evicted by the same least-recently-used rule when idle.
- [ ] **Step 3:** Cover the Tart box backend behind the shell seam (a fake box backend for tests), a VM per session, the recipe and default image, clone in and push out through the host, builtin tools and `task` in the VM, and confirmations.
- [ ] **Step 4:** The plan follows `skills/qa/*.md` (fake box in unit tests; routes functional; the real Tart boot is a live check, not a gate test).
- [ ] **Step 5:** Commit the plan.
- [ ] **Step 6:** Noah reviews it before it is executed.

### Task 12: Write plan 5 — the chat redesign (mockups first)

**Files:**
- Create: `docs/superpowers/plans/2026-11-1x-chat-redesign.md`

- [ ] **Step 1:** Invoke the `superpowers:writing-plans` skill.
- [ ] **Step 2:** Read the spec sections "Thread, transport, and the redesign", "Mobile: a full PWA" and "Order of work" (phase 5), and the code as it stands after Tasks 1-9 and plans 3-4. The redesign builds on the `Status` shape and the `engines` store from Task 8, and on the turn route's message stream.
- [ ] **Step 3:** The first task of that plan is three mockups for Noah to pick from (airlie web work is mockups-first: the design and ux roles mock up, Noah approves before build). The build tasks (left nav with pinned repositories, the Vue thread on `@ai-sdk/vue`, tool blocks, diffs, todos, permission prompts, status and context in the header) are written after the pick.
- [ ] **Step 4:** The plan follows `skills/qa/*.md` (component tests for each state, and an e2e only for a critical journey).
- [ ] **Step 5:** Commit the plan.
- [ ] **Step 6:** Noah reviews it before it is executed.
