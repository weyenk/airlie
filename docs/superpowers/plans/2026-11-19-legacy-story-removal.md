# Legacy Story Removal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete the hand-written story pipeline and its engine (`airlie.flow` and everything wired to it); replace with config-driven generic nodes covering every multi-agent orchestration pattern.

**Architecture:** A new `src/airlie/workflow/` package replaces `src/airlie/flow/` and `src/airlie/story/`'s pipeline modules. **Four node kinds**, each with distinct configuration because the runtime differs:
- `WORK` — single model call (`prompt`/`prompt_id`) OR a shell command (`run`); gate evidence (`gate`) lives on this node.
- `ROUND_ROBIN` — agents take turns in sequence (three-amigos, AutoGen teams).
- `SUPERVISOR_WORKERS` — supervisor splits task into N chunks; workers each run one in parallel; supervisor assembles.
- `K_INDEPENDENT_TEAMS` — K independent teams run the same task; `aggregate` picks winner.

**Out of scope (kept in the design, to be defined in later plans):** Coordinator, Review/Critique, Iterative Refinement, Hierarchical Task Decomposition, Swarm, ReAct — each folds into one of the four by editing the prompt.

**Tech Stack:** Python 3.12, Pydantic (validation), typer (CLI), uv, Taskfile.

**Spec:** `docs/superpowers/specs/2026-11-19-legacy-story-removal-design.md`

## Global Constraints

- Four node kinds; if an edge case seems to need a fifth kind, fold it into one of the four by editing the prompt.
- Gates are evidence (`gate:` on `WORK`), not a node kind.
- Conditions and loops live on wires (edges); any node may loop back to any earlier node.
- An unmet condition must declare where the second path goes; no silent dead ends.
- Lines only route: carry a condition and the artifacts their source produces.
- No VM or container engine; repositories are built in git worktrees under the run directory.
- Costs and checks face outward: reporting, not workflow steps.
- One trigger kind — chat "Start story", serve beat, launchd every 15 min, `task story:tick`, and any workflow a card names all call the same `start_workflow`.
- `src/airlie/teams/` and `src/airlie/topology/` are used *by* the runner code as helpers, not deleted.
- `src/airlie/story/` modules that are data or runtime helpers stay until the rebuild lands.

## Review Focus

1. **K_INDEPENDENT with `teams: 1`** — validate as one-team execution, don't error.
2. **ROUND_ROBIN with only one role** — must error at load; can't have single-agent round-robin.
3. **`when` conditions that all resolve false** — must stall, not silently skip.
4. **`gate: {type: script, cmd: "..."}` that returns non-zero** — the work is not done; don't advance.
5. **Two triggers racing to run same workflow+card** — the second refuses on worktree lock.

---

## Task 1: Define the four-node-kind model and wire-edge types

**Files:**
- Create: `src/airlie/workflow/__init__.py`
- Create: `src/airlie/workflow/spec.py`
- Test: `tests/unit/test_workflow_spec.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `NodeKind = Literal["WORK", "ROUND_ROBIN", "SUPERVISOR_WORKERS", "K_INDEPENDENT_TEAMS"]`
  - `GateEvidence = ScriptEvidence | PromptJudgment | StepCount` (union — `type: "script"`/`"prompt"`/`"steps"`; lives on `NodeSpec`, not its own kind)
  - `NodeSpec` (union: `WorkNode`, `RoundRobinNode`, `SupervisorWorkersNode`, `KIndependentTeamsNode`, all discriminated by `kind`)
  - `WireSpec` (`target: str`, `when: str | None = None`, `loop: LoopBound | None = None`, `carries: list[str]`)
  - `WorkflowSpec` (`title: str`, `inputs: list[str]`, `nodes: dict[str, NodeSpec]`, `edges: dict[str, list[WireSpec]]`)
  - `validate_workflow(raw: dict) -> list[str]` — returns strings describing problems; empty if valid.
  - `load_workflow(path: Path) -> WorkflowSpec` — parse YAML, validate against `WorkflowSpec`.

**Node semantics:**
- `WORK` — either `prompt: str` / `prompt_id: str` (single model call) or `run: str` (shell command to execute in the run directory). Has `inputs`, `outputs`, `steps`, optional `gate: GateEvidence`.
- `ROUND_ROBIN` — `roles: list[str]` (≥ 2 role names), `prompt: str`, `outputs`, `steps`, `agreement_when: str`. Runs one conversation among those roles until `agreement_when` is satisfied.
- `SUPERVISOR_WORKERS` — `supervisor: str`, `worker: str`, `workers: int`, `prompt: str`, `outputs`, `steps`. Supervisor splits into N chunks; workers each run one; supervisor assembles.
- `K_INDEPENDENT_TEAMS` — `teams: int`, `roles: list[str]`, `prompt: str`, `outputs`, `aggregate: "test-score" | "llm"`, `test_cmd: str | None`. K teams run same task in parallel; aggregate picks winner. `teams == 1` must fall through to single-team execution.

- [ ] **Step 1: Write the failing test — `tests/unit/test_workflow_spec.py`**

```python
from airlie.workflow.spec import validate_workflow


