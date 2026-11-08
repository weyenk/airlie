# mlx-vlm Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace vllm-mlx and llama-server with mlx-vlm's own server as airlie's only local engine, with zero airlie code inside the engine process, behind the existing `Engine` seam.

**Architecture:** A new engine kind `mlx_vlm` (one process per model) is added beside `vllm_mlx` and `llama_server`; `HTTPEngine` learns the mlx-vlm request and reply dialect while still speaking vllm-mlx's; the committed config switches to four mlx_vlm engines; a validation gate runs feature tests on both engines; then the `vllm_mlx` and `llama_server` kinds, airlie's six engine patches, two parsers and their tests are deleted in one pull request.

**Tech Stack:** Python 3.12, uv, pydantic (config), httpx (client and `MockTransport` tests), pytest with `pytest -q -n auto`, mlx-vlm `python -m mlx_vlm.server` (release pinned in Task 1), Taskfile.

**Spec:** `docs/superpowers/specs/2026-11-08-mlx-vlm-engine-design.md` (the approved design; its readable copy is the Claude Doc linked at its top). The plan argues from it; read both.

## Global Constraints

- Every change passes, in order, from the repo root: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate` (AGENTS.md). Mac-only tests run with `task test:mac`.
- One pull request per phase (spec, Phases). Each PR leaves the current engine working until phase 5.
- Zero airlie code inside the engine process: no monkey-patches, no vendored parsers, no source rewrites. If mlx-vlm needs a fix it is filed upstream and the feature waits (spec, anti-goals).
- The `Engine` protocol in `src/airlie/engine/types.py` does not change (spec, Where we are).
- mlx-vlm is pinned to one exact release: the latest on PyPI that passes Task 1 (0.7.12 when the spec was written). `mlx>=0.32.4`. The upstream server has no mlx-lm dependency.
- Ports: main 8080, decision 8081, reviewer 8082, small 8083, bench engines 8091–8093, hand-started test server 8094. Port 8090 is `task serve`'s and is never used for an engine.
- Prefix cache defaults per engine: `cache_memory_gb` main 8, reviewer 4, small 1, decision 1; `cache_checkpoints` 8 on main, 4 elsewhere; `cache_checkpoint_interval` 2048; disk tier off (`APC_DISK_ENABLED=0`).
- KV quantization is a bit count (`kv_bits`, default 8 where the studio profile had it on), not a boolean.
- Tool parsers are mlx-vlm names sent per request: `qwen3_coder` for generalist and coder8, `atem` for reviewer, unset elsewhere. Reasoning parsers go.
- The reviewer seat is a rule, not a model: any adversarial model from a different lab than the coder's (spec, Target architecture). Nothing in code may assume Muse.
- The phase 4b gate and the phase 5 delete need Noah's go after reading the gate results. The implementer stops after 4b.
- Nodes-are-config (branch `nodes-are-config`) merges first; rebase before Task 1. The only shared file is `src/airlie/config/schema.py` (it edits the `AirlieConfig` area; this plan edits `EngineSpec` and `ModelSpec`).

## Review Focus

Inputs the spec implies that no task's happy path exercises; each line's test is pinned to the task that owns the code:

1. mlx-vlm's `/v1/models` lists every model in the HF cache with a `loaded` flag, not only the served one. Readiness must require `loaded: true` for the engine's model, or `task up` returns before the weights are in memory and the first request pays the load (Task 4).
2. A request that names a model other than the resident one makes mlx-vlm unload the resident model. The client must always send the engine's own served name, never a role's model name resolved elsewhere (Task 6).
3. When `api_key` is set, mlx-vlm's `/health` and `/v1/metrics` require it. The readiness prober and the busy probe must send it or the engine reads as down forever (Tasks 4 and 8).
4. mlx-vlm has no generic `chat_template_kwargs` passthrough. A role whose `chat_template_kwargs` holds a key other than `enable_thinking` or `reasoning_strength` must fail config validation for mlx_vlm engines, not be dropped silently (Task 6).
5. The reasoning field in a reply is `reasoning_content` with `reasoning` as a deprecated alias; a stream may use either. The client reads both (Task 7).

---

## File Structure

New files:

- `src/airlie/engine/mlx_vlm.py` — the `mlx_vlm` kind's command line and environment from an `EngineSpec`.
- `src/airlie/engine/dialect.py` — the per-engine-kind request fields (thinking, budget, parser, template arguments) shared by `HTTPEngine`, the AutoGen client and serve.
- `tests/unit/test_mlx_vlm_command.py`, `tests/unit/test_dialect.py`, `tests/unit/test_http_engine_mlx_vlm.py`.
- `tests/fixtures/mlx_vlm/` — recorded wire fixtures (Task 1).

Modified files (by task): `pyproject.toml`, `uv.lock` (T1); `scripts/record_fixtures.py` (T1); `src/airlie/config/schema.py` (T2); `src/airlie/engine/process.py` (T4); `src/airlie/doctor.py` (T5, T10); `src/airlie/engine/http_engine.py` (T6, T7); `src/airlie/teams/autogen_team.py`, `src/airlie/serve/app.py` (T6); `src/airlie/serve/bridge/busy.py` (T8); `src/airlie/router/jev.py`, `src/airlie/router/__init__.py`, `src/airlie/tools/decide.py` (T9); `src/airlie/cli.py` (T4, T10); `airlie.yaml`, `config/models.yaml`, `config/profiles.yaml`, `src/airlie/story/line.py`, `Taskfile.yml`, `README.md` (T10); everything under "What gets deleted" in the spec (T12).

---

## Phase 1: Prerequisites

### Task 1: Pin mlx-vlm, prove the server answers, record fixtures

**Files:**
- Modify: `pyproject.toml` (dependencies block, lines 8–14), `uv.lock`
- Modify: `scripts/record_fixtures.py`
- Create: `tests/fixtures/mlx_vlm/{health.json,models.json,metrics.json,nonstream.json,plain.sse,reasoning.sse,tool_call.sse,resume.sse,error_bad_model.json}`

**Interfaces:**
- Produces: the fixture files named above, read by Tasks 4, 7 and 8; the pinned version string used in Task 3's `MLX_VLM_ENTRY` fallback.

- [ ] **Step 1: Rebase on main after the nodes-are-config merge**

Run: `git fetch origin && git rebase origin/main`
Expected: clean rebase; `uv run pytest -q -n auto --maxprocesses 8` green.

- [ ] **Step 2: Find the latest mlx-vlm release**

Run: `curl -s https://pypi.org/pypi/mlx-vlm/json | python3 -c "import json,sys;print(json.load(sys.stdin)['info']['version'])"`
Expected: a version, `0.7.12` or newer. Use it as `<VER>` below.

- [ ] **Step 3: Add the pin and re-lock**

In `pyproject.toml` add, beside the existing darwin pins:

```toml
    "mlx>=0.32.4; sys_platform == 'darwin'",
    "mlx-vlm==<VER>; sys_platform == 'darwin'",
```

and change the existing `mlx>=0.32.2` line to the `>=0.32.4` line above (one `mlx` entry). Run `uv lock` then `uv sync --frozen`.
Expected: resolves; `uv run python -c "import mlx_vlm, vllm_mlx; print(mlx_vlm.__version__)"` prints `<VER>`.

- [ ] **Step 4: Check vllm-mlx still runs on the newer mlx-vlm**

Run: `task test:mac`
Expected: PASS. If the hybrid-prefix-cache or MTP patch tests fail because vllm-mlx 0.5.0 breaks on mlx-vlm `<VER>`, revert Step 3's `mlx-vlm` line (keep `mlx>=0.32.4` only if it still resolves), re-lock, and serve mlx-vlm from an isolated tool environment instead: `uv tool install mlx-vlm==<VER>`. Record which path was taken in the PR description; Task 3's `entry` default depends on it.

- [ ] **Step 5: Start the server by hand with a text-only model**

Run (snapshot path from `ls ~/.cache/huggingface/hub/models--mlx-community--Qwen3-4B-4bit/snapshots/*/`):

```bash
APC_ENABLED=1 APC_DISK_ENABLED=0 uv run python -m mlx_vlm.server --model <snapshot> --port 8094 --max-num-seqs 4
curl -s http://127.0.0.1:8094/health
curl -s http://127.0.0.1:8094/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"<snapshot>","messages":[{"role":"user","content":"Reply with the single word: pong"}],"max_tokens":8}'
```

Expected: health answers `{"status":"healthy",...}`; the completion contains `pong`. If the server refuses the text-only checkpoint (`Missing N parameters: vision_tower...`), stop and report: this is upstream issue 1958 and the spec says the work halts here.

- [ ] **Step 6: Extend the recorder for the mlx-vlm dialect**

In `scripts/record_fixtures.py` add arguments `--out` (default `vllm_mlx`; the fixture subdirectory) and `--dialect` (`vllm_mlx` | `mlx_vlm`, default `vllm_mlx`). For `mlx_vlm`: the budget key in the reasoning request is `thinking_budget`, the tool request carries `"tool_parser": "qwen3_coder"`, and two more GET recordings are made: `metrics` from `GET {root}/v1/metrics` and `models` from `GET {root}/v1/models`. The streamed cases are the same five as today (plain, reasoning, tool_call, resume, error_bad_model).

- [ ] **Step 7: Record the fixtures**

Run: `uv run python scripts/record_fixtures.py --base-url http://127.0.0.1:8094/v1 --model <snapshot> --out mlx_vlm --dialect mlx_vlm`
Expected: nine files under `tests/fixtures/mlx_vlm/`; `reasoning.sse` contains either `"reasoning_content"` or `"reasoning"` deltas; `tool_call.sse` contains `"tool_calls"` with an `"index"`; `metrics.json` contains `"in_flight"`.

- [ ] **Step 8: Replace the snapshot path in the fixtures with the name `small`**

Run: `sed -i '' "s#<snapshot>#small#g" tests/fixtures/mlx_vlm/*` (so fixtures carry no local path), then stop the server.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock scripts/record_fixtures.py tests/fixtures/mlx_vlm
git commit -m "engine: pin mlx-vlm <VER> and record its wire fixtures"
```

---

## Phase 2: The engine kind

### Task 2: `EngineSpec` fields for the mlx_vlm kind

**Files:**
- Modify: `src/airlie/config/schema.py` (`ModelSpec` ~line 50, `EngineSpec` ~line 83, `AirlieConfig._problems` ~line 980)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `EngineSpec.kind` accepts `"mlx_vlm"`; new fields `model: str | None`, `autostart: bool = True`, `kv_bits: int | None`, `prefix_cache: bool = False`, `cache_memory_gb: float = 1.0`, `cache_checkpoints: int = 4`, `cache_checkpoint_interval: int = 2048`, `max_image_px: int | None`, `env: dict[str, str]`, `entry: list[str] | None`. `ModelSpec.tool_call_parser` keeps its name and takes mlx-vlm parser names.

- [ ] **Step 1: Write the failing tests**

```python
def test_mlx_vlm_engine_spells_one_model_as_model_or_models(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, engines={"main": {"kind": "mlx_vlm", "port": 8080, "model": "base"}})
    assert cfg.engines["main"].models == ["base"]
    assert cfg.engines["main"].base_url == "http://127.0.0.1:8080/v1"
    assert cfg.engine_for_model("base") == "main"


def test_mlx_vlm_engine_refuses_two_models(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="engines.main: an mlx_vlm engine serves exactly one model"):
        _cfg(tmp_path, engines={"main": {"kind": "mlx_vlm", "port": 8080, "models": ["base", "small"]}})


def test_mlx_vlm_engine_defaults(tmp_path: Path) -> None:
    e = _cfg(tmp_path, engines={"main": {"kind": "mlx_vlm", "port": 8080, "model": "base"}}).engines["main"]
    assert (e.autostart, e.kv_bits, e.prefix_cache) == (True, None, False)
    assert (e.cache_memory_gb, e.cache_checkpoints, e.cache_checkpoint_interval) == (1.0, 4, 2048)
    assert e.env == {} and e.entry is None and e.max_image_px is None


def test_a_role_on_an_mlx_vlm_engine_may_only_pass_known_template_arguments(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="roles.coder.chat_template_kwargs: mlx_vlm engines accept only"):
        _cfg(
            tmp_path,
            engines={"main": {"kind": "mlx_vlm", "port": 8080, "model": "base"}},
            roles={"coder": {"prompt": "p.md", "model": "base", "chat_template_kwargs": {"foo": 1}}},
        )
```

`_cfg` is the module's existing helper that writes a minimal `airlie.yaml` into `tmp_path` and calls `load_config`; extend it to accept `engines=` and `roles=` overrides if it does not already.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -q -k mlx_vlm`
Expected: FAIL (`mlx_vlm` is not a permitted `kind`).

- [ ] **Step 3: Implement the fields**

