# HTTP-only `workflow` engines — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop managed mlx-vlm / weights_only process ownership; keep `task serve` as the OpenAI `/v1` front door that proxies to a single env-configured `workflow` engine with `format: openai | anthropic`.

**Architecture:** EngineSpec becomes HTTP-only (`kind: http`, `format`, `base_url`, `api_key`, `models`, `passthru`, `timeout_s`). `HTTPEngine` speaks openai or anthropic upstream; serve keeps translating client Chat Completions. Delete process spawn, jev, models:pull, and mlx dialect remaps. Thin model catalog (name + label). Router → `chief`.

**Tech Stack:** Python 3.12, Pydantic, httpx, FastAPI serve, uv, Taskfile, pytest.

**Spec:** `docs/superpowers/specs/2026-11-20-http-workflow-engines-design.md`

## Global Constraints

- Serve stays the front door; clients always see OpenAI-compatible `/v1`.
- `format: openai | anthropic` (schema default `openai`).
- Committed engine name is `workflow`; hostnames/secrets via env / `airlie.local.yaml`.
- Noah’s local override: `engines.workflow.format: anthropic`.
- `passthru` replaces `served_names` (catalog name → upstream id; empty = identity).
- Hard cut: no mlx_vlm, weights_only, process spawn, jev, models:pull, mlx doctor memory.
- Router kind `chief` only after cut (delete jev path).
- AGENTS.md gate must pass at the end.

## File map

| Path | Responsibility |
|------|----------------|
| `src/airlie/config/schema.py` | EngineSpec / ModelSpec / RouterSpec hard cut |
| `src/airlie/config/load.py` | Drop mlx served_names path fill; resolve `${ENV}` if not already |
| `airlie.yaml`, `config/models.yaml` | `workflow` engine + thin catalog |
| `airlie.local.yaml` (gitignored) | Noah: `format: anthropic` (+ url/key/passthru) |
| `src/airlie/engine/dialect.py` | `openai` \| `anthropic` only; drop mlx helpers |
| `src/airlie/engine/anthropic.py` (new) | Chat Completions ↔ Messages translation |
| `src/airlie/engine/http_engine.py` | Default format openai; anthropic path |
| `src/airlie/serve/app.py` | Drop mlx remaps; use `passthru` / format |
| Delete / gut | `engine/process.py`, `engine/mlx_vlm.py`, jev, decide-on-decision, models_cmd pull |
| `src/airlie/doctor.py`, `Taskfile.yml`, `README.md`, `pyproject.toml` | Reachability, no `task up` for models |

---

### Task 1: EngineSpec / ModelSpec schema + failing config tests

**Files:**
- Modify: `src/airlie/config/schema.py`
- Modify: `tests/unit/test_config.py` (add focused tests; delete/rewrite mlx blocks later)
- Test: `tests/unit/test_http_engine_spec.py` (new, focused)

**Interfaces:**
- Produces:
  - `EngineFormat = Literal["openai", "anthropic"]`
  - `EngineSpec.kind: Literal["http"]`
  - `EngineSpec.format: EngineFormat = "openai"`
  - `EngineSpec.base_url: str` (required)
  - `EngineSpec.models: list[str]` (min length 1)
  - `EngineSpec.passthru: dict[str, str] = {}`
  - `EngineSpec.api_key: str | None = None`
  - `EngineSpec.timeout_s: float = 600.0`
  - `EngineSpec.passthru_name(model: str) -> str` (was `served_name`)
  - `ModelSpec`: `label: str | None` only (plus any Strict base); no repo/revision/mtp/…
  - `RouterSpec.kind: Literal["chief"]` only (or keep other non-jev kinds if already used: check schema — drop `jev` only)

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_http_engine_spec.py
import pytest
from pydantic import ValidationError

from airlie.config.schema import EngineSpec, ModelSpec


def test_engine_requires_base_url_and_models():
    with pytest.raises(ValidationError):
        EngineSpec(kind="http", models=["smart"])  # type: ignore[call-arg]


def test_engine_passthru_defaults_to_identity():
    e = EngineSpec(
        kind="http",
        base_url="http://127.0.0.1:8080/v1",
        models=["smart"],
    )
    assert e.format == "openai"
    assert e.passthru_name("smart") == "smart"
    e2 = EngineSpec(
        kind="http",
        base_url="http://127.0.0.1:8080/v1",
        models=["smart"],
        passthru={"smart": "Qwen3.8-Flash-Next"},
    )
    assert e2.passthru_name("smart") == "Qwen3.8-Flash-Next"


