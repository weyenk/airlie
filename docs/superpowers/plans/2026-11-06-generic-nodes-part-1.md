# Generic Nodes, Part 1: Composer and the Story Workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The story pipeline runs as a workflow file of typed nodes through a composer, behind `story.composer`, producing the same events and state as today's driver.

**Architecture:** A new `airlie.flow` package holds the node contract (`spec.py`), the artifact store over `StoryState` (`store.py`) and the interpreter (`composer.py`). Story-specific pieces (checks, briefs, parsers, hooks) register by name in `airlie.story.flow`; the workflow YAML under `config/workflows/` names them. The build goes in as a strangler: first every node runner calls today's `StoryPipeline` method by node name, then each type's generic loop is lifted into its runner one type at a time, with the existing end-to-end tests run with the flag on after every task.

**Tech Stack:** Python 3.12, uv, pydantic `Strict` config models, pytest with `FakeEngine`, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-11-06-generic-nodes-design.md`

**Part 2** (a separate plan, written after this one lands): the studio split that dissolves the layer build into write, done-check, review, fix, final gate, follow-up and squash nodes, the advisor on the write node, the gate-advice ask, the bench trace diff, and the flag flip. Until Part 2, the layer build is one `write` node that wraps `_build_once`. The design's finish line is reached at the end of Part 2.

## Global Constraints

- Every config model extends `Strict` (`ConfigDict(extra="forbid")`, `src/airlie/config/schema.py:15`). New keys must be declared fields.
- `story.composer` defaults to `False`. With it off, `_drive` is byte-for-byte today's code path.
- No prompt, brief, role, model or limit changes. A runner calls the same brief builder with the same arguments the method does today.
- The same `events.jsonl` kinds and fields in the same order, plus only `node_started`, `node_finished` and `node_skipped`.
- The same `state.json` fields. Artifact names are `StoryState` field names (`src/airlie/story/state.py:56-80`), dotted for per-layer entries (`built.01`).
- Sessions are never written to `state.json`.
- The test gate from `AGENTS.md` passes after every task: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- Commit after every task. Commit subjects start with `flow:`.

## Review Focus

1. **A workflow names an input nothing produces.** Loading `airlie.yaml` must fail with the workflow and node named, before any story runs. Pinned in Task 1.
2. **A resumed story at layer 03 must not rerun layers 01 and 02 or the drafts.** The skip rule must see `built.01`, `built.02` and `drafted`. Pinned in Task 4 and Task 7.
3. **A loop whose bound runs out must apply `on_fail`, never spin.** Pinned in Task 4.
4. **The drafting session is gone after a process restart.** A package retry must fall back to a fresh ask, not crash. Pinned in Task 10.
5. **A gate node ends the run.** The build lane lock is released and the next `build()` resumes after the gate. Pinned in Task 12.

---

## File Structure

**Create:**
- `src/airlie/flow/__init__.py` — re-exports `NodeSpec`, `WorkflowSpec`, `Composer`, `Registry`, `Store`, `Sessions`.
- `src/airlie/flow/spec.py` — node and workflow models, static validation.
- `src/airlie/flow/store.py` — `Store` protocol, `MemoryStore`, `StateStore` over `StoryState`, `Sessions`.
- `src/airlie/flow/composer.py` — `Composer`, `RunContext`, `Registry`, `EndRun`, `MissingInput`, wiring moves.
- `src/airlie/story/flow.py` — the story's runners and registry: one function per node that calls today's method, replaced type by type in Tasks 8–12.
- `config/workflows/story.yaml`, `config/workflows/layer.yaml` — the story workflow and the layer bundle.
- `tests/unit/test_flow_spec.py`, `tests/unit/test_flow_store.py`, `tests/unit/test_flow_composer.py`, `tests/unit/test_story_flow.py`.

**Modify:**
- `src/airlie/config/schema.py` — `workflows: dict[str, WorkflowSpec]` on `AirlieConfig` (L937-963); `composer: bool` on `StorySpec` (L771).
- `src/airlie/config/load.py` — nothing: includes already merge fragments (L89).
- `airlie.yaml` — add `config/workflows/story.yaml` and `config/workflows/layer.yaml` to `includes:` (L5-8).
- `src/airlie/story/pipeline.py` — `_drive` (L623) dispatches to the composer under the flag; `_handoff` (L731) ignores node events; methods shrink in Tasks 8–12.
- `tests/unit/test_story_pipeline.py` — `pipeline()` helper (L181) gains `composer: bool = False`; chosen end-to-end tests are parametrized over the flag.

---

### Task 1: Node and workflow models with static validation

**Files:**
- Create: `src/airlie/flow/__init__.py`, `src/airlie/flow/spec.py`
- Modify: `src/airlie/config/schema.py:937-963` (`AirlieConfig`), `src/airlie/config/schema.py:771` (`StorySpec`)
- Test: `tests/unit/test_flow_spec.py`

**Interfaces:**
- Produces, in `spec.py`:
  ```python
  NodeType = Literal["ask", "conversation", "write", "meeting", "check", "gate", "parallel", "each"]
  Format = Literal["json", "markdown", "text", "bool", "list", "ref:branch", "ref:worktree", "ref:pr", "file", "session"]
  class LoopSpec(Strict):
      to: str                     # node to run again
      max: int | str              # a count, or a dotted config path such as "story.layer_attempts"
      when: str | None = None     # registry predicate id; None means "when the node failed"
      carry: str | None = None    # output name copied into the store as "feedback.<to>"
  class NodeSpec(Strict):
      type: NodeType
      role: str | list[str] | None = None
      prompt: str | None = None   # registry id: brief builder (ask/conversation/write), opening brief for a conversation
      turn: str | None = None     # registry id: a conversation's turn prompt
      parse: str | None = None    # registry id: reply parser
      run: str | None = None      # registry id: a check's or gate's callable
      after: str | None = None    # registry id: side effect after the outputs land (a board comment)
      inputs: list[str] = []
      outputs: dict[str, Format] = {}
      steps: int | str | None = None
      tools: list[str] | None = None
      done: str | None = None     # registry predicate id
      on_fail: Literal["block", "continue"] | LoopSpec = "block"
      when: str | None = None     # registry predicate id; false skips the node
      next: str | None = None
      on: str | None = None       # branch: which output
      branch: dict[str, str] | None = None   # output value -> node; "*" is the default
      loop: LoopSpec | None = None
      nodes: list[str] = []       # parallel
      over: str | None = None     # each: list artifact
      workflow: str | None = None # each: bundle name
      args: dict[str, Any] = {}   # extra values handed to the callable
      session: str | None = None  # conversation/write: the session this node continues
      advisor: str | None = None  # write, Part 2
  class WorkflowSpec(Strict):
      start: str
      loose: list[str] = []       # inputs the caller supplies: card, repo
      nodes: dict[str, NodeSpec]
  def check_workflows(workflows: Mapping[str, WorkflowSpec]) -> list[str]
  ```
  Each problem string is `"workflow <w>: node <n>: <what>"`.
- `AirlieConfig.workflows: dict[str, WorkflowSpec] = Field(default_factory=dict)`; `AirlieConfig.validate_references()` (schema.py ~L966) appends `check_workflows(self.workflows)`.
- `StorySpec.composer: bool = Field(default=False, description="run the story through the workflow composer")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_flow_spec.py
from airlie.flow.spec import NodeSpec, WorkflowSpec, check_workflows