In `EngineSpec`: `kind: Literal["vllm_mlx", "openai_http", "llama_server", "mlx_vlm"]`; `models: list[str] = Field(default_factory=list)` (drop `min_length=1`); add `model: str | None = None` and the fields in Interfaces with the defaults given. A `model_validator(mode="before")` copies `model` into `models` as `[model]` when `models` is absent or empty. In `_derive_or_require_base_url` include `"mlx_vlm"` in the kinds that derive `base_url`, and raise `ValueError("an engine needs model or models")` when `models` is empty. In `AirlieConfig._problems`: for `kind == "mlx_vlm"` and `len(e.models) != 1` append `f"engines.{ename}: an mlx_vlm engine serves exactly one model"`; add `"mlx_vlm"` to the port-collision check's managed kinds; for every role whose model is served by an `mlx_vlm` engine, keys of `chat_template_kwargs` outside `{"enable_thinking", "reasoning_strength"}` append `f"roles.{rname}.chat_template_kwargs: mlx_vlm engines accept only enable_thinking and reasoning_strength (got {sorted(keys)})"`. `ModelSpec.tool_call_parser`'s description becomes "the engine's parser name (mlx-vlm: qwen3_coder, atem, ...); unset = inferred from the chat template".

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py -q`
Expected: PASS, the existing tests included (vllm_mlx engines still require their models list).

- [ ] **Step 5: Commit**

```bash
git add src/airlie/config/schema.py tests/unit/test_config.py
git commit -m "config: the mlx_vlm engine kind, one model per engine, cache and autostart fields"
```

### Task 3: The mlx-vlm command line and environment

**Files:**
- Create: `src/airlie/engine/mlx_vlm.py`
- Test: `tests/unit/test_mlx_vlm_command.py`

**Interfaces:**
- Consumes: `EngineSpec`, `ModelSpec` (Task 2); `SnapshotResolver = Callable[[ModelSpec], Path]` from `airlie.engine.registry`.
- Produces: `MLX_VLM_ENTRY: list[str] = ["python", "-m", "mlx_vlm.server"]`; `build_mlx_vlm_command(cfg: AirlieConfig, engine_name: str, snapshot_dir: SnapshotResolver) -> tuple[list[str], dict[str, str]]` returning argv and the environment to add; `served_model(cfg, engine_name) -> str` returning `e.served_name(e.models[0])`.

- [ ] **Step 1: Write the failing tests**

```python
def test_main_engine_command_pins_the_spec_flags(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, main={"kind": "mlx_vlm", "port": 8080, "model": "base", "max_num_seqs": 16,
                               "max_kv_size": 131072, "kv_bits": 8, "prefix_cache": True, "cache_memory_gb": 8,
                               "cache_checkpoints": 8, "buffer_cache_mb": 8192, "max_image_px": 1536})
    argv, env = build_mlx_vlm_command(cfg, "main", snap)
    assert argv == [
        "python", "-m", "mlx_vlm.server",
        "--model", "/hf/org/base/r1",
        "--draft-model", "/hf/org/base-mtp/h1", "--draft-kind", "mtp",
        "--host", "127.0.0.1", "--port", "8080",
        "--max-num-seqs", "16", "--max-kv-size", "131072", "--kv-bits", "8",
        "--prefill-step-size", "2048", "--max-pixels", "2359296",
    ]
    assert env == {
        "APC_ENABLED": "1", "APC_DISK_ENABLED": "0", "APC_MEMORY_MAX_GB": "8",
        "APC_CHECKPOINT_ENTRIES": "8", "APC_CHECKPOINT_INTERVAL_TOKENS": "2048",
        "MLX_BUFFER_CACHE_LIMIT": "8192M",
    }


def test_a_model_without_a_head_gets_no_draft_flags_and_no_cache_env_when_off(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, main={"kind": "mlx_vlm", "port": 8083, "model": "small"})
    argv, env = build_mlx_vlm_command(cfg, "main", snap)
    assert "--draft-model" not in argv and "--kv-bits" not in argv and "--max-pixels" not in argv
    assert env == {}


def test_api_key_extra_args_env_and_entry_override(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, main={"kind": "mlx_vlm", "port": 8080, "model": "base", "api_key": "k",
                               "extra_args": ["--log-level", "debug"], "env": {"FOO": "1"},
                               "entry": ["uvx", "--from", "mlx-vlm==0.7.12", "mlx_vlm.server"]})
    argv, env = build_mlx_vlm_command(cfg, "main", snap)
    assert argv[:4] == ["uvx", "--from", "mlx-vlm==0.7.12", "mlx_vlm.server"]
    assert argv[-4:] == ["--api-key", "k", "--log-level", "debug"]
    assert env["FOO"] == "1"
```

`_cfg` writes a config whose models are `base` (with `mtp: {repo: org/base-mtp, revision: h1}`, `estimated_memory_gb: 16`) and `small`; `snap(m) = Path(f"/hf/{m.repo}/{m.revision}")` as in `tests/unit/test_registry.py`. `--max-pixels` is `max_image_px` squared.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_mlx_vlm_command.py -q`
Expected: FAIL with `ModuleNotFoundError: airlie.engine.mlx_vlm`.

- [ ] **Step 3: Implement `build_mlx_vlm_command`**

Flag order exactly as the first test lists; `--draft-model` is the snapshot of `ModelSpec(repo=m.mtp.repo, revision=m.mtp.revision)` when `m.mtp` is set (the head is used as published; nothing is rebuilt), `--draft-kind mtp`. Environment keys only when their source is set: the four `APC_*` keys when `prefix_cache` is true (`APC_MEMORY_MAX_GB` formatted with `:g`), `MLX_BUFFER_CACHE_LIMIT` as `f"{buffer_cache_mb}M"` when set, then `e.env` merged last. `entry` replaces `MLX_VLM_ENTRY` when set.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_mlx_vlm_command.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/engine/mlx_vlm.py tests/unit/test_mlx_vlm_command.py
git commit -m "engine: build the mlx_vlm server command and environment from the spec"
```

### Task 4: Start, probe and stop an mlx_vlm engine

**Files:**
- Modify: `src/airlie/engine/process.py` (`ProcessRunner.spawn`, `RealRunner.spawn`, `HTTPProber`, `EngineProcess.start/_wait_ready/_probe`)
- Modify: `src/airlie/cli.py` (`_managed` ~line 203, `engine_up` ~line 222)
- Test: `tests/unit/test_process.py`, `tests/unit/test_cli_engine.py`

**Interfaces:**
- Consumes: `build_mlx_vlm_command`, `MLX_VLM_ENTRY`, `served_model` (Task 3).
- Produces: `ProcessRunner.spawn(self, argv: list[str], log_path: Path, cwd: Path, env: dict[str, str] | None = None) -> int`; `Prober.loaded_model_ids(self) -> list[str]` (ids whose `/v1/models` entry has `loaded: true`); `EngineProcess` serves `kind == "mlx_vlm"`; `airlie engine up [NAME]` starts one engine by name, else every managed engine with `autostart` true.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_process.py`, with the module's `FakeRunner` and `FakeProber` extended to record `env` and to answer `loaded_model_ids`:

```python
async def test_mlx_vlm_engine_starts_with_its_command_and_env_and_waits_for_loaded(tmp_path: Path) -> None:
    cfg = mlx_cfg(tmp_path)  # engines.main: kind mlx_vlm, model base, prefix_cache True
    runner, prober = FakeRunner(), FakeProber(health=True, ids=["base", "other"], loaded=["other"])
    p = EngineProcess(cfg, "main", state_dir=tmp_path, runner=runner, prober=prober, snapshot_dir=snap, poll_s=0.01)
    task = asyncio.create_task(p.start(timeout_s=1))
    await asyncio.sleep(0.05)
    assert task.done() is False  # listed but not loaded: still waiting
    prober.loaded = ["base", "other"]
    h = await task
    assert runner.spawned[0].argv[:3] == ["python", "-m", "mlx_vlm.server"]
    assert runner.spawned[0].env["APC_ENABLED"] == "1"
    assert h.ok and h.models == {"base": "ready"}
    assert not (tmp_path / "engines" / "main" / "models.yaml").exists()  # no registry file for this kind


async def test_mlx_vlm_engine_times_out_naming_the_unloaded_model(tmp_path: Path) -> None:
    ...  # loaded never includes "base": EngineError matches "base: loading"


async def test_prober_sends_the_api_key_to_health_and_models(tmp_path: Path) -> None:
    ...  # HTTPProber("http://e/v1", api_key="k") with a MockTransport: both requests carry "Bearer k"
```

In `tests/unit/test_cli_engine.py`:

```python
def test_engine_up_skips_engines_with_autostart_false_unless_named(...) -> None:
    ...  # engines: main (autostart true), bench (autostart false): `engine up` starts main only;
         # `engine up bench` starts bench only
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_process.py tests/unit/test_cli_engine.py -q -k "mlx_vlm or autostart or api_key"`
Expected: FAIL.

- [ ] **Step 3: Implement**

`RealRunner.spawn` passes `env={**os.environ, **(env or {})}` to `subprocess.Popen` and prefixes `uv run --quiet` when `argv[:3]` is `SERVE_ENTRY` or `MLX_VLM_ENTRY`. `HTTPProber` gains `loaded_model_ids` reading `data[*].loaded`. `EngineProcess.start`: for `mlx_vlm`, `argv, env = build_mlx_vlm_command(...)`, no registry file, spawn with `env`. `_probe` for `mlx_vlm`: the one served name is `"ready"` when in `loaded_model_ids()`, `"loading"` when health is ok but not loaded, `"missing"` when health fails. `_wait_ready` treats the mlx_vlm engine's model as preloaded. `cli._managed` includes `"mlx_vlm"`; `engine_up` gains an optional positional `name: str | None` and starts `[name]` when given, else `[n for n in _managed(cfg) if cfg.engines[n].autostart]`; `engine_down`/`restart`/`status` keep iterating every managed engine.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_process.py tests/unit/test_cli_engine.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/engine/process.py src/airlie/cli.py tests/unit/test_process.py tests/unit/test_cli_engine.py
git commit -m "engine: start, probe and stop mlx_vlm engines; engine up honours autostart and a name"
```

### Task 5: The doctor sees mlx_vlm engines

**Files:**
- Modify: `src/airlie/doctor.py` (`cache_memory_gb` ~line 208, `project_memory_gb` ~line 219, `projection_detail`, the `tool:vllm-mlx` row ~line 431, hybrid-attention row ~line 522)
- Test: `tests/unit/test_doctor.py`

**Interfaces:**
- Produces: `cache_memory_gb(e) -> float` returns `e.cache_memory_gb` when `e.kind == "mlx_vlm"` and `e.prefix_cache`, else the legacy `--cache-memory-mb` parse; `project_memory_gb` counts an `mlx_vlm` engine's model weights when `e.autostart` is true (regardless of `ModelSpec.preload`) and sums `cache_memory_gb` over every managed engine, not only the one named.

- [ ] **Step 1: Write the failing tests**

```python
def test_memory_projection_counts_every_autostart_mlx_vlm_engine_and_its_cache(tmp_path: Path) -> None:
    # main (base 16 GB, cache 8), reviewer (rev 19 GB, cache 4), bench (autostart False, 20 GB, cache 1)
    gb, unknown = project_memory_gb(cfg, "main")
    assert gb == pytest.approx(16 + 19 + 8 + 4 + live_kv_gb(cfg, cfg.engines["main"]) + cfg.memory.os_reserve_gb)
    assert unknown == []


def test_doctor_reports_mlx_vlm_installed_instead_of_vllm_mlx_for_mlx_vlm_engines(tmp_path: Path) -> None:
    ...  # the tool row is "tool:mlx-vlm" and names `python -m mlx_vlm.server`


def test_hybrid_attention_model_on_an_mlx_vlm_engine_is_ok_without_a_patch(tmp_path: Path) -> None:
    ...  # the row's detail says mlx-vlm's prefix cache checkpoints recurrent layers; no patch named
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_doctor.py -q -k "mlx_vlm"`
Expected: FAIL.

- [ ] **Step 3: Implement**

As in Interfaces. The hybrid-attention row (`is_hybrid_attention`) keeps its `vllm_mlx` wording for `vllm_mlx` engines and says, for `mlx_vlm`, "hybrid (linear/SSM) attention: mlx-vlm's prefix cache stores it as checkpoints (`cache_checkpoints`)". The tool row for `mlx_vlm` checks `importlib.util.find_spec("mlx_vlm")` (or the `entry` command when set) and prints `mlx-vlm <version>`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_doctor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/doctor.py tests/unit/test_doctor.py
git commit -m "doctor: project mlx_vlm engines' weights and prefix cache; name the mlx-vlm tool"
```

Phase 2 ends here: open the pull request "engine: the mlx_vlm engine kind (phase 2)". Proves: with an `mlx_vlm` engine in `airlie.local.yaml` on port 8094, `uv run airlie engine up main94` starts it and `airlie engine status` shows the model `ready`.

---

## Phase 3: The client dialect

### Task 6: One place for the request fields: `dialect.py`

**Files:**
- Create: `src/airlie/engine/dialect.py`
- Modify: `src/airlie/engine/http_engine.py` (`__init__` ~line 40, `_body` ~line 108), `src/airlie/teams/autogen_team.py` (~lines 145–157), `src/airlie/serve/app.py` (~lines 681–685), `src/airlie/cli.py` (`_http_engine` ~line 300: pass `dialect=e.kind`)
- Test: `tests/unit/test_dialect.py`, `tests/unit/test_http_engine_mlx_vlm.py`, `tests/unit/test_serve.py`, `tests/unit/test_autogen_team.py`

**Interfaces:**
- Produces: `Dialect = Literal["vllm_mlx", "mlx_vlm"]`; `dialect_for(kind: str) -> Dialect` (`"mlx_vlm"` for `mlx_vlm`, else `"vllm_mlx"`; `openai_http` is `"vllm_mlx"` until phase 5); `thinking_fields(*, dialect: Dialect, thinking: bool, budget: int | None, chat_template_kwargs: Mapping[str, object], tool_parser: str | None = None, tools: bool = False) -> dict[str, Any]`.
- `HTTPEngine.__init__` gains `dialect: Dialect = "vllm_mlx"`; `_body` merges `thinking_fields(...)` instead of building those keys inline.

