# Qwen Flash Architect + Gemma Product Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `generalist` team model with catalog model `architect` (Qwen3.8-Flash-Next + MTP), consolidate all coding into the `architect` role (absorb `coder`, delete `tester`), keep Gemma for product-facing work at 128k context, and add image/video generation weights to the catalog with doctor/pull support.

**Architecture:** Rename/replace `models.generalist` → `models.architect` pointing at pinned MLX repos; `engines.main.model: architect` with unchanged pinned lifecycle on port 8080. Retarget every role that used `generalist` to `architect`; move `product` to `gemma4`. Two-seat amigos (`product`, `architect`) with proof owned by `architect`. Chroma and Wan are diffusion/video MLX repos (not mlx-vlm chat servers); extend `EngineSpec.kind` with `weights_only` so they have on_demand lifecycle entries for pull/doctor without spawning mlx-vlm.

**Tech Stack:** Python 3.12, uv, YAML config (`config/models.yaml`, `config/roles.yaml`, `airlie.yaml`), mlx-vlm engines, Hugging Face pinned revisions, pytest.

## Global Constraints

- No `generalist` catalog model in the end state (replaced by `architect`).
- No `reviewer` catalog model in the end state (Muse-Glimmer removed 2026-11-13; `reviewer` role stays on `gemma4`).
- End-state catalog (seven keys): `architect`, `gemma4`, `small`, `decider_2b`, `controller_base`, `chroma_hd`, `wan_t2v`.
- No `coder` role in the end state (behaviors absorbed into `architect`).
- No `tester` role in the end state (amigos becomes `product` + `architect` only).
- Catalog model name `architect` matches the coding role name; `engines.main.model: architect`.
- Architect weights: `sh0wie/Qwen3.8-Flash-Next-REAP-288-MLX-4bit` revision `668f31bcc56bf9400e64c9463445eee47597c2d9`; MTP `sh0wie/Qwen3.8-Flash-Next-MTP-Drafter-MLX-bf16` revision `be6e2bc325d41ac7858b99269a96c495349b494d`.
- Image catalog: `tillknuesting/chroma1-hd-mlx` revision `0b794d9391239d85530d2db3b4091b8b093d1af6`.
- Video catalog: `PocketAiHub/Wan2.2-T2V-A14B-MLX` revision `86bfc9847512a9eb66886388b8b92ba81e0b6729`.
- Main engine: port 8080, lifecycle pinned (default), `max_kv_size: 131072`.
- Gemma engine `max_kv_size` and `roles.chat.max_context`: `131072` (was `262144`).
- `roles.product.model`: `gemma4`.
- Former-generalist roles on `architect` model: architect, docs, research, security, chief, lhr, aggregator.
- Image engine `chroma`, video engine `wan`: `lifecycle: on_demand`, `kind: weights_only` (no mlx-vlm process).
- Generation inference tools deferred (YAGNI); catalog + pull + doctor + config tests only.
- Test gate at end (repository root): `uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -n auto --maxprocesses 8 && task web:gate`.

---

## File structure map