def wf(**nodes):
    return WorkflowSpec(start=next(iter(nodes)), loose=["card"], nodes=nodes)

def test_an_input_nothing_produces_is_a_problem_naming_the_node():
    w = wf(refine=NodeSpec(type="ask", role="product", prompt="p", inputs=["survey"], outputs={"story": "json"}))
    assert check_workflows({"story": w}) == ["workflow story: node refine: input 'survey' is not produced by any node and not loose"]

def test_a_loose_input_is_fine():
    w = wf(refine=NodeSpec(type="ask", role="product", prompt="p", inputs=["card"], outputs={"story": "json"}))
    assert check_workflows({"story": w}) == []

def test_each_type_requires_its_fields():
    w = wf(a=NodeSpec(type="ask"), c=NodeSpec(type="check"), e=NodeSpec(type="each"), p=NodeSpec(type="parallel"))
    problems = check_workflows({"w": w})
    assert "workflow w: node a: ask needs role and prompt" in problems
    assert "workflow w: node c: check needs run" in problems
    assert "workflow w: node e: each needs over and workflow" in problems
    assert "workflow w: node p: parallel needs nodes" in problems

def test_wires_must_name_known_nodes_and_bundles():
    w = wf(a=NodeSpec(type="check", run="r", next="zz", branch={"x": "yy"}, on="kind"),
           e=NodeSpec(type="each", over="layers", workflow="nope"))
    problems = check_workflows({"w": w})
    assert "workflow w: node a: next names unknown node 'zz'" in problems
    assert "workflow w: node a: branch names unknown node 'yy'" in problems
    assert "workflow w: node e: workflow names unknown workflow 'nope'" in problems

def test_a_bundle_may_read_what_its_caller_produces():
    outer = wf(base=NodeSpec(type="check", run="r", outputs={"base": "json", "layers": "list"}),
               each=NodeSpec(type="each", over="layers", workflow="layer"))
    inner = WorkflowSpec(start="pkg", nodes={"pkg": NodeSpec(type="check", run="r", inputs=["base"], outputs={"built.{item}": "json"})})
    assert check_workflows({"story": outer, "layer": inner}) == []

def test_the_config_rejects_a_bad_workflow(tmp_path):
    from tests.unit.test_config import write, MINIMAL
    from airlie.config import load_config, ConfigError
    data = {**MINIMAL, "workflows": {"w": {"start": "a", "nodes": {"a": {"type": "ask", "inputs": ["ghost"], "outputs": {}}}}}}
    with pytest.raises(ConfigError, match="node a"):
        load_config(write(tmp_path, data))

def test_story_composer_defaults_off(tmp_path):
    from tests.unit.test_config import write, MINIMAL
    from airlie.config import load_config
    assert load_config(write(tmp_path, MINIMAL)).story.composer is False
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_flow_spec.py -q`
Expected: FAIL, `ModuleNotFoundError: airlie.flow`

- [ ] **Step 3: Implement `spec.py` and the two config fields**

`check_workflows` walks every workflow; for inputs it collects the outputs of all nodes in that workflow plus, for a bundle (a workflow some `each` names), the outputs of the calling workflow; `{item}` in an output name is kept literally for matching. Import `Strict` from `airlie.config.schema`; declare `workflows` in `AirlieConfig` after `story` (L956) and call `check_workflows` in `validate_references`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_flow_spec.py tests/unit/test_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/airlie/flow src/airlie/config/schema.py tests/unit/test_flow_spec.py
git commit -m "flow: node and workflow models with static validation; story.composer flag"
```

---

### Task 2: The artifact store and sessions

**Files:**
- Create: `src/airlie/flow/store.py`
- Test: `tests/unit/test_flow_store.py`

**Interfaces:**
- Produces:
  ```python
  class Store(Protocol):
      def has(self, name: str) -> bool
      def get(self, name: str) -> Any
      def put(self, name: str, value: Any) -> None
      def save(self) -> None
      def emit(self, kind: str, **fields: Any) -> None
  class MemoryStore:            # dict-backed; .events: list[tuple[str, dict]]
      def __init__(self, loose: Mapping[str, Any] = {}) -> None
  class StateStore:             # over StoryState
      def __init__(self, st: StoryState, *, loose: Mapping[str, Any]) -> None
  class Sessions:
      def get(self, name: str) -> Any | None
      def put(self, name: str, value: Any) -> None
      def pop(self, name: str) -> Any | None
      def clear(self) -> None
  ```
- `has(name)` is `False` for `None`, `{}`, `[]`, `False` and `""`. A dotted name `built.01` reads `st.built["01"]`; `feedback.write` reads the in-memory feedback dict, which `StateStore` keeps beside the state and never saves. Loose names (`card`, `repo`) come from the mapping given to the constructor. `put` on an unknown `StoryState` field raises `KeyError`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_flow_store.py
def test_memory_store_treats_empty_values_as_absent():
    s = MemoryStore()
    for v in (None, {}, [], False, ""):
        s.put("x", v); assert not s.has("x")
    s.put("x", {"a": 1}); assert s.has("x")

def test_state_store_reads_and_writes_story_fields_and_dotted_layers(tmp_path, repo_with_marker_tests):
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo_with_marker_tests)
    s = StateStore(st, loose={"card": "c"})
    assert not s.has("story") and s.get("card") == "c"
    s.put("story", {"title": "t"}); s.put("built.01", {"sha": "a"}); s.save()
    again = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo_with_marker_tests)
    assert again.story == {"title": "t"} and again.built["01"] == {"sha": "a"}