def test_rejects_mlx_vlm_kind():
    with pytest.raises(ValidationError):
        EngineSpec(kind="mlx_vlm", model="smart", port=8080)  # type: ignore[arg-type]


def test_model_spec_is_label_only():
    m = ModelSpec(label="Qwen Flash")
    assert m.label == "Qwen Flash"
    with pytest.raises(ValidationError):
        ModelSpec(repo="x", revision="abc")  # type: ignore[call-arg]
```

- [ ] **Step 2: Run tests — expect FAIL** (EngineSpec still mlx-shaped)

```bash
uv run pytest tests/unit/test_http_engine_spec.py -q
```

- [ ] **Step 3: Implement schema hard cut** in `schema.py`:
  - Replace `EngineSpec` fields per Interfaces; rename method `served_name` → `passthru_name` (update all call sites in later tasks; for this task add alias `served_name = passthru_name` temporarily OR update every call site in the same commit — prefer update all call sites in Task 1 if greppable).
  - Thin `ModelSpec`.
  - Drop jev from `RouterSpec` / validation that requires decision engines.
  - Remove mlx port / decision validators.

- [ ] **Step 4: Re-run tests — expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/airlie/config/schema.py tests/unit/test_http_engine_spec.py
git commit -m "$(cat <<'EOF'
feat(config): HTTP-only EngineSpec with format and passthru

EOF
)"
```

---

### Task 2: Committed config + Noah local anthropic

**Files:**
- Modify: `airlie.yaml` (engines + router)
- Modify: `config/models.yaml`
- Modify: `airlie.local.yaml` (gitignored — create/update on Noah’s machine only)
- Modify: `src/airlie/config/load.py` (drop mlx `served_names` fill; ensure `${VAR}` expansion works for engine base_url/api_key if not already)
- Fix any import/test fixtures that embed old engine YAML

**Interfaces:**
- Consumes: Task 1 schema
- Produces: loadable committed config when env `AIRLIE_WORKFLOW_BASE_URL` (and optional key) are set

Committed engines block:

```yaml
engines:
  workflow:
    kind: http
    format: openai
    base_url: ${AIRLIE_WORKFLOW_BASE_URL}
    api_key: ${AIRLIE_WORKFLOW_API_KEY}
    models: [smart, small, fast]
    passthru: {}
```

`config/models.yaml`:

```yaml
models:
  small:
    label: Small
  smart:
    label: Smart
  fast:
    label: Fast
```

(Include every model name roles still reference — grep `model:` in `config/roles.yaml` and list them all under `workflow.models` and the catalog.)

Router:

```yaml
router:
  kind: chief
```

Noah `airlie.local.yaml`:

```yaml
engines:
  workflow:
    format: anthropic
    base_url: https://mac-studio.taila5f450.ts.net/v1
    # api_key from env or inline local-only
```

- [ ] **Step 1: Rewrite YAML files; update `load.py`**
- [ ] **Step 2: `uv run airlie config` / load test with env set — expect success**
- [ ] **Step 3: Commit committed files only (never commit secrets in local.yaml)**

```bash
git add airlie.yaml config/models.yaml src/airlie/config/load.py
git commit -m "$(cat <<'EOF'
config: single workflow HTTP engine and thin model catalog

EOF
)"
```

---

### Task 3: Dialect openai|anthropic + Anthropic translator

**Files:**
- Rewrite: `src/airlie/engine/dialect.py`
- Create: `src/airlie/engine/anthropic.py`
- Test: `tests/unit/test_anthropic_format.py`

**Interfaces:**
- Produces:
  - `Dialect = Literal["openai", "anthropic"]` (alias of engine format)
  - `dialect_for(format: str) -> Dialect` — identity / validate
  - `thinking_fields(...)` — openai-compatible kwargs only (no mlx branches)
  - `to_anthropic_messages_body(openai_chat_body: dict) -> dict`
  - `from_anthropic_message_response(anthropic_json: dict) -> dict` (Chat Completions shape)
  - Streaming: `iter_openai_chunks_from_anthropic_sse(lines) -> Iterator[dict]` (minimal viable: non-stream first if stream is large; prefer both)

- [ ] **Step 1: Failing tests for request/response translation**

```python
from airlie.engine.anthropic import from_anthropic_message_response, to_anthropic_messages_body


def test_to_anthropic_moves_system_and_maps_messages():
    body = {
        "model": "smart",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ],
        "max_tokens": 64,
    }
    out = to_anthropic_messages_body(body)
    assert out["model"] == "smart"
    assert out["system"] == "You are helpful."
    assert out["messages"] == [{"role": "user", "content": "Hi"}]
    assert out["max_tokens"] == 64


def test_from_anthropic_response_to_chat_completion():
    raw = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }
    cc = from_anthropic_message_response(raw)
    assert cc["choices"][0]["message"]["content"] == "Hello"
    assert cc["usage"]["prompt_tokens"] == 3
```