| File | Responsibility after change |
|------|--------------------------------|
| `config/models.yaml` | `architect` model + MTP; remove `generalist` and unused Muse `reviewer` catalog; add `chroma_hd`, `wan_t2v`; product comments |
| `airlie.yaml` | `engines.main.model: architect`; gemma 128k; story/teams/chat defaults → `architect`; image/video engines |
| `config/roles.yaml` | Expanded `architect`; `product` → gemma4; retarget models; delete `coder` and `tester` |
| `src/airlie/config/schema.py` | `EngineSpec.kind` adds `weights_only`; optional port for weights engines; default `teams.role` / `build_role` / `chat.task` |
| `src/airlie/config/load.py` | Migration hint if old key `generalist` appears (optional one-release) |
| `src/airlie/doctor.py` | Memory projection includes `weights_only` engines; architect swap |
| `src/airlie/engine/mlx_vlm.py` | No command for `weights_only` (guard) |
| `src/airlie/story/amigos.py` | `ROLES = ("product", "architect")`; proof owner `architect`; prompts text |
| `src/airlie/flow/spec.py` | `MEETINGS` roles tuple |
| `src/airlie/story/pipeline.py` | Package critic / proof routing uses `architect` not `tester` |
| `src/airlie/story/flow.py` | Imports `ROLES` (unchanged import, new tuple) |
| `prompts/roles/architect.md` | Owns planning + implementation + testing (merge coder mission) |
| `prompts/roles/coder.md` | **Delete** after references removed |
| `prompts/roles/tester.md` | **Delete** after references removed |
| `prompts/rules/*`, `prompts/steering/*` | Retarget `roles:` frontmatter away from coder/tester |
| `config/workflows/story.yaml` | Meeting node unchanged structurally; validation via spec |
| `tests/unit/test_config.py` | architect catalog, 128k gemma, no coder/tester |
| `tests/unit/test_doctor.py` | Memory totals with architect + chroma + wan weights |
| `tests/unit/test_mlx_vlm_command.py` | `engines.main` still builds mlx-vlm argv for architect |
| `tests/unit/test_flow_spec.py` | Meeting roles `product`, `architect` |
| `tests/support/*.py` | STORY_ROLES, layer golden, serve tests → `architect` |
| `docs/integrations/opencode.json`, `docs/integrations/*.md` | No coder/generalist; architect model paths |

---

### Task 1: Replace `generalist` catalog entry with `architect`

**Files:**
- Modify: `config/models.yaml`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `cfg.models["architect"]` with repo, revision, mtp, `tool_call_parser: qwen3_coder`, `bench: true`
- Consumes: existing `ModelSpec` / `ModelRef` in `src/airlie/config/schema.py`

- [ ] **Step 1: Write the failing test**

Add near existing generalist assertions in `tests/unit/test_config.py`:

```python
def test_architect_catalog_replaces_generalist() -> None:
    cfg = load()
    assert "generalist" not in cfg.models
    arch = cfg.models["architect"]
    assert arch.repo == "sh0wie/Qwen3.8-Flash-Next-REAP-288-MLX-4bit"
    assert arch.revision == "668f31bcc56bf9400e64c9463445eee47597c2d9"
    assert arch.mtp is not None
    assert arch.mtp.repo == "sh0wie/Qwen3.8-Flash-Next-MTP-Drafter-MLX-bf16"
    assert arch.mtp.revision == "be6e2bc325d41ac7858b99269a96c495349b494d"
    assert arch.tool_call_parser == "qwen3_coder"
    assert {"architect"} <= {n for n, m in cfg.models.items() if m.bench}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config.py::test_architect_catalog_replaces_generalist -v`  
Expected: FAIL (`generalist` still present or `architect` missing)

- [ ] **Step 3: Write minimal implementation**

In `config/models.yaml`, remove the `generalist:` block and the unused Muse `reviewer:` catalog stanza (`mlx-community/Muse-Glimmer-30B-4bit`), and add:

```yaml
  architect:                       # Qwen3.8-Flash-Next REAP (team/coding model; same name as the role)
    repo: sh0wie/Qwen3.8-Flash-Next-REAP-288-MLX-4bit
    label: Qwen 3.8 Flash Next
    revision: 668f31bcc56bf9400e64c9463445eee47597c2d9   # 2026-11-13
    mtp:
      repo: sh0wie/Qwen3.8-Flash-Next-MTP-Drafter-MLX-bf16
      revision: be6e2bc325d41ac7858b99269a96c495349b494d   # 2026-11-13
      estimated_memory_gb: 0.6
    bench: true
    estimated_memory_gb: 10.5      # REAP 4-bit MoE; `task doctor` warns if pull differs — re-measure after first pull
    kv_mb_per_1k_tokens: 48        # Flash-Next hybrid; tighten after doctor documents actual layers
    tool_call_parser: qwen3_coder
    tags: [modern, coder, vision, hybrid-attention]
```