def test_feedback_lives_beside_the_state_and_is_never_saved(tmp_path, repo_with_marker_tests):
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo_with_marker_tests)
    s = StateStore(st, loose={})
    s.put("feedback.write", "red"); s.save()
    assert s.get("feedback.write") == "red"
    assert "feedback" not in json.loads((tmp_path / "AIR-1" / "state.json").read_text())

def test_put_on_an_unknown_field_is_refused(tmp_path, repo_with_marker_tests):
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo_with_marker_tests)
    with pytest.raises(KeyError):
        StateStore(st, loose={}).put("ghost", 1)

def test_sessions_hold_objects_in_memory_only():
    ss = Sessions(); obj = object()
    ss.put("drafts", obj); assert ss.get("drafts") is obj
    assert ss.pop("drafts") is obj and ss.get("drafts") is None
```

Use `text_card` from `tests/unit/test_story_pipeline.py` and `repo_with_marker_tests` from `tests/unit/test_fork_orchestrator.py`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_flow_store.py -q`
Expected: FAIL, `ImportError`

- [ ] **Step 3: Implement `store.py`**

`StateStore.emit` is `st.emit`; `StateStore.save` is `st.save`. The valid field names are `{f.name for f in dataclasses.fields(StoryState)} - {"path"}`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_flow_store.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/airlie/flow/store.py tests/unit/test_flow_store.py
git commit -m "flow: artifact store over the story state, and in-process sessions"
```

---

### Task 3: The composer: sequence, skip, missing inputs, node events

**Files:**
- Create: `src/airlie/flow/composer.py`
- Test: `tests/unit/test_flow_composer.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass
  class RunContext:
      cfg: AirlieConfig
      store: Store
      sessions: Sessions
      registry: Registry
      workflow: str
      node: str
      item: str | None          # the each-loop item, else None
      feedback: str | None      # the carried loop feedback for this node, else None
  Runner = Callable[[NodeSpec, RunContext], Awaitable[Mapping[str, Any]]]
      # returns the outputs by name; a Mapping with key "__fail__" (str reason) marks a failed node
  class Registry:
      def __init__(self) -> None
      def add(self, id: str, fn: Callable[..., Any]) -> None
      def __getitem__(self, id: str) -> Callable[..., Any]   # KeyError names the id
  class MissingInput(RuntimeError): ...   # "workflow story: node refine: missing input 'card'"
  class EndRun(Exception): ...            # a gate or waiting check raises it; run() returns the node name
  class Composer:
      def __init__(self, cfg: AirlieConfig, *, runners: Mapping[NodeType, Runner], registry: Registry, store: Store, sessions: Sessions) -> None
      async def run(self, workflow: str, *, until: str | None = None, item: str | None = None) -> str
          # runs cfg.workflows[workflow] from its start; stops after node `until` if given; returns the last node run
  ```
- Rules in this task: sequence is the `nodes` dict order unless `next` is set; a node whose `outputs` is non-empty and all present in the store is skipped with `node_skipped`; a `when` predicate that returns `False` skips the same way; before running, every `inputs` name must be present or `MissingInput` is raised; `node_started(node, type, workflow, item)` and `node_finished(node, type, outcome, ms)` are emitted around each run with `outcome` one of `done`, `failed`, `skipped`; outputs returned by a runner are `put` and `save`d once per node; `{item}` in an output name is substituted; `steps` and `loop.max` given as a dotted string resolve against `cfg` (`"story.stage_attempts"` → `cfg.story.stage_attempts`) by `resolve_count(cfg, value) -> int`; `on_fail: block` raises `StoryBlocked(reason)` from `airlie.story.asking`; `on_fail: continue` moves on.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_flow_composer.py
def composer(tmp_path, workflows: dict, runners: dict, *, store=None, registry=None):
    cfg = cfg_for(tmp_path, extra={"workflows": workflows})   # from tests/unit/test_studio_orchestrator.py
    return Composer(cfg, runners=runners, registry=registry or Registry(), store=store or MemoryStore({"card": "c"}), sessions=Sessions())

def check_that(fn):
    async def run(node, ctx): return fn(node, ctx)
    return run

async def test_nodes_run_in_order_and_outputs_land_in_the_store(tmp_path):
    seen = []
    runner = check_that(lambda n, c: (seen.append(c.node), {k: c.node for k in n.outputs})[1])
    wf = {"w": {"start": "a", "loose": ["card"], "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}}, "b": {"type": "check", "run": "r", "inputs": ["x"], "outputs": {"y": "text"}}}}}
    c = composer(tmp_path, wf, {"check": runner})
    assert await c.run("w") == "b"
    assert seen == ["a", "b"] and c.store.get("y") == "b"

async def test_a_node_whose_outputs_exist_is_skipped(tmp_path):
    store = MemoryStore({"card": "c"}); store.put("x", "old")
    seen = []
    c = composer(tmp_path, {"w": {"start": "a", "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}}}}},
                 {"check": check_that(lambda n, c: (seen.append(1), {"x": "new"})[1])}, store=store)
    await c.run("w")
    assert seen == [] and store.get("x") == "old"
    assert [k for k, _ in store.events] == ["node_skipped"]

async def test_a_missing_input_refuses_to_run_the_node(tmp_path):
    c = composer(tmp_path, {"w": {"start": "a", "loose": ["ghost"], "nodes": {"a": {"type": "check", "run": "r", "inputs": ["ghost"], "outputs": {"x": "text"}}}}},
                 {"check": check_that(lambda n, c: {"x": 1})})
    with pytest.raises(MissingInput, match="node a: missing input 'ghost'"):
        await c.run("w")

async def test_node_events_bracket_each_run(tmp_path):
    c = composer(tmp_path, {"w": {"start": "a", "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}}}}},
                 {"check": check_that(lambda n, c: {"x": 1})})
    await c.run("w")
    kinds = [(k, f["node"], f.get("outcome")) for k, f in c.store.events]
    assert kinds == [("node_started", "a", None), ("node_finished", "a", "done")]

async def test_until_stops_after_the_named_node(tmp_path):
    seen = []
    runner = check_that(lambda n, c: (seen.append(c.node), {k: 1 for k in n.outputs})[1])
    wf = {"w": {"start": "a", "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}}, "b": {"type": "check", "run": "r", "outputs": {"y": "text"}}}}}
    assert await composer(tmp_path, wf, {"check": runner}).run("w", until="a") == "a"
    assert seen == ["a"]

async def test_a_when_predicate_skips_the_node(tmp_path):
    reg = Registry(); reg.add("never", lambda ctx: False)
    c = composer(tmp_path, {"w": {"start": "a", "nodes": {"a": {"type": "check", "run": "r", "when": "never", "outputs": {"x": "text"}}}}},
                 {"check": check_that(lambda n, c: {"x": 1})}, registry=reg)
    await c.run("w")
    assert not c.store.has("x")

async def test_on_fail_block_raises_story_blocked_and_continue_moves_on(tmp_path):
    failing = check_that(lambda n, c: {"__fail__": "no"})
    wf = {"w": {"start": "a", "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}}}}}
    with pytest.raises(StoryBlocked, match="no"):
        await composer(tmp_path, wf, {"check": failing}).run("w")
    wf["w"]["nodes"]["a"]["on_fail"] = "continue"
    assert await composer(tmp_path, wf, {"check": failing}).run("w") == "a"

def test_counts_resolve_from_config(tmp_path):
    cfg = cfg_for(tmp_path, extra={"story": {"stage_attempts": 3}})
    assert resolve_count(cfg, "story.stage_attempts") == 3 and resolve_count(cfg, 2) == 2
    with pytest.raises(ConfigError, match="story.nope"):
        resolve_count(cfg, "story.nope")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_flow_composer.py -q`