- [ ] **Step 1: Write the failing tests**

```python
def test_vllm_mlx_fields_are_what_the_client_sends_today() -> None:
    assert thinking_fields(dialect="vllm_mlx", thinking=True, budget=4096, chat_template_kwargs={"x": 1}) == {
        "enable_thinking": True,
        "chat_template_kwargs": {"enable_thinking": True, "x": 1},
        "thinking_token_budget": 4096,
    }


def test_mlx_vlm_fields_use_its_names_and_map_reasoning_strength() -> None:
    assert thinking_fields(dialect="mlx_vlm", thinking=True, budget=4096,
                           chat_template_kwargs={"reasoning_strength": "low"},
                           tool_parser="atem", tools=True) == {
        "enable_thinking": True, "thinking_budget": 4096, "reasoning_effort": "low", "tool_parser": "atem",
    }


def test_mlx_vlm_fields_omit_what_is_unset() -> None:
    assert thinking_fields(dialect="mlx_vlm", thinking=False, budget=4096, chat_template_kwargs={}) == {
        "enable_thinking": False,
    }  # no budget when not thinking; no tool_parser without tools or a parser name
```

In `tests/unit/test_http_engine_mlx_vlm.py` (copy the `Recorder`/`engine` helpers from `test_http_engine.py`, fixtures from `tests/fixtures/mlx_vlm`, `HTTPEngine(..., dialect="mlx_vlm", model_spec_for=lambda r: ModelSpec(repo="x", revision="r", tool_call_parser="qwen3_coder"))`):

```python
async def test_request_body_speaks_mlx_vlm(...) -> None:
    body = json.loads(rec.requests[0].content)
    assert body["model"] == "small" and "chat_template_kwargs" not in body
    assert "thinking_token_budget" not in body and body["enable_thinking"] is False
    assert body["tool_parser"] == "qwen3_coder"  # the tool_call request
```

In `test_serve.py` and `test_autogen_team.py`: one test each asserting that, for a role served by an `mlx_vlm` engine, the forwarded body carries `thinking_budget` and no `chat_template_kwargs`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_dialect.py tests/unit/test_http_engine_mlx_vlm.py -q`
Expected: FAIL with `ModuleNotFoundError: airlie.engine.dialect`.

- [ ] **Step 3: Implement**

`thinking_fields` for `vllm_mlx` reproduces exactly today's keys from `HTTPEngine._body` (top-level `enable_thinking`, `chat_template_kwargs` with `enable_thinking` first, `thinking_token_budget` only when `thinking and budget`). For `mlx_vlm`: `enable_thinking`; `thinking_budget` when `thinking and budget`; `reasoning_effort` from `chat_template_kwargs["reasoning_strength"]` when present; `tool_parser` when `tools and tool_parser`. The three callers replace their inline keys with the helper; `serve/app.py` and `autogen_team.py` look the dialect up with `dialect_for(cfg.engines[cfg.engine_for_model(model)].kind)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_dialect.py tests/unit/test_http_engine_mlx_vlm.py tests/unit/test_http_engine.py tests/unit/test_serve.py tests/unit/test_autogen_team.py -q`
Expected: PASS, every existing vllm-mlx body assertion included.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/engine/dialect.py src/airlie/engine/http_engine.py src/airlie/teams/autogen_team.py src/airlie/serve/app.py src/airlie/cli.py tests/unit/test_dialect.py tests/unit/test_http_engine_mlx_vlm.py tests/unit/test_serve.py tests/unit/test_autogen_team.py
git commit -m "engine: one dialect module for the thinking, budget and parser request fields"
```

### Task 7: Read mlx-vlm replies

**Files:**
- Modify: `src/airlie/engine/http_engine.py` (`generate` stream loop ~lines 160–260, `health` ~line 90)
- Test: `tests/unit/test_http_engine_mlx_vlm.py`

**Interfaces:**
- Produces: `GenResult.reasoning` filled from `delta.reasoning_content` or `delta.reasoning`; `cached_tokens` from `usage.cached_tokens` or `usage.prompt_tokens_details.cached_tokens`; `accepted_draft_tokens`/`draft_tokens` from `usage.draft_n_accepted`/`usage.draft_n` or the vllm-mlx `generation_metadata`; `ttft_ms` is the client's first-delta time when no `airlie_ttft_ms` is sent; an HTTP error status raises `EngineError` (already) and `finish_reason == "error"` keeps raising.

- [ ] **Step 1: Write the failing tests**

```python
async def test_reasoning_stream_splits_on_either_field(...) -> None:
    res = await eng.generate(req_thinking(), on_token=seen.append)
    assert res.reasoning and res.text and any(e.reasoning for e in seen)


async def test_usage_counters_are_read_from_mlx_vlm_names(...) -> None:
    res = await eng.generate(plain_request())
    assert res.cached_tokens == <value in plain.sse> and res.prompt_tokens > 0
    assert res.ttft_ms > 0  # measured by the client


async def test_tool_call_stream_assembles_by_index(...) -> None:
    res = await eng.generate(tool_request())
    assert [c.name for c in res.tool_calls] == ["<name in tool_call.sse>"]
    assert json.loads(res.tool_calls[0].arguments)


async def test_health_reads_loaded_models(...) -> None:
    h = await eng.health()  # fixtures health.json + models.json
    assert h.ok and h.models == {"small": "ready"}  # only loaded entries are ready; others "lazy"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_http_engine_mlx_vlm.py -q`
Expected: FAIL on the reasoning and usage assertions.

- [ ] **Step 3: Implement**

In the stream loop read `delta.get("reasoning_content") or delta.get("reasoning")`; read `usage.cached_tokens` before the `prompt_tokens_details` fallback; read `usage.draft_n_accepted` / `usage.draft_n` when present. `health()` marks a model `"ready"` when its `/v1/models` entry has `loaded` true or no `loaded` key (vllm-mlx), else `"lazy"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_http_engine_mlx_vlm.py tests/unit/test_http_engine.py tests/unit/test_http_engine_wire.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/engine/http_engine.py tests/unit/test_http_engine_mlx_vlm.py
git commit -m "engine: read mlx-vlm's reasoning, usage and loaded-model fields"
```

### Task 8: The busy probe reads `/v1/metrics`

**Files:**
- Modify: `src/airlie/serve/bridge/busy.py` (`_engine_roots`, `_models`, `busy_reason`)
- Test: `tests/unit/test_bridge_busy.py`

**Interfaces:**
- Produces: `_engine_roots(cfg) -> list[tuple[str, str, str | None, str]]` adds the engine kind; for `kind == "mlx_vlm"` the probe is `GET {root}/v1/metrics` with the engine's key and reads `summary.in_flight` (> 0 means busy: `f"engine {engine} has {n} request(s) in flight"`) and `summary.last_request_at` (epoch seconds; within `bridge.idle_s` and not the bridge's own means busy). Other kinds keep `/v1/status`.