Update the file header comment (lines 5–8) to say `architect`, not `generalist`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_config.py::test_architect_catalog_replaces_generalist -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config/models.yaml tests/unit/test_config.py
git commit -m "feat: catalog architect model with Flash-Next and MTP pins"
```

---

### Task 2: Point `engines.main` at `architect`

**Files:**
- Modify: `airlie.yaml`
- Modify: `tests/unit/test_config.py` (replace generalist engine assertions)
- Modify: `tests/unit/test_mlx_vlm_command.py` if model name hard-coded

**Interfaces:**
- Produces: `cfg.engines["main"].models == ["architect"]`, port 8080, `max_kv_size: 131072`
- Consumes: `cfg.models["architect"]` from Task 1

- [ ] **Step 1: Write the failing test**

```python
def test_main_engine_serves_architect() -> None:
    cfg = load()
    assert cfg.engines["main"].models == ["architect"]
    assert cfg.engines["main"].port == 8080
    assert cfg.engines["main"].max_kv_size == 131072
    from airlie.engine.mlx_vlm import served_name

    assert (
        served_name(cfg, "main")
        .endswith(
            "models--sh0wie--Qwen3.8-Flash-Next-REAP-288-MLX-4bit/snapshots/"
            f"{cfg.models['architect'].revision}"
        )
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config.py::test_main_engine_serves_architect -v`  
Expected: FAIL (`generalist` on main)

- [ ] **Step 3: Write minimal implementation**

In `airlie.yaml`:

```yaml
engines:
  main:
    kind: mlx_vlm
    port: 8080
    model: architect
    max_num_seqs: 16
    max_kv_size: 131072
    # ... unchanged kv_bits, prefix_cache, etc.
```

Remove or update commented bridge `llm.model: generalist` → `architect`.

Fix `tests/unit/test_config.py` tests that still assert `generalist` on main (grep `generalist` in that file).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_config.py::test_main_engine_serves_architect tests/unit/test_mlx_vlm_command.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add airlie.yaml tests/unit/test_config.py tests/unit/test_mlx_vlm_command.py
git commit -m "feat: main mlx-vlm engine serves architect model"
```

---

### Task 3: Shrink Gemma chat context to 128k

**Files:**
- Modify: `airlie.yaml` (`engines.gemma.max_kv_size`)
- Modify: `config/roles.yaml` (`roles.chat.max_context`)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `cfg.engines["gemma"].max_kv_size == 131072`, `cfg.roles["chat"].max_context == 131072`

- [ ] **Step 1: Write the failing test**

Rename/update existing test around line 1575:

```python
def test_gemma_chat_context_is_128k() -> None:
    cfg = load()
    assert cfg.engines["gemma"].max_kv_size == 131072
    assert cfg.roles["chat"].max_context == 131072
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_config.py::test_gemma_chat_context_is_128k -v`  
Expected: FAIL (262144)

- [ ] **Step 3: Implement**

```yaml
# airlie.yaml engines.gemma
    max_kv_size: 131072
```

```yaml
# config/roles.yaml roles.chat
    max_context: 131072
```

- [ ] **Step 4: Run test**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add airlie.yaml config/roles.yaml tests/unit/test_config.py
git commit -m "feat: shrink gemma engine and chat role to 128k context"
```

---

### Task 4: Retarget roles from `generalist` to `architect`; `product` to `gemma4`

**Files:**
- Modify: `config/roles.yaml`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: roles `chief`, `lhr`, `architect`, `docs`, `research`, `security`, `aggregator` with `model: architect`; `product` with `model: gemma4`

- [ ] **Step 1: Write the failing test**

```python
def test_studio_roles_use_architect_and_product_on_gemma() -> None:
    cfg = load()
    for name in ("chief", "lhr", "architect", "docs", "research", "security", "aggregator"):
        assert cfg.roles[name].model == "architect", name
    assert cfg.roles["product"].model == "gemma4"
    assert "generalist" not in cfg.models
```

Remove or rewrite `test_the_tester_is_a_read_only_critic_on_the_generalist` (tester deleted in Task 8).

- [ ] **Step 2: Run test — FAIL**

- [ ] **Step 3: Implement**

In `config/roles.yaml`, change every `model: generalist` under the listed roles to `model: architect`; set `product.model: gemma4`.

- [ ] **Step 4: Run test — PASS**

- [ ] **Step 5: Commit**

```bash
git add config/roles.yaml tests/unit/test_config.py
git commit -m "feat: retarget studio roles to architect and product to gemma"
```

---

### Task 5: Add `weights_only` engine kind and image/video catalog

**Files:**
- Modify: `src/airlie/config/schema.py` (`EngineSpec`)
- Modify: `config/models.yaml`
- Modify: `airlie.yaml`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `EngineSpec.kind: Literal["mlx_vlm", "openai_http", "weights_only"]`; validation: `weights_only` engines have exactly one model, no port collision with mlx_vlm, no role may reference weights-only models until generation exists
- Produces: `models.chroma_hd`, `models.wan_t2v`; engines `chroma`, `wan`

- [ ] **Step 1: Write the failing test**

```python
def test_generation_models_catalog_and_weights_engines() -> None:
    cfg = load()
    chroma = cfg.models["chroma_hd"]
    assert chroma.repo == "tillknuesting/chroma1-hd-mlx"
    assert chroma.revision == "0b794d9391239d85530d2db3b4091b8b093d1af6"
    wan = cfg.models["wan_t2v"]
    assert wan.repo == "PocketAiHub/Wan2.2-T2V-A14B-MLX"
    assert wan.revision == "86bfc9847512a9eb66886388b8b92ba81e0b6729"
    assert cfg.engines["chroma"].kind == "weights_only"
    assert cfg.engines["chroma"].lifecycle == "on_demand"
    assert cfg.engines["chroma"].models == ["chroma_hd"]
    assert cfg.engines["wan"].kind == "weights_only"
    assert cfg.engines["wan"].models == ["wan_t2v"]
    load_check = cfg.validate()
    assert not load_check, load_check
```

- [ ] **Step 2: Run test — FAIL**

- [ ] **Step 3: Implement schema**

In `src/airlie/config/schema.py`:

```python
class EngineSpec(Strict):
    kind: Literal["mlx_vlm", "openai_http", "weights_only"]
    # ...
```

In `AirlieConfig` validation loop:

- Skip mlx_vlm-only port checks for `weights_only`.
- For `weights_only`: require `len(e.models) == 1`; `port` optional (default `0`); do not require mlx_vlm single-model rule on openai_http/weights_only the same way.
- Do not require a role-serving engine for models only referenced by `weights_only` (no role uses them yet).

Add to `config/models.yaml`:

```yaml
  chroma_hd:
    repo: tillknuesting/chroma1-hd-mlx
    label: Chroma HD
    revision: 0b794d9391239d85530d2db3b4091b8b093d1af6
    estimated_memory_gb: 14.0
    tags: [image-generation, mlx, diffusers]
  wan_t2v:
    repo: PocketAiHub/Wan2.2-T2V-A14B-MLX
    label: Wan 2.2 T2V
    revision: 86bfc9847512a9eb66886388b8b92ba81e0b6729
    estimated_memory_gb: 32.0
    tags: [video-generation, mlx, moe]
```

Add to `airlie.yaml`:

```yaml
  chroma:
    kind: weights_only
    model: chroma_hd
    lifecycle: on_demand
    idle_unload_s: 300
  wan:
    kind: weights_only
    model: wan_t2v
    lifecycle: on_demand
    idle_unload_s: 300
```

Ensure `engine_for_role` / serve manager ignores `weights_only` for chat (grep `engines` iteration; skip non-mlx_vlm).

- [ ] **Step 4: Run test — PASS**

- [ ] **Step 5: Commit**

```bash
git add src/airlie/config/schema.py config/models.yaml airlie.yaml tests/unit/test_config.py
git commit -m "feat: weights_only engines for chroma and wan catalog models"
```

---

### Task 6: Doctor memory projection for architect and generation weights

**Files:**
- Modify: `src/airlie/doctor.py`
- Modify: `tests/unit/test_doctor.py`

**Interfaces:**
- Consumes: `weights_only` engines from Task 5
- Produces: doctor includes chroma/wan `estimated_memory_gb` in total; strings mention architect not generalist

- [ ] **Step 1: Write the failing test**

Update studio projection test comments and asserts that referenced `generalist` → `architect`; add:

```python
def test_doctor_counts_weights_only_generation_models(load_cfg) -> None:
    cfg = load_cfg()  # use existing fixture pattern in test_doctor.py
    report = run_doctor(cfg)
    assert "chroma_hd" in report.detail or any("chroma" in r.name for r in report.checks)
```

(Adapt to the project's actual `run_doctor` helper name — read `tests/unit/test_doctor.py` top fixtures.)

- [ ] **Step 2: Run test — FAIL**

- [ ] **Step 3: Implement**

When summing model weight memory, include every catalog model referenced by any engine (including `weights_only`). Replace user-facing `generalist` strings in doctor messages with `architect`.

- [ ] **Step 4: Run `tests/unit/test_doctor.py` — PASS**

- [ ] **Step 5: Commit**

```bash
git add src/airlie/doctor.py tests/unit/test_doctor.py
git commit -m "feat: doctor accounts for weights_only generation models"
```

---

### Task 7: Absorb `coder` into `architect` role configuration

**Files:**
- Modify: `config/roles.yaml`
- Modify: `prompts/roles/architect.md`
- Modify: `airlie.yaml` (`teams.role`, `chat.task`)
- Modify: `src/airlie/config/schema.py` defaults (`StorySpec.build_role`, `TeamsSpec.role`, `ChatTaskSpec.default_role`)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `architect` role with builtin write tools + `green_before_done`, `think_when_red`, `write_schedule`, coder-like `max_tokens` / sampling for build turns
- Produces: `story.build_role == "architect"`, `teams.role == "architect"`, `chat.task.default_role == "architect"`

- [ ] **Step 1: Write the failing test**

```python
def test_architect_is_build_role_with_coder_behaviors() -> None:
    cfg = load()
    assert cfg.story.build_role == "architect"
    assert cfg.teams.role == "architect"
    assert cfg.chat.task.default_role == "architect"
    arch = cfg.roles["architect"]
    tools = set(arch.tools or cfg.tools.builtin)
    assert "write_file" in tools and "run_tests" in tools
    assert arch.green_before_done is True
    assert arch.think_when_red is True
    assert arch.write_schedule is not None
```

- [ ] **Step 2: Run test — FAIL**

- [ ] **Step 3: Implement**

Merge from `config/roles.yaml` `coder` block into `architect`:

```yaml
  architect:
    prompt: prompts/roles/architect.md
    model: architect
    tools: [read_file, write_file, edit_file, list_dir, grep, shell, run_tests, git, compute]
    max_context: 131072
    max_tokens: 10240
    thinking: false
    nudge_stuck: true
    green_before_done: true
    think_when_red: true
    thinking_budget: 4096
    write_schedule:
      tests: 0.25
      code: 0.75
    max_steps: 40
    sampling:
      temperature: 0.3
      top_p: 0.8
      top_k: 20
      min_p: 0.0
      presence_penalty: 0.0
      repetition_penalty: 1.05
    # keep answer_after_cuts / package planning overrides only if still used for package subtask — see pipeline
```

For package/architect planning turns that need `thinking: true`, the pipeline already selects role by subtask; if package uses `architect_role` with different thinking, keep a note in `architect.md` frontmatter for planning vs build (pipeline uses `architect_role` vs `build_role` — both become `architect` with same config; ensure package path still sets thinking via session if needed — read `layer_build.py` and restore `thinking: true` + higher `max_tokens` for package-only via `RoleSpec` duplication is impossible with one role — **use `thinking: true` on architect with `last_turn_thinking: false` and `answer_after_cuts: 3` retained from old architect for package, and `think_when_red` covers build**).

Update `prompts/roles/architect.md`: merge coder "How you work" / write rules; change mission from "plan a coder can execute" to "plan and implement"; set `governs.repo.edit` to allow writes.

In `airlie.yaml`:

```yaml
teams:
  role: architect
chat:
  task:
    default_role: architect
    roles: [architect, reviewer, product]
```

In `schema.py` change defaults: `build_role: str = "architect"`, `TeamsSpec.role` default `architect`, `ChatTaskSpec.default_role` default `architect`, `roles` list without coder/tester.

- [ ] **Step 4: Run test — PASS**

- [ ] **Step 5: Commit**

```bash
git add config/roles.yaml prompts/roles/architect.md airlie.yaml src/airlie/config/schema.py tests/unit/test_config.py
git commit -m "feat: architect role owns coding and build defaults"
```

---

### Task 8: Delete `coder` and `tester` roles; remove config keys

**Files:**
- Modify: `config/roles.yaml` (delete `coder`, `tester` stanzas)
- Modify: `tests/unit/test_config.py` (remove coder-specific tests)
- Grep: entire repo for `roles.coder`, `"coder"`, `roles.tester`

**Interfaces:**
- Produces: `cfg.roles` keys must not include `coder` or `tester`; config load succeeds

- [ ] **Step 1: Write the failing test**

```python
def test_coder_and_tester_roles_removed() -> None:
    cfg = load()
    assert "coder" not in cfg.roles
    assert "tester" not in cfg.roles
```

- [ ] **Step 2: Run test — FAIL**

- [ ] **Step 3: Delete role stanzas and fix validation errors**

Remove `coder` and `tester` from `config/roles.yaml`. Fix any remaining references in yaml (supervisor, story, etc.) — grep `tester` and `coder` under `config/` and `airlie.yaml`.

- [ ] **Step 4: Run test — PASS**

- [ ] **Step 5: Commit**

```bash
git add config/roles.yaml tests/unit/test_config.py
git commit -m "refactor: remove coder and tester role definitions"
```

---

### Task 9: Two-seat amigos protocol (`product`, `architect`)

**Files:**
- Modify: `src/airlie/story/amigos.py`
- Modify: `src/airlie/flow/spec.py`
- Modify: `prompts/rules/amigos-*.md`, `prompts/steering/amigos-*.md`
- Delete: `prompts/roles/tester.md` (after prompt retarget)
- Test: `tests/unit/test_flow_spec.py`

**Interfaces:**
- Produces: `ROLES = ("product", "architect")`; `OWNER = {..., "proof": "architect"}`; `MEETINGS[0]["roles"] == ("product", "architect")`

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_flow_spec.py`, update meeting cases:

```python
def test_meeting_amigos_roles_are_product_and_architect() -> None:
    from airlie.flow.spec import MEETINGS
    assert MEETINGS[0]["roles"] == ("product", "architect")
```

Add amigos import test:

```python
def test_amigos_roles_tuple() -> None:
    from airlie.story.amigos import ROLES, OWNER
    assert ROLES == ("product", "architect")
    assert OWNER["proof"] == "architect"
```

- [ ] **Step 2: Run tests — FAIL**

- [ ] **Step 3: Implement**

`amigos.py`:

```python
ROLES: tuple[str, ...] = ("product", "architect")
OWNER: dict[Part, str] = {"requirements": "product", "changes": "architect", "proof": "architect"}
```

Update module docstrings and user-facing checker messages that say "tester" → "architect" for proof parts.

`flow/spec.py`:

```python
MEETINGS: tuple[dict[str, tuple[str, ...]], ...] = (
    {"roles": ("product", "architect"), "inputs": ("story", "layers")},
)
```

Update `prompts/rules/amigos-opening.md` and related files: replace tester seat with architect owning proof; product still never posts proof JSON.

- [ ] **Step 4: Run tests — PASS**

- [ ] **Step 5: Commit**

```bash
git add src/airlie/story/amigos.py src/airlie/flow/spec.py prompts/ tests/unit/test_flow_spec.py
git commit -m "feat: two-seat amigos with architect owning proof"
```

---

### Task 10: Pipeline package critic and proof routing without `tester`

**Files:**
- Modify: `src/airlie/story/pipeline.py`
- Modify: `src/airlie/story/epic_pipeline.py` if it references tester
- Test: `tests/integration/test_story_pipeline.py` (meeting roles fixtures)

**Interfaces:**
- Consumes: `cfg.story.architect_role` for proof/critic (same as `build_role` now)

- [ ] **Step 1: Write the failing test**

Update `tests/support/story.py` `MEETING_ROLES` to drop tester and adjust scripted replies; add unit assertion:

```python
def test_pipeline_package_critic_uses_architect(load_pipeline_fixture):
    # inspect pipeline method or cfg — no roles["tester"]
    cfg = load_pipeline_fixture.cfg
    assert "tester" not in cfg.roles
```

- [ ] **Step 2: Run targeted tests — FAIL**

- [ ] **Step 3: Implement**

Replace `self.cfg.roles.get("tester")` / `["tester"]` in `pipeline.py` and `epic_pipeline.py` with `self.cfg.roles[self.s.architect_role]` for:

- `_routed_guideline` (move QA skills to architect if `skills/qa` still needed)
- `_conventions_text`
- `_critique` docstrings and engine role name

Update `_meeting_tools` comments.

- [ ] **Step 4: Run `tests/unit/test_flow_spec.py` and relevant story tests**

- [ ] **Step 5: Commit**

```bash
git add src/airlie/story/pipeline.py src/airlie/story/epic_pipeline.py tests/support/story.py tests/integration/test_story_pipeline.py
git commit -m "refactor: package critic and proof on architect role"
```

---

### Task 11: Sweep tests and fixtures from `coder` / `generalist` / `tester`

**Files:**
- Modify: `tests/support/layer_golden.py`, `tests/support/story_layer_checks_flow.py`, `tests/unit/test_serve.py`, `tests/unit/test_opencode_integration.py`, `tests/unit/test_bridge.py`, `tests/integration/test_story_flow.py`, etc.
- Delete: `prompts/roles/coder.md`

**Interfaces:**
- Consumes: all Tasks 1–10

- [ ] **Step 1: Write a grep-driven checklist**

Run: `rg -l 'generalist|"coder"|tester' tests config docs/integrations --glob '!vendor/**'`  
Expected: list of files; each must be updated or test removed.

- [ ] **Step 2: Add regression test**

```python
def test_repo_config_has_no_generalist_coder_tester_names(load):
    cfg = load()
    assert "generalist" not in cfg.models
    assert "coder" not in cfg.roles
    assert "tester" not in cfg.roles
```

- [ ] **Step 3: Mechanical updates**

- `test_serve.py`: model `"architect"` in chat completion payloads; role ordering assertions without coder.
- `layer_golden.py`: rename scripted role keys `coder` → `architect` in fake reply maps.
- `test_opencode_integration.py`: `airlie/architect` not `airlie/generalist`; remove coder agent expectations.
- `MEETING_ROLES` in `tests/support/story.py`: two roles only.

- [ ] **Step 4: Run**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_serve.py tests/unit/test_flow_spec.py -q`  
Expected: PASS (iterate until green)

- [ ] **Step 5: Commit**

```bash
git add tests prompts/roles/coder.md
git commit -m "test: retarget fixtures from coder/generalist/tester to architect"
```

---

### Task 12: Integrations docs (OpenCode, Cursor, story)

**Files:**
- Modify: `docs/integrations/opencode.json`
- Modify: `docs/integrations/opencode/agent/coder.md` → remove or merge into architect agent
- Modify: `docs/integrations/story.md`, `docs/integrations/local-agents.md`, `docs/integrations/cursor.md`
- Test: `tests/unit/test_opencode_integration.py`

**Interfaces:**
- Produces: OpenCode agents list without coder; model id `airlie/architect`

- [ ] **Step 1: Update failing opencode test expectations** (from Task 11)

- [ ] **Step 2: Edit docs**

Document: one coding role (`architect`), product on Gemma, amigos two-seat, generation models catalog-only until tools land.

- [ ] **Step 3: Run `uv run pytest tests/unit/test_opencode_integration.py -v`**

- [ ] **Step 4: Commit**

```bash
git add docs/integrations tests/unit/test_opencode_integration.py
git commit -m "docs: integrations reflect architect model and two-seat amigos"
```

---

### Task 13: Config load migration message (optional one release)

**Files:**
- Modify: `src/airlie/config/load.py`
- Test: `tests/unit/test_config.py`

- [ ] **Step 1: Test old key rejected with clear message**

```python
def test_generalist_model_key_rejected(tmp_path, write_yaml):
    write_yaml(tmp_path / "airlie.yaml", {"models": {"generalist": {"repo": "x", "revision": "a"*40}}})
    with pytest.raises(ConfigError, match="generalist.*architect"):
        load_from(tmp_path)
```

- [ ] **Step 2: Implement rename hint in load merger** if raw yaml still contains `generalist` under models — raise `ConfigError` pointing to `architect`.

- [ ] **Step 3: Commit**

```bash
git add src/airlie/config/load.py tests/unit/test_config.py
git commit -m "feat: reject deprecated generalist model key with rename hint"
```

---

### Task 14: Full test gate

**Files:**
- All prior tasks

- [ ] **Step 1: Run full gate**

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q -n auto --maxprocesses 8
task web:gate
```

Expected: all pass

- [ ] **Step 2: Measure memory after pull (human on studio)**

```bash
task models:pull -- architect chroma_hd wan_t2v
task doctor
```

If `estimated_memory_gb` for architect/chroma/wan differs by >15% from doctor measurement, update `config/models.yaml` numbers and re-run `tests/unit/test_doctor.py`.

- [ ] **Step 3: Final commit if memory numbers adjusted**

```bash
git add config/models.yaml
git commit -m "chore: tune estimated_memory_gb from doctor after pull"
```

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Flash-Next KV / memory estimates wrong | Doctor false positives on studio | Task 14 remeasure after `models:pull` |
| Single `architect` role for plan + build | Package thinking vs build thinking conflict | Keep `think_when_red`; package path may need session-level thinking kwargs (verify in `layer_build.py` during Task 7) |
| `weights_only` not wired to serve manager | Dead code path | Explicit skip in engine up/list; tests assert no mlx-vlm command |
| Large test sweep misses a `coder` string | CI failure late | `rg` checklist in Task 11 |
| Amigos proof on architect conflicts with "architect owns changes" | Protocol confusion | Opening prompt: architect posts proof after product requirements and architect changes |
| Wan/Chroma pull size on disk | Slow CI / doctor | on_demand lifecycle; not autostart |

## Execution Notes

- Tasks 1–4 are sequential (catalog before roles).
- Tasks 5–6 can parallelize after Task 1.
- Task 7 before Task 8 (need merged architect before deleting coder stanza).
- Task 9–10 before Task 11 sweep.
- Task 13 optional but low cost.
- Suggested order: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14.

---

## Self-Review

**1. Spec coverage**

| Requirement | Task |
|-------------|------|
| No generalist; architect catalog + main engine | 1, 2, 13 |
| No Muse `reviewer` catalog; reviewer role on gemma4 | 1 (done 2026-11-13) |
| End-state catalog seven keys (no Muse) | 1, 5 |
| MTP pinned SHAs | 1 |
| Gemma 128k | 3 |
| product → gemma4 | 4 |
| Role model retargets | 4 |
| Image/video catalog + engines | 5, 6 |
| Absorb coder into architect | 7, 8 |
| Two-seat amigos | 9 |
| Delete tester | 8, 9, 10 |
| Tests/docs/opencode | 11, 12 |
| End gate | 14 |

**2. Placeholder scan:** No TBD/TODO in task steps; memory numbers are explicit provisional GB with Task 14 verification command.

**3. Type consistency:** `architect` used consistently as catalog model name and role name; `weights_only` kind introduced in Task 5 before engines reference it.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-11-13-qwen-flash-architect-gemma-product.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