Expected: FAIL, `ImportError`

- [ ] **Step 3: Implement `composer.py` for sequence only**

Leave `parallel`, `each`, `branch` and `loop` raising `NotImplementedError` for Task 4. Time `node_finished.ms` with `time.monotonic()`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_flow_composer.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/airlie/flow/composer.py tests/unit/test_flow_composer.py
git commit -m "flow: the composer runs a workflow in sequence with skip, input checks and node events"
```

---

### Task 4: The composer: parallel, branch, loop, for-each bundles

**Files:**
- Modify: `src/airlie/flow/composer.py`
- Test: `tests/unit/test_flow_composer.py`

**Interfaces:**
- Consumes Task 3's `Composer`.
- Produces these rules:
  - `parallel`: `asyncio.gather` over `nodes`, each with the sequence rules; the next node after the parallel node is the one after it in dict order. A failing branch cancels the others (as `_refine_with_survey` does at `pipeline.py:947`).
  - `branch`: after the node runs (or is skipped, reading the stored output), `next = branch[str(outputs[on])]`, else `branch["*"]`, else dict order.
  - `loop`: after the node runs, if `when` (registry predicate over `(ctx, outputs)`) is true, or the node failed and `when` is None, and the loop count for `(workflow, item, node)` is below `max`: copy output `carry` into the store as `feedback.<to>`, increment the count, and continue from `to`. When the count is spent, apply the node's `on_fail` if it failed, else move on. Counts live in `Composer`, not the store.
  - `each`: for every item of the list artifact `over` (items are `"01"`, `"02"`… for layers: `f"{i + 1:02d}"`), run the bundle `workflow` with `item` set; a bundle shares the store; `until` applies to the outer workflow only. `EndRun` propagates.

- [ ] **Step 1: Write the failing tests**

```python
async def test_parallel_nodes_both_run_then_the_next_in_order(tmp_path):
    seen = []
    runner = check_that(lambda n, c: (seen.append(c.node), {k: 1 for k in n.outputs})[1])
    wf = {"w": {"start": "both", "nodes": {
        "both": {"type": "parallel", "nodes": ["s", "r"]},
        "s": {"type": "check", "run": "r", "outputs": {"survey": "text"}},
        "r": {"type": "check", "run": "r", "outputs": {"story": "json"}},
        "after": {"type": "check", "run": "r", "inputs": ["survey", "story"], "outputs": {"z": "text"}}}}}
    assert await composer(tmp_path, wf, {"check": runner}).run("w") == "after"
    assert set(seen[:2]) == {"s", "r"} and seen[2] == "after"

async def test_a_branch_picks_the_next_node_by_an_output(tmp_path):
    seen = []
    runner = check_that(lambda n, c: (seen.append(c.node), {"kind": "pinned"} if c.node == "base" else {"x": 1})[1])
    wf = {"w": {"start": "base", "nodes": {
        "base": {"type": "check", "run": "r", "outputs": {"kind": "text"}, "on": "kind", "branch": {"pinned": "p", "*": "t"}},
        "t": {"type": "check", "run": "r", "outputs": {"x": "text"}},
        "p": {"type": "check", "run": "r", "outputs": {"x": "text"}}}}}
    await composer(tmp_path, wf, {"check": runner}).run("w")
    assert seen == ["base", "p"]

async def test_a_loop_carries_feedback_and_stops_at_its_bound(tmp_path):
    calls = []
    runner = check_that(lambda n, c: (calls.append(c.feedback), {"__fail__": "red", "tail": f"tail{len(calls)}"})[1])
    wf = {"w": {"start": "write", "nodes": {"write": {"type": "check", "run": "r", "outputs": {"tail": "text"},
          "loop": {"to": "write", "max": 2, "carry": "tail"}, "on_fail": "continue"}}}}
    await composer(tmp_path, wf, {"check": runner}).run("w")
    assert calls == [None, "tail1", "tail2"]

async def test_a_loop_bound_from_config_and_block_when_spent(tmp_path):
    runner = check_that(lambda n, c: {"__fail__": "red"})
    wf = {"w": {"start": "a", "nodes": {"a": {"type": "check", "run": "r", "outputs": {"x": "text"}, "loop": {"to": "a", "max": "story.layer_attempts"}}}}}
    cfg = cfg_for(tmp_path, extra={"workflows": wf, "story": {"layer_attempts": 1}})
    c = Composer(cfg, runners={"check": runner}, registry=Registry(), store=MemoryStore({"card": "c"}), sessions=Sessions())
    with pytest.raises(StoryBlocked):
        await c.run("w")

async def test_each_runs_the_bundle_per_item_and_skips_built_items(tmp_path):
    store = MemoryStore({"card": "c"}); store.put("layers", [{}, {}, {}]); store.put("built.01", {"sha": "a"})
    seen = []
    runner = check_that(lambda n, c: (seen.append((c.node, c.item)), {"built.{item}": {"sha": c.item}})[1])
    wf = {"story": {"start": "lay", "nodes": {"lay": {"type": "each", "over": "layers", "workflow": "layer"}}},
          "layer": {"start": "b", "nodes": {"b": {"type": "check", "run": "r", "outputs": {"built.{item}": "json"}}}}}
    await composer(tmp_path, wf, {"check": runner}, store=store).run("story")
    assert seen == [("b", "02"), ("b", "03")] and store.get("built.03") == {"sha": "03"}

