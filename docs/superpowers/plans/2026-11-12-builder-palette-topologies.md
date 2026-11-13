# Builder Palette & Topology Nodes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The workflow builder palette matches the product vision: one generic model step for ordinary LLM work, plus specialized topology steps (supervisor/worker **team**, k-independent **teams**, **fork** when stuck), alongside **check**, **gate**, and **bundle**—with YAML migration, catalog/API parity, and at least one new topology runnable end-to-end through the composer in the first shipping slice.

**Architecture:** Schema v2 gains a unified `model` node (`protocol` selects ask / conversation / write / meeting), plus new `teams` and `fork` types wired to existing topology code (`ParallelAgentTeamOrchestrator`, `ForkOrchestrator`). Legacy `ask`, `conversation`, `write`, and `meeting` remain loadable via `upgrade_topology_types()` and normalize to `model` on save/publish. **Studio is not a node type** (mlx-vlm and generic-nodes design): the story/layer workflows *are* studio; a layer's in-run studio is the `studio.{item}` **session** artifact from `story.place_layer`, consumed by catalog checks in `layer.yaml` (Part 2 as-built). The palette never offers a "Studio" tile; document that in builder copy.

**Tech stack:** Python 3.12 (`src/airlie/flow/*`, `story/flow.py`), Vue builder (`web/src/screens/builder/*`), catalog route `GET /v1/workflows/catalog`, full `AGENTS.md` gate after each task.

**Specs (read together):**

- [`docs/superpowers/specs/2026-11-06-generic-nodes-design.md`](../specs/2026-11-06-generic-nodes-design.md) — D1 "type is a protocol"; team/pair deferred in D2; layer studio split.
- [`docs/superpowers/specs/2026-11-08-nodes-are-config-design.md`](../specs/2026-11-08-nodes-are-config-design.md) — model fields, team node (D9), checks from catalog (D1).
- [`docs/superpowers/specs/2026-11-08-mlx-vlm-engine-design.md`](../specs/2026-11-08-mlx-vlm-engine-design.md) — "Team shapes as workflow nodes" table (plan 3); **studio = story workflow, not a node**.
- [`docs/superpowers/plans/2026-11-12-generic-nodes-part-2.md`](2026-11-12-generic-nodes-part-2.md) — layer build as catalog checks; do not fight Part 2 by converting layer steps to model nodes in this plan.

## Design tensions (resolved)

| Tension | Evidence | Resolution |
|--------|----------|------------|
| Seven palette types vs one generic | D1: protocol ≠ node identity in *behavior*, but builder UX can unify surface types | **`model` + `protocol`** for ask/conversation/write/meeting; runners dispatch to existing `ask_runner` / `conversation_runner` / `write_runner` / `meeting_runner` (no behavior change). |
| mlx says `fork` is `conversation` + fork policy | mlx-vlm table row for fork | **Dedicated `fork` type** in schema for palette clarity; fields = conversation fields + `ForkSpec`-aligned block (`after_failures`, `width`, `diversify`, `child_steps`, `max_rounds`, `resolve`). Runner wraps `topology/fork.py` (same code path as CLI `--topology fork`). Document as intentional UX deviation from the table. |
| `team` vs `teams` | mlx table: supervisor/worker = `team`; k-parallel = `teams` | Keep **`team`** (already in `flow/team.py`, tested). Add **`teams`** as a new type (k independent, shared LTS bank per node run). Do not rename `team` → avoid breaking YAML and tests. |
| User expected "studio" on palette | mlx: studio not a node; Part 2: `studio.{item}` session from `place` | **No `studio` NodeType.** Builder help text: "Studio topology = run the story (or open the layer bundle)." Optional follow-up: workflow `type: story` badge on canvas title, not a draggable node. |
| Part 2 layer = checks not writes | part-2 plan material #15 | This plan **does not** migrate `layer.yaml` build steps to model nodes. Palette change is orthogonal. |
| Collapse now vs palette-only | User wants plan 3 "now", early PRs | **PR1:** palette + `model`/`protocol` + legacy upgrade (vertical slice: survey still runs). **PR2:** `teams` runner + palette tile + one integration test. **PR3:** `fork` runner + palette tile. No feature flag unless PR1 risks production drafts—in that case `workflows.legacy_model_types: true` defaults false after upgrade. |

## Recommended NodeType set (target)

```python
NodeType = Literal[
    "model",    # protocol: ask | conversation | write | meeting
    "team",     # supervisor + N workers (nodes-are-config D9)
    "teams",    # k independent teams, shared memory bank (mlx plan 3)
    "fork",     # fork-when-stuck (mlx plan 3; dedicated type)
    "check",
    "gate",
    "bundle",
]
```