def test_rejects_unknown_node_kind():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "gate", "prompt": "x"}},
           "edges": {"a": []}}
    assert any("kind" in e.lower() for e in validate_workflow(raw))


def test_allows_edge_loop_to_earlier_node():
    raw = {
        "title": "loop", "inputs": [],
        "nodes": {"a": {"kind": "WORK", "prompt": "do"},
                  "b": {"kind": "WORK", "prompt": "check", "inputs": ["x"]}},
        "edges": {"a": [{"target": "b"}],
                  "b": [{"target": "a", "when": "not done", "loop": {"max": 3}}]},
    }
    assert validate_workflow(raw) == []


def test_work_runs_shell_command():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "WORK", "run": "uv run pytest -q", "outputs": {"out": {"format": "text"}}}},
           "edges": {"a": []}}
    assert validate_workflow(raw) == []


def test_round_robin_three_amigos():
    raw = {"title": "amigos", "inputs": ["story", "layers"],
           "nodes": {"agree": {"kind": "ROUND_ROBIN",
                               "roles": ["product", "architect", "tester"],
                               "prompt": "agree on {story}",
                               "agreement_when": "agreement exists",
                               "outputs": {"agreement": {"format": "json"}},
                               "steps": 12}},
           "edges": {"agree": []}}
    assert validate_workflow(raw) == []


def test_round_robin_needs_two_roles():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "ROUND_ROBIN", "roles": ["solo"], "prompt": "hi",
                            "agreement_when": "done", "outputs": {}}},
           "edges": {"a": []}}
    assert validate_workflow(raw), "one-role round_robin must error"


def test_supervisor_workers_configures_correctly():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "SUPERVISOR_WORKERS", "supervisor": "arch", "worker": "code",
                            "workers": 5, "prompt": "do", "inputs": ["x"],
                            "outputs": {"built": {"format": "json"}}}},
           "edges": {"a": []}}
    assert validate_workflow(raw) == []


def test_supervisor_workers_with_zero_workers_invalid():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "SUPERVISOR_WORKERS", "supervisor": "arch", "worker": "code",
                            "workers": 0, "prompt": "do", "outputs": {}}},
           "edges": {"a": []}}
    assert validate_workflow(raw)


def test_k_independent_teams_validates():
    raw = {"title": "kin", "inputs": [],
           "nodes": {"try": {"kind": "K_INDEPENDENT_TEAMS", "teams": 3, "roles": ["architect"],
                              "prompt": "solve", "outputs": {"winner": {"format": "json"}},
                              "aggregate": "test-score", "test_cmd": "uv run pytest"}},
           "edges": {"try": []}}
    assert validate_workflow(raw) == []


def test_k_independent_with_one_team_falls_through():
    raw = {"title": "kin", "inputs": [],
           "nodes": {"try": {"kind": "K_INDEPENDENT_TEAMS", "teams": 1, "roles": ["architect"],
                              "prompt": "solve", "outputs": {"winner": {"format": "json"}},
                              "aggregate": "test-score", "test_cmd": "uv run pytest"}},
           "edges": {"try": []}}
    assert validate_workflow(raw) == []  # single team is fine, not error