async def test_end_run_from_a_gate_returns_the_gate_node(tmp_path):
    async def gate(node, ctx): raise EndRun()
    wf = {"w": {"start": "g", "nodes": {"g": {"type": "gate", "run": "r"}, "later": {"type": "check", "run": "r", "outputs": {"x": "text"}}}}}
    c = composer(tmp_path, wf, {"gate": gate, "check": check_that(lambda n, c: {"x": 1})})
    assert await c.run("w") == "g" and not c.store.has("x")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_flow_composer.py -q`
Expected: FAIL with `NotImplementedError`

- [ ] **Step 3: Implement the four moves**

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_flow_composer.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/airlie/flow/composer.py tests/unit/test_flow_composer.py
git commit -m "flow: parallel, branch, loop and for-each bundles in the composer"
```

---

### Task 5: The story workflow files

**Files:**
- Create: `config/workflows/story.yaml`, `config/workflows/layer.yaml`
- Modify: `airlie.yaml:5-8` (`includes:`)
- Test: `tests/unit/test_story_flow.py`

**Interfaces:**
- Produces `cfg.workflows["story"]` and `cfg.workflows["layer"]`, with these nodes in this order (the box list from `docs/superpowers/specs/2026-11-02-pipeline-matches-diagram-design.md`). Registry ids are `story.<name>`; the runners in Task 6 and 7 look them up.

`story.yaml`:

| node | type | role | key fields |
|---|---|---|---|
| `intake` | check | | `run: story.intake` |
| `base` | check | | `run: story.base`, `outputs: {base: json}` |
| `gate` | check | | `run: story.gate`, `inputs: [base]`, `outputs: {gate: json}`, `when: story.to_build` |
| `plan_story` | parallel | | `nodes: [survey, refine]` |
| `survey` | ask | architect | `prompt: story.survey`, `inputs: [card]`, `outputs: {survey: markdown}`, `steps: 8`, `on_fail: continue`, `when: story.survey_on` |
| `refine` | ask | product | `prompt: story.refine`, `parse: story.story`, `after: story.refined`, `inputs: [card]`, `outputs: {story: json}`, `steps: story.stage_attempts` |
| `costed_refine` | check | | `run: story.costed`, `args: {stages: [survey, refine]}` |
| `layers` | conversation | architect | `prompt: story.layers`, `turn: story.layers_again`, `parse: story.layers`, `inputs: [story, base]`, `outputs: {layers: list}`, `steps: story.plan_max_rounds` |
| `costed_plan` | check | | `run: story.costed`, `args: {stages: [plan]}` |
| `amigos` | meeting | `[product, architect, tester]` | `run: story.amigos`, `inputs: [story, layers, base]`, `outputs: {agreement: json}`, `steps: story.amigos.max_turns` |
| `costed_amigos` | check | | `run: story.costed`, `args: {stages: [amigos]}` |
| `planned` | check | | `run: story.planned` (emits `story_planned`; the plan lane's `until`) |
| `build_layers` | each | | `over: layers`, `workflow: layer` |
| `built` | check | | `run: story.build_done` (`st.finish("build")`) |
| `publish` | check | | `run: story.publish`, `inputs: [layers]`, `outputs: {prs: list}` |
| `costed_publish` | check | | `run: story.costed`, `args: {stages: [publish]}` |
| `review` | gate | | `run: story.finish` |

`layer.yaml` (`loose: [card, story, layers, base, gate]`):

| node | type | role | key fields |
|---|---|---|---|
| `covered` | check | | `run: story.covered`, `outputs: {satisfied.{item}: json}`, `on_fail: continue` (a layer not covered below produces nothing and moves on) |
| `drafts` | conversation | architect | `prompt: story.drafts`, `turn: story.next_package`, `outputs: {drafted: bool, drafts_session: session}`, `when: story.not_built`, `steps: story.package_steps` |
| `package` | ask | architect | `run: story.ready_package`, `inputs: [base]`, `outputs: {package.{item}: json}`, `session: drafts_session` |
| `build` | write | coder | `run: story.build_once`, `inputs: [package.{item}]`, `outputs: {built.{item}: json}`, `when: story.has_package`, `loop: {to: package, max: story.layer_attempts, carry: feedback}` |
| `costed_layer` | check | | `run: story.costed`, `args: {stages: [package, layer, review]}` |

The `package.{item}` artifact is new: it is `StoryState.drafts[nn]` after `_ready_package` consumed it today. Add `package: dict[str, dict[str, Any]]` to `StoryState` only if the Task 7 implementer finds no existing field holds the ready package; otherwise reuse. Mark the choice in the commit message.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_story_flow.py
def test_the_shipped_workflow_lists_the_diagram_boxes_in_order():
    cfg = load_config(Path("airlie.yaml"))
    assert list(cfg.workflows["story"].nodes) == ["intake", "base", "gate", "plan_story", "survey", "refine", "costed_refine", "layers", "costed_plan", "amigos", "costed_amigos", "planned", "build_layers", "built", "publish", "costed_publish", "review"]
    assert list(cfg.workflows["layer"].nodes) == ["covered", "drafts", "package", "build", "costed_layer"]
    assert cfg.workflows["story"].nodes["amigos"].role == ["product", "architect", "tester"]
    assert cfg.workflows["layer"].nodes["build"].loop.max == "story.layer_attempts"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/test_story_flow.py -q`
Expected: FAIL, `KeyError: 'story'`

- [ ] **Step 3: Write the two YAML files and add them to `airlie.yaml`'s includes**

Both files start with `version: 1` only if `_read_yaml` requires it (check `load.py:69`); otherwise a bare `workflows:` key.

- [ ] **Step 4: Run the test and the config suite**

Run: `uv run pytest tests/unit/test_story_flow.py tests/unit/test_config.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config/workflows airlie.yaml tests/unit/test_story_flow.py
git commit -m "flow: the story workflow and the layer bundle as config"
```

---

### Task 6: Story runners by node name, and the plan lane under the flag

**Files:**
- Create: `src/airlie/story/flow.py`
- Modify: `src/airlie/story/pipeline.py:623-710` (`_drive`), `src/airlie/story/pipeline.py:731` (`_handoff`)
- Modify: `tests/unit/test_story_pipeline.py:181` (`pipeline()` helper)
- Test: `tests/unit/test_story_flow.py`

**Interfaces:**
- Produces, in `story/flow.py`:
  ```python
  def story_registry(pipe: StoryPipeline, st: StoryState, card: Card) -> Registry
      # registers every "story.*" id the YAML names: checks, briefs, parsers, predicates, hooks
  def story_runners(pipe: StoryPipeline, st: StoryState, card: Card) -> dict[NodeType, Runner]
  async def compose(pipe: StoryPipeline, st: StoryState, card: Card, *, until: Literal["planned", "review"]) -> None
      # builds StateStore(st, loose={"card": card, "repo": pipe.repo}), Sessions(), Composer; runs "story" with until="planned" or None
  ```
- In this task every runner dispatches on `ctx.node` and calls today's method with today's arguments, then returns the outputs read back from `st`. Example: the `ask` runner for `refine` is `await pipe._refine(st, card)` and returns `{"story": st.story}`. The `check` runner for `gate` calls `pipe._gate(st, st.base)`. The `planned` check emits `story_planned` exactly as `_drive` does at L680 and raises nothing. `story.to_build` is `lambda ctx: _to_build(st)`. `story.survey_on` is `lambda ctx: pipe.s.survey`.
- `_drive` under `self.s.composer`: after the shared prologue (open state, `story_start`, the planned early return, lane and phase, `_stacked_base`, the GitHub block, the resumed note) it calls `await compose(self, st, card, until=until)` instead of the step calls, inside the same `try/except` mapping. The `story_planned` and `story_done` emits stay in `_drive` for the planned return. The build half's `AsyncExitStack` for `_held` stays in `_drive` around `compose` (Task 10 moves it).
- `_handoff` (L731): the set of kinds that do not count as a resume gains `node_started`, `node_finished`, `node_skipped`.
- `pipeline()` test helper: `composer: bool = False` adds `"composer": composer` to the `story` extra.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_story_flow.py
from tests.unit.test_story_pipeline import pipeline, happy_engine, text_card, kinds_all, repo_with_marker_tests

@pytest.mark.parametrize("composer", [False, True])
async def test_the_plan_lane_produces_the_same_events_and_state_either_way(tmp_path, composer):
    repo = repo_with_marker_tests(tmp_path / "repo")
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=composer)
    st = await pipe.plan(text_card("add a marker"))
    kinds = [k for k in kinds_all(st.path / "events.jsonl") if not k.startswith("node_")]
    assert kinds == EXPECTED_PLAN_KINDS          # capture from the composer=False run once, as a module constant
    assert st.story is not None and st.layers is not None and st.agreement is not None

async def test_a_handover_after_planning_is_not_a_resume_despite_node_events(tmp_path):
    repo = repo_with_marker_tests(tmp_path / "repo")
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=True)
    card = text_card("add a marker")
    st = await pipe.plan(card)
    assert StoryPipeline._handoff(st, "review") is True
```

Capture `EXPECTED_PLAN_KINDS` by running the driver once and pasting the list; it is the contract the composer meets.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_story_flow.py -q`
Expected: the `composer=True` case FAILS (`compose` not defined), the handover test FAILS.

- [ ] **Step 3: Implement `story/flow.py`, the `_drive` dispatch and the `_handoff` fix**

Plan-lane runners only in this task: `intake`, `base`, `gate`, `plan_story` children `survey` and `refine`, `costed_*`, `layers`, `amigos`, `planned`. `compose` with `until="planned"` passes `until="planned"` to `Composer.run`. The `each`, `write` and `gate` runners raise `NotImplementedError` until Task 7.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_story_flow.py tests/unit/test_story_pipeline.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/airlie/story/flow.py src/airlie/story/pipeline.py tests/unit/test_story_flow.py tests/unit/test_story_pipeline.py
git commit -m "flow: the plan lane runs through the composer behind story.composer"
```

---

### Task 7: The build lane under the flag

**Files:**
- Modify: `src/airlie/story/flow.py`, `src/airlie/story/pipeline.py:1514-1580` (`_build`), `src/airlie/story/state.py:56` if a `package` field is needed
- Test: `tests/unit/test_story_flow.py`

**Interfaces:**
- Consumes `_build`'s pieces: `_covered_below` (L467), `_satisfy` (L1581), `_draft_packages` (L1995), `_ready_package` (L2419), `_build_once` (L1606), `_publish` (L2731), `_finish` (L2868).
- Produces the `each`, `write` and `gate` runners, and the layer bundle's registry ids:
  - `story.covered`: calls `_covered_below(st, layers, i)`; when it returns a `Satisfied`, calls `pipe._satisfy(...)` and returns `{"satisfied.{item}": st.satisfied[nn]}`; else returns `{"__fail__": "not covered"}` so `on_fail: continue` moves on. Also runs the `publisher.author` and `_clear_attempt` lines from `_build` L1514-1580 when not covered.
  - `story.not_built`: `lambda ctx: not st.drafted and not st.built`.
  - `drafts` conversation runner: calls `pipe._draft_packages(st, story, layers)` and returns `{"drafted": True, "drafts_session": pipe._drafting}`; the session goes into `ctx.sessions`, not the store (the runner returns it under the `session` format and the composer routes `session` outputs to `Sessions`).
  - `package` ask runner: calls `pipe._ready_package(...)` with `feedback=ctx.feedback`; returns `{"package.{item}": package.to_dict()}` or for `Satisfied` calls `_satisfy` and returns the satisfied entry (the composer's `branch` is not used here; the `build` node's `when: story.has_package` predicate skips it when the layer was satisfied).
  - `build` write runner: calls `pipe._build_once(...)` for `attempt = loop count + 1`; on `None` returns `{"built.{item}": st.built[nn]}`; on feedback text returns `{"__fail__": "layer red", "feedback": text}`.
  - `story.build_done`, `story.publish`, `story.finish` (the gate runner calls `_finish` then raises `EndRun`).
- `Composer`: outputs whose format is `session` go to `Sessions` under the output name; `has()` for a session name asks `Sessions`.
- `_drive`: the `_held` exit stack wraps `compose` when `until == "review"`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("composer", [False, True])
async def test_a_story_builds_publishes_and_reaches_review_the_same_either_way(tmp_path, composer):
    repo = repo_with_marker_tests(tmp_path / "repo")
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=composer)
    st = await pipe.run(text_card("add a marker"))
    kinds = [k for k in kinds_all(st.path / "events.jsonl") if not k.startswith("node_")]
    assert kinds == EXPECTED_RUN_KINDS
    assert st.stage == "review" and list(st.built) == ["01", "02"]