- [ ] **Step 1: Write the failing tests**

```python
def test_mlx_vlm_engine_in_flight_is_busy(tmp_path: Path) -> None:
    ...  # FakeStatus answers /v1/metrics {"summary": {"in_flight": 1, "last_request_at": NOW}} → busy


def test_mlx_vlm_engine_recent_request_is_busy_old_is_idle(tmp_path: Path) -> None:
    ...


def test_mlx_vlm_metrics_probe_sends_the_engine_key(tmp_path: Path) -> None:
    ...  # engines.main.api_key "k": the /v1/metrics request carries "Bearer k"
```

The module's `_cfg` loads the repo's `airlie.yaml`; for these tests build the config from a tmp `airlie.yaml` with one `mlx_vlm` engine (the repo file still says `vllm_mlx` until phase 4a).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_bridge_busy.py -q -k mlx_vlm`
Expected: FAIL (probe hits `/v1/status`).

- [ ] **Step 3: Implement** as in Interfaces; a 404 from `/v1/metrics` still reads as idle; a transport error as "not answering".

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_bridge_busy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/serve/bridge/busy.py tests/unit/test_bridge_busy.py
git commit -m "bridge: the busy probe reads mlx-vlm's metrics for in-flight requests"
```

### Task 9: Decisions through mlx-vlm's endpoint