def test_dead_end_condition_must_declare_alternative():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "WORK", "prompt": "x", "inputs": [], "outputs": {"r": {"format": "bool"}}},
                     "b": {"kind": "WORK", "prompt": "y", "inputs": ["r"]}},
           "edges": {"a": [{"target": "b", "when": "false"}]}}  # no else-edge
    assert validate_workflow(raw), "unmet condition needs another path"


def test_edge_to_undeclared_node_invalid():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "WORK", "prompt": "x"}},
           "edges": {"a": [{"target": "ghost"}]}}
    assert validate_workflow(raw)


def test_graph_must_reach_every_node():
    raw = {"title": "t", "inputs": [],
           "nodes": {"a": {"kind": "WORK", "prompt": "do"}, "orphan": {"kind": "WORK", "prompt": "x"}},
           "edges": {"a": []}}
    assert validate_workflow(raw), "unreachable node must be rejected"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_workflow_spec.py -v`
Expected: FAIL — module `airlie.workflow` does not exist.

- [ ] **Step 3: Implement `src/airlie/workflow/__init__.py` and `src/airlie/workflow/spec.py`**

`spec.py` defines: `NodeKind` (`Literal["WORK", "ROUND_ROBIN", "SUPERVISOR_WORKERS", "K_INDEPENDENT_TEAMS"]`), the four Pydantic node models (`WorkNode`, `RoundRobinNode`, `SupervisorWorkersNode`, `KIndependentTeamsNode`), `NodeSpec` as their `Annotated[Union[...], Field(discriminator="kind")]`, `WireSpec`, `LoopBound`, `WorkflowSpec`, and `validate_workflow`.

Validation must reject: unknown `kind`; `WORK` nodes without `prompt`/`prompt_id`/`run`; `ROUND_ROBIN` with <2 roles; `SUPERVISOR_WORKERS` with 0 workers; `K_INDEPENDENT_TEAMS` with <2 teams; edges to undeclared nodes; graphs where a node is unreachable; wires whose `when` has no `else` alternative.

`K_INDEPENDENT_TEAMS` with `teams == 1` validates as a single-team execution (works like `WORK`, not error).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_workflow_spec.py -v`
Expected: All 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/workflow/__init__.py src/airlie/workflow/spec.py tests/unit/test_workflow_spec.py
git commit -m "feat(workflow): four-node-kind model"
```

---

## Task 2: Build the generic runner for all four kinds

**Files:**
- Create: `src/airlie/workflow/runner.py`
- Test: `tests/unit/test_workflow_runner.py`

**Interfaces:**
- Consumes: `NodeSpec`, `WireSpec`, `WorkflowSpec` from Task 1's `spec.py`; `TeamOrchestrator`, `NativeTeam`, `AutoGenTeam`, `TeamContext`, `TeamResult`, `StepObserver`, `TestScoreAggregator`, `LLMAggregator` from `airlie.teams.*`; `AirlieConfig` from `airlie.config`; `Engine` from `airlie.engine.protocol`.
- Produces:
  - `RunContext` (dataclass: `cfg: AirlieConfig`, `engine: Engine`, `workdir: Path`, `inputs: dict[str, Any]`, `node: str`, `item: str | None`, `sessions: dict[str, Session]`, `held: SessionStack`)
  - `WorkflowResult` (dataclass: `outputs: dict[str, Any]`, `events: list[dict[str, Any]]`)
  - `async run_workflow(spec: WorkflowSpec, ctx: RunContext) -> WorkflowResult`

**Runner design note:** `run_workflow` walks the node graph via edges: find ready nodes, run WORK nodes (single engine call or shell), run `ROUND_ROBIN` via `NativeTeam`/`AutoGenTeam` until `agreement_when`, run `SUPERVISOR_WORKERS` by implementing supervisor-assigns/N-workers-parallel/supervisor-assembles, run `K_INDEPENDENT_TEAMS` by running K teams in parallel and aggregating.

**Constraint:** NOTHING in the runner may import from `airlie.flow.*`. Reuse `airlie.teams/*` and `airlie.topology/*`; reimplement the `team_runner` logic inline.

- [ ] **Step 1: Write the failing test — `tests/unit/test_workflow_runner.py`**

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from airlie.workflow.spec import WorkflowSpec, NodeSpec, WireSpec, FormatSpec, LoopBound
from airlie.workflow.runner import RunContext, WorkflowResult, run_workflow


def _make_spec():
    """A two-node WORK workflow: 'do' → 'check'."""
    return WorkflowSpec(
        title="simple",
        inputs=["card"],
        nodes={
            "do": NodeSpec(kind="WORK", prompt="do {card}", outputs={"result": FormatSpec(format="text")}),
            "check": NodeSpec(kind="WORK", prompt="check {result}", inputs=["result"],
                             outputs={"verified": FormatSpec(format="bool")}),
        },
        edges={
            "do": [WireSpec(target="check", carries=["result"])],
            "check": [],
        },
    )


def test_run_workflow_work_node_produces_output():
    spec = _make_spec()
    engine = MagicMock()
    engine.generate = AsyncMock(return_value="done")
    ctx = RunContext(cfg=MagicMock(), engine=engine, workdir=MagicMock(),
                     inputs={"card": "do stuff"}, node="", item=None, sessions={}, held=MagicMock())
    result = asyncio.run(run_workflow(spec, ctx))
    assert "result" in result.outputs


def test_run_workflow_loop_retries_then_stops():
    """A wire with loop.max=2 back to an earlier node runs at most 3 tries."""
    ...


def test_run_workflow_dead_end_raises():
    """An edge with when=False whose alternative is absent raises Stalled."""
    ...


def test_supervisor_workers_with_single_worker():
    """ONE supervisor works through tasks."""
    ...


def test_k_independent_teams_picks_winner():
    """K independent teams run same task; aggregate picks winner."""
    ...
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/test_workflow_runner.py -v`
Expected: FAIL — `runner.py` does not exist.

- [ ] **Step 3: Implement `src/airlie/workflow/runner.py`**

`async run_workflow` must:
1. Find start nodes (no inbound edges), and iteratively run ready nodes.
2. WORK nodes: if `prompt`/`prompt_id` → call engine; if `run` → `asyncio.create_subprocess_shell`.
3. ROUND_ROBIN nodes: use `NativeTeam`/`AutoGenTeam` with a `TeamContext`.
4. SUPERVISOR_WORKERS nodes: implement supervisor→workers→synthesis inline (migrate from `airlie/flow/team.py:team_runner`).
5. K_INDEPENDENT_TEAMS nodes: build K `TeamContext`s, run in parallel via `asyncio.gather`, then aggregate (`TestScoreAggregator`/`LLMAggregator`) to pick winner.
6. Handle `loop` wires (`loop.max` counting), `Stalled` when nothing can start.

`RunContext`, `WorkflowResult` per Interfaces.

- [ ] **Step 4: Run to verify tests pass**

Run: `uv run pytest tests/unit/test_workflow_runner.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/workflow/runner.py tests/unit/test_workflow_runner.py
git commit -m "feat(workflow): generic runner; all four node kinds implemented"
```

---

## Task 3: One trigger seam — `start_workflow`

**Files:**
- Create: `src/airlie/workflow/trigger.py`
- Create: `src/airlie/workflow/worktree.py`
- Modify: `src/airlie/workflow/__init__.py` (re-export `start_workflow`)
- Test: `tests/unit/test_workflow_trigger.py`

**Interfaces:**
- Consumes: `run_workflow` from Task 2, `WorkflowSpec` from Task 1.
- Produces:
  ```python
  async def start_workflow(
      workflow_name: str, *, card: dict, config: AirlieConfig, engine: Engine, workdir: Path
  ) -> WorkflowResult: ...
  ```
  Single entry point called by chat slash-command, card button, and hub job bus.

- [ ] **Step 1: Write the failing test — `tests/unit/test_workflow_trigger.py`**

```python
def test_start_workflow_locks_workdir():
    """Two concurrent starts for same workflow+card: second refuses."""
    ...

def test_start_workflow_rejects_unknown_workflow_name():
    """If the YAML doesn't exist, raises KeyError."""
    ...

def test_all_three_triggers_call_same_function():
    """Chat slash-command, card, and hub use the same seam."""
    ...
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_workflow_trigger.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `src/airlie/workflow/trigger.py` and `src/airlie/workflow/worktree.py`**

`async start_workflow` loads the workflow from `config/workflows/<name>.yaml`, validates it, creates/locks a git worktree under the run directory, builds `RunContext`, calls `run_workflow`. If locked, return `WorkflowResult(events=[{"kind": "already_running"}])`. If YAML not found, raise `KeyError`.

`worktree.py` uses existing `airlie.teams.worktree` helpers — `add_worktree`/`remove_worktree`/`rev_parse`/`commit_wip`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_workflow_trigger.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/workflow/trigger.py src/airlie/workflow/worktree.py src/airlie/workflow/__init__.py tests/unit/test_workflow_trigger.py
git commit -m "feat(workflow): single start_workflow trigger for chat/card/hub"
```

---

## Task 4: Rebuild workflow configs using only the four kinds

**Files:**
- Delete: `config/workflows/story.yaml` (old)
- Delete: `config/workflows/layer.yaml` (old)
- Create: `config/workflows/story.yaml` (new, WORK + ROUND_ROBIN + SUPERVISOR_WORKERS)
- Create: `config/workflows/layer.yaml` (new, only WORK)

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: Configs that load and validate against the new `WorkflowSpec`. Both `story` and `layer` use only the four kinds with `when`/`loop` on wires.

- [ ] **Step 1: Rewrite `config/workflows/story.yaml`** — replace every `type: check` with `kind: WORK` and `run:`; replace `type: model` with `kind: WORK` and `prompt:`; replace `type: meeting` with `kind: ROUND_ROBIN` and roles `[product, architect, tester]`; replace `type: bundle` (running `layer` per item) — say whether the story now ends when `layer` builds all pieces from chat; either keep a `kind: WORK` that runs `airlie run --workflow layer` per item, or delete layer entirely.

- [ ] **Step 2: Rewrite `config/workflows/layer.yaml`** — all nodes were `type: check`; change to `kind: WORK` with `run:` commands.

- [ ] **Step 3: Validate both files** — run `uv run python -c "from airlie.workflow.spec import load_workflow, validate_workflow; from pathlib import Path; assert validate_workflow(load_workflow(Path('config/workflows/story.yaml'))), 'FAIL'"` and same for `layer.yaml`.

- [ ] **Step 4: Commit**

```bash
git add config/workflows/
git commit -m "feat(workflow): rebuild configs on four kinds"
```

---

## Task 5: Delete old engine; fix remaining imports

**Files:**
- Delete: `src/airlie/flow/` (all files)
- Modify: `src/airlie/config/schema.py` — change import from `airlie.flow.spec` to `airlie.workflow.spec`; replace `check_workflows` with `validate_workflow`.
- Modify: `src/airlie/serve/flow_nodes.py`, `src/airlie/serve/workflows_api.py`, `src/airlie/serve/run_flow.py`, `src/airlie/serve/flow.py` — fix imports to `airlie.workflow`.
- Modify: Any remaining `src/` or `tests/` file that imports `airlie.flow.*` — fix or delete.

**Interfaces:**
- Consumes: Task 4.
- Produces: Zero files in `src/` or `tests/` import `airlie.flow.*`.

- [ ] **Step 1: Grep for remaining broken imports** — `grep -rn 'from airlie\.flow\|import airlie\.flow' src/ tests/ --include='*.py' | grep -v __pycache__` — Expected: empty.

- [ ] **Step 2: Run tests and fix until green** — `uv run pytest tests/ -q --ignore=tests/unit/quarantine --maxfail=3` — Expected: all remaining tests pass.

- [ ] **Step 3: Verify the gate** — `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "feat: delete airlie.flow engine; fix all remaining imports"
```

---

## Task 6: Delete story pipeline plumbing; unify Taskfile and CLI

**Files:**
- Delete: `src/airlie/story/pipeline.py`, `src/airlie/story/flow.py`, `src/airlie/story/factory.py`, `src/airlie/story/epic_pipeline.py`, `src/airlie/story/initiative_pipeline.py`, `src/airlie/story/workflow_publish.py`
- Modify: `src/airlie/cli.py` — delete `story_run`, `line_tick`, `story_tick`, `story_restack`, `story_restart`, `story_adopt_workflow`, `story_pr_body`, `story_describe`; delete `run` (topology) and `studio`; add `run` calling `start_workflow`; delete `story_app`, `line_app`; delete Taskfile targets `story`, `story:tick`, `story:restack`, `story:restart`, `story:describe`, `story:steps`, `story:check`, `line:tick`, `line:stats`, `line:pause`, `line:resume`, `line:why`, `line:deploy`; add `run` → `airlie run -- "..."`.
- Modify: `Taskfile.yml` — same deletion of `story:*` and `line:*` targets; add `run`.

**Interfaces:**
- Consumes: Task 3's `start_workflow`.
- Produces: Taskfile has `task run`, `task serve` which use `airlie run`; no `task story*`/`task line:*`; CLI has no `airlie story`/`airlie line` commands.

- [ ] **Step 1: Delete all story pipeline modules** — remove `pipeline.py`, `flow.py`, `factory.py`, `epic_pipeline.py`, `initiative_pipeline.py`, `workflow_publish.py`.

- [ ] **Step 2: Remove `airlie story` and `airlie line` commands from `cli.py`** — delete handlers and typer apps.

- [ ] **Step 3: Replace `airlie run` and `airlie studio` with single `airlie run`** — `async start_workflow(workflow=<first_arg>, ...)`. Same default if no arg.

- [ ] **Step 4: Remove container/VM from Taskfile** — delete `test:*` targets that use container engine and `searxng:*` targets.

- [ ] **Step 5: Remove dead test/support files** — anything in `tests/support` or `tests/unit` referencing deleted modules.

- [ ] **Step 6: Run full gate** — Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/airlie/story/ src/airlie/cli.py Taskfile.yml
 git commit -m "feat: delete story pipeline; unify Taskfile and CLI on run"
```

---

## Task 7: Final cleanup of remaining files and tests

**Files:**
- Modify: Any remaining file that imported from deleted modules.

- [ ] **Step 1: Verify no remaining imports**

Run: `grep -rn 'from airlie.story.pipeline\|from airlie.story.flow\|from airlie.story.factory' src/ tests/ --include='*.py' | grep -v __pycache__`
Expected: empty.

- [ ] **Step 2: Verify the test suite passes**

Run: `uv run pytest -q -n auto --maxprocesses 8`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "chore: remove last references to deleted modules"
```

---

## Status (built 2026-11-19, branch `feature/legacy-story-removal`)

The new engine is built, tested, and running independently of the old `airlie.flow`. Full gate green (5102 passed, ruff clean).

- **Task 1** (spec) — DONE, `4eadae22`. `airlie.workflow.spec`: four kinds + `WireSpec`/`LoopBound`/`GateEvidence`/`WorkflowSpec` + `validate_workflow`/`load_workflow`; `conditions.py` copied. 12 tests.
- **Task 2** (runner) — DONE, `f1dfb840`. `airlie.workflow.runner`: `RunState`, the `_Run` dataflow scheduler, one runner per kind, `Stalled`/`StoryBlocked`, `run_workflow`. 10 tests: forward fire/die + downstream die, retry-loop to bound, failed+block, stall on unproducible input, all four kinds.
- **Task 3** (trigger) — DONE, `3f3e1e92`. `airlie.workflow.trigger.start_workflow` + `airlie.workflow.worktree` lock. 3 tests: runs + releases lock, refuses when locked (already_running), KeyError on unknown name.

## Re-plan of Tasks 4-7 (measured blast radius)

The deletion pass is one coupled **frontend + backend** change, not "fix imports." Switching `config.schema` (which types `config.workflows` as `airlie.flow.spec.WorkflowSpec`) to the new spec **forces** three things at once: (a) rewrite `config/workflows/{story,layer}.yaml`, (b) rewrite the **serve web-builder**, (c) delete `airlie.flow`. They land together.

Measured surface:
- `airlie.flow` engine importers (7 src): `config/schema.py`, `serve/flow_nodes.py`, `serve/workflows_api.py`, `story/{workflow_publish,flow,pipeline,workflows}.py`; plus ~22 old-engine test files (most deleted).
- Web-builder: `serve/flow_nodes.py` (186 lines), `serve/workflows_api.py` (imports `DEPRECATED_NODE_TYPES`, `MEETINGS`, `Format`, `NodeType`, `old_schema_problems`, `upgrade_v1`, `STORY_CATALOG`), and the React `web/src` (references `"model"/"check"/"gate"/"bundle"/"meeting"` across many `.vue`/`.ts`).
- `airlie.story` (126 importers): only the 6 pipeline modules are deleted; the rest of the package stays.

### Revised order (each step ends gate-green)

- **Task 4a:** Rewrite `config/workflows/{story,layer}.yaml` to the four kinds.
- **Task 4b:** Switch `config.schema` to `airlie.workflow.spec`; rewrite `serve/flow_nodes.py` + `serve/workflows_api.py` + the React builder for four kinds. *The big one.*
- **Task 5:** Delete `src/airlie/flow/` + its tests; fix the surviving importers.
- **Task 6:** Delete the 6 story pipeline modules; rewire the CLI (`story`/`line`/`run`/`studio` → one `airlie run` → `start_workflow`) + Taskfile.
- **Task 7:** Final cleanup + full gate (`ruff`/`mypy`/`pytest`/`task web:gate`).

### Open design question (the spec is silent — needs a call before 4b)

The spec says "nothing on a screen changes; this retires plumbing, not visuals," but the node kinds go nine → four. How should the web builder's node palette and shape rendering map to the four kinds? Rebuild the palette to four, or keep the nine as prompt-level variations that fold in? That answer determines the size of Task 4b.

---

## Session status — 2026-11-19 (recovered after a corrupted agent session)

This plan file was restored from cline checkpoint `3453840b`; the previous agent had
clobbered it with scratch text mid-edit and died debugging a web test. Recovered state,
verified against the working tree (not from the corrupted session's claims):

- Task 5 is **in flight**: `web/src/screens/builder/` was rebuilt (WIP commit
  `9b21d67 fix(web): BuilderScreen.test.ts asserts on the current tree` — do not
  re-do) plus ~10 uncommitted files (builder tests rewritten green:
  `graph.test.ts`, `yaml.test.ts`, `graph.edit.test.ts`, `BuilderBox.test.ts`,
  `Palette.test.ts`, plus `NodeModal.vue`/`graph.ts`/`useBuilder.ts` tweaks).
- **web tests are red at HEAD *and* with the WIP — same 87 failures.** The
  remaining red is four test files still asserting the pre-modal-rewrite contract:
  `builder/Canvas.test.ts` (19), `screens/builder/NodeModal.test.ts` (19),
  `builder/WireModal.test.ts` (~6, run-to-run flaky), `BuilderScreen.test.ts` (53+).
  Verified cause on Canvas: fixtures make nodes without `kind`, which now 400s
  ("workflow w: node a has no kind") so no boxes render. These are stale tests
  against a deliberate redesign, not regressions — update them to the current
  contract (lowercase ids everywhere, modal-based editing) file by file.
- Debug litter from the dead session was removed (`web/dbg.py`; a `SHOWME` probe
  in BuilderScreen.test.ts:643 — the restore also restored the assertion it had
  clobbered; re-verify that test when porting BuilderScreen.test.ts).

Next step, in order: port `Canvas.test.ts` fixtures to give every node a kind;
then NodeModal/WireModal/BuilderScreen against the modal contract. Gate per AGENTS.md:
`task web:gate` then the python gates (untouched by this chunk). Then Task 5 commits,
and before any prompt-hub work (Task 4b) the open model-profile decision needs Noah.