async def test_a_resumed_build_skips_the_layers_already_built(tmp_path):
    # run once with the composer, then delete st.built["02"] and st.prs, reset stage to "layers", run build() again
    ...
    assert [f["node"] for k, f in events if k == "node_skipped"] >= ["drafts", "package", "build"]   # for item 01
```

Reuse the stage-event assertion from `test_a_request_becomes_two_stacked_layers_and_the_card_reaches_review` (`tests/unit/test_story_pipeline.py:633`) for `EXPECTED_RUN_KINDS`.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_story_flow.py -q`
Expected: FAIL with `NotImplementedError` for `each`

- [ ] **Step 3: Implement the build lane runners**

- [ ] **Step 4: Run the whole story suite both ways**

Run: `uv run pytest tests/unit/test_story_flow.py tests/unit/test_story_pipeline.py tests/unit/test_story_restart.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/airlie/story/flow.py src/airlie/story/pipeline.py src/airlie/story/state.py tests/unit/test_story_flow.py
git commit -m "flow: the build lane runs through the composer; the layer bundle runs per layer"
```

---

### Task 8: The ask runner owns the retry loop

**Files:**
- Modify: `src/airlie/story/flow.py`, `src/airlie/story/pipeline.py:930-1004` (`_survey`, `_refine`)
- Test: `tests/unit/test_story_flow.py`

**Interfaces:**
- Produces a generic `ask` runner in `src/airlie/flow/runners.py`:
  ```python
  async def ask_runner(node: NodeSpec, ctx: RunContext, *, asker: Asker, st: Recorder) -> Mapping[str, Any]
  ```
  It builds the brief with `ctx.registry[node.prompt](ctx, feedback)` and parses with `ctx.registry[node.parse]` when set, through `asker.usable(st, ctx.node, role, what=<output name>, brief=..., parse=..., attempts=resolve_count(cfg, node.steps))` (`asking.py:389`); without `parse` it calls `asker.ask(st, ctx.node, role, brief, steps=node.steps, long=False)` once (the survey). It runs `ctx.registry[node.after](ctx, value)` when set. `NotActionable` becomes `StoryBlocked` as `_refine` does today.
- Registry ids: `story.survey` → `survey_brief(card)`; `story.refine` → `refine_brief(card, feedback)`; `story.story` → the parser `_refine` uses today; `story.refined` → the board note `_refine` posts and `st.finish("refine")`. The story-card short cut (`refine_skipped`) becomes the predicate `story.not_story_card` on the `refine` node's `when`, with a `story.take_card_as_story` check node before it in `story.yaml` that sets `story` from the card when it is a story card. Add that node to the Task 5 order test.
- `_survey` and `_refine` in `pipeline.py` become two-line wrappers that the driver path still calls; their bodies move to the registry functions.

- [ ] **Step 1: Write the failing tests**

```python
async def test_the_ask_runner_retries_until_the_reply_parses(tmp_path):
    # FakeEngine scripted: product replies garbage then STORY; registry parse = Story.from_json
    ...
    assert [k for k, _ in events if k == "retry"] == ["retry"] and store.get("story")["title"]

async def test_a_story_card_skips_refine_and_logs_refine_skipped(tmp_path):
    ...
    assert "refine_skipped" in kinds and "refine" in [f["node"] for k, f in events if k == "node_skipped"]
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement `ask_runner`, register the pieces, shrink `_survey` and `_refine`**
- [ ] **Step 4: Run the story suite both ways**

Run: `uv run pytest tests/unit/test_story_flow.py tests/unit/test_story_pipeline.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "flow: the ask runner owns the retry loop; survey and refine are briefs and parsers"
```

---

### Task 9: The check runner owns the deterministic steps

**Files:**
- Modify: `src/airlie/story/flow.py`, `src/airlie/flow/runners.py`, `src/airlie/story/pipeline.py` (`_intake` L753, `_base` L760, `_gate` L834, `_publish` L2731, `_covered_below` L467)
- Test: `tests/unit/test_story_flow.py`

**Interfaces:**
- Produces `check_runner(node, ctx) -> Mapping[str, Any]`: calls `ctx.registry[node.run](ctx, **node.args)`; the callable returns the outputs mapping, or raises `StoryWaiting` (propagates to `_drive`'s mapping) or `StoryBlocked`.
- The registry functions take `(ctx, **args)` and read inputs from `ctx.store`, not from `pipe` attributes. `story.base` reads nothing and returns `{"base": ...}`; `story.gate` reads `ctx.store.get("base")`.

- [ ] **Step 1: Write the failing test**

```python
async def test_check_callables_read_inputs_from_the_store_not_the_pipeline(tmp_path):
    # a MemoryStore with base set; story.gate registered; call check_runner; assert gate output and the `gate` event
```

- [ ] **Step 2–5:** as in Task 8. Commit: `flow: the check runner owns the deterministic steps`.

---

### Task 10: The conversation runner owns held conversations and sessions

**Files:**
- Modify: `src/airlie/flow/runners.py`, `src/airlie/story/flow.py`, `src/airlie/story/pipeline.py:1082-1140` (`_layers`), `:1995-2180` (`_draft_packages`, `_draft_each`), `:1815-1866` (`_package`, `_resumed`), `:623-710` (`_drive`'s `_held` stack)
- Test: `tests/unit/test_story_flow.py`

**Interfaces:**
- Produces `conversation_runner(node, ctx, *, asker, st)`:
  - Opens `asker.conversation(st, ctx.node, role, opening, steps=resolve_count(cfg, node.steps), writes=...)` (`asking.py:258`) on an `AsyncExitStack` owned by `Composer` (one per run; closed when `run()` returns) and stores the `Say` in `ctx.sessions` under the `session`-format output name.
  - Each turn calls `ctx.registry[node.turn](ctx, turn_index, feedback)` for the nudge and `say(nudge, at=...)`; the registry's `parse` turns the reply into that turn's artifact; `done` ends it early.
  - A node with `session:` set continues `ctx.sessions.get(node.session)` instead of opening a new conversation. When the session is absent (process restarted), it falls back to the node's own `prompt` as a fresh ask: this is today's `_package` path when `_drafting` is `None`.
- `layers` becomes a conversation: the opening brief is `layers_brief(...)` (`briefs.py:221`), the turn prompt is the `again` nudge `ask_in_rounds` builds (`asking.py:335`), `steps` is `plan_max_rounds`, each turn's parse is the layers parser plus `_size_review`. The emitted round events stay identical.
- `_drive` drops its `_held` stack; the composer owns one.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_package_retry_continues_the_drafting_session(tmp_path):
    # composer=True, scripted so layer 01's first build is red (layer_attempts=2); assert "package_resumed" event and no second packages_drafting
    ...

async def test_a_lost_session_falls_back_to_a_fresh_package_ask(tmp_path):
    # run with composer=True until drafted, stop, run build() again in a fresh pipeline; assert the package node emits package_start without package_resumed and the story still reaches review
    ...

async def test_plan_rounds_are_turns_of_one_conversation(tmp_path):
    # plan_round_steps=2, plan_max_rounds=2, architect replies "more" then LAYERS; assert the round events match the driver's
    ...
```

