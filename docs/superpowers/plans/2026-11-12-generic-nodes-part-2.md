# Generic Nodes, Part 2: The Layer Build Dissolved — Implementation Plan (reconciled 2026-11-12)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Reconciles `docs/superpowers/plans/2026-11-07-generic-nodes-part-2.md` (c913dcf4, written against c89170c2)
> with main at b22204ff, after the nodes-are-config redesign (#261 engine, #263 builder) and #265–#285.
> What changed and why: "Changes from the 2026-11-07 plan" at the end.

**Goal:** The layer build runs as separate nodes (place, coder turn, done-check, review, fix, final gate, gate advice, follow-up, regate, outcome, squash) sharing one in-process layer studio, with the supervisor attached to the studio as today, producing the same events, state, prompts and ledger rows as today's one-call build; then the composer flag flips after bench runs match.

**Architecture:** `StudioOrchestrator.execute_task` is split into public step methods that keep all shared state on the orchestrator object; `execute_task` becomes the wrapper that calls them in order, so the CLI, serve and `Asker` do not change. A new `airlie.story.layer_build` module holds the per-layer glue (`LayerStudio`: the orchestrator, the supervisor, the brief and the attempt) that today sits around the call in `_build_once`; the driver's `_build_once` becomes a wrapper over the same pieces, so driver and composer share code and the equivalence tests compare them. On the schema v2 surface every step is a **catalog check** (nodes-are-config D1: model nodes carry no code ids; D18: place, run tests and squash arrive with Part 2), joined by `to` lines whose `when` conditions read the steps' transient outputs. The `LayerStudio` is the one `session` artifact (made by `place`, read only by checks, so D25's same-role rule holds); every intermediate result is a transient output `build.<step>.{item}`, held in run memory, never in `state.json`, cleared by a loop's send-back.

**Tech Stack:** Python 3.12, uv, pydantic `Strict` config models, pytest with `FakeEngine` (unit and integration layers), ruff, mypy; Vue builder tests under `task web:gate`.

**Spec:** `docs/superpowers/specs/2026-11-06-generic-nodes-design.md` ("The layer bundle", "The supervisor", "Sessions", "Running the same", "Migration", D3, D11–D15) as amended by `docs/superpowers/specs/2026-11-08-nodes-are-config-design.md` (D1, D2, D11–D14, D18, D21, D23–D25, "As built (plan 1)"). Code map at execution time: the controller's `part2-map.md` (file:line for every phase named below).

## Global Constraints

- Every config model extends `Strict` (`src/airlie/flow/outputs.py:20`, re-exported by `airlie.flow.spec` and used by `src/airlie/config/schema.py`). New keys are declared fields.
- `story.composer` stays `False` (schema default, `src/airlie/config/schema.py:815`; `airlie.yaml` does not set it) until Task 8. With it off, and with no `workflow:` label and no snapshot, a story's `events.jsonl` (story stream), each studio run's `events.jsonl`, `state.json` and the supervisor ledger rows are identical to the base commit's for the same `FakeEngine` script. Task 1 pins this with a golden test recorded before any refactor.
- No prompt, brief, role, model or limit changes. A step method calls the same brief builder, steer id and `_context` arguments the phase does today.
- The same events in the same order, plus only `node_started`, `node_finished`, `node_skipped`.
- Model nodes stay configuration only (D1): no node in `layer.yaml` is an ask, write or conversation with a `run`; the build's steps are check nodes over catalog operations, each declared with its inputs, outputs, formats and transient outputs (`check_workflows(..., catalog=)` rule 9).
- The `LayerStudio` is never written to `state.json`; `build.*` outputs are transient (`OutputSpec.transient`, `Check.transient`).
- Cards in flight finish on their snapshot (D21): `story.build_once` stays in the catalog, a wrapper over the new steps, until the driver goes.
- Test placement follows `docs/superpowers/specs/2026-11-09-test-suite-by-layer-design.md` and `skills/qa/unit.md` / `skills/qa/integration.md`: anything that runs a story or a studio on a real git worktree, or reads SQLite, is `tests/integration/`; pure logic is `tests/unit/`. Shared rigs go in `tests/support/`. A unit test over 500 ms or on the real clock is listed in `tests/unit/quarantine.toml` with a `slow:` or `clock:` reason (`task test:quarantine` must pass); prefer making it fast.
- The test gate from `AGENTS.md` after every task, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate` (always when `config/workflows/` or the catalog changes: the builder's tests read `config/workflows/*.yaml` and `web/src/test-utils/catalog.json`).
- Commit after every task. Subjects start with `flow:`.

## Review Focus

1. **The process dies between the coder turn and squash.** On resume the layer has no `built.{item}`, its `studio` session is gone and `attempt` names a kept worktree: `place` must run again (reusing the branch as today's `_build_once` does when it exists), and no node after it may be skipped (each step has a transient output, so none is skipped by its outputs or the done set). Pinned in Task 5.
2. **Review rounds run out with blocking findings.** The gate must still run; the layer never blocks on the review bound. Pinned in Task 5.
3. **The gate's advice says the acceptance test is wrong.** No follow-up turn runs, `test_wrong` is set, and the package retry carries `package-test-wrong` first. Pinned in Task 5.
4. **A layer that changed nothing.** The outcome is `gate_red` / `changed nothing` with a replan, whether or not the gate passed. Pinned in Task 5.
5. **A node raises mid-layer.** The supervisor's open ledger rows are closed "unknown" and the worktree stays for the restart, exactly as today's `finally: sup.close()`: `place` pushes `LayerStudio.close` on the run's `ctx.held` stack. Pinned in Task 3 (driver) and Task 5 (composer).
6. **The coder was cut off or broke.** The final gate's red sends no follow-up and asks no advice (today's early return in `_gate`): `build.gate.{item}.follow_up` is false. Pinned in Task 5.

---

## File Structure

**Create:**
- `src/airlie/story/layer_build.py` — `LayerStudio`, `open_layer`, `layer_outcome`, `squash_layer`, `LayerDied`: the code that today surrounds `execute_task` inside `_build_once`.
- `src/airlie/bench/trace_diff.py` — diff two story runs: event kinds and fields (neutralised), studio prompts, ledger rows.
- `tests/integration/test_studio_split.py`, `tests/integration/test_layer_build.py`, `tests/unit/test_trace_diff.py`, `tests/integration/test_trace_diff.py`.
- `tests/support/layer_golden.py` — `SCRIPT_LAYER`, `golden_layer_events` and the recorded constants (shared by Tasks 1, 3, 5).

**Modify:**
- `src/airlie/topology/studio.py` — split `execute_task` (851-941), `_run_subtask` (497-537), `_done_check` (549-582), `_review_and_fix` (584-643), `_gate` (982-1020) into step methods.
- `src/airlie/story/pipeline.py` — `_build_once` (1607-1735) becomes a wrapper over `layer_build` and the studio steps.
- `src/airlie/flow/spec.py` (`_handed_back`) and `src/airlie/flow/store.py` (`BundleStore`), `src/airlie/flow/composer.py` (`bundle_items`) — transient outputs inside a bundle.
- `src/airlie/story/flow.py` — the new catalog operations; `story.build_once` (534-554) rewritten over them.
- `config/workflows/layer.yaml` — the dissolved bundle.
- `tests/unit/test_flow_catalog.py` (`DECLARED`), `web/src/test-utils/catalog.json`, `web/src/screens/builder/graph.test.ts:89` (the `build` → `costed_layer` line).
- `src/airlie/cli.py` (under `bench_app`), `Taskfile.yml` — `airlie bench trace-diff`, `task bench:trace-diff`.
- `airlie.yaml` — `story.composer: true` (Task 8 only); `AGENTS.md` (the workflows line).
- `docs/superpowers/specs/2026-11-06-generic-nodes-design.md` — "Part 2 as built".

---

### Task 1: Golden events before the split

**Files:**
- Create: `tests/support/layer_golden.py`, `tests/integration/test_studio_split.py`
- Read for helpers: `tests/support/story.py` (`pipeline(..., composer=)`, `kinds`), `tests/support/studio.py` (`APPROVE`, `DONE`, `cfg_for`), `tests/integration/test_story_flow.py` (`neutral`, `built_run`), `tests/integration/test_story_supervisor_flow.py` (`scripted`, `answer`, `ledger`, `rows`: move the fake triage helpers to `tests/support/` so both files import them)

**Interfaces:**
- Produces: `SCRIPT_LAYER` (a `FakeEngine` script that drives a layer through: coder turn with a done-check miss, one review round with a blocking finding and a fix, a red gate with advice, a follow-up and a green regate) and `golden_layer_events(tmp_path) -> tuple[list[dict], list[dict], list[Row]]` returning the story events, the studio run's events and the ledger rows, neutralised with `neutral`. Constants `GOLDEN_STORY_KINDS`, `GOLDEN_STUDIO_KINDS`, `GOLDEN_LEDGER`, `GOLDEN_STATE`, `GOLDEN_NUDGES`.

- [ ] **Step 1: Write the recording helper and the test**

Build the story with the driver (`pipeline(..., composer=False)`, supervisor on with the fake triage). Record the sequences on the current code and paste them in as constants.

```python
async def test_a_layer_build_emits_the_recorded_events_state_and_ledger_rows(tmp_path: Path) -> None:
    story, studio, ledger = await golden_layer_events(tmp_path)
    assert [e["kind"] for e in story] == GOLDEN_STORY_KINDS
    assert [e["kind"] for e in studio] == GOLDEN_STUDIO_KINDS  # run_start … gate_start, gate_failed, follow_up, subtask_done, handoff, gate_start, gate_passed, run_done
    assert [(r.trigger, r.outcome) for r in ledger] == GOLDEN_LEDGER
    assert neutral_state(tmp_path) == GOLDEN_STATE  # built.01 keys, attempt None, runs length

async def test_a_layer_builds_continuation_nudges_are_the_recorded_texts(tmp_path: Path) -> None:
    ...  # trace/build/round-N/prompt.json last message == GOLDEN_NUDGES (done-check, fix, follow-up)
```

- [ ] **Step 2: Run it; it passes on the current code**

Run: `uv run pytest tests/integration/test_studio_split.py -v`. Expected: PASS (the baseline every later task keeps green).

- [ ] **Step 3: Commit** — `flow: golden events, state, prompts and ledger rows of a layer build before the studio split`

### Task 2: The studio split

**Files:**
- Modify: `src/airlie/topology/studio.py:497-537, 549-681, 851-941, 982-1020`
- Test: `tests/integration/test_studio_split.py`; unchanged and must pass: `tests/integration/test_studio_orchestrator.py`, `tests/integration/test_story_supervisor_flow.py`, `tests/integration/test_story_pipeline.py`

**Interfaces:**
- Produces, on `StudioOrchestrator` (all state stays on `self`):
  - `async def open_worktree(self, repo: Path, *, base: str | None, branch: str | None, detach: bool, test_cmd: str | None, test_steps: Sequence[str], scope_tests: bool, checkpoints: Mapping[str, Callable[[], Awaitable[bool]]] | None, max_steps: int | None, goal: str, plan: Plan | None, images: Sequence[Path]) -> Path` — `step_limit`, `pin`/`check_plan`, `run_start`, worktree, `seed_mypy_cache`, base commit, gate memo. Returns the worktree.
  - `async def place(self, prepare: Callable[[Path], Awaitable[None]] | None) -> None`.
  - `async def plan_phase(self, query_text: str, *, plan: Plan | None, repo_map: bool, map_focus: Sequence[str], map_chars: int | None) -> Plan | None` — the map, then the given plan (`_emit_plan`) or the chief's.
  - `async def coder_turn(self, subtask: Subtask) -> SubtaskRun` — `_route`, `SubtaskRun`, then `_run_subtask` up to and including the first `team.run` (skill, handoffs, `subtask_start`, the turn under `write_lock`/`sem`); an exception becomes the `engine_error` result as today.
  - `async def done_check(self, run: SubtaskRun) -> None` — today's `_done_check`, its skip rules kept.
  - `async def review_round(self, run: SubtaskRun, round_no: int) -> Review | None` — one round: commit, handoff, review, trace, `review` event, handoff; `None` when review is off for the role.
  - `def wants_fix(self, review: Review | None, round_no: int) -> bool` — request_changes with a blocking finding and `round_no < studio.review.rounds`.
  - `async def fix_turn(self, run: SubtaskRun, review: Review) -> None` — `fix_rounds += 1`, `fix_round` event, the turn.
  - `def close_subtask(self, run: SubtaskRun) -> None` — `subtask_done` and the result handoff.
  - `async def gate_once(self, test_cmd: str | None) -> bool | None` — `_tests_pass`; stores `gate_output`.
  - `def gate_follows_up(self, passed: bool | None, follow_up: str | None) -> bool` — `_gate`'s early-return rules (`test_wrong` from the stop reason; only a finished, ok subtask is sent back).
  - `def gate_tail(self, follow_up: str) -> str` — `gate_failed` event and the `gate-failed` steer text with the tail.
  - `async def gate_advice(self, follow_up: str, text: str) -> str` — the advisor call; on `WRONG_TEST` sets `test_wrong`, folds the advice into `gate_output` and raises `WrongTest` (new `class WrongTest(Exception)` in studio.py); else `with_advice(text, advice)` (or `text`).
  - `async def follow_up(self, subtask_id: str, nudge: str, *, max_steps: int | None = None) -> None` — exists, unchanged.
  - `def settle_gate(self, passed: bool | None, follow_up: str) -> None` — `advisor.settle("gate_retry", …)` with the suppressed exception.
  - `def finish(self, *, goal: str, final: str, passed: bool | None, started: float, branch: str) -> RunReport` — `_report` and `run_done`.
- `execute_task` keeps its signature and calls these in today's order; `_gate(test_cmd, follow_up)` becomes `gate_once → (not gate_follows_up? return) → gate_tail → gate_advice (WrongTest → return False) → follow_up → gate_once → settle_gate`. `_run_plan` keeps the wave/checkpoint loop for multi-subtask plans and calls `coder_turn`/`done_check`/the review loop/`close_subtask` for a writing subtask.

- [ ] **Step 1: Write the step-sequence test**

```python
async def test_the_steps_in_order_emit_what_execute_task_emits(tmp_path: Path) -> None:
    a = await run_execute_task(tmp_path / "a", SCRIPT_LAYER)
    b = await run_steps(tmp_path / "b", SCRIPT_LAYER)  # open_worktree, place, plan_phase, coder_turn, done_check, review loop, close_subtask, gate chain, finish
    assert [neutral(e) for e in a] == [neutral(e) for e in b]
```

- [ ] **Step 2: Run it; fails with `AttributeError: open_worktree`**
- [ ] **Step 3: Split `execute_task` into the methods above**, moving each phase's body verbatim; `_run_subtask`, `_done_check`, `_review_and_fix`, `_gate` become the wrappers that call the steps.
- [ ] **Step 4: Run the gate** — the four integration files above plus Task 1's golden test pass.
- [ ] **Step 5: Commit** — `flow: the studio's execute_task is a sequence of step methods sharing the orchestrator's state`

### Task 3: `LayerStudio` and `_build_once` as a wrapper

**Files:**
- Create: `src/airlie/story/layer_build.py`
- Modify: `src/airlie/story/pipeline.py:1607-1735`
- Test: `tests/integration/test_layer_build.py`

**Interfaces:**
- In `layer_build.py`:
  - `@dataclass class LayerStudio: orch: StudioOrchestrator; sup: LayerSupervisor | None; nn: str; i: int; attempt: int; last: bool; where: tuple[str, str, str, Any]; package: Package; brief: Brief; plan: Plan; started: float; run: SubtaskRun | None = None; review: Review | None = None; round_no: int = 0; passed: bool | None = None; follow_up_text: str | None = None` and `async def close(self) -> None` (leaves `decoding`, then `sup.close()`; idempotent).
  - `async def open_layer(pipe: StoryPipeline, st: StoryState, card: Card, story: Story, layers: tuple[Layer, ...], i: int, package: Package, where: tuple[str, str, str, Any], built: str, attempt: int) -> LayerStudio` — today's lines from `orch = self.studio()` to `layer_plan(...)` (orch, `st.runs`, `st.attempt`, `layer_start`, supervisor, brief + `brief` event, plan), then enters `decoding(...)`, `orch.open_worktree(...)`, `orch.place(pipe._place(st, orch, package, nn))` and `orch.plan_phase(..., repo_map=False)` with the arguments `_build_once` passes today.
  - `async def layer_outcome(pipe, ls: LayerStudio, st, card, story, layers) -> str | None` — `orch.finish(...)`, then today's tail: final commit, cut-off, kept, failure → `gate_tail` event, lesson, `StoryBlocked` on the last attempt, else `layer_replanned` + feedback; success → `squash_layer`. Raises `LayerDied` when `ls.run is None`.
  - `async def squash_layer(pipe, ls, st, card, story, layers) -> None` — `squash_since`, `remove_worktree`, `st.built[nn]`, `layer_built`, board comment.
  - `class LayerDied(RuntimeError)`.
- `_build_once` keeps its signature: `ls = await open_layer(...)`, `try:` coder turn, done-check, review loop (`review_round` / `wants_fix` / `fix_turn`), `close_subtask`, gate chain, `return await layer_outcome(...)`, `finally: await ls.close()`.

- [ ] **Step 1: Write the tests**

```python
async def test_build_once_through_the_wrapper_matches_the_golden_run(tmp_path): ...       # Task 1's constants, driver path
async def test_a_raise_inside_the_coder_turn_closes_the_supervisors_rows_unknown_and_keeps_the_worktree(tmp_path): ...
async def test_layer_outcome_without_a_coder_turn_is_a_layer_died(tmp_path): ...
```

- [ ] **Step 2: Run; the golden test stays green while Step 3 lands**
- [ ] **Step 3: Implement `layer_build.py` and rewrite `_build_once`**
- [ ] **Step 4: Run the gate** — the golden story, studio, ledger and prompt assertions unchanged.
- [ ] **Step 5: Commit** — `flow: the layer build's glue lives in layer_build; _build_once is a wrapper over the studio's steps`

### Task 4: Transient outputs inside a bundle

Already on main (no work): the write runner, the session artifact and the session-only-between-same-role check (#261: `flow/runners.py:358`, `flow/store.py:Sessions`, `spec._session_problems`); a loop line that sends back its target, everything downstream and every reader, popping their sessions and clearing their outputs (#261: `composer._Run.send_back`, `readers`; `test_a_loop_line_reruns_the_target_and_everything_downstream_and_clears_their_outputs`); transient outputs that are never saved or skipped (`OutputSpec.transient`, `StateStore(transient=)`, `test_a_node_of_transient_outputs_and_an_every_run_check_run_on_every_run`). What is missing: a bundle workflow's node may not write a transient output the bundle does not hand back.

**Files:**
- Modify: `src/airlie/flow/spec.py` (`_handed_back`), `src/airlie/flow/store.py` (`BundleStore.__init__`, `put`), `src/airlie/flow/composer.py` (`bundle_items` passes the heads)
- Test: `tests/unit/test_flow_spec.py`, `tests/unit/test_flow_composer.py`

**Interfaces:**
- `_handed_back` skips outputs with `transient: true`.
- `BundleStore(parent, *, workflow, outputs, item, transient: Collection[str] = ())`: `put` accepts a name whose head is in `transient`, like a memory head; the parent store holds it in run memory.
- `_Run.bundle_items` passes `transient_heads(self.c.workflows)`.

- [ ] **Step 1: Tests**

```python
def test_a_bundle_workflows_transient_output_needs_no_hand_back(): ...           # check_workflows: no problem for build.x.{item} transient
async def test_a_transient_output_written_inside_a_bundle_item_is_never_saved(tmp_path): ...  # StateStore: not in state.json; a condition reads it
async def test_a_loop_inside_a_bundle_clears_its_transient_outputs(tmp_path): ...
```

- [ ] **Step 2: Run; fail** — [ ] **Step 3: Implement** — [ ] **Step 4: Gate** — [ ] **Step 5: Commit** — `flow: a bundle's nodes may write transient outputs, held in run memory and never handed back`

### Task 5: The dissolved layer bundle

**Files:**
- Modify: `config/workflows/layer.yaml`, `src/airlie/story/flow.py` (catalog operations; `story.build_once` over them), `src/airlie/story/layer_build.py`, `tests/unit/test_flow_catalog.py` (`DECLARED`, the Part 2 group), `web/src/test-utils/catalog.json` (regenerate from `airlie workflows catalog`), `web/src/screens/builder/graph.test.ts`
- Test: `tests/integration/test_story_flow.py` (side-by-side, `built_run` style), `tests/integration/test_layer_build.py`

**Interfaces:**
- Catalog operations (`@check` in `story/flow.py`, bodies in `layer_build.py`); every `build.*` output is `json`, transient, item-scoped:

| id | inputs | outputs | does |
|---|---|---|---|
| `story.place_layer` | card, repo, story, layers, `package.{item}` | `studio` (session) | `r.attempts[nn] += 1`; `open_layer(...)` with `r.sites[nn]`; pushes `ls.close` on `ctx.held`; `ctx.held.callback(ctx.sessions.pop, "studio")` |
| `story.coder_turn` | studio | `build.coder.{item}` `{stop_reason, ok}` | `ls.run = await orch.coder_turn(ls.plan.subtasks[0])` |
| `story.done_check` | studio, `build.coder.{item}` | `build.done_check.{item}` | `await orch.done_check(ls.run)` |
| `story.review_round` | studio | `build.review.{item}` `{verdict, blocking, fix}` | `ls.review = await orch.review_round(ls.run, ls.round_no)`; `fix = orch.wants_fix(...)`; `ls.round_no += 1` |
| `story.fix_turn` | studio, `build.review.{item}` | `build.fix.{item}` | `await orch.fix_turn(ls.run, ls.review)` |
| `story.final_gate` | studio | `build.gate.{item}` `{passed, follow_up}` | `orch.close_subtask(ls.run)`; `ls.passed = await orch.gate_once(...)`; `follow_up = orch.gate_follows_up(...)` |
| `story.gate_advice` | studio, `build.gate.{item}` | `build.advice.{item}` `{verdict: go \| wrong_test}` | `orch.gate_tail("build")`, then `orch.gate_advice(...)`; `WrongTest` → `wrong_test` |
| `story.follow_up_turn` | studio, `build.advice.{item}` | `build.follow_up.{item}` | `await orch.follow_up("build", ls.follow_up_text)` |
| `story.regate` | studio | `build.regate.{item}` `{passed}` | `gate_once`, then `settle_gate` |
| `story.layer_outcome` | studio | `build.outcome.{item}` | `layer_outcome(...)`: `None` → ok; a string → `{FAIL_KEY: "layer red", "feedback": text}`; `StoryBlocked` propagates |
| `story.squash_layer` | studio, `build.outcome.{item}` | `built.{item}` | `squash_layer(...)` → `st.built[nn]` |

- `story.build_once` stays declared as it is (D21) and calls `_build_once`, now the wrapper; its title says "a card on an old snapshot".
- `layer.yaml` from `package` on (start, covered, drafts and package unchanged; `package.to` names `place` where it named `build`):

```yaml
place:       {type: check, run: story.place_layer, inputs: [card, repo, story, layers, "package.{item}"], outputs: {studio: session}, to: [build]}
build:       {type: check, run: story.coder_turn, inputs: [studio], outputs: {"build.coder.{item}": {format: json, transient: true}}, to: [done_check]}
done_check:  {type: check, run: story.done_check, inputs: [studio, "build.coder.{item}"], outputs: {"build.done_check.{item}": {format: json, transient: true}}, to: [review]}
review:      {type: check, run: story.review_round, inputs: [studio], outputs: {"build.review.{item}": {format: json, transient: true}},
              to: [{node: fix, when: "build.review.{item}.fix"}, {node: final_gate, when: "not build.review.{item}.fix"}]}
fix:         {type: check, run: story.fix_turn, inputs: [studio, "build.review.{item}"], outputs: {"build.fix.{item}": {format: json, transient: true}},
              to: [{node: review, when: "build.fix.{item} exists", loop: {max: studio.review.rounds}}]}
final_gate:  {type: check, run: story.final_gate, inputs: [studio], outputs: {"build.gate.{item}": {format: json, transient: true}},
              to: [{node: gate_advice, when: "build.gate.{item}.follow_up"}, {node: outcome, when: "not build.gate.{item}.follow_up"}]}
gate_advice: {type: check, run: story.gate_advice, inputs: [studio, "build.gate.{item}"], outputs: {"build.advice.{item}": {format: json, transient: true}},
              to: [{node: follow_up, when: "build.advice.{item}.verdict == go"}, {node: outcome, when: "build.advice.{item}.verdict == wrong_test"}]}
follow_up:   {type: check, run: story.follow_up_turn, inputs: [studio, "build.advice.{item}"], outputs: {"build.follow_up.{item}": {format: json, transient: true}}, to: [regate]}
regate:      {type: check, run: story.regate, inputs: [studio], outputs: {"build.regate.{item}": {format: json, transient: true}}, to: [outcome]}
outcome:     {type: check, run: story.layer_outcome, inputs: [studio], outputs: {"build.outcome.{item}": {format: json, transient: true}},
              to: [{node: package, loop: {max: story.layer_attempts, carry: feedback}}, squash]}
squash:      {type: check, run: story.squash_layer, inputs: [studio, "build.outcome.{item}"], outputs: {"built.{item}": json}, to: [costed_layer]}
```

The review bound sits on the line back from `fix` and the "is there a round left" test in `wants_fix`, so the review after the last fix runs and goes to the gate (Review Focus 2). While a round goes to `fix`, the gate chain's lines die (`node_skipped` events) and the loop sends them back to pending: node events only. Write it in block YAML with the comments the file keeps, and a `layout` only if the builder needs one.

- [ ] **Step 1: Side-by-side tests** (driver vs composer, `built_run` style), one per script:

```python
test_the_dissolved_layer_runs_the_golden_script_the_same_either_way        # Task 1's script: story, studio events, state, ledger rows, prompts
test_review_rounds_running_out_still_reach_the_gate_either_way             # studio.review.rounds=1, two blocking reviews
test_a_wrong_test_verdict_at_the_gate_skips_the_follow_up_either_way       # no follow_up event; package-test-wrong steer first
test_a_cut_off_coder_gets_no_gate_follow_up_either_way                     # stop_reason max_steps: no gate_failed advice, no follow_up
test_a_layer_that_changed_nothing_is_replanned_either_way                  # gate green, no edits
test_a_run_killed_after_the_coder_turn_resumes_from_place_either_way       # second build(): place re-runs, branch reused, no node after it skipped
test_the_last_attempt_blocks_either_way                                    # StoryBlocked, worktree kept, ledger rows closed unknown
test_a_card_on_the_old_layer_snapshot_still_builds_through_build_once      # snapshot with story.build_once: same events as the driver
```

  and in `tests/unit/test_flow_catalog.py`: `DECLARED` gains the eleven ids; `test_the_committed_workflows_pass_the_catalog_rules` (or today's equivalent) stays green.

- [ ] **Step 2: Run; fail (unknown catalog ids)** — [ ] **Step 3: Implement** — [ ] **Step 4: Full gate incl. `task web:gate`** — [ ] **Step 5: Commit** — `flow: the layer bundle runs the build as place, coder, done-check, review/fix, gate, advice, follow-up, regate, outcome and squash nodes`

### Task 6: Trace diff

**Files:**
- Create: `src/airlie/bench/trace_diff.py`, `tests/unit/test_trace_diff.py`, `tests/integration/test_trace_diff.py`
- Modify: `src/airlie/cli.py` (a `trace-diff` command on `bench_app`), `Taskfile.yml` (`bench:trace-diff`)

**Interfaces:**
- Pure: `def diff_events(a: Sequence[Mapping[str, Any]], b: Sequence[Mapping[str, Any]]) -> list[str]`, `def diff_prompts(a: Sequence[str], b: Sequence[str], *, cap: int = 40) -> list[str]`, `def diff_ledger(a: Sequence[tuple[str, str, str]], b: Sequence[tuple[str, str, str]]) -> list[str]`. Volatile fields (timestamps, ids, ms, run ids, paths, shas) neutralised; node events dropped from either side.
- Reading: `def diff_story_runs(a: Path, b: Path, *, prompts: bool = True) -> TraceDiff` where `a`, `b` are bench state dirs (`<state_dir>/stories/<key>/` beside `<state_dir>/supervisor.sqlite`); `@dataclass class TraceDiff: events: list[str]; prompts: list[str]; ledger: list[str]` with `clean -> bool`. Studio runs paired by order (`runs/*/events.jsonl`); prompts from each run's `trace/**/prompt.json`; ledger rows for the story's key as (trigger, category, outcome).
- CLI: `airlie bench trace-diff A B [--no-prompts]` prints the lines and exits 1 when not clean. Taskfile: `bench:trace-diff` passing `{{.CLI_ARGS}}`.

- [ ] **Step 1: Tests** — unit: identical lists → no lines; a changed field, an extra event, a changed prompt line, a different ledger outcome → one line each naming the first divergence; node events ignored. Integration: two synthetic state dirs with a real `supervisor.sqlite` → clean, then one row changed → one ledger line.
- [ ] **Step 2–5:** fail, implement, gate, commit — `flow: airlie bench trace-diff compares two story runs' events, prompts and ledger rows`

### Task 7: Bench: a driver run and three composer runs on roman (controller-run)

Not a subagent task. One driver run and three composer runs of the roman story with `task bench:story -- roman --no-publish`, the composer runs with `--config` naming a config whose `story.composer` is true (there is no `airlie.bench.yaml`); all on the mlx-vlm engine now on main (#270), so the driver run is recorded fresh rather than compared with an older record. `task bench:trace-diff -- A B` of each composer run against the driver run; clean means only node events differ. Record in `docs/bench/<date>-part2-roman.md` (wall clock, tokens, diff summary). Check in with Noah every 30 minutes while runs are live. A run that is not clean stops the task; the divergence goes back to Task 5 as a fix round.

### Task 8: Flip the flag and write up

**Files:**
- Modify: `airlie.yaml` (`story:` block gains `composer: true`), `AGENTS.md` (the workflows line: the composer runs every story), `docs/superpowers/specs/2026-11-06-generic-nodes-design.md` ("Part 2 as built")
- Test: `tests/unit/test_flow_spec.py` (the schema default stays `False`; the committed config turns it on)

- [ ] **Step 1:** `test_the_committed_config_runs_stories_through_the_composer` — `load_config()` of the repo's `airlie.yaml` has `story.composer` true.
- [ ] **Step 2–3:** flip, write the as-built section (what differs from the design's layer table: the steps are catalog checks, not write/ask nodes; `regate` its own node; `outcome` before `squash`; `build.*` transient outputs; `LayerStudio` as the `studio` session; `WrongTest` as the branch signal; `execute_task` kept as the wrapper; `story.build_once` kept for snapshots), gate, commit — `flow: story.composer on; Part 2 as built`.

The driver's deletion (`_build_once`, `_build`, the `_methods`, `story.build_once`) is a later PR after a week of stories, per the design's Migration section.

---

## Self-review

- Spec coverage: layer table rows → Task 5 (covered/drafts/package unchanged; place; coder; done_check; review; fix; final_gate; gate_advice; follow_up; outcome loop; squash); supervisor at the gate and in the loop → Tasks 3 and 5 (attached to the studio, the gate advice a node); sessions → Tasks 3–5; "Running the same" → Tasks 1, 5, 6, 7; Migration → Tasks 7–8; D15 studio split → Tasks 2–3; nodes-are-config D1/D18/D21/D25 → Global Constraints and Task 5.
- Type consistency: `SubtaskRun`, `Review`, `Plan`, `Subtask`, `RunReport`, `Brief`, `Package`, `LayerSupervisor`, `Row` are today's classes; `LayerStudio`, `WrongTest`, `LayerDied`, `TraceDiff` are defined where first named.
- Review Focus 1–6 pinned in Tasks 3 and 5.

---

## Changes from the 2026-11-07 plan

Already done on main, so dropped or shrunk (each MECHANICAL):

1. **MECHANICAL** — Task 4's "a loop jump pops the bundle's sessions and clears what is downstream": `composer._Run.send_back`/`readers` (#261) do it for the target, its downstream and every reader.
2. **MECHANICAL** — `write_runner`, the `session` format, `Sessions` and the same-role session check exist (#261); Task 4 no longer creates them.
3. **MECHANICAL** — Task 4's `StateStore.MEMORY` gaining `"build"` is unnecessary: transient outputs (#261) already give in-memory, never-saved, never-skipped names; what remains is letting them cross a bundle's boundary.

Names, paths and shapes updated, same intent (each MECHANICAL):

4. **MECHANICAL** — `layer.yaml` written in schema v2: `to` lines with `when` and `loop`, no `next`/`on`/`branch`/node-level `loop` (refused as old schema by `spec._old_node_fields`).
5. **MECHANICAL** — test files moved by #282: `tests/unit/test_story_flow.py`, `test_studio_orchestrator.py`, `test_story_supervisor_flow.py`, `test_story_pipeline.py` are now under `tests/integration/`; new story-running tests go there, rigs into `tests/support/`.
6. **MECHANICAL** — the trace diff is split into pure diff functions (unit) and the directory/SQLite reader (integration), because SQLite counts as I/O under the test-layer design.
7. **MECHANICAL** — the quarantine rule (#271) and the six-command `AGENTS.md` gate replace the old five-command list.
8. **MECHANICAL** — `_build_once` now spans 1607-1735 and takes `where` and `built` (`StoryRun.sites`); `open_layer` takes them too, and `place_layer` counts `r.attempts` as `story.build_once` does.
9. **MECHANICAL** — `_gate` takes `(test_cmd, follow_up)`; `gate_once` takes `test_cmd`; `coder_turn` routes the subtask itself (today `_run_plan` does), and drops the `role` parameter.
10. **MECHANICAL** — the review bound's config path is `studio.review.rounds`, not `review.rounds`.
11. **MECHANICAL** — `place` outputs only `studio` (session); `st.attempt` stays a state field written by the operation, since a bundle's node may write only the bundle's declared outputs (`BundleStore`).
12. **MECHANICAL** — the catalog test (`tests/unit/test_flow_catalog.py` `DECLARED`) and the builder's fixtures (`web/src/test-utils/catalog.json`, `graph.test.ts`) are updated with the new ids.
13. **MECHANICAL** — Task 7 has no `airlie.bench.yaml`: the composer runs pass `--config`; the driver baseline is recorded fresh on mlx-vlm (#270).
14. **MECHANICAL** — Task 8: `airlie.yaml` has no `composer` key today, so the flip adds it; `AGENTS.md`'s workflows line changes with it.

Design and scope (each MATERIAL):

15. **MATERIAL** — the build's steps are catalog **check** nodes, not `write`/`ask` nodes with `run:` ids: nodes-are-config D1 forbids code ids on model nodes, and the generic write/ask runners cannot reproduce the studio's protocol (team loop under the write lock, continuations, reviewer, its events). Turning the coder, review and advice turns into config-only model nodes is left for a later plan.
16. **MATERIAL** — one `studio` session (the `LayerStudio`) instead of `studio` plus a `coder` session: every reader is a check, so D25's same-role rule holds with no exception, and the coder's conversation stays inside the studio as today.
17. **MATERIAL** — intermediate results are item-scoped transient outputs (`build.<step>.{item}`) rather than a new memory head, which needs a small engine change: a bundle's nodes may write transient outputs they do not hand back (Task 4). Unlike memory heads, a loop's send-back clears them.
18. **MATERIAL** — no `advisor:` id on any node and no `RunContext.session`/`advisor`: the supervisor is attached in `place_layer` as `_build_once` attaches it, and the gate advice is a check over `orch.gate_advice`, not an ask on the supervisor seat. `NodeSpec.advisor` stays unused until the turns become model nodes.
19. **MATERIAL** — `final_gate` outputs `follow_up` (today's `_gate` early return: a cut-off or broken coder gets no advice and no follow-up); the old plan sent every red gate to `gate_advice`. New Review Focus 6 and a side-by-side test pin it.
20. **MATERIAL** — the review loop: the bound is on the line back from `fix`, and the review operation decides whether a round is left (`wants_fix`), so the review after the last fix still runs and goes to the gate. While a round goes to `fix`, the gate chain is skipped and then run again, which adds `node_skipped` events only.
21. **MATERIAL** — `story.build_once` stays in the catalog (the old plan removed it): cards in flight finish on their snapshot (D21). A side-by-side test pins an old-snapshot card.
22. **MATERIAL** — scope: plan 1's as-built note also defers to "Part 2" making `refine`, `plan_layers`, `draft_packages` and `ready_package` model nodes, plus the `plan_check`, `size_review` and `critic` nodes and five prompt files. This plan keeps the original Part 2 scope (the layer build, the trace diff, the flip) and leaves those for a follow-up plan.