**Fate of old types in committed YAML:**

| Old | Becomes |
|-----|---------|
| `ask` | `type: model`, `protocol: ask` |
| `conversation` | `model` + `protocol: conversation` |
| `write` | `model` + `protocol: write` |
| `meeting` | `model` + `protocol: meeting` (role stays list) |
| `team` | unchanged |
| `check` / `gate` / `bundle` | unchanged |

**`NodeSpec` additions:**

- `protocol: Literal["ask", "conversation", "write", "meeting"] | None` — required when `type == "model"`; forbidden on other types.
- For `teams`: `k: int | str`, `stop_on_first_pass: bool`, `memory: Literal["shared", "none"]` (default `shared`), reuses role/steps/tools like a model node.
- For `fork`: embed fork policy fields (mirror `ForkSpec` in `config/schema.py` where sensible, scoped to the node).

**Validation (`check_workflows`):**

- `_MODEL_TYPES` becomes `("model", "team", "teams", "fork")` with protocol checks on `model`.
- Meeting protocol check applies when `model` + `protocol: meeting`.
- Session same-role rule (D25) applies to `model` with `protocol` in `write`, `conversation`.
- On load, legacy four types upgraded in-memory before validation (see migration).

## Behavior summary

### Generic `model` node

Same protocols as today (`runners.py`). One palette tile **Model**; NodeModal **Protocol** select drives `protocol` and shows/hides `turn`, `advisor`, meeting-fixed roles, etc. Shape: `model` (same CSS as today's model shape). Catalog `types` lists the new set.

### `team` (supervisor / workers)

Unchanged protocol (`flow/team.py`). Palette label **Team (supervisor)** to disambiguate from `teams`. Already e2e-capable in composer.

### `teams` (k-independent)

Protocol: mlx-vlm design row — k parallel `NativeTeam` (or orchestrator slice) per node invocation, one LTS bank keyed by composer run + node id, `stop_on_first_pass` cancels siblings. **Not** the same as `team`: no dynamic assignment JSON; fixed `k` from config. New module `src/airlie/flow/teams.py` adapts `ParallelAgentTeamOrchestrator.execute_task` to `RunContext` (inputs: goal text from rendered prompt, repo from loose `repo`, optional `test_cmd` from inputs).

### `fork`

Protocol: `ForkOrchestrator` with node-level policy fields. Inputs: rendered brief, repo worktree ref if declared. Outputs: branch result + optional `ref:worktree` / session per design. Palette **Fork when stuck**.

### Studio (not a node)

- **Story/layer studio topology:** the `story` workflow and `layer` bundle—what cards run today.
- **Per-layer studio session:** `place` → `studio.{item}` session artifact; builder shows `session` outputs on check nodes, not a separate topology tile.

## Global constraints

- Every task passes `AGENTS.md` gate (including `task web:gate` when touching `web/` or `config/workflows/*.yaml`).
- No edits under `vendor/superpowers/`.
- `story.composer` behavior unchanged unless a task explicitly runs equivalence tests.
- Committed `config/workflows/*.yaml` upgraded in the PR that introduces `model` (mechanical: only `survey` is still `ask` today).
- Catalog JSON fixture `web/src/test-utils/catalog.json` regenerated when catalog `types` changes.
- Commit subjects: `flow:` for schema/runners, `builder:` for UI.

## Review focus

1. **Legacy draft with `type: ask`.** Opening in builder shows Model + protocol Ask; save writes `model` or keeps legacy until publish (pick one in Task 2—recommended: normalize on save).
2. **Two topology tiles.** Palette order: Model, Team (supervisor), Teams (parallel), Fork, Check, Gate; bundles unchanged.
3. **`teams` with k=3 on FakeEngine.** Composer finishes; events include expected team ids; no cross-node LTS bleed (second node gets fresh bank).
4. **Meeting under `model`.** Amigos protocol validation still fires (`MEETINGS` in `spec.py`).
5. **Publish PR for story workflow.** Layout-only diff plus `survey` type change; comments preserved.

---

## File structure (by phase)

**Create:** `src/airlie/flow/teams.py`, `src/airlie/flow/fork_runner.py` (name TBD), `tests/unit/test_flow_model.py`, `tests/unit/test_flow_teams_node.py`, `tests/integration/test_flow_fork_node.py` (fork), `web/src/screens/builder/palette_types.ts` (palette vs wire types).

**Modify:** `src/airlie/flow/spec.py`, `src/airlie/flow/runners.py` (thin `model_runner` dispatcher), `src/airlie/flow/upgrade.py`, `src/airlie/story/flow.py` (`story_runners`), `src/airlie/serve/workflows_api.py`, `config/workflows/story.yaml`, `web/src/screens/builder/{graph.ts,Palette.vue,NodeModal.vue,shapes.ts,graph.test.ts,Palette.test.ts,NodeModal.test.ts}`, `web/src/test-utils/catalog.json`, `docs/superpowers/specs/2026-11-08-nodes-are-config-design.md` (palette as-built paragraph—optional).

---

## Phase 1 — PR1: Generic model on the palette (e2e: survey still runs)

**Ships:** User sees one **Model** tile; YAML uses `model` + `protocol`; legacy types load; `story.yaml` survey migrated; composer + builder tests green.

### Task 1: Schema — `model` and `protocol`

**Files:** `src/airlie/flow/spec.py`, `tests/unit/test_flow_spec.py`

- Extend `NodeType`; add `Protocol = Literal["ask", "conversation", "write", "meeting"]` and `NodeSpec.protocol`.
- `_requirements`: `model` needs `protocol`, role(s), prompt per protocol (mirror current per-type rules).
- `_wrong_fields`: treat `model` as model node; reject `protocol` on non-model types.
- `_prompt_file` / meeting check: gate on `model` + `protocol == "meeting"`.
- Keep accepting legacy types in `NodeType` **or** map in validator `mode="before"` only—**recommended:** legacy types remain in `Literal` for one release, deprecated in catalog docs string; `check_workflows` warns `"node X: type ask is deprecated; use model with protocol ask"`.

**Acceptance:**

```bash
uv run pytest tests/unit/test_flow_spec.py -q
uv run mypy
```

### Task 2: Upgrade + committed YAML

**Files:** `src/airlie/flow/upgrade.py`, `config/workflows/story.yaml`, `tests/unit/test_flow_upgrade.py`

- `upgrade_topology_types(mapping) -> mapping` (or extend `upgrade_v1`): rewrite the four legacy types to `model` + `protocol`.
- Apply to `story.yaml` `survey` node only in repo.
- Draft upgrade path: builder `toYaml` calls normalizer before emit (Task 4).

**Acceptance:**

```bash
uv run pytest tests/unit/test_flow_upgrade.py -q
python -c "from airlie.config import load_config; load_config('airlie.yaml')"  # from repo root
```

### Task 3: `model_runner` + registry

**Files:** `src/airlie/flow/runners.py`, `src/airlie/story/flow.py`, `tests/integration/test_story_flow.py` (parametrize one survey path if needed)

- `async def model_runner(node, ctx, ...)` dispatches on `node.protocol` to existing four runners.
- `story_runners`: register `"model": model_runner`; keep legacy keys pointing to same partials for one release **or** remove legacy keys once YAML migrated—**recommended:** single `model` key only after Task 2.

**Acceptance:**

```bash
uv run pytest tests/integration/test_story_flow.py -q -k survey
uv run pytest tests/unit/test_flow_composer.py -q -k model
```

### Task 4: Builder palette + graph types

**Files:** `web/src/screens/builder/graph.ts`, `Palette.vue`, `NodeModal.vue`, `shapes.ts`, tests, `web/src/test-utils/catalog.json`

- `PALETTE_TYPES = ["model", "team", "check", "gate"]` (teams/fork added in later PRs); wire `NODE_TYPES` / `NodeType` to full backend list.
- `defaults("model")`: `{ protocol: "ask", role: "", prompt: "", inputs: [], outputs: {} }`.
- `addNode(..., "model", ...)`.
- `NodeModal`: protocol selector; field visibility matches old per-type forms.
- Box card shows `model (ask)` or protocol label.
- Regenerate `catalog.json` from `uv run airlie workflows catalog` (or project task).
- Update `graph.test.ts` / `Palette.test.ts` expectations.

**Acceptance:**

```bash
task web:gate
```

### Task 5: Catalog API

**Files:** `src/airlie/serve/workflows_api.py`, `tests/unit/` (route test if present)

- `types` in catalog response = new `NodeType` list (include deprecated aliases in `deprecated_types` field if useful for old drafts).

**Acceptance:**

```bash
uv run pytest -q -k workflows_catalog
```

**PR1 definition of done:** Palette shows single Model tile; `config/workflows/story.yaml` survey is `model`; legacy drafts open; full gate green.

---

## Phase 2 — PR2: `teams` topology node (first new topology e2e)

**Ships:** **Teams (parallel)** palette tile; minimal workflow in tests (not necessarily shipped story/layer).

### Task 6: Schema + validation for `teams`

**Files:** `src/airlie/flow/spec.py`, `tests/unit/test_flow_spec.py`

- Fields: `k`, `stop_on_first_pass`, `memory`, plus model-like `role`, `prompt`, `steps`, `tools`, `inputs`, `outputs`.
- Validation: `k >= 1`; outputs include at least one structured result.

### Task 7: `teams_runner`

**Files:** `src/airlie/flow/teams.py`, `src/airlie/story/flow.py`, `tests/unit/test_flow_teams_node.py`, `tests/integration/` (one composer test with `FakeEngine` and k=2)

- Adapt `ParallelAgentTeamOrchestrator` with scoped `run_id` / bank id = `f"{ctx.run_id}:{node_id}"`.
- Register in `story_runners`.
- Prove concurrent teams do not require `--topology teams` on CLI.

**Acceptance:**

```bash
uv run pytest tests/unit/test_flow_teams_node.py tests/integration/test_flow_teams_node.py -q
```

### Task 8: Builder for `teams`

**Files:** `Palette.vue`, `NodeModal.vue`, `graph.ts`, tests

- Palette tile with `data-palette-type=teams`; form fields for `k`, `stop_on_first_pass`, `memory`.
- Shape: still `model` styling or distinct border (designer call: dashed border + "k" badge in `BoxCard`).

**Acceptance:**

```bash
task web:gate
```

---

## Phase 3 — PR3: `fork` topology node

### Task 9: Schema for `fork`

**Files:** `src/airlie/flow/spec.py`, `tests/unit/test_flow_spec.py`

- Fork policy fields on `NodeSpec` (only when `type == "fork"`).
- `protocol`-like: treat as model node for prompt/role/steps/tools.

### Task 10: `fork_runner`

**Files:** `src/airlie/flow/fork_runner.py`, `story/flow.py`, `tests/integration/test_flow_fork_node.py`

- Wire `ForkOrchestrator`; map outputs to declared `outputs`.
- Reuse dialect/engine from `RunContext` (no profile restart).

**Acceptance:**

```bash
uv run pytest tests/integration/test_flow_fork_node.py -q
```

### Task 11: Builder for `fork`

**Files:** palette + modal + tests

**Acceptance:** `task web:gate`

---

## Phase 4 — Follow-ups (not blocking "palette now")

| Item | Notes |
|------|--------|
| Remove deprecated `ask`…`meeting` from `NodeType` Literal | After 30-day or next breaking window; upgrade only. |
| Sample workflow using `teams` on board | Dogfood card + draft workflow YAML in docs, not necessarily `story.yaml`. |
| Convert interim `story.refine` / `plan_layers` checks to `model` nodes | Generic nodes follow-up per Part 2 scope note #22—not this plan. |
| mlx concurrency gate for k teams on one engine | Already engine plan; teams node should respect `max_num_seqs` errors gracefully. |
| `pair` type | Still unnamed in generic design; defer. |

---

## Risks & mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| `model` migration breaks in-flight snapshots | Cards fail mid-run | Snapshots store normalized spec at start; upgrade only on load for drafts; `story` snapshot hash test in Task 2 |
| `teams` runner diverges from CLI orchestrator | Wrong events | Share `ParallelAgentTeamOrchestrator` code; one integration golden |
| Fork node vs conversation policy duplication | Two configs drift | Single `ForkSpec` parser shared with `schema.py` |
| Users still expect Studio tile | Confusion | In-palette subtitle + AGENTS.md/workflows line |
| Part 2 check-only layer | Palette looks "code-heavy" | Expected; Model tile still used on `survey` and future model migrations |

---

## Execution notes

- **Parallelize:** Task 4 (web) and Task 3 (runners) after Task 1; Task 2 blocks committed YAML.
- **Executor:** Appa for phased plans; Katara for test/fix only.
- **Do not** implement studio as NodeType without amending mlx-vlm and nodes-are-config specs first.

---

## Self-review (momus)

- Referenced files exist on main (`spec.py`, `graph.ts`, `layer.yaml`, `team.py`, mlx design § topology table).
- PR1 acceptance is agent-executable (pytest + task web:gate).
- Studio contradiction called out with spec citation.
- Smallest vertical slice: PR1 palette + survey path; PR2 first special topology `teams`.
- Blocking questions: **none** — default path above is shippable; optional Noah call: deprecate legacy types in catalog warnings vs silent upgrade (Task 1 recommends warnings).