- [ ] **Step 2–5:** as in Task 8. Commit: `flow: the conversation runner owns held conversations; sessions continue across nodes`.

---

### Task 11: The meeting runner owns the three amigos

**Files:**
- Modify: `src/airlie/flow/runners.py`, `src/airlie/story/flow.py`, `src/airlie/story/pipeline.py:1265-1370` (`_agree`)
- Test: `tests/unit/test_story_flow.py`

**Interfaces:**
- Produces `meeting_runner(node, ctx, *, meeting: MeetingFactory, tools, on_turn)`: builds the meeting as `_agree` does (`pipeline.py:1300-1330`: `self.meeting(cfg, self._meeting_tools(wt), self._on_turn(st))`, `before_turn = partial(asker.wait, st, "amigos", True)`), calls `run(opening, story, layers, levels, guidelines, repo=, base=, state=ctx.store.get("meeting"))` (`amigos.py:1004`) and returns `{"agreement": ..., "story": ..., "layers": ...}`; the per-turn state save stays in `on_turn` (`st.meeting = dict(state); st.save()`, L1179-1187). `node.steps` is `amigos.max_turns`. The `when` predicate `story.amigos_on` is `cfg.story.amigos.enabled`; the disabled path (plain split becomes the agreement) is the check node `story.agree_plain` before `amigos` in `story.yaml`, guarded by the opposite predicate. Add both to the Task 5 order test.

- [ ] **Step 1: Write the failing test**

```python
async def test_the_meeting_runs_and_resumes_from_saved_state_the_same_as_the_driver(tmp_path):
    # reuse MEETING / MEETING_ROLES scripts from test_story_pipeline.py:2813; parametrize composer; compare amigos_* events
```

- [ ] **Step 2–5:** as in Task 8. Commit: `flow: the meeting runner owns the three amigos`.

---

### Task 12: The gate runner ends the run, and the full suite runs both ways

**Files:**
- Modify: `src/airlie/flow/runners.py`, `src/airlie/story/flow.py`, `src/airlie/story/pipeline.py:2868` (`_finish`)
- Modify: `tests/unit/test_story_pipeline.py` — parametrize these tests over `composer`: `test_a_request_becomes_two_stacked_layers_and_the_card_reaches_review` (L633), `test_an_issue_keeps_its_key_in_branch_names_and_its_trail_and_findings_reach_the_prs` (L1051), `test_each_layer_is_built_from_its_package_drafted_with_the_others_up_front` (L255), `test_run_is_plan_then_build_and_each_resumes_at_the_boundary` (L3336); and `tests/unit/test_story_restart.py::test_a_story_started_over_from_a_later_stage_keeps_the_stages_before_it` (L259).
- Test: `tests/unit/test_story_flow.py`

**Interfaces:**
- Produces `gate_runner(node, ctx)`: calls `ctx.registry[node.run](ctx)` then raises `EndRun`. `story.finish` posts the links, moves the card to `review` and finishes the stage as `_finish` does; `_drive` returns normally on `EndRun`.

- [ ] **Step 1: Write the failing test**

```python
async def test_the_review_gate_ends_the_run_and_releases_the_build_lane(tmp_path):
    # run with composer=True; assert st.stage == "review", the run returned, and lock_held(stories_dir, "build") is False after
```

- [ ] **Step 2: Run it to verify it fails**
- [ ] **Step 3: Implement `gate_runner`, parametrize the listed tests**
- [ ] **Step 4: Run the full gate**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -n auto --maxprocesses 8`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "flow: the gate runner ends the run; the story suite runs with the composer on and off"
```

---

### Task 13: Bench run with the composer on

**Files:**
- Modify: `airlie.bench.yaml` (in the main checkout, untracked today; add `composer: true` under `story:`), `docs/bench/` (one new note)

This task needs the local engine and is run by a person, not a subagent.

- [ ] **Step 1: Run the roman case on the composer**

Run: `task bench:story -- roman --no-publish -c airlie.bench.yaml`
Expected: the verdict line reads the same as the last driver run recorded in `docs/bench/` (first-try pass, both layers built).

- [ ] **Step 2: Compare the event kinds with the last driver run**

Run from the bench state root: `jq -r .kind events.jsonl | grep -v '^node_' > composer.kinds` for both runs and `diff`.
Expected: no difference.

- [ ] **Step 3: Record the comparison** as `docs/bench/2026-11-NN-composer-roman.md` with the two run ids and the diff result, and commit.

---

## Part 2 outline (its own plan, after Part 1 lands)

1. Split `StudioOrchestrator.execute_task` (`studio.py:851`) into `open_worktree`, `place`, `coder_turn`, `done_check`, `review`, `fix_turn`, `final_gate`, `follow_up_turn`, `squash`, sharing a `SubtaskRun` session; keep `execute_task` as the wrapper that runs them in order for the CLI, serve and `Asker`.
2. A `write_runner` that runs the coder turn as a ReAct loop with the role's write tools on the `ref:worktree` input, emitting a `session`; `done_check`, `fix` and `follow_up` nodes that continue it.
3. The `advisor` on the write node (`LayerSupervisor` as `TeamContext.on_stuck`), and `gate_advice` as an ask node with a branch on the wrong-test category to `package`.
4. `layer.yaml` rewritten with the dissolved nodes; `_build_once` deleted.
5. Trace diff between a driver run and a composer run on the bench; `story.composer: true` on main; the driver deleted after a week of stories.