- [ ] **Step 2: Implement `anthropic.py` + slim `dialect.py` (delete mlx remaps)**
- [ ] **Step 3: Tests pass**
- [ ] **Step 4: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(engine): openai|anthropic format translation

EOF
)"
```

---

### Task 4: HTTPEngine uses format; serve drops mlx remaps

**Files:**
- Modify: `src/airlie/engine/http_engine.py`
- Modify: `src/airlie/serve/app.py`
- Modify: call sites using `dialect_for(e.kind)` → `dialect_for(e.format)`
- Test: `tests/contract/test_http_engine.py` (retarget fixtures away from mlx_vlm)
- Delete or gut: `tests/contract/test_http_engine_mlx_vlm.py`

**Interfaces:**
- Consumes: Task 3 translators
- `HTTPEngine(..., dialect: Dialect = "openai")`
- For anthropic: POST `{root}/v1/messages` with anthropic headers (`x-api-key` or Bearer + `anthropic-version: 2023-06-01`); translate body/response
- Serve `upstream()` uses `passthru_name`; no `normalize_mlx_vlm_*`

- [ ] **Step 1: Contract test with httpx MockTransport for openai chat/completions**
- [ ] **Step 2: Contract test MockTransport for anthropic messages path**
- [ ] **Step 3: Implement HTTPEngine branches; strip serve mlx remaps**
- [ ] **Step 4: Tests pass**
- [ ] **Step 5: Commit**

```bash
git commit -m "$(cat <<'EOF'
feat(engine): HTTPEngine openai/anthropic; serve without mlx remaps

EOF
)"
```

---

### Task 5: Delete managed inference path (process, mlx_vlm, jev, pull)

**Files:**
- Delete: `src/airlie/engine/process.py`, `src/airlie/engine/mlx_vlm.py`
- Delete or gut: `src/airlie/router/jev.py`, jev wiring in `router/__init__.py`
- Gut: `src/airlie/tools/decide.py` (remove or no-op if no decision engine)
- Gut: `src/airlie/cli.py` engine up/down/restart/logs spawn; `models_cmd.py` pull
- Gut: `src/airlie/manager/engines.py` — no HF download / process start
- Delete tests: `tests/unit/test_mlx_vlm_command.py`, `test_process.py`, mlx-heavy doctor cases as needed
- Grep and remove remaining `mlx_vlm` / `served_names` / `models:pull` references

- [ ] **Step 1: Grep for leftovers**

```bash
rg -n "mlx_vlm|weights_only|served_names|models:pull|vllm_mlx|EngineProcess|build_mlx" src tests airlie.yaml Taskfile.yml
```

- [ ] **Step 2: Delete modules; fix imports until `uv run mypy` and unit tests compile**
- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
refactor: remove managed mlx-vlm process ownership

EOF
)"
```

---

### Task 6: Doctor, Taskfile, README, pyproject

**Files:**
- Modify: `src/airlie/doctor.py` — check HTTP reachability of each engine `base_url` (`GET …/models` or `/health`)
- Modify: `Taskfile.yml` — remove/repurpose `up`/`down` for inference; document `task serve`
- Modify: `README.md` — engines are remote HTTP; `workflow` + env
- Modify: `pyproject.toml` — drop `mlx-vlm` Darwin extra if unused

- [ ] **Step 1: Doctor unit test with mocked httpx — unreachable URL → problem string**
- [ ] **Step 2: Implement + docs**
- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
chore: doctor reachability; drop task up for local engines

EOF
)"
```

---

### Task 7: Full gate

- [ ] **Step 1: Run AGENTS.md gate in order**

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q -n auto --maxprocesses 8
task web:gate
```

- [ ] **Step 2: Fix failures until green**
- [ ] **Step 3: Final commit if needed**

---

## Spec coverage check

| Spec requirement | Task |
|------------------|------|
| Serve front door / proxy | 4 |
| format openai\|anthropic | 1, 3, 4 |
| Hard cut mlx/weights/process/jev/pull | 1, 5 |
| Engine `workflow` + env | 2 |
| Noah local anthropic | 2 |
| passthru | 1, 2, 4 |
| Thin models catalog | 1, 2 |
| Router chief | 2, 5 |
| Doctor reachability | 6 |
| Gate green | 7 |