**Files:**
- Modify: `src/airlie/router/jev.py` (`JevClient.__init__`, `ask`), `src/airlie/router/__init__.py` (`jev_client`), `src/airlie/tools/decide.py` (`attach_decide`)
- Test: `tests/unit/test_router_jev.py` (or the module's existing router test file), `tests/unit/test_decide_tool.py`

**Interfaces:**
- Produces: `JevClient(base_url, *, model, transport, timeout_s, api_key, endpoint: str = "/systemone")`; `jev_client(cfg)` passes `endpoint="/decisions"` when the serving engine's kind is `mlx_vlm`; `ask` accepts both answer shapes: `choice` or `value` for the chosen option, `confidence` when present else the top probability; `attach_decide` adds the tool when the serving engine's kind is `llama_server` or `mlx_vlm`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_ask_reads_an_mlx_vlm_decision_answer() -> None:
    # POST /v1/decisions answers {"answers": {"role": {"type": "choice", "value": "coder",
    #   "probabilities": {"coder": 0.8, "chief": 0.2}}}, "usage": {"input_tokens": 42}}
    ans = await client.ask("state", "role", "which seat", {"coder": "writes", "chief": "plans"})
    assert (ans.choice, ans.confidence, ans.input_tokens) == ("coder", 0.8, 42)
    assert recorded_path == "/v1/decisions"


def test_jev_client_targets_decisions_on_an_mlx_vlm_engine(tmp_path: Path) -> None:
    ...


def test_decide_tool_attaches_for_an_mlx_vlm_decision_engine(tmp_path: Path) -> None:
    ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit -q -k "decision or jev or decide"`
Expected: FAIL.

- [ ] **Step 3: Implement** as in Interfaces; the existing systemone tests stay green.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit -q -k "decision or jev or decide"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/router src/airlie/tools/decide.py tests/unit
git commit -m "router: typed decisions through mlx-vlm's /v1/decisions as well as llama.cpp's /v1/systemone"
```

Phase 3 ends here: open the pull request "engine: the mlx-vlm client dialect (phase 3)". Proves, on Noah's Mac with an `mlx_vlm` engine on port 8094 in `airlie.local.yaml` serving the generalist: `uv run airlie run --topology single "write a function that reverses a string, with a test" --repo <scratch repo>` completes one coder layer with the roles unchanged while the committed config still points at vllm-mlx. Record, while vllm-mlx is still the configured engine, the vllm-mlx half of every gate row in the spec's Feature comparison table (live tests, prefill and decode tokens per second on the same prompts, time to first token, one coder layer's wall clock, attachments, a review round, cache hits, concurrency); paste the numbers into the PR.

---

## Phase 4a: Switch the config

### Task 10: Four mlx_vlm engines, profiles without engine flags, no `--topology` on restart

**Files:**
- Modify: `airlie.yaml` (engines block), `config/models.yaml` (parser names, `preload` comments), `config/profiles.yaml` (strip `engines:` overrides and `models.*.preload`), `src/airlie/doctor.py` (`engine_matches_profile` → `engine_matches_config`, `_PROFILE_FLAGS`), `src/airlie/story/line.py` (`_profile_gate`, the "engine down" message), `src/airlie/cli.py` (drop `topology: ProfileOpt` from `engine_up/down/restart/status`), `Taskfile.yml` (`up`/`restart` descriptions), `README.md` (engine sections)
- Test: `tests/unit/test_doctor.py`, `tests/unit/test_config.py`, `tests/unit/test_story_line.py` (or wherever `_profile_gate` is tested)

**Interfaces:**
- Produces: `engine_matches_config(cfg: AirlieConfig, probe: SystemProbe, engine_name: str) -> CheckResult` comparing the running `mlx_vlm.server` process on the engine's port (`probe.process_args("mlx_vlm.server")`, filtered by `--port`) against `build_mlx_vlm_command` on `--max-num-seqs`, `--max-kv-size`, `--kv-bits`, `--draft-model`; `fail` names the flags and the command `task restart`. The story line calls it for `cfg.engine_for_role(cfg.story.build_role)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_committed_config_declares_four_autostart_mlx_vlm_engines_and_three_bench_engines() -> None:
    cfg = load_config(ROOT / "airlie.yaml", local_path=ROOT / "no-local-overrides.yaml")
    kinds = {n: e.kind for n, e in cfg.engines.items()}
    assert kinds == {"main": "mlx_vlm", "reviewer": "mlx_vlm", "small": "mlx_vlm", "decision": "llama_server",
                     "decision_mlx": "mlx_vlm", "decision_mlx2": "mlx_vlm",
                     "coder8": "mlx_vlm", "llama70b": "mlx_vlm", "qwen25_32b": "mlx_vlm"}
    assert [n for n, e in cfg.engines.items() if e.autostart] == ["main", "reviewer", "small", "decision"]
    assert cfg.engines["main"].models == ["generalist"] and cfg.engines["main"].port == 8080
    assert cfg.models["generalist"].tool_call_parser == "qwen3_coder"
    assert cfg.models["reviewer"].tool_call_parser == "atem"
    assert cfg.models["generalist"].reasoning_parser is None


def test_profiles_carry_no_engine_overrides() -> None:
    for name, profile in cfg.topology.profiles.items():
        assert "engines" not in profile, name


def test_an_mlx_vlm_engine_running_other_flags_than_the_config_is_a_mismatch_naming_task_restart(...) -> None:
    ...  # probe returns argv with --max-kv-size 65536 while config says 131072 → fail, detail ends "task restart"


def test_the_story_line_gates_on_the_build_engines_config_not_a_profile(...) -> None:
    ...
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_doctor.py -q -k "committed or profiles_carry or mismatch"`
Expected: FAIL.

- [ ] **Step 3: Rewrite the engines block of `airlie.yaml`**

```yaml
engines:
  main:                            # the team model; its MTP head rides as the draft model
    kind: mlx_vlm
    port: 8080
    model: generalist
    max_num_seqs: 16
    max_kv_size: 131072
    kv_bits: 8
    prefill_step_size: 2048
    prefix_cache: true
    cache_memory_gb: 8
    cache_checkpoints: 8
    buffer_cache_mb: 8192
    max_image_px: 1536
  reviewer:                        # the adversarial reviewer: any model from a different lab than the coder's
    kind: mlx_vlm
    port: 8082
    model: reviewer
    max_num_seqs: 4
    max_kv_size: 65536
    kv_bits: 8
    prefix_cache: true
    cache_memory_gb: 4
  small:
    kind: mlx_vlm
    port: 8083
    model: small
    max_num_seqs: 8
    max_kv_size: 16384
    prefix_cache: true
    cache_memory_gb: 1
  decision:                        # Kev on llama-server, unchanged until the phase 4b gate picks its replacement
    kind: llama_server
    port: 8081
    models: [kev4b]
    ctx_size: 16384
  decision_mlx:  { kind: mlx_vlm, port: 8084, model: clef_flash, autostart: false, max_num_seqs: 4 }   # gate candidate
  decision_mlx2: { kind: mlx_vlm, port: 8085, model: decider_2b, autostart: false, max_num_seqs: 4 }   # gate candidate
  coder8:  { kind: mlx_vlm, port: 8091, model: coder8, autostart: false, max_kv_size: 65536, kv_bits: 8 }
  llama70b: { kind: mlx_vlm, port: 8092, model: llama70b, autostart: false, max_kv_size: 65536, kv_bits: 8 }
  qwen25_32b: { kind: mlx_vlm, port: 8093, model: qwen25_32b, autostart: false, max_kv_size: 65536, kv_bits: 8 }
```

The studio profile's values (131072 KV, 8-bit KV, 8192 MB buffer cache) become the base, as the spec says. `models.yaml`: `tool_call_parser: qwen3_coder` on generalist and coder8, `atem` on reviewer, remove every `reasoning_parser`, remove `preload` lines (autostart on the engine decides); add the two gate candidates, `clef_flash` (`mlx-community/clef-flash-4bit`) and `decider_2b` (`Mapika/decider-2b`), each pinned to the revision `task models:pull` fetches. `profiles.yaml`: delete every `engines:` and `models:` block from `studio`, `studio-qwen25`, `studio-coder8`; the two experiment profiles keep `roles.coder.model` (the bench engine must be started by hand: `airlie engine up coder8`). Update the comment at the top of each profile accordingly. The `decision` engine stays Kev on `llama_server` through 4a so the gate can compare against it; Task 11 step 6 settles which candidate becomes `decision` (kind `mlx_vlm`, port 8081), and Task 12 removes the loser and `llama_server`.

- [ ] **Step 4: Implement `engine_matches_config`, retire `--topology` on engine commands, update the line**

`doctor.py`: rename and reshape as in Interfaces (keep a thin `engine_matches_profile` that calls it for the story's build engine, so `story/line.py` and tests change minimally); `_PROFILE_FLAGS = ("--max-num-seqs", "--max-kv-size", "--kv-bits", "--draft-model")`. `cli.py`: remove the `topology` parameter from the four engine commands. `story/line.py`: the engine-down message becomes "engine down: start it with `task restart`". `Taskfile.yml` descriptions say "mlx-vlm engines". `README.md`: the `task up` row, the engine section (lines ~97–105, 238–241, 281–285, 298–303) describe mlx_vlm engines, one process per model, `airlie engine up <name>` for bench engines.

- [ ] **Step 5: Run the gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -n auto --maxprocesses 8 && task web:gate`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add airlie.yaml config/models.yaml config/profiles.yaml src/airlie/doctor.py src/airlie/story/line.py src/airlie/cli.py Taskfile.yml README.md tests/unit
git commit -m "config: four mlx_vlm engines, profiles without engine flags, task restart without --topology"
```

Phase 4a ends here: open the pull request "config: switch to mlx_vlm engines (phase 4a)". The production line keeps running on vllm-mlx until Noah restarts it.

---

## Phase 4b: The gate (a scheduled run, not a pull request)

### Task 11: Run every gate row on mlx-vlm and paste the results into the 4a pull request

Run only when the story line is idle (`task line:status` shows no lane held) and with the vllm-mlx engine stopped (`uv run airlie engine down` on the old config). Each row is run twice: `main.prefix_cache: true` with `models.generalist.mtp` removed, then `prefix_cache: false` with the head back (spec, Validation gate). Paste a table per row with the vllm-mlx numbers recorded in Phase 3 beside the mlx-vlm numbers.

- [ ] **Step 1: Start the engines**

Run: `task up` then `task status`
Expected: main, reviewer, small, decision `ready`.

- [ ] **Step 2: Live tests**

Run: `task test:live`
Expected: `tests/live/test_engine_live.py` and `tests/live/test_parsers_live.py` PASS against main; run the reviewer's subset with `AIRLIE_LIVE_ENGINE=reviewer` (add the environment switch to `tests/live/conftest.py` if it does not exist: it picks `cfg.engines[name]`).

- [ ] **Step 3: Text-only models**

Run: `uv run airlie engine up coder8 && curl -s http://127.0.0.1:8091/v1/chat/completions -d '{"model":"coder8","messages":[{"role":"user","content":"Reply with the single word: pong"}],"max_tokens":8}' -H 'content-type: application/json'`; then `uv run airlie engine down coder8`.
Expected: `pong`. Same for `small` on 8083.

- [ ] **Step 4: Attachments, reviewer, cache hits, MTP, concurrency**

Through the web app's chat: one picture and one video described correctly. One review round via `task studio -- "review this diff" --repo .` returns a parsed verdict with no client-side parsing (`output_parse` logs nothing). Cache hits: send a two-turn conversation twice while four other requests stream; the second turn's `usage.cached_tokens` > 0 on main and reviewer. MTP: `usage.draft_n` > 0 on main with the head configured; report `draft_n_accepted / draft_n`. Concurrency: two `airlie run` jobs on main at once, no interleaved or corrupted text (the PR 181 failure).

- [ ] **Step 5: Speed**

Run `task bench:models -- --ctx 4k --n 1,4` for the generalist on both engines (the vllm-mlx numbers from Phase 3) and one coder layer's wall clock from brief to green tests on the same scratch repo task as Phase 3.
Expected: mlx-vlm not worse on the same hardware; if it is, say so in the PR and stop.

- [ ] **Step 6: Decision model**

Run airlie's router questions (the `ROLE_QUESTION` criteria from `src/airlie/router/protocol.py` over twenty recorded briefs from `docs/bench/`) through Kev on llama-server (8081) and through `decision_mlx` (Clef-flash, 8084) and `decision_mlx2` (decider-2b, 8085) via `JevClient(..., endpoint="/decisions")`; a short script under `scripts/` is fine. Report agreement with Kev's choices and the mean absolute difference of confidences. The winner becomes `engines.decision.model` in a follow-up commit on the 4a branch; if neither matches Kev, leave `decision` on llama_server and say so.

- [ ] **Step 7: Paste the results and stop**

Paste every row into the 4a pull request description under "Gate results" and stop. Noah reads them and gives the go for phase 5.

---

## Phase 5: Delete vllm-mlx and llama-server (after Noah's go)

### Task 12: One pull request, "vllm-mlx and llama-server leave"

**Files:**
- Delete: `src/airlie/engine/vllm_mlx_serve.py`, `src/airlie/engine/patches/` (whole directory), `src/airlie/engine/parsers/` (whole directory), `src/airlie/engine/mtp.py`, `src/airlie/engine/registry.py`'s `build_registry`, `write_registry`, `build_serve_command`, `build_llama_command`, `gguf_path`, `SERVE_ENTRY` (keep `SnapshotResolver` and `uses_mtp`, or move them into `mlx_vlm.py`), the parser-registry use in `src/airlie/engine/output_parse.py` (the function keeps only the `<think>` strip, or is deleted with its tests if nothing calls it), `tests/fixtures/vllm_mlx/`, and the test files: `test_vllm_mlx_serve.py`, `test_mtp_batch_join.py`, `test_hybrid_prefix_cache.py`, `test_active_requests.py`, `test_first_output.py`, `test_qwen3_xml_raw.py`, `test_muse_parsers.py`, `test_engine_mtp.py`, `tests/unit/vllm_helpers.py`, `test_registry.py`'s vllm and llama tests, `test_http_engine.py` and `test_http_engine_wire.py` (replaced by the mlx_vlm fixture tests; keep any contract test that is engine-neutral by pointing it at `tests/fixtures/mlx_vlm`).
- Modify: `src/airlie/config/schema.py` (`kind: Literal["mlx_vlm", "openai_http"]`; remove `memory_budget_gb`, `continuous_batching`, `use_paged_cache`, `server_parsers`, `per_request_draft`, `kv_cache_quantization`, `gpu_memory_utilization`, `max_request_tokens`, `ctx_size`, `binary`; remove `ModelSpec.reasoning_parser`, `preload`, `enable_mtp`, `continuous_batching`, `file`), `src/airlie/engine/dialect.py` (`Dialect = Literal["mlx_vlm"]`, `dialect_for` returns it for every kind), `src/airlie/engine/http_engine.py` (drop `airlie_ttft_ms`, `generation_metadata`, `prompt_tokens_details` and `finish_reason == "error"` branches and the `thinking_token_budget` path), `src/airlie/serve/bridge/busy.py` (metrics only), `src/airlie/router/jev.py` (`endpoint` fixed to `/decisions`), `src/airlie/tools/decide.py`, `src/airlie/doctor.py` (the vllm-mlx tool row, `cache_memory_gb`'s legacy parse, `[mllm_cache]` and `engine:install` text, `is_hybrid_attention` wording), `src/airlie/cli.py` (`_managed` is `mlx_vlm` only), `tests/unit/conftest.py` (`MAC_ONLY` keeps only `test_learned_controller.py`; `MAC_ONLY_TESTS` keeps only the entries that import mlx; `HAS_MLX` checks `mlx` not `vllm_mlx`), `pyproject.toml` (remove `vllm-mlx` and `mlx-lm`; `mac_functions`/mutation lists drop the deleted files), `uv.lock`, `airlie.yaml` (remove the `llama_server` `decision` engine and the two `decision_mlx*` engines, leaving `decision: {kind: mlx_vlm, model: <winner>}`), `config/models.yaml` (remove `kev4b` and the loser), `README.md`, `docs/bench/README.md`, `config/profiles.yaml` comments, `src/airlie/prompts/steering/reasoning-carried.md` (re-read against mlx-vlm's template behaviour from the gate; rewrite or delete).

- [ ] **Step 1: Delete the files and fields listed above**

- [ ] **Step 2: Run the whole gate**

Run: `uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -n auto --maxprocesses 8 && task web:gate && task test:mac`
Expected: PASS.

- [ ] **Step 3: Prove nothing is left**

Run: `grep -rn -E "vllm_mlx|vllm-mlx|mlx_lm|mlx-lm|llama_server|llama-server|systemone" src tests scripts airlie.yaml config Taskfile.yml pyproject.toml README.md | grep -v docs/`
Expected: no output. `grep -c "vllm-mlx\|mlx-lm" uv.lock` prints `0`.

- [ ] **Step 4: Commit and open the pull request**

```bash
git add -A
git commit -m "engine: vllm-mlx and llama-server leave; mlx-vlm is the one local engine"
```

---

## Not in this plan

- Phase 6 of the spec, team shapes as workflow nodes (`teams`, `fork` node types), is nodes-are-config plan 3 and is not handed to this implementer.
- Filing the upstream issues airlie never posted (spec, Risks) is Noah's or a later session's; nothing here depends on them.

## Self-review notes

- Spec coverage: Intent and Target architecture → T2–T5, T10; the mlx_vlm engine kind table → T2, T3; Client changes table → T6–T9; What gets deleted → T12; Validation gate → T11 (and the Phase 3 note records the vllm-mlx half); Team shapes → out of scope by the spec's own phase 7; Decision model → T9, T10 step 3, T11 step 6; Risks → T1 step 4 (vllm-mlx on newer mlx-vlm), T1 step 5 (issue 1958), T3 (APC disk off), T10 (cache sizes), T11 (issue 1471 via the two configurations).
- Deviation from the spec, stated: `EngineSpec.model` is accepted as the spec writes it and stored in the existing `models` list, so `engine_for_model` and every validator keep working through phases 2–4; `ModelSpec.preload` is removed in phase 5, not phase 2, because the vllm_mlx kind still reads it until then.
- Interfaces named consistently: `build_mlx_vlm_command`, `MLX_VLM_ENTRY`, `served_model` (T3) used by T4, T5, T10; `thinking_fields`, `dialect_for`, `Dialect` (T6) used by T7, T12; `loaded_model_ids` (T4) used by T7's `health`; `engine_matches_config` (T10) used by `story/line.py`.
