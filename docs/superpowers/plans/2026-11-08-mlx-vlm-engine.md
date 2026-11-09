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
- The `Engine` protocol in `src/airlie/engine/protocol.py` (its types in `src/airlie/engine/types.py`) does not change (spec, Where we are).
- mlx-vlm is pinned to one exact release: the latest on PyPI that passes Task 1 (0.7.12 when the spec was written). `mlx>=0.32.4`. The upstream server has no mlx-lm dependency.
- Ports: main 8080, decision 8081, reviewer 8082, small 8083, decision gate candidates 8084–8085, bench engines 8091–8093, hand-started test server 8094, the phase 3 proof's local small and reviewer engines 8095–8096. Port 8090 is `task serve`'s and is never used for an engine.
- An mlx_vlm server knows its model only by the exact `--model` (or `--decision-model`) string, and a request naming anything else makes it unload the resident model. Airlie passes the model's directory (`models.<m>.path`, else the HF snapshot) as that string, the client sends the same string as `model`, and readiness matches it. There is no served alias: `EngineSpec.served_name(m)` returns that path for mlx_vlm engines, filled in by `load_config` (Task 2).
- Prefix cache defaults per engine: `cache_memory_gb` main 8, reviewer 4, small 1, decision 1; `cache_checkpoints` 8 on main, 4 elsewhere; `cache_checkpoint_interval` 2048; disk tier off (`APC_DISK_ENABLED=0`, always emitted with `prefix_cache` because upstream defaults it to 1). The `APC_*` names are environment only; mlx-vlm has no CLI flags for them.
- mlx-vlm reads no `MLX_BUFFER_CACHE_LIMIT` (live test 2026-11-08), so airlie does not set it for mlx_vlm engines and `buffer_cache_mb` is a config error on them. The spec's field-table row for it is overtaken by that finding.
- mlx-vlm's `--log-level` takes uppercase names (`DEBUG`, `INFO`, ...); argparse rejects `debug`.
- KV quantization is a bit count (`kv_bits`, default 8 where the studio profile had it on), not a boolean.
- Tool parsers are mlx-vlm names sent per request: `qwen3_coder` for generalist and coder8, `atem` for reviewer, unset elsewhere. Reasoning parsers go.
- The reviewer seat is a rule, not a model: any adversarial model from a different lab than the coder's (spec, Target architecture). Nothing in code may assume Muse.
- The phase 4b gate and the phase 5 delete need Noah's go after reading the gate results. The implementer stops after 4b.
- Nodes-are-config plan 1 merged as b6df184 (#261), followed by #262 (9fa5ae7); this plan's branch starts from 9fa5ae7. The only shared file is `src/airlie/config/schema.py` (it edited the `AirlieConfig` area; this plan edits `EngineSpec`, `ModelSpec` and `AirlieConfig.validate_references`). The builder files (`config/workflows/*.yaml`, `web/src/screens/builder/*`, `src/airlie/serve/workflows_api.py`) belong to another session; no task here touches them.

## Review Focus

Inputs the spec implies that no task's happy path exercises; each line's test is pinned to the task that owns the code:

1. mlx-vlm's `/v1/models` lists every model in the HF cache with a `loaded` flag, not only the served one, and the loaded entry's `id` is the literal `--model` string. Readiness must require the entry whose `id` equals the engine's exact model path with `loaded: true`, or `task up` returns before the weights are in memory and the first request pays the load. `/health` answers `"healthy"` even after a failed preload (and reports `loaded_model: null` for a decision-only engine), so it is no readiness signal on its own; `/v1/metrics` `server.preload_failures` fails the start fast (Task 4).
2. A request that names a model other than the resident one makes mlx-vlm unload the resident model (seen live: a bad name left `loaded_model` null). The client must always send the engine's exact model path, and each role's request must go to the engine that serves the role's model: today one vllm-mlx process serves every role, so `_http_engine` hands the story line and `airlie run` one engine for all roles, which after Task 10 would send the reviewer's and summarizer's models to main (Tasks 2, 6).
3. When `api_key` is set, mlx-vlm's `/health`, `/v1/models` and `/v1/metrics` all answer 401 without `Authorization: Bearer <key>`. The readiness prober and the busy probe must send it or the engine reads as down forever (Tasks 4 and 8).
4. mlx-vlm has no generic `chat_template_kwargs` passthrough: it reads only `enable_thinking`, and `reasoning_effort` per request. A role whose `chat_template_kwargs` holds a key other than `enable_thinking` or `reasoning_strength` must fail config validation for mlx_vlm engines, not be dropped silently (Task 2).
5. A reply carries both `reasoning_content` and its alias `reasoning`, in stream deltas and in the non-stream message, with null keys on most chunks. The client reads one (`reasoning_content`, else `reasoning`), never both, and tolerates nulls everywhere (`index`, `id`, `usage` fields) (Task 7).
6. Usage and full `timings` arrive only in a final chunk with `choices: []`, and only when the request sends `stream_options: {"include_usage": true}`. Cached tokens are at `usage.prompt_tokens_details.cached_tokens` (also `timings.cache_n`), not `usage.cached_tokens`; the draft counters are at `timings.draft_n` / `timings.draft_n_accepted` (null without a drafter); `/v1/metrics` `latest.reasoning_tokens` is wrong and is never read (Task 7).
7. A decision model is started with `--decision-model`, not `--model`; started with `--model` it lands in text generation and `/v1/decisions` answers 400. Its answer's choice is `value` and its confidence `metadata.confidence` (Tasks 3, 9).

## Amendments 2026-11-08

From the mlx-vlm 0.7.12 live test and source read (`.superpowers/sdd/2026-11-08-mlx-vlm-engine/live-findings.md`) and a check of every task against main at 9fa5ae7 (after nodes-are-config #261 and #262):

1. Model naming: the client sends, and readiness matches, the exact `--model` path; no served alias. `load_config` fills `served_names` with `model_dir(m)` for mlx_vlm engines and refuses an explicit one (Global Constraints; T2, T3, T4, T6, T9, T11).
2. Fixtures carry the placeholder path `/models/small`, not the name `small`, and the tests configure `models.small.path: /models/small` (T1 step 8; T4, T6, T7).
3. Each role's request goes to the engine serving its model: `RoutedEngine` behind `cli._http_engine`, since the story line and `airlie run` hand one engine to every role today (T6; Review Focus 2).
4. Reply fields: cached tokens stay at `usage.prompt_tokens_details.cached_tokens` (the existing read), draft counters come from `timings`, usage arrives in the final `choices: []` chunk, every read tolerates nulls (T7, T11, T12).
5. `stream_options: {"include_usage": true}` is asserted on the client's body and kept in the recorder (T1, T6).
6. Reasoning is read from `reasoning_content`, else `reasoning`, never both (T7; Review Focus 5).
7. No client-side `parse_output` on the mlx_vlm dialect, in `HTTPEngine` and the AutoGen client (T7).
8. `MLX_BUFFER_CACHE_LIMIT` is dropped: `buffer_cache_mb` is refused on mlx_vlm engines and leaves the base `main` engine (Global Constraints; T2, T3, T10, T12).
9. `--log-level` values are uppercase (`DEBUG`) in T3's test (Global Constraints).
10. Decision engines: `EngineSpec.decision` and a `--decision-model` branch in `build_mlx_vlm_command`; the gate candidates set it (T2, T3, T10, T11).
11. `JevClient` reads mlx-vlm's answer (`value`, `metadata.confidence`, top probability fallback), sends the exact path as `model`, and falls back across `usage` key names (T9).
12. `router.kind: jev` validation and `attach_decide` accept an mlx_vlm engine with `decision: true` (T9).
13. Readiness: the `/v1/models` entry whose `id` equals the exact path with `loaded: true`; `/v1/metrics` `server.preload_failures` fails the start fast; Bearer on every probe; `/health` alone is not trusted (T4; Review Focus 1, 3).
14. Recorder: `metrics` recorded, top-level `enable_thinking`, `logprobs` skipped for mlx_vlm, `error_bad_model` recorded last because it unloads the model (T1 steps 6–7).
15. The hand start passes `--host 127.0.0.1`, and the APC self-check log line is named as noise (T1 step 5).
16. `APC_DISK_ENABLED=0` is always emitted with `APC_ENABLED=1`; the five `APC_*` keys are environment only (Global Constraints; T3).
17. T1 step 4 says the mlx-vlm and vllm-mlx coexistence in the main environment is untested and checks the import first.
18. Stale references fixed: the `Engine` protocol lives in `engine/protocol.py`; `AirlieConfig._problems` is `validate_references`; nodes-are-config has merged; line numbers re-read across T1–T10 (Global Constraints; T1 step 1).
19. Test helpers match the tree: `test_config.py` has `MINIMAL`/`write`, not `_cfg`; `test_process.py`'s `FakeRunner`/`FakeProber` shapes; the router tests are `test_jev_client.py` and `test_router.py` (T2, T3, T4, T9).
20. `hf_snapshot_dir` moves from `engine/process.py` to `config/load.py` so the loader can fill the path (T2).
21. Dialect plumbing reaches the AutoGen path: `build_model_client`/`AutoGenTeam` take `dialect`, set by `orchestrator.py`, `topology/fork.py` and `story/amigos.py`; `yes_no` uses `thinking_fields` too (T6).
22. Doctor: `SystemProbe.package_version` for the mlx-vlm binary row; `all_process_args` filtered by `--port`, because `process_args` requires `" serve"` and never sees `mlx_vlm.server` (T5, T10).
23. `qwen25_32b` and `llama70b` lose their `qwen`/`llama` parser names: mlx-vlm has neither and answers 422 to an unknown one (T10).
24. T10 names the existing tests it must rewrite: the qwen25 and coder8 profile tests and the `--topology` engine test (T10).
25. The phase 2 and phase 3 proofs use local configs that keep every role model on exactly one engine (Phase 2 and 3 notes).
26. T11's live conftest builds its engine with `cli._http_engine`; the hand `curl` and the decision comparison use the exact path; one raw `/v1/decisions` reply is recorded (T11).
27. T12 keeps the `prompt_tokens_details` read, removes `buffer_cache_mb`, and writes the winning decision engine with `decision: true` and `router.model` (T12).
28. The busy probe treats a null `last_request_at` and a 401 as idle (T8).
29. Ports 8084–8085 (decision candidates) and 8095–8096 (phase 3 proof) join the port list (Global Constraints).

---

## File Structure

New files:

- `src/airlie/engine/mlx_vlm.py` — the `mlx_vlm` kind's command line and environment from an `EngineSpec`.
- `src/airlie/engine/dialect.py` — the per-engine-kind request fields (thinking, budget, parser, template arguments) shared by `HTTPEngine`, the AutoGen client and serve.
- `src/airlie/engine/routed.py` — `RoutedEngine`: one `Engine` over several `HTTPEngine`s, each role's request sent to the engine that serves its model (Task 6).
- `tests/unit/test_mlx_vlm_command.py`, `tests/unit/test_dialect.py`, `tests/unit/test_http_engine_mlx_vlm.py`, `tests/unit/test_routed_engine.py`.
- `tests/fixtures/mlx_vlm/` — recorded wire fixtures (Task 1).

Modified files (by task): `pyproject.toml`, `uv.lock` (T1); `scripts/record_fixtures.py` (T1); `src/airlie/config/schema.py`, `src/airlie/config/load.py` (T2, T9); `src/airlie/engine/process.py` (T2, T4); `src/airlie/doctor.py` (T5, T10); `src/airlie/engine/http_engine.py` (T6, T7); `src/airlie/teams/autogen_team.py`, `src/airlie/orchestrator.py`, `src/airlie/topology/fork.py`, `src/airlie/story/amigos.py`, `src/airlie/serve/app.py`, `src/airlie/story/factory.py` (T6); `src/airlie/serve/bridge/busy.py` (T8); `src/airlie/router/jev.py`, `src/airlie/router/__init__.py`, `src/airlie/tools/decide.py` (T9); `src/airlie/cli.py` (T4, T6, T10); `airlie.yaml`, `config/models.yaml`, `config/profiles.yaml`, `src/airlie/story/line.py`, `Taskfile.yml`, `README.md` (T10); `tests/live/conftest.py` (T11); everything under "What gets deleted" in the spec (T12).

---

## Phase 1: Prerequisites

### Task 1: Pin mlx-vlm, prove the server answers, record fixtures

**Files:**
- Modify: `pyproject.toml` (dependencies block, lines 8–14; the darwin pins are lines 10–12), `uv.lock`
- Modify: `scripts/record_fixtures.py`
- Create: `tests/fixtures/mlx_vlm/{health.json,models.json,metrics.json,nonstream.json,plain.sse,reasoning.sse,tool_call.sse,resume.sse,error_bad_model.json}`

**Interfaces:**
- Produces: the fixture files named above, read by Tasks 4, 7 and 8; every occurrence of the server's model path in them is the placeholder `/models/small`, and the tests that read them configure `models.small.path: /models/small` so the client sends and matches exactly that string; the pinned version string used in Task 3's `MLX_VLM_ENTRY` fallback.

- [ ] **Step 1: Start from main after the nodes-are-config merge**

Run: `git fetch origin && git rebase origin/main` (main is at or past 9fa5ae7, which already holds b6df184 #261 and #262)
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

Run: `uv run python -c "import vllm_mlx, mlx_vlm, mlx.core; print(mlx_vlm.__version__)"`, then `task test:mac`
Expected: PASS. The 2026-11-08 live test ran mlx-vlm 0.7.12 on mlx 0.32.4 in a scratch environment without vllm-mlx, so whether the two coexist in the main environment is untested until this step. If the resolve in Step 3 fails, or the hybrid-prefix-cache or MTP patch tests fail because vllm-mlx 0.5.0 breaks on mlx-vlm `<VER>`, revert Step 3's `mlx-vlm` line (keep `mlx>=0.32.4` only if it still resolves), re-lock, and serve mlx-vlm from an isolated tool environment instead: `uv tool install mlx-vlm==<VER>`. Record which path was taken in the PR description; Task 3's `entry` default depends on it.

- [ ] **Step 5: Start the server by hand with a text-only model**

Run (snapshot path from `ls ~/.cache/huggingface/hub/models--mlx-community--Qwen3-4B-4bit/snapshots/*/`):

```bash
APC_ENABLED=1 APC_DISK_ENABLED=0 uv run python -m mlx_vlm.server --model <snapshot> --host 127.0.0.1 --port 8094 --max-num-seqs 4
curl -s http://127.0.0.1:8094/health
curl -s http://127.0.0.1:8094/v1/models
curl -s http://127.0.0.1:8094/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"<snapshot>","messages":[{"role":"user","content":"Reply with the single word: pong"}],"max_tokens":8}'
```

`--host 127.0.0.1` because the server binds 0.0.0.0 by default. `<snapshot>` is the exact string passed to `--model`, in every request: any other `model` value makes the server unload what it holds. The startup log's `ERROR - APC self-check failed ... model has no make_cache` is noise (APC works; seen live), not a failure.
Expected: health answers `{"status":"healthy",...}` with `loaded_model` equal to `<snapshot>`; `/v1/models` has an entry with `"id": "<snapshot>"` and `"loaded": true`; the completion contains `pong`. If the server refuses the text-only checkpoint (`Missing N parameters: vision_tower...`), stop and report: this is upstream issue 1958 and the spec says the work halts here.

- [ ] **Step 6: Extend the recorder for the mlx-vlm dialect**

In `scripts/record_fixtures.py` add arguments `--out` (default `vllm_mlx`; the fixture subdirectory, replacing the module's fixed `OUT`) and `--dialect` (`vllm_mlx` | `mlx_vlm`, default `vllm_mlx`); `--model` stays required and for `mlx_vlm` is the exact `--model` string the server was started with. For `mlx_vlm`: every request sends top-level `"enable_thinking"` instead of `chat_template_kwargs`; the reasoning request adds `"thinking_budget": 128`; the tool request carries `"tool_parser": "qwen3_coder"`; every streamed request keeps `"stream_options": {"include_usage": true}` (the `base` dict already has it: without it mlx-vlm sends no usage or timings chunk); the `logprobs` case is skipped; and one more GET is recorded, `metrics` from `GET {root}/v1/metrics` (`health` and `models` are already recorded, `models` from `{base_url}/models`). Order for `mlx_vlm`: `health`, `models`, `metrics`, then `plain`, `reasoning`, `tool_call`, `resume`, `nonstream`, and `error_bad_model` last, because a request naming another model unloads the resident one and every recording after it would hit an empty server. The vllm_mlx order and cases stay as they are.

- [ ] **Step 7: Record the fixtures**

Run: `uv run python scripts/record_fixtures.py --base-url http://127.0.0.1:8094/v1 --model <snapshot> --out mlx_vlm --dialect mlx_vlm`
Expected: nine files under `tests/fixtures/mlx_vlm/`; `reasoning.sse` contains `"reasoning_content"` (and its alias `"reasoning"`) deltas; `tool_call.sse` contains `"tool_calls"` with an `"index"`; every `.sse` ends with a chunk whose `"choices"` is `[]` carrying `"usage"` and `"timings"`; `resume.sse`'s usage has `"prompt_tokens_details"`; `metrics.json` contains `"in_flight"`, `"last_request_at"` and `"preload_failures"`; `models.json` has the entry `"id": "<snapshot>"` with `"loaded": true`.

- [ ] **Step 8: Replace the snapshot path in the fixtures with the placeholder `/models/small`**

Run: `sed -i '' "s#<snapshot>#/models/small#g" tests/fixtures/mlx_vlm/*`, then replace any other local path left in `models.json` (other HF-cache entries) with `/models/other-1`, `/models/other-2`, ..., so fixtures carry no local path; then stop the server. The placeholder stays a path, not a short name: the tests configure `models.small.path: /models/small`, so a test that passes proves the client sends, and readiness matches, the configured path string.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock scripts/record_fixtures.py tests/fixtures/mlx_vlm
git commit -m "engine: pin mlx-vlm <VER> and record its wire fixtures"
```

---

## Phase 2: The engine kind

### Task 2: `EngineSpec` fields for the mlx_vlm kind

**Files:**
- Modify: `src/airlie/config/schema.py` (`ModelSpec` line 51, `EngineSpec` line 83, `AirlieConfig.validate_references` line 976: there is no `_problems`), `src/airlie/config/load.py` (`load_config` line 223), `src/airlie/engine/process.py` (`hf_snapshot_dir` line 117 moves out)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `EngineSpec.kind` accepts `"mlx_vlm"`; new fields `model: str | None`, `autostart: bool = True`, `decision: bool = False` (mlx_vlm: the model is a typed-decision model, started with `--decision-model` and asked through `/v1/decisions`), `kv_bits: int | None`, `prefix_cache: bool = False`, `cache_memory_gb: float = 1.0`, `cache_checkpoints: int = 4`, `cache_checkpoint_interval: int = 2048`, `max_image_px: int | None`, `env: dict[str, str]`, `entry: list[str] | None`. `ModelSpec.tool_call_parser` keeps its name and takes mlx-vlm parser names. `hf_snapshot_dir(m, hf_cache=None) -> Path` and `model_dir(m) -> Path` (`m.path` when set, else `hf_snapshot_dir(m)`) live in `airlie.config.load`; `airlie.engine.process` re-imports `hf_snapshot_dir` so its callers keep working. After validation `load_config` sets, for every mlx_vlm engine, `served_names[name] = str(model_dir(cfg.models[name]))`, so `e.served_name(name)` is the exact `--model` string everywhere a client is built (`cli._http_engine`, `serve/app.py` `upstream`, `orchestrator.py`, `topology/fork.py`, `story/amigos.py`, `router.jev_client`).

- [ ] **Step 1: Write the failing tests**

The module has no `_cfg`; it has `MINIMAL` (one `vllm_mlx` engine `main` serving `team`) and `write(tmp_path, data)`. Add one helper beside them:

```python
def mlx(tmp_path: Path, engines: dict, *, roles: dict | None = None, models: dict | None = None):
    data = {
        **MINIMAL,
        "engines": engines,
        "models": models
        or {
            "team": {"repo": "org/team", "revision": "r0", "path": "/models/team"},
            "base": {"repo": "org/base", "revision": "r1"},
        },
        "roles": {**MINIMAL["roles"], **(roles or {})},
    }
    return load_config(write(tmp_path, data))


def test_mlx_vlm_engine_spells_one_model_as_model_or_models(tmp_path: Path) -> None:
    cfg = mlx(tmp_path, {"main": {"kind": "mlx_vlm", "port": 8080, "model": "team"}})
    assert cfg.engines["main"].models == ["team"]
    assert cfg.engines["main"].base_url == "http://127.0.0.1:8080/v1"
    assert cfg.engine_for_model("team") == "main"


def test_an_mlx_vlm_engine_is_addressed_by_its_exact_model_path(tmp_path: Path) -> None:
    cfg = mlx(
        tmp_path,
        {"main": {"kind": "mlx_vlm", "port": 8080, "model": "team"},
         "other": {"kind": "mlx_vlm", "port": 8083, "model": "base", "autostart": False}},
    )
    assert cfg.engines["main"].served_name("team") == "/models/team"  # models.team.path
    assert cfg.engines["other"].served_name("base").endswith("/models--org--base/snapshots/r1")  # HF cache


def test_an_mlx_vlm_engine_takes_no_served_alias(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="served_names"):
        mlx(tmp_path, {"main": {"kind": "mlx_vlm", "port": 8080, "model": "team", "served_names": {"team": "t"}}})


def test_mlx_vlm_engine_refuses_two_models(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="engines.main: an mlx_vlm engine serves exactly one model"):
        mlx(tmp_path, {"main": {"kind": "mlx_vlm", "port": 8080, "models": ["team", "base"]}})


def test_mlx_vlm_engine_refuses_a_buffer_cache_limit(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="engines.main.buffer_cache_mb: mlx-vlm reads no buffer-cache limit"):
        mlx(tmp_path, {"main": {"kind": "mlx_vlm", "port": 8080, "model": "team", "buffer_cache_mb": 8192}})


def test_mlx_vlm_engine_defaults(tmp_path: Path) -> None:
    e = mlx(tmp_path, {"main": {"kind": "mlx_vlm", "port": 8080, "model": "team"}}).engines["main"]
    assert (e.autostart, e.decision, e.kv_bits, e.prefix_cache) == (True, False, None, False)
    assert (e.cache_memory_gb, e.cache_checkpoints, e.cache_checkpoint_interval) == (1.0, 4, 2048)
    assert e.env == {} and e.entry is None and e.max_image_px is None


def test_a_role_on_an_mlx_vlm_engine_may_only_pass_known_template_arguments(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="roles.coder.chat_template_kwargs: mlx_vlm engines accept only"):
        mlx(
            tmp_path,
            {"main": {"kind": "mlx_vlm", "port": 8080, "model": "team"}},
            roles={"coder": {"prompt": "prompts/roles/coder.md", "model": "team", "chat_template_kwargs": {"foo": 1}}},
        )
```

Use absolute placeholder paths under `/models/`, never `tmp_path`: on macOS `tmp_path` resolves through `/private`, and `load_config` resolves `models.*.path`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -q -k mlx_vlm`
Expected: FAIL (`mlx_vlm` is not a permitted `kind`).

- [ ] **Step 3: Implement the fields**

In `EngineSpec`: `kind: Literal["vllm_mlx", "openai_http", "llama_server", "mlx_vlm"]`; `models: list[str] = Field(default_factory=list)` (drop `min_length=1`); add `model: str | None = None` and the fields in Interfaces with the defaults given. A `model_validator(mode="before")` copies `model` into `models` as `[model]` when `models` is absent or empty. In `_derive_or_require_base_url` (line 127) include `"mlx_vlm"` in the kinds that derive `base_url`, raise `ValueError("an engine needs model or models")` when `models` is empty, and for `mlx_vlm` raise `ValueError("served_names: an mlx_vlm engine is addressed by its exact --model path; set models.<name>.path instead")` when `served_names` is non-empty. In `AirlieConfig.validate_references` (line 976): for `kind == "mlx_vlm"` and `len(e.models) != 1` append `f"engines.{ename}: an mlx_vlm engine serves exactly one model"`; for `kind == "mlx_vlm"` with `buffer_cache_mb` set append `f"engines.{ename}.buffer_cache_mb: mlx-vlm reads no buffer-cache limit (MLX_BUFFER_CACHE_LIMIT); remove it"`; add `"mlx_vlm"` to the port-collision check's managed kinds (line 994); for every role whose model is served by an `mlx_vlm` engine, keys of the role's `chat_template_kwargs` outside `{"enable_thinking", "reasoning_strength"}` append `f"roles.{rname}.chat_template_kwargs: mlx_vlm engines accept only enable_thinking and reasoning_strength (got {sorted(keys)})"`. Move `hf_snapshot_dir` from `engine/process.py` into `config/load.py` (process.py imports it back), add `model_dir`, and at the end of `load_config`, after `validate_references` passes and after `models.*.path` is resolved, fill `served_names` for each mlx_vlm engine as in Interfaces. `ModelSpec.tool_call_parser`'s description becomes "the engine's parser name (mlx-vlm: qwen3_coder, atem, ...); unset = inferred from the chat template".

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py -q`
Expected: PASS, the existing tests included (vllm_mlx engines still require their models list).

- [ ] **Step 5: Commit**

```bash
git add src/airlie/config/schema.py src/airlie/config/load.py src/airlie/engine/process.py tests/unit/test_config.py
git commit -m "config: the mlx_vlm engine kind, one model per engine addressed by its path, cache and autostart fields"
```

### Task 3: The mlx-vlm command line and environment

**Files:**
- Create: `src/airlie/engine/mlx_vlm.py`
- Test: `tests/unit/test_mlx_vlm_command.py`

**Interfaces:**
- Consumes: `EngineSpec`, `ModelSpec`, `EngineSpec.served_name` filled with the model path (Task 2); `SnapshotResolver = Callable[[ModelSpec], Path]` from `airlie.engine.registry` (line 17).
- Produces: `MLX_VLM_ENTRY: list[str] = ["python", "-m", "mlx_vlm.server"]`; `build_mlx_vlm_command(cfg: AirlieConfig, engine_name: str, snapshot_dir: SnapshotResolver) -> tuple[list[str], dict[str, str]]` returning argv and the environment to add; `served_model(cfg, engine_name) -> str` returning `e.served_name(e.models[0])`, the exact string passed to `--model` (or `--decision-model`), which Tasks 4, 6, 7 and 9 send and match. `snapshot_dir` resolves only the MTP head's directory; the served model's own path comes from `served_model`, so the command and every client agree by construction.

- [ ] **Step 1: Write the failing tests**

```python
def test_main_engine_command_pins_the_spec_flags(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, main={"kind": "mlx_vlm", "port": 8080, "model": "base", "max_num_seqs": 16,
                               "max_kv_size": 131072, "kv_bits": 8, "prefix_cache": True, "cache_memory_gb": 8,
                               "cache_checkpoints": 8, "max_image_px": 1536})
    argv, env = build_mlx_vlm_command(cfg, "main", snap)
    assert argv == [
        "python", "-m", "mlx_vlm.server",
        "--model", "/models/base",
        "--draft-model", "/hf/org/base-mtp/h1", "--draft-kind", "mtp",
        "--host", "127.0.0.1", "--port", "8080",
        "--max-num-seqs", "16", "--max-kv-size", "131072", "--kv-bits", "8",
        "--prefill-step-size", "2048", "--max-pixels", "2359296",
    ]
    assert served_model(cfg, "main") == "/models/base"  # what the client sends and readiness matches
    assert env == {
        "APC_ENABLED": "1", "APC_DISK_ENABLED": "0", "APC_MEMORY_MAX_GB": "8",
        "APC_CHECKPOINT_ENTRIES": "8", "APC_CHECKPOINT_INTERVAL_TOKENS": "2048",
    }  # no MLX_BUFFER_CACHE_LIMIT: mlx-vlm reads none


def test_a_model_without_a_head_gets_no_draft_flags_and_no_cache_env_when_off(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, main={"kind": "mlx_vlm", "port": 8083, "model": "small"})
    argv, env = build_mlx_vlm_command(cfg, "main", snap)
    assert argv[3:5] == ["--model", "/models/small"]
    assert "--draft-model" not in argv and "--kv-bits" not in argv and "--max-pixels" not in argv
    assert env == {}


def test_a_decision_engine_starts_its_model_with_decision_model(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, main={"kind": "mlx_vlm", "port": 8084, "model": "small", "decision": True,
                               "max_num_seqs": 4})
    argv, _ = build_mlx_vlm_command(cfg, "main", snap)
    assert argv[3:5] == ["--decision-model", "/models/small"] and "--model" not in argv
    assert "--draft-model" not in argv  # a decision engine never drafts
    assert served_model(cfg, "main") == "/models/small"


def test_api_key_extra_args_env_and_entry_override(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, main={"kind": "mlx_vlm", "port": 8080, "model": "base", "api_key": "k",
                               "extra_args": ["--log-level", "DEBUG"], "env": {"FOO": "1"},
                               "entry": ["uvx", "--from", "mlx-vlm==0.7.12", "mlx_vlm.server"]})
    argv, env = build_mlx_vlm_command(cfg, "main", snap)
    assert argv[:4] == ["uvx", "--from", "mlx-vlm==0.7.12", "mlx_vlm.server"]
    assert argv[-4:] == ["--api-key", "k", "--log-level", "DEBUG"]  # mlx-vlm's log levels are uppercase
    assert env["FOO"] == "1"
```

`_cfg(tmp_path, main=...)` is a new helper in the test module (load it through `load_config` as `tests/unit/test_process.py`'s `cfg` does) whose models are `base` (`path: /models/base`, `mtp: {repo: org/base-mtp, revision: h1}`, `estimated_memory_gb: 16`) and `small` (`path: /models/small`), with every role on `base` or `small` as the engine serves; `snap(m) = Path(f"/hf/{m.repo}/{m.revision}")` as in `tests/unit/test_registry.py`. `--max-pixels` is `max_image_px` squared.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_mlx_vlm_command.py -q`
Expected: FAIL with `ModuleNotFoundError: airlie.engine.mlx_vlm`.

- [ ] **Step 3: Implement `build_mlx_vlm_command`**

Flag order exactly as the first test lists: the entry, then `--model served_model(...)` (or `--decision-model served_model(...)` when `e.decision`; a decision engine gets no draft flags), `--draft-model` the snapshot of `ModelSpec(repo=m.mtp.repo, revision=m.mtp.revision)` when `m.mtp` is set and the engine is not a decision engine (the head is used as published; nothing is rebuilt), `--draft-kind mtp`, then `--host e.host` always (mlx-vlm binds 0.0.0.0 when it is left out), `--port`, `--max-num-seqs`, `--max-kv-size`, `--kv-bits` when set, `--prefill-step-size`, `--max-pixels` when set, `--api-key` when set, `extra_args` last. Environment keys only when their source is set: the five `APC_*` keys when `prefix_cache` is true (`APC_ENABLED=1` and `APC_DISK_ENABLED=0` always together, since upstream's disk tier defaults on; `APC_MEMORY_MAX_GB` formatted with `:g`), then `e.env` merged last. No `MLX_BUFFER_CACHE_LIMIT` (Task 2 refuses `buffer_cache_mb` on mlx_vlm engines). `entry` replaces `MLX_VLM_ENTRY` when set.

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
- Modify: `src/airlie/engine/process.py` (`ProcessRunner.spawn` line 35, `Prober` line 41, `RealRunner.spawn` line 57, `HTTPProber` line 93, `EngineProcess.start` line 183, `_wait_ready` line 203, `_probe` line 223)
- Modify: `src/airlie/cli.py` (`_managed` line 207, `engine_up` line 228, `engine_status`'s "no managed (vllm_mlx) engines" line 282)
- Test: `tests/unit/test_process.py`, `tests/unit/test_cli_engine.py`

**Interfaces:**
- Consumes: `build_mlx_vlm_command`, `MLX_VLM_ENTRY`, `served_model` (Task 3).
- Produces: `ProcessRunner.spawn(self, argv: list[str], log_path: Path, cwd: Path, env: dict[str, str] | None = None) -> int`; `Prober.loaded_model_ids(self) -> list[str]` (the `id`s whose `/v1/models` entry has `loaded: true`; for mlx-vlm the `id` is the literal `--model` / `--decision-model` string); `Prober.preload_failures(self) -> list[str]` (`/v1/metrics` `server.preload_failures`, each item as text; `[]` when the endpoint or key is absent); `HTTPProber` sends `Authorization: Bearer <api_key>` on `/health`, `/v1/models` and `/v1/metrics` (it already builds the header for the first two); `EngineProcess` serves `kind == "mlx_vlm"` and reports its model under the catalog name (`{"base": "ready"}`) while matching on `served_model(...)`; `airlie engine up [NAME]` starts one engine by name, else every managed engine with `autostart` true.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_process.py`: the module's `FakeRunner.spawned` holds `(argv, log_path)` tuples and its `FakeProber(health=[...], models=[[...]])` pops scripted answers. Extend `FakeRunner.spawn` to take `env=None` and record `(argv, log_path, env)`; update the existing tests that unpack two items. Extend `FakeProber` with `loaded: list[list[str]] | None = None` (popped like `models`; when the list runs out it repeats its last answer) and `failures: list[str] | None = None` (returned by `preload_failures`). Add `mlx_cfg(tmp_path)`: engines `main: {kind: mlx_vlm, port: 8080, model: base, prefix_cache: true}`, models `base` (`path: /models/base`, `estimated_memory_gb: 16`), every role on `base`.

```python
async def test_mlx_vlm_engine_starts_with_its_command_and_env_and_waits_for_loaded(tmp_path: Path) -> None:
    prober = FakeProber(health=[True] * 3, models=[], loaded=[["/models/other"], ["/models/other"], ["/models/base"]])
    runner = FakeRunner()
    p = EngineProcess(mlx_cfg(tmp_path), "main", state_dir=tmp_path / "state", runner=runner, prober=prober,
                      snapshot_dir=lambda m: Path(f"/hf/{m.repo}/{m.revision}"), poll_s=0)
    h = await p.start(timeout_s=5)  # listed-but-not-loaded twice, then loaded under the exact --model path
    argv, _, env = runner.spawned[0]
    assert argv[:5] == ["python", "-m", "mlx_vlm.server", "--model", "/models/base"]
    assert env["APC_ENABLED"] == "1" and env["APC_DISK_ENABLED"] == "0"
    assert h.ok and h.models == {"base": "ready"}
    assert not (tmp_path / "state" / "engines" / "main" / "models.yaml").exists()  # no registry file


async def test_mlx_vlm_engine_is_not_ready_while_another_name_is_loaded(tmp_path: Path) -> None:
    ...  # loaded stays [["base"]] (the catalog name, not the path) and then [["/models/other"]]:
         # EngineError after the timeout matches "base: loading"


async def test_mlx_vlm_engine_start_fails_fast_on_a_preload_failure(tmp_path: Path) -> None:
    ...  # health ok, loaded [], failures ["/models/base: out of memory"]: EngineError on the first poll,
         # naming the failure and the log path, long before the timeout


async def test_prober_sends_the_api_key_to_health_models_and_metrics(tmp_path: Path) -> None:
    ...  # HTTPProber("http://e/v1", api_key="k") with an httpx.MockTransport (inject it: HTTPProber gains
         # `transport: httpx.AsyncBaseTransport | None = None`): /health, /v1/models and /v1/metrics all
         # carry "Bearer k"; loaded_model_ids reads data[*].loaded from tests/fixtures/mlx_vlm/models.json
         # and returns ["/models/small"]; preload_failures reads metrics.json's server.preload_failures
```

In `tests/unit/test_cli_engine.py`:

```python
def test_engine_up_skips_engines_with_autostart_false_unless_named(tmp_path: Path, monkeypatch) -> None:
    ...  # write_cfg's engines (main vllm_mlx, ext openai_http) plus bench (kind mlx_vlm, port 8091,
         # autostart false, a catalog model no role uses with path /models/bench), with `_engine_process`
         # patched to StubProcess as the module's other tests do: `engine up` records ["start:main"];
         # `engine up bench` records ["start:bench"]; `engine up nope` exits 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_process.py tests/unit/test_cli_engine.py -q -k "mlx_vlm or autostart or api_key or preload"`
Expected: FAIL.

- [ ] **Step 3: Implement**

`RealRunner.spawn` passes `env={**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})}` to `subprocess.Popen` and prefixes `uv run --quiet` when `argv` starts with `SERVE_ENTRY` or `MLX_VLM_ENTRY`. `HTTPProber` gains an optional `transport`, `loaded_model_ids` (`GET {root}/v1/models`, the `id`s with `loaded` true) and `preload_failures` (`GET {root}/v1/metrics`, `server.preload_failures`: a list is returned item by item as text, a number above 0 as `[f"{n} preload failure(s)"]`, anything else or a non-200 as `[]`; the shape is pinned by `metrics.json` from Task 1). `EngineProcess.start`: for `mlx_vlm`, `argv, env = build_mlx_vlm_command(...)`, no registry file and no `built_models()` (that is the vllm-mlx MTP directory), spawn with `env`. `_probe` for `mlx_vlm`: health first; the engine's one catalog model is `"ready"` when `served_model(cfg, name)` is in `loaded_model_ids()` (an exact string match: a catalog name or another path in the list does not count), `"loading"` when health is ok but it is not loaded, `"missing"` when health fails; `/health`'s own `loaded_model` is not read (it is null for a decision-only engine and stays "healthy" after a failed preload). `_wait_ready` treats the mlx_vlm engine's model as preloaded regardless of `ModelSpec.preload`, and on each poll for an mlx_vlm engine raises `EngineError(f"engine '{name}' failed to load its model: {'; '.join(failures)}; see {log_path}")` when `preload_failures()` is non-empty. `cli._managed` includes `"mlx_vlm"`; `engine_up` gains an optional positional `name: str | None` and starts `[name]` when given (unknown or unmanaged name: error, exit 1), else `[n for n in _managed(cfg) if cfg.engines[n].autostart]`; `engine_down`/`restart`/`status`/`logs` keep iterating every managed engine; the status line's "no managed (vllm_mlx) engines" says "no managed engines". `engine_restart` (line 267) calls `engine_up` positionally today; pass its arguments by keyword once `name` is a parameter.

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
- Modify: `src/airlie/doctor.py` (`SystemProbe` line 30 and `RealProbe`, `cache_memory_gb` line 208, `project_memory_gb` line 218, `projection_detail` line 261, the per-engine `engine:{ename}:binary` row in the engines loop line ~425–441, hybrid-attention row ~line 522)
- Test: `tests/unit/test_doctor.py`

**Interfaces:**
- Produces: `cache_memory_gb(e) -> float` returns `e.cache_memory_gb` when `e.kind == "mlx_vlm"` and `e.prefix_cache`, else the legacy `--cache-memory-mb` parse; `project_memory_gb` counts an `mlx_vlm` engine's model weights when `e.autostart` is true (regardless of `ModelSpec.preload`) and sums `cache_memory_gb` over every managed engine, not only the one named; `SystemProbe.package_version(self, dist: str) -> str | None` (`RealProbe`: `importlib.metadata.version`, `None` on `PackageNotFoundError`; the test fakes inherit the protocol's stub).

- [ ] **Step 1: Write the failing tests**

```python
def test_memory_projection_counts_every_autostart_mlx_vlm_engine_and_its_cache(tmp_path: Path) -> None:
    # main (base 16 GB, cache 8), reviewer (rev 19 GB, cache 4), bench (autostart False, 20 GB, cache 1)
    gb, unknown = project_memory_gb(cfg, "main")
    assert gb == pytest.approx(16 + 19 + 8 + 4 + live_kv_gb(cfg, cfg.engines["main"]) + cfg.memory.os_reserve_gb)
    assert unknown == []


def test_doctor_reports_mlx_vlm_installed_instead_of_vllm_mlx_for_mlx_vlm_engines(tmp_path: Path) -> None:
    ...  # an mlx_vlm engine's row is "engine:<name>:binary" (as vllm_mlx's is today), ok with
         # "mlx-vlm <version> (python -m mlx_vlm.server)" from probe.package_version("mlx-vlm"), and fail
         # naming `uv sync` when it is None; no "task engine:install" (no such task) on this row


def test_hybrid_attention_model_on_an_mlx_vlm_engine_is_ok_without_a_patch(tmp_path: Path) -> None:
    ...  # the row's detail says mlx-vlm's prefix cache checkpoints recurrent layers; no patch named
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_doctor.py -q -k "mlx_vlm"`
Expected: FAIL.

- [ ] **Step 3: Implement**

As in Interfaces. The hybrid-attention row (`is_hybrid_attention`) keeps its `vllm_mlx` wording for models on `vllm_mlx` engines and says, for a model on an `mlx_vlm` engine, "hybrid (linear/SSM) attention: mlx-vlm's prefix cache stores it as checkpoints (`cache_checkpoints`)". In the engines loop add an `mlx_vlm` branch before the `if e.kind != "vllm_mlx": continue`: it adds the engine's models to `managed_models` and the `engine:{ename}:binary` row from `probe.package_version("mlx-vlm")`, or, when `entry` is set, from `probe.tool_version(entry[0])` naming the entry command. The `memory_budget` row stays vllm_mlx only.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_doctor.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/doctor.py tests/unit/test_doctor.py
git commit -m "doctor: project mlx_vlm engines' weights and prefix cache; name the mlx-vlm tool"
```

Phase 2 ends here: open the pull request "engine: the mlx_vlm engine kind (phase 2)". Proves: with this in `airlie.local.yaml`, `uv run airlie engine up main94` starts it and `airlie engine status` shows `small94: ready`:

```yaml
models:
  small94: { repo: mlx-community/Qwen3-4B-4bit, revision: <models.small.revision> }   # a catalog name no role uses
engines:
  main94: { kind: mlx_vlm, port: 8094, model: small94, autostart: false, max_num_seqs: 4, prefix_cache: true }
```

A second catalog name is needed because every role's model is already served by `main`, and a role model served by two engines is a config error; `autostart: false` keeps `task up` from starting it beside vllm-mlx.

---

## Phase 3: The client dialect

### Task 6: One place for the request fields: `dialect.py`; each role to its own engine

**Files:**
- Create: `src/airlie/engine/dialect.py`, `src/airlie/engine/routed.py`
- Modify: `src/airlie/engine/http_engine.py` (`__init__` line 40, `_body` line 106, `yes_no` line 280), `src/airlie/teams/autogen_team.py` (`build_model_client` line 129, its extra-body block lines 145–156; `AutoGenTeam.__init__` line 216), `src/airlie/orchestrator.py` (line 182) and `src/airlie/topology/fork.py` (line 127) (`AutoGenTeam(...)` gains `dialect=`), `src/airlie/story/amigos.py` (`build_model_client` call line 1229), `src/airlie/serve/app.py` (the role defaults block lines 689–694), `src/airlie/cli.py` (`_http_engine` line 307: pass `dialect=dialect_for(e.kind)`; route roles on other engines), `src/airlie/story/factory.py` (`_engine` line 246 keeps calling `_http_engine`; no change unless the return annotation needs it)
- Test: `tests/unit/test_dialect.py`, `tests/unit/test_http_engine_mlx_vlm.py`, `tests/unit/test_routed_engine.py`, `tests/unit/test_serve.py`, `tests/unit/test_autogen_team.py`

**Interfaces:**
- Produces: `Dialect = Literal["vllm_mlx", "mlx_vlm"]`; `dialect_for(kind: str) -> Dialect` (`"mlx_vlm"` for `mlx_vlm`, else `"vllm_mlx"`; `openai_http` is `"vllm_mlx"` until phase 5); `thinking_fields(*, dialect: Dialect, thinking: bool, budget: int | None, chat_template_kwargs: Mapping[str, object], tool_parser: str | None = None, tools: bool = False) -> dict[str, Any]`.
- `HTTPEngine.__init__` gains `dialect: Dialect = "vllm_mlx"`; `_body` and `yes_no` merge `thinking_fields(...)` instead of building those keys inline; `stream_options: {"include_usage": True}` stays on every streamed body (mlx-vlm sends usage and timings only when asked).
- `build_model_client(..., dialect: Dialect = "vllm_mlx")` and `AutoGenTeam(base_url, served_model, api_key, *, dialect="vllm_mlx")`; the call sites pass `dialect_for(e.kind)` of the engine they already looked up.
- `RoutedEngine(primary: str, engines: Mapping[str, Engine], engine_for_role: Callable[[str], str])` in `engine/routed.py`, an `Engine`: `generate` and `yes_no` go to `engines[engine_for_role(role)]` (`req.role` for `generate`); `cancel(request_id)` goes to the engine that took that request (remembered in `generate`, forgotten when it returns), else to every engine; `start`/`stop` reach every engine; `health` and `capabilities` are the primary's; `stats` sums every engine's `EngineStats` fields.
- `cli._http_engine(cfg, engine_name, model_override=None, *, transport=None) -> Engine`: unchanged (one `HTTPEngine`) when `model_override` is set or every role's model is served by `engine_name`; otherwise a `RoutedEngine` whose primary is `engine_name` and which holds one `HTTPEngine` per engine that serves a role (each built with that engine's `base_url`, `api_key`, `dialect`, and `served(role) = e.served_name(cfg.roles[role].model)`). With today's config every role is on `main`, so nothing changes until Task 10.

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

In `tests/unit/test_http_engine_mlx_vlm.py`: copy the `Recorder` helper from `test_http_engine.py` (fixture directory `tests/fixtures/mlx_vlm`). Build the engine the way production does, through `cli._http_engine(cfg, "main", transport=rec)`, from a tmp config with `engines.main: {kind: mlx_vlm, port: 8094, model: small}`, `models.small: {repo: mlx-community/Qwen3-4B-4bit, revision: r, path: /models/small, tool_call_parser: qwen3_coder}` and every role on `small`, so the test passes only if `load_config` filled the path in and `_http_engine` passed the dialect:

```python
async def test_request_body_speaks_mlx_vlm(tmp_path: Path) -> None:
    body = json.loads(rec.requests[0].content)
    assert body["model"] == "/models/small"  # the exact --model path, the string the fixtures carry
    assert "chat_template_kwargs" not in body and "thinking_token_budget" not in body
    assert body["enable_thinking"] is False and body["stream_options"] == {"include_usage": True}
    assert body["tool_parser"] == "qwen3_coder"  # the tool_call request


async def test_yes_no_speaks_mlx_vlm(tmp_path: Path) -> None:
    ...  # the yes_no body has "model" "/models/small", "enable_thinking" False, no "chat_template_kwargs"
```

In `tests/unit/test_routed_engine.py`:

```python
async def test_each_role_goes_to_the_engine_that_serves_its_model(tmp_path: Path) -> None:
    ...  # config: main (mlx_vlm, generalist, path /models/generalist) and reviewer (mlx_vlm, reviewer, path
         # /models/reviewer); roles coder on generalist, reviewer on reviewer. One MockTransport records
         # request URLs and bodies. _http_engine(cfg, "main", transport=t) is a RoutedEngine; a generate for
         # role "reviewer" goes to http://127.0.0.1:8082/v1/chat/completions with model "/models/reviewer",
         # one for "coder" to :8080 with "/models/generalist"; health() is main's


def test_one_engine_serving_every_role_stays_a_plain_http_engine(tmp_path: Path) -> None:
    ...  # today's shape (one vllm_mlx engine serving every role's model): isinstance(..., HTTPEngine)


async def test_cancel_reaches_the_engine_that_took_the_request() -> None:
    ...  # two fake engines; generate on "a" blocks; cancel(request_id) is called on "a" only
```

In `test_serve.py` and `test_autogen_team.py`: one test each asserting that, for a role served by an `mlx_vlm` engine, the forwarded body (serve) or the client's `extra_body` (`build_model_client(..., dialect="mlx_vlm")`) carries `enable_thinking` and `thinking_budget`, and no `chat_template_kwargs` and no `thinking_token_budget`; the serve test also asserts `"model"` is the engine's path.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_dialect.py tests/unit/test_http_engine_mlx_vlm.py tests/unit/test_routed_engine.py -q`
Expected: FAIL with `ModuleNotFoundError: airlie.engine.dialect`.

- [ ] **Step 3: Implement**

`thinking_fields` for `vllm_mlx` reproduces exactly today's keys from `HTTPEngine._body` (top-level `enable_thinking`, `chat_template_kwargs` with `enable_thinking` first, `thinking_token_budget` only when `thinking and budget`). For `mlx_vlm`: `enable_thinking`; `thinking_budget` when `thinking and budget`; `reasoning_effort` from `chat_template_kwargs["reasoning_strength"]` when present (mlx-vlm reads no other template argument; Task 2 refuses the rest at load); `tool_parser` when `tools and tool_parser`. Callers: `HTTPEngine._body` (with `tool_parser` from `self._model(req.role).tool_call_parser` when `model_spec_for` is set, `tools=bool(req.tools)`) and `HTTPEngine.yes_no` (thinking off, no budget, no tools); `build_model_client` (`tools=True`: an AutoGen agent always carries its tools; `tool_parser` from `model.tool_call_parser` when `model` is given); `serve/app.py`, which keeps its `setdefault` semantics: for an mlx_vlm engine it sets `enable_thinking` and `thinking_budget` by `setdefault` and never adds `chat_template_kwargs` (it looks the dialect up with `dialect_for(cfg.engines[cfg.engine_for_model(model)].kind)`; `upstream()` at line 563 already resolves that engine). `RoutedEngine` as in Interfaces; `_http_engine` returns it as in Interfaces, and its return annotation becomes `Engine` (from `airlie.engine.protocol`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_dialect.py tests/unit/test_http_engine_mlx_vlm.py tests/unit/test_routed_engine.py tests/unit/test_http_engine.py tests/unit/test_serve.py tests/unit/test_autogen_team.py tests/unit/test_prompt_audit.py tests/unit/test_cli_supervisor.py -q`, then `uv run mypy`
Expected: PASS, every existing vllm-mlx body assertion included (the last two files patch `cli._http_engine`).

- [ ] **Step 5: Commit**

```bash
git add src/airlie/engine/dialect.py src/airlie/engine/routed.py src/airlie/engine/http_engine.py src/airlie/teams/autogen_team.py src/airlie/orchestrator.py src/airlie/topology/fork.py src/airlie/story/amigos.py src/airlie/serve/app.py src/airlie/cli.py src/airlie/story/factory.py tests/unit/test_dialect.py tests/unit/test_http_engine_mlx_vlm.py tests/unit/test_routed_engine.py tests/unit/test_serve.py tests/unit/test_autogen_team.py
git commit -m "engine: one dialect module for the thinking, budget and parser fields; each role to its own engine"
```

### Task 7: Read mlx-vlm replies

**Files:**
- Modify: `src/airlie/engine/http_engine.py` (`generate` stream loop lines 147–268, `health` line 88), `src/airlie/teams/autogen_team.py` (`build_model_client`: no `model` handed to the client for `dialect="mlx_vlm"`)
- Test: `tests/unit/test_http_engine_mlx_vlm.py`, `tests/unit/test_autogen_team.py`

**Interfaces:**
- Produces: `GenResult.reasoning` filled from `delta.reasoning_content`, else `delta.reasoning` (mlx-vlm sends both with the same text; reading both would double it); `cached_tokens` from `usage.prompt_tokens_details.cached_tokens` (the path the client already reads; mlx-vlm has no `usage.cached_tokens`); `accepted_draft_tokens`/`draft_tokens` from the frame's `timings.draft_n_accepted`/`timings.draft_n` when not null, or the vllm-mlx `generation_metadata`; usage and timings come in the final chunk whose `choices` is `[]`, which the loop already reads because it looks at `usage` before `choices`; every field read tolerates an explicit null (`tc.get("index") or 0`, `usage.get("prompt_tokens") or prompt_tokens`, ...); `ttft_ms` is the client's first-delta time when no `airlie_ttft_ms` is sent (already so; mlx-vlm sends no TTFT, only `timings.prompt_ms`); an HTTP error status raises `EngineError` (already) and `finish_reason == "error"` keeps raising; `parse_output` runs only for the `vllm_mlx` dialect, since mlx-vlm parses tool calls and reasoning itself and its parser names (`atem`, ...) are not in vllm-mlx's registries; `health()` keys are the server's model `id`s.

- [ ] **Step 1: Write the failing tests**

```python
async def test_reasoning_stream_splits_and_is_not_doubled(...) -> None:
    res = await eng.generate(req_thinking(), on_token=seen.append)  # reasoning.sse carries both fields
    assert res.reasoning and res.text and any(e.reasoning for e in seen)
    assert res.reasoning == "".join(e.text for e in seen if e.reasoning)  # once per delta, not twice


async def test_usage_counters_are_read_from_the_final_chunk(...) -> None:
    res = await eng.generate(resume_request())  # resume.sse
    assert res.cached_tokens == <usage.prompt_tokens_details.cached_tokens in resume.sse>
    assert res.prompt_tokens > 0 and res.completion_tokens > 0
    assert res.draft_tokens is None and res.accepted_draft_tokens is None  # timings.draft_n is null: no drafter
    assert res.ttft_ms > 0  # measured by the client


async def test_draft_counters_are_read_from_timings() -> None:
    ...  # a hand-written two-frame SSE (a content delta, then {"choices": [], "usage": {...},
         # "timings": {"draft_n": 40, "draft_n_accepted": 31}}): draft_tokens 40, accepted_draft_tokens 31


async def test_tool_call_stream_assembles_by_index(...) -> None:
    res = await eng.generate(tool_request())
    assert [c.name for c in res.tool_calls] == ["read_file"]  # the recorder's one tool
    assert json.loads(res.tool_calls[0].arguments)


async def test_raw_output_is_not_reparsed_on_mlx_vlm(...) -> None:
    ...  # model_spec tool_call_parser "atem": plain.sse's text comes back unchanged and nothing imports
         # vllm_mlx (patch airlie.engine.http_engine.parse_output to raise)


async def test_health_reads_loaded_models(...) -> None:
    h = await eng.health()  # fixtures health.json + models.json
    assert h.ok and h.models["/models/small"] == "ready"
    assert all(s == "lazy" for k, s in h.models.items() if k != "/models/small")  # listed, not loaded
```

In `test_autogen_team.py`: `build_model_client(..., model=spec, dialect="mlx_vlm")` builds a client with no model spec to parse with (the `RepairingOpenAIClient`'s `model_spec` is None); `dialect="vllm_mlx"` keeps it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_http_engine_mlx_vlm.py tests/unit/test_autogen_team.py -q`
Expected: FAIL on the reasoning-alias, timings, parse and health assertions.

- [ ] **Step 3: Implement**

In the stream loop read `delta.get("reasoning_content") or delta.get("reasoning")`; after the `generation_metadata` block read `frame.get("timings")` when it is a dict and take `draft_n_accepted` / `draft_n` when they are numbers; make the `usage` and `tool_calls` reads null-tolerant as in Interfaces. Keep the `prompt_tokens_details.cached_tokens` read as it is. Call `parse_output` only when `self._dialect == "vllm_mlx"`. `health()` marks a model `"ready"` when its `/v1/models` entry has `loaded` true or no `loaded` key (vllm-mlx), else `"lazy"`; `detail` falls back to `loaded_model=` from `/health` when there is no `engine_type`. `build_model_client` passes `model` to the client only for `dialect == "vllm_mlx"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_http_engine_mlx_vlm.py tests/unit/test_http_engine.py tests/unit/test_http_engine_wire.py tests/unit/test_hal_wiring.py tests/unit/test_autogen_team.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/engine/http_engine.py src/airlie/teams/autogen_team.py tests/unit/test_http_engine_mlx_vlm.py tests/unit/test_autogen_team.py
git commit -m "engine: read mlx-vlm's reasoning, usage, timings and loaded-model fields; no client-side parse on it"
```

### Task 8: The busy probe reads `/v1/metrics`

**Files:**
- Modify: `src/airlie/serve/bridge/busy.py` (`_engine_roots` line 50, `_models` line 73, `busy_reason` line 95, the module docstring)
- Test: `tests/unit/test_bridge_busy.py`

**Interfaces:**
- Produces: `_engine_roots(cfg) -> list[tuple[str, str, str | None, str]]` adds the engine kind; for `kind == "mlx_vlm"` the probe is `GET {root}/v1/metrics` with the engine's key as `Authorization: Bearer` (mlx-vlm answers 401 without it) and reads `summary.in_flight` (> 0 means busy: `f"engine {engine} has {n} request(s) in flight"`) and `summary.last_request_at` (epoch seconds, may be null: null means no request yet; within `bridge.idle_s` and not the bridge's own means busy, reported as `f"model {m} used {s} s ago on engine {engine}"` with `m` the engine's catalog model so `reason_key` still strips the age). Nothing reads `latest.*` (its `reasoning_tokens` is wrong upstream). Other kinds keep `/v1/status`.

- [ ] **Step 1: Write the failing tests**

```python
def test_mlx_vlm_engine_in_flight_is_busy(tmp_path: Path) -> None:
    ...  # FakeStatus answers /v1/metrics {"summary": {"in_flight": 1, "last_request_at": NOW}} → busy


def test_mlx_vlm_engine_recent_request_is_busy_old_or_null_is_idle(tmp_path: Path) -> None:
    ...  # last_request_at NOW - 14 → busy; NOW - 121 → idle; null → idle


def test_mlx_vlm_metrics_probe_sends_the_engine_key(tmp_path: Path) -> None:
    ...  # engines.main.api_key "k": the /v1/metrics request carries "Bearer k"; a 401 reads as idle
```

The module's `_cfg` (line 20) loads the repo's `airlie.yaml`; for these tests build the config from a tmp `airlie.yaml` with one `mlx_vlm` engine (the repo file still says `vllm_mlx` until phase 4a). `FakeStatus` (line 26) answers `/v1/status`; teach it a `metrics=` answer for `/v1/metrics`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_bridge_busy.py -q -k mlx_vlm`
Expected: FAIL (probe hits `/v1/status`).

- [ ] **Step 3: Implement** as in Interfaces; a non-200 from `/v1/metrics` (404, 401) still reads as idle; a transport error as "not answering". Update the docstring's engine paragraph for mlx-vlm.

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
- Modify: `src/airlie/router/jev.py` (`JevClient.__init__` line 22, `ask` line 37), `src/airlie/router/__init__.py` (`jev_client` line 15), `src/airlie/tools/decide.py` (`attach_decide` line 85), `src/airlie/config/schema.py` (the `router.kind == "jev"` check in `validate_references` line 1007; `RouterSpec.kind`'s description line 509)
- Test: `tests/unit/test_jev_client.py`, `tests/unit/test_router.py`, `tests/unit/test_decide_tool.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces: `JevClient(base_url, *, model, transport, timeout_s, api_key, endpoint: str = "/systemone")`; the request body is unchanged in shape for both endpoints, `{"model", "state", "questions": {q: {"type": "choice", "instructions", "criteria"}}}`, with `model` the engine's `served_name`, which for an mlx_vlm engine is the exact `--decision-model` path (Task 2), never another name; error texts use the endpoint's name (`"systemone returned ..."` stays as the existing tests pin it; `"decisions returned ..."` on mlx-vlm). `ask` accepts both answer shapes: llama.cpp's `{"choice", "probabilities", "confidence"}` and mlx-vlm's `{"type": "choice", "value", "probabilities", "metadata": {"confidence"}}`: the chosen option is `choice`, else `value`; confidence is `confidence`, else `metadata.confidence`, else the top probability; input tokens are `usage.input_tokens`, else `usage.prompt_tokens`, else 0. `jev_client(cfg)` passes `endpoint="/decisions"` when the serving engine's kind is `mlx_vlm`. `attach_decide` adds the tool when the serving engine is `llama_server`, or `mlx_vlm` with `decision: true`. `validate_references` accepts `router.model` served by a `llama_server` engine or an `mlx_vlm` engine with `decision: true` (message: `router.model '<m>' must be served by a llama_server engine or an mlx_vlm engine with decision: true`).

- [ ] **Step 1: Write the failing tests**

```python
async def test_ask_reads_an_mlx_vlm_decision_answer() -> None:
    # POST /v1/decisions answers {"model": "/models/clef", "answers": {"role": {"type": "choice",
    #   "value": "coder", "probabilities": {"coder": 0.8, "chief": 0.2}, "metadata": {"confidence": 0.8}}},
    #   "usage": {"input_tokens": 42}}
    client = JevClient("http://d/v1", model="/models/clef", transport=t, endpoint="/decisions")
    ans = await client.ask("state", "role", "which seat", {"coder": "writes", "chief": "plans"})
    assert (ans.choice, ans.confidence, ans.input_tokens) == ("coder", 0.8, 42)
    assert recorded.url.path == "/v1/decisions" and json.loads(recorded.content)["model"] == "/models/clef"


async def test_an_mlx_vlm_answer_without_metadata_takes_the_top_probability() -> None:
    ...  # no "metadata": confidence 0.8 from probabilities


def test_jev_client_targets_decisions_on_an_mlx_vlm_decision_engine(tmp_path: Path) -> None:
    ...  # router.kind jev, router.model clef on {kind: mlx_vlm, port: 8084, model: clef, decision: true,
         # path /models/clef}: jev_client(cfg) posts to http://127.0.0.1:8084/v1/decisions with model
         # "/models/clef"


def test_decide_tool_attaches_for_an_mlx_vlm_decision_engine(tmp_path: Path) -> None:
    ...  # same config: attach_decide is True; with decision: false it is False


def test_router_model_may_be_served_by_an_mlx_vlm_decision_engine(tmp_path: Path) -> None:
    ...  # in test_config.py: loads with decision: true; ConfigError naming "decision: true" without it
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit -q -k "decision or jev or decide or router"`
Expected: FAIL.

- [ ] **Step 3: Implement** as in Interfaces; the existing systemone tests stay green. The `usage` key mlx-vlm uses for input tokens was not captured by the live test; the fallback order above covers both names, and Task 11 step 6 confirms it on a real decision engine.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit -q -k "decision or jev or decide or router"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/router src/airlie/tools/decide.py src/airlie/config/schema.py tests/unit
git commit -m "router: typed decisions through mlx-vlm's /v1/decisions as well as llama.cpp's /v1/systemone"
```

Phase 3 ends here: open the pull request "engine: the mlx-vlm client dialect (phase 3)". Proves, on Noah's Mac with the generalist served by an `mlx_vlm` engine on port 8094 from `airlie.local.yaml`: `uv run airlie run --topology single "write a function that reverses a string, with a test" --repo <scratch repo>` completes one coder layer with the roles unchanged while the committed config still points at vllm-mlx. The local file replaces `main` and serves the other role models on their own mlx_vlm engines, because a role model must be served by exactly one engine and the summarizer roles use `small`:

```yaml
engines:
  main: { kind: mlx_vlm, port: 8094, models: [generalist], prefix_cache: true, cache_memory_gb: 8, cache_checkpoints: 8 }
  small95: { kind: mlx_vlm, port: 8095, model: small, prefix_cache: true }
  reviewer96: { kind: mlx_vlm, port: 8096, model: reviewer, autostart: false }
```

(`models:` replaces the base list in the deep merge; the base's vllm-only keys under `main` are still accepted until phase 5 and the mlx_vlm kind does not read them.) Start with `uv run airlie engine up main && uv run airlie engine up small95` after `task down`. Record, while vllm-mlx is still the configured engine, the vllm-mlx half of every gate row in the spec's Feature comparison table (live tests, prefill and decode tokens per second on the same prompts, time to first token, one coder layer's wall clock, attachments, a review round, cache hits, concurrency); paste the numbers into the PR.

---

## Phase 4a: Switch the config

### Task 10: Four mlx_vlm engines, profiles without engine flags, no `--topology` on restart

**Files:**
- Modify: `airlie.yaml` (engines block, lines 15–35), `config/models.yaml` (parser names, `preload` comments), `config/profiles.yaml` (strip `engines:` overrides at lines 23, 68, 92 and every `models:` block), `src/airlie/doctor.py` (`SystemProbe.process_args` line 42 / `RealProbe.process_args` line 144, `engine_matches_profile` line 313 → `engine_matches_config`, `_PROFILE_FLAGS` line 280), `src/airlie/story/line.py` (`_gates`' "engine down" message line 211, `_profile_gate` line 218), `src/airlie/cli.py` (drop `topology: ProfileOpt` from `engine_up` line 228, `engine_down` line 256, `engine_restart` line 267, and `ProfileOpt` itself line 222 once unused; `engine_status` takes none today), `Taskfile.yml` (`up` line 49, `restart` line 59 descriptions), `README.md` (engine rows and sections: lines ~97, 102, 143, 205, 238–254, 281–285, 301)
- Test: `tests/unit/test_doctor.py`, `tests/unit/test_config.py` (new tests, and rewrite `test_the_qwen25_experiment_is_a_profile_beside_the_default` line 342 and `test_the_coder8_experiment_is_a_profile_beside_the_default` line 360, which pin engine overrides, `preload` and the old parser names), `tests/unit/test_cli_engine.py` (rewrite `test_engine_up_and_restart_take_a_topology_profile` line 118: the option is gone), `tests/unit/test_story_line.py` (the profile-gate tests at lines 277–320 keep patching `engine_matches_profile`)

**Interfaces:**
- Produces: `SystemProbe.all_process_args(self, marker: str) -> list[list[str]]` (every running process whose command line holds `marker`; no `" serve"` requirement: `python -m mlx_vlm.server` has none, so today's `process_args` never finds it; the fakes inherit the protocol stub and the caller treats `None` as `[]`). `engine_matches_config(cfg: AirlieConfig, probe: SystemProbe, engine_name: str) -> CheckResult` for an mlx_vlm engine: the running `mlx_vlm.server` whose `--port` is the engine's (`probe.all_process_args("mlx_vlm.server")`) against `build_mlx_vlm_command(cfg, engine_name, hf_snapshot_dir)` on `--max-num-seqs`, `--max-kv-size`, `--kv-bits`, `--draft-model`; `warn` when none runs on that port; `fail` names the flags and the command `task restart`. `engine_matches_profile(cfg, probe, profile)` keeps its signature (the line and its tests call it): when the story's build engine is mlx_vlm it returns `engine_matches_config(cfg, probe, cfg.engine_for_role(cfg.story.build_role))` with the check named `engine:config[<engine>]`, else today's vllm-mlx comparison.

- [ ] **Step 1: Write the failing tests**

```python
def test_committed_config_declares_four_autostart_engines_and_the_bench_and_gate_engines() -> None:
    cfg = load_config(ROOT / "airlie.yaml", NO_LOCAL)
    kinds = {n: e.kind for n, e in cfg.engines.items()}
    assert kinds == {"main": "mlx_vlm", "reviewer": "mlx_vlm", "small": "mlx_vlm", "decision": "llama_server",
                     "decision_mlx": "mlx_vlm", "decision_mlx2": "mlx_vlm",
                     "coder8": "mlx_vlm", "llama70b": "mlx_vlm", "qwen25_32b": "mlx_vlm"}
    assert [n for n, e in cfg.engines.items() if e.autostart] == ["main", "reviewer", "small", "decision"]
    assert cfg.engines["main"].models == ["generalist"] and cfg.engines["main"].port == 8080
    assert cfg.engines["main"].buffer_cache_mb is None  # mlx-vlm reads none
    assert [n for n, e in cfg.engines.items() if e.decision] == ["decision_mlx", "decision_mlx2"]
    assert cfg.engines["main"].served_name("generalist").endswith(
        f"models--mlx-community--Qwen3.8-27B-4bit/snapshots/{cfg.models['generalist'].revision}"
    )  # the HF-cache snapshot of mlx-community/Qwen3.8-27B-4bit: the exact --model string
    assert cfg.models["generalist"].tool_call_parser == "qwen3_coder"
    assert cfg.models["coder8"].tool_call_parser == "qwen3_coder"
    assert cfg.models["reviewer"].tool_call_parser == "atem"
    assert cfg.models["qwen25_32b"].tool_call_parser is None  # mlx-vlm has no `qwen` parser: inferred
    assert cfg.models["llama70b"].tool_call_parser is None  # nor `llama`; an unknown name is a 422
    assert all(m.reasoning_parser is None for m in cfg.models.values())


def test_profiles_carry_no_engine_or_model_overrides() -> None:
    cfg = load_config(ROOT / "airlie.yaml", NO_LOCAL)
    for name, profile in cfg.topology.profiles.items():
        assert "engines" not in profile and "models" not in profile, name


def test_an_mlx_vlm_engine_running_other_flags_than_the_config_is_a_mismatch_naming_task_restart(...) -> None:
    ...  # all_process_args returns two mlx_vlm.server argvs, on --port 8083 and --port 8080; the 8080 one has
         # --max-kv-size 65536 while config says 131072 → fail, detail names --max-kv-size and ends
         # "task restart"; the 8083 one is ignored


def test_no_mlx_vlm_server_on_the_engines_port_is_a_warning(...) -> None:
    ...


def test_the_story_line_gates_on_the_build_engines_config_not_a_profile(...) -> None:
    ...  # engine_matches_profile on an mlx_vlm config compares the build role's engine (main), not a profile
```

The two experiment tests become: `studio-qwen25` and `studio-coder8` still load and set `roles.coder.model` (`qwen25_32b`, `coder8`), and change no engine or model entry; `cfg.engines["qwen25_32b"].autostart is False`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_doctor.py -q -k "committed or profiles_carry or mismatch or no_mlx_vlm_server"`
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
  decision_mlx:  { kind: mlx_vlm, port: 8084, model: clef_flash, decision: true, autostart: false, max_num_seqs: 4 }   # gate candidate
  decision_mlx2: { kind: mlx_vlm, port: 8085, model: decider_2b, decision: true, autostart: false, max_num_seqs: 4 }   # gate candidate
  coder8:  { kind: mlx_vlm, port: 8091, model: coder8, autostart: false, max_kv_size: 65536, kv_bits: 8 }
  llama70b: { kind: mlx_vlm, port: 8092, model: llama70b, autostart: false, max_kv_size: 65536, kv_bits: 8 }
  qwen25_32b: { kind: mlx_vlm, port: 8093, model: qwen25_32b, autostart: false, max_kv_size: 65536, kv_bits: 8 }
```

The studio profile's values (131072 KV, 8-bit KV) become the base, as the spec says; its 8192 MB buffer-cache cap does not carry over, because mlx-vlm reads no `MLX_BUFFER_CACHE_LIMIT` (live test 2026-11-08) and Task 2 refuses the field on mlx_vlm engines. `models.yaml`: `tool_call_parser: qwen3_coder` on generalist and coder8, `atem` on reviewer, remove `tool_call_parser` from qwen25_32b and llama70b (mlx-vlm 0.7.12 has no `qwen` or `llama` parser, and an unknown name answers 422; unset lets the server infer one from the chat template), remove every `reasoning_parser`, remove `preload` lines (autostart on the engine decides); add the two gate candidates, `clef_flash` (`mlx-community/clef-flash-4bit`) and `decider_2b` (`Mapika/decider-2b`), each pinned to the revision `task models:pull` fetches. `profiles.yaml`: delete every `engines:` and `models:` block from `studio`, `studio-qwen25`, `studio-coder8`; the two experiment profiles keep `roles.coder.model` (the bench engine must be started by hand: `airlie engine up coder8`). Update the comment at the top of each profile accordingly, dropping `server_parsers`, chunked-prefill and `task restart -- --topology` text. The `decision` engine stays Kev on `llama_server` through 4a (and `router.model` stays `kev4b`) so the gate can compare against it; Task 11 step 6 settles which candidate becomes `decision` (kind `mlx_vlm`, `decision: true`, port 8081), and Task 12 removes the loser and `llama_server`.

- [ ] **Step 4: Implement `engine_matches_config`, retire `--topology` on engine commands, update the line**

`doctor.py`: add `all_process_args` to `SystemProbe` and `RealProbe` (the `ps -axo args=` read `process_args` already does, every matching line, no `" serve"` filter); add `engine_matches_config` and reshape `engine_matches_profile` as in Interfaces (the vllm-mlx branch and `_PROFILE_FLAGS`/`_PROFILE_SWITCHES` stay for vllm_mlx engines until Task 12; the mlx_vlm comparison uses its own `_MLX_VLM_FLAGS = ("--max-num-seqs", "--max-kv-size", "--kv-bits", "--draft-model")`). `cli.py`: remove the `topology` parameter from `engine_up`, `engine_down` and `engine_restart`, and `ProfileOpt` once nothing uses it. `story/line.py`: the engine-down message becomes "engine down: start it with `task restart`". `Taskfile.yml` descriptions say "mlx-vlm engines". `README.md`: the `task up` row and the engine sections describe mlx_vlm engines, one process per model, `airlie engine up <name>` for bench engines, and drop `task restart -- --topology ...`.

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
Expected: `tests/live/test_engine_live.py` and `tests/live/test_parsers_live.py` PASS against main; run the reviewer's subset with `AIRLIE_LIVE_ENGINE=reviewer`. `tests/live/conftest.py` has no such switch today, and its `engine` fixture builds a bare `HTTPEngine` that sends `e.served_name(cfg.roles[role].model)` to `cfg.engines["main"]` with no dialect: on mlx_vlm engines that sends the reviewer's or small's path to main, which unloads the generalist. Change the fixture to `name = os.environ.get("AIRLIE_LIVE_ENGINE", "main")`, the health check against `cfg.engines[name]` (with its `api_key` as Bearer), and `return airlie.cli._http_engine(cfg, name)`, so the dialect, the exact model path and the per-role routing come from the same code the line runs.

- [ ] **Step 3: Text-only models**

Run:

```bash
uv run airlie engine up coder8
M=$(uv run python -c "from airlie.config import load_config; e = load_config().engines['coder8']; print(e.served_name(e.models[0]))")
curl -s http://127.0.0.1:8091/v1/chat/completions -H 'content-type: application/json' \
  -d "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the single word: pong\"}],\"max_tokens\":8}"
uv run airlie engine down coder8
```

`model` must be the engine's exact `--model` path (`$M`); the catalog name `coder8` would make the server unload its model and fail to load one called `coder8`.
Expected: `pong`. Same for `small` on 8083 (with `engines['small']`).

- [ ] **Step 4: Attachments, reviewer, cache hits, MTP, concurrency**

Through the web app's chat: one picture and one video described correctly. One review round via `task studio -- "review this diff" --repo .` returns a parsed verdict with no client-side parsing (`output_parse` logs nothing). Cache hits: send a two-turn conversation twice while four other requests stream; the second turn's `usage.prompt_tokens_details.cached_tokens` (also `timings.cache_n`) > 0 on main and reviewer (requests stream with `stream_options.include_usage`, or the final usage chunk never comes). MTP: `timings.draft_n` > 0 on main with the head configured; report `timings.draft_n_accepted / timings.draft_n`. Never read `/v1/metrics` `latest.reasoning_tokens` (wrong upstream). Concurrency: two `airlie run` jobs on main at once, no interleaved or corrupted text (the PR 181 failure).

- [ ] **Step 5: Speed**

Run `task bench:models -- --ctx 4k --n 1,4` for the generalist on both engines (the vllm-mlx numbers from Phase 3) and one coder layer's wall clock from brief to green tests on the same scratch repo task as Phase 3.
Expected: mlx-vlm not worse on the same hardware; if it is, say so in the PR and stop.

- [ ] **Step 6: Decision model**

Run airlie's router questions (the `ROLE_QUESTION` criteria from `src/airlie/router/protocol.py` over twenty recorded briefs from `docs/bench/`) through Kev on llama-server (8081) and through `decision_mlx` (Clef-flash, 8084) and `decision_mlx2` (decider-2b, 8085), each started by name (`uv run airlie engine up decision_mlx`), via `JevClient(e.base_url, model=e.served_name(e.models[0]), api_key=e.api_key, endpoint="/decisions")` for `e = cfg.engines["decision_mlx"]` (the exact `--decision-model` path, never the catalog name); a short script under `scripts/` is fine. Record one raw `/v1/decisions` reply in the PR: it settles which `usage` key carries the input tokens (Task 9 reads `input_tokens`, then `prompt_tokens`). Report agreement with Kev's choices and the mean absolute difference of confidences. The winner becomes, in a follow-up commit on the 4a branch, `engines.decision: {kind: mlx_vlm, port: 8081, model: <winner>, decision: true}` with `router.model: <winner>`; if neither matches Kev, leave `decision` on llama_server and say so.

- [ ] **Step 7: Paste the results and stop**

Paste every row into the 4a pull request description under "Gate results" and stop. Noah reads them and gives the go for phase 5.

---

## Phase 5: Delete vllm-mlx and llama-server (after Noah's go)

### Task 12: One pull request, "vllm-mlx and llama-server leave"

**Files:**
- Delete: `src/airlie/engine/vllm_mlx_serve.py`, `src/airlie/engine/patches/` (whole directory), `src/airlie/engine/parsers/` (whole directory), `src/airlie/engine/mtp.py`, `src/airlie/engine/registry.py`'s `build_registry`, `write_registry`, `build_serve_command`, `build_llama_command`, `gguf_path`, `SERVE_ENTRY` (keep `SnapshotResolver` and `uses_mtp`, or move them into `mlx_vlm.py`), the parser-registry use in `src/airlie/engine/output_parse.py` (the function keeps only the `<think>` strip, or is deleted with its tests if nothing calls it), `tests/fixtures/vllm_mlx/`, and the test files: `test_vllm_mlx_serve.py`, `test_mtp_batch_join.py`, `test_hybrid_prefix_cache.py`, `test_active_requests.py`, `test_first_output.py`, `test_qwen3_xml_raw.py`, `test_muse_parsers.py`, `test_engine_mtp.py`, `tests/unit/vllm_helpers.py`, `test_registry.py`'s vllm and llama tests, the llama-server and MTP-directory cases in `test_process.py`, `test_http_engine.py` and `test_http_engine_wire.py` (replaced by the mlx_vlm fixture tests; keep any contract test that is engine-neutral by pointing it at `tests/fixtures/mlx_vlm`).
- Modify: `src/airlie/config/schema.py` (`kind: Literal["mlx_vlm", "openai_http"]`; remove `memory_budget_gb`, `continuous_batching`, `use_paged_cache`, `server_parsers`, `per_request_draft`, `kv_cache_quantization`, `gpu_memory_utilization`, `max_request_tokens`, `ctx_size`, `binary`, `buffer_cache_mb` (and Task 2's refusal of it); `served_names` stays for `openai_http`, and `load_config` keeps filling it with the model path for mlx_vlm; remove `ModelSpec.reasoning_parser`, `preload`, `enable_mtp`, `continuous_batching`, `file`), `src/airlie/engine/dialect.py` (`Dialect = Literal["mlx_vlm"]`, `dialect_for` returns it for every kind), `src/airlie/engine/http_engine.py` (drop the `airlie_ttft_ms`, `generation_metadata` and `finish_reason == "error"` branches, the `thinking_token_budget` path and the `parse_output` call; keep the `usage.prompt_tokens_details.cached_tokens` read, which is where mlx-vlm reports cached tokens, and the `timings` draft counters), `src/airlie/serve/bridge/busy.py` (metrics only), `src/airlie/router/jev.py` (`endpoint` fixed to `/decisions`), `src/airlie/tools/decide.py`, `src/airlie/doctor.py` (the vllm-mlx binary row, `cache_memory_gb`'s legacy parse, the vllm branch of `engine_matches_profile` with `_PROFILE_FLAGS`, `_PROFILE_SWITCHES`, `_registry_models` and the `" serve"`-filtered `process_args`, `[mllm_cache]` and `engine:install` text, `is_hybrid_attention` wording), `src/airlie/cli.py` (`_managed` is `mlx_vlm` only), `tests/unit/conftest.py` (`MAC_ONLY` keeps only `test_learned_controller.py`; `MAC_ONLY_TESTS` keeps only the entries that import mlx; `HAS_MLX` checks `mlx` not `vllm_mlx`), `pyproject.toml` (remove `vllm-mlx` and `mlx-lm`; `mac_functions`/mutation lists drop the deleted files), `uv.lock`, `airlie.yaml` (remove the `llama_server` `decision` engine and the two `decision_mlx*` engines, leaving `decision: {kind: mlx_vlm, port: 8081, model: <winner>, decision: true}`, and `router.model: <winner>`), `config/models.yaml` (remove `kev4b` and the loser), `README.md`, `docs/bench/README.md`, `config/profiles.yaml` comments, `src/airlie/prompts/steering/reasoning-carried.md` (re-read against mlx-vlm's template behaviour from the gate; rewrite or delete).

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
- Interfaces named consistently: `build_mlx_vlm_command`, `MLX_VLM_ENTRY`, `served_model` (T3) used by T4, T5, T10; `model_dir`, `hf_snapshot_dir` and the filled `served_names` (T2) used by T3, T6, T9, T11; `thinking_fields`, `dialect_for`, `Dialect` (T6) used by T7, T12; `RoutedEngine` (T6) used by `cli._http_engine` and through it the story line, `airlie run` and T11's live conftest; `loaded_model_ids`, `preload_failures` (T4) used by `EngineProcess`; `HTTPEngine.health` (T7) reads `/v1/models` itself; `EngineSpec.decision` (T2) used by T3, T9, T10; `all_process_args`, `engine_matches_config` (T10) used by `engine_matches_profile` and through it `story/line.py`.
- Amendment-era deviations from the spec, stated: `buffer_cache_mb` / `MLX_BUFFER_CACHE_LIMIT` and the spec's `usage.cached_tokens` / `usage.draft_n` locations are overtaken by the 2026-11-08 live test; `EngineSpec.decision` and `RoutedEngine` are the smallest additions that let the spec's decision engine and one-process-per-model layout work with the current code.
