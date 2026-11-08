# Generic Nodes, Part 2: The Layer Build Dissolved — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The layer build runs as separate nodes (place, coder turn, done-check, review, fix, final gate, gate advice, follow-up, regate, outcome, squash) sharing one coder session, with the supervisor as an advisor on the write nodes and an ask node at the gate, producing the same events, state, prompts and ledger rows as today's one-call build; then the composer flag flips after bench runs match.

**Architecture:** `StudioOrchestrator.execute_task` is split into public step methods that keep all shared state on the orchestrator object; `execute_task` becomes the wrapper that calls them in order, so the CLI, serve and `Asker` do not change. A new `airlie.story.layer_build` module holds the per-layer glue (`LayerStudio`: the orchestrator, the supervisor, the brief and the attempt) that today sits around the call in `_build_once`; the driver's `_build_once` becomes a wrapper over the same pieces, so driver and composer share code and the equivalence tests compare them. The in-process `LayerStudio` and the coder's `SubtaskRun` are the two `session` artifacts the layer bundle passes between nodes; intermediate results live under a new in-memory store head `build.*`, never in `state.json`.

**Tech Stack:** Python 3.12, uv, pydantic `Strict` config models, pytest with `FakeEngine`, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-11-06-generic-nodes-design.md` (sections "The layer bundle", "The supervisor", "Sessions", "Running the same", "Migration", D3, D11–D15). Code map at execution time: the controller's `part2-map.md` (file:line for every phase named below).

## Global Constraints

- Every config model extends `Strict` (`src/airlie/config/schema.py:15`). New keys are declared fields.
- `story.composer` stays `False` until Task 8. With it off, and with no `workflow:` label, a story's `events.jsonl` (story stream), each studio run's `events.jsonl`, `state.json` and the supervisor ledger rows are identical to the base commit's for the same `FakeEngine` script. Task 1 pins this with a golden events test recorded before any refactor.
- No prompt, brief, role, model or limit changes. A step method calls the same brief builder, steer id and `_context` arguments the phase does today.
- The same events in the same order, plus only `node_started`, `node_finished`, `node_skipped`.
- Sessions (`LayerStudio`, `SubtaskRun`) are never written to `state.json`; `build.*` artifacts are in-memory only.
- The test gate from `AGENTS.md` after every task: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `FORCE_COLOR= uv run pytest -q -n auto --maxprocesses 8`, `task web:gate` (web only when `web/` or `config/workflows` changed).
- Commit after every task. Subjects start with `flow:`.

## Review Focus

1. **The process dies between the coder turn and squash.** On resume the layer has no `built.{item}`, its sessions are gone and `attempt` names a kept worktree: `place` must run again (reusing the branch as today's `_build_once` does when it exists), and no node after it may be skipped on a stale output. Pinned in Task 5.
2. **Review rounds run out with blocking findings.** The gate must still run; the layer never blocks on the review bound. Pinned in Task 5.
3. **The gate's advice says the acceptance test is wrong.** No follow-up turn runs, `test_wrong` is set, and the package retry carries `package-test-wrong` first. Pinned in Task 5.
4. **A layer that changed nothing.** The outcome is `gate_red` / `changed nothing` with a replan, whether or not the gate passed. Pinned in Task 5.
5. **A node raises mid-layer.** The supervisor's open ledger rows are closed "unknown" and the worktree stays for the restart, exactly as today's `finally: sup.close()`. Pinned in Task 4.

---

## File Structure

**Create:**
- `src/airlie/story/layer_build.py` — `LayerStudio` (the per-layer in-memory build), `open_layer`, `layer_outcome`, `squash_layer`: the code that today surrounds `execute_task` inside `_build_once`.
- `src/airlie/bench/trace_diff.py` — diff two story runs: event kinds and fields (neutralised), studio prompts, ledger rows.
- `tests/unit/test_layer_build.py`, `tests/unit/test_trace_diff.py`.

**Modify:**
- `src/airlie/topology/studio.py` — split `execute_task` (851-941), `_run_subtask` (497-537), `_gate` (982-1020) into step methods.
- `src/airlie/story/pipeline.py` — `_build_once` (1598-1725) becomes a wrapper over `layer_build` and the studio steps.
- `src/airlie/flow/store.py` — `MEMORY` gains `"build"`.
- `src/airlie/flow/runners.py` — `write_runner`.
- `src/airlie/flow/composer.py` — `RunContext.session`, `RunContext.advisor`; the `write` type dispatches to `write_runner`.
- `src/airlie/story/flow.py` — registry ids and `CATALOG` entries for the new nodes.
- `config/workflows/layer.yaml` — the dissolved bundle.
- `src/airlie/cli.py`, `Taskfile.yml` — `airlie bench trace-diff`, `task bench:trace-diff`.
- `airlie.yaml` — `story.composer: true` (Task 8 only).
- `docs/superpowers/specs/2026-11-06-generic-nodes-design.md` — "Part 2 as built".

---

### Task 1: Golden events before the split

**Files:**
- Test: `tests/unit/test_studio_split.py` (create)

**Interfaces:**
- Produces: `SCRIPT_LAYER` (a `FakeEngine` script that drives a layer through: coder turn with a done-check miss, one review round with a blocking finding and a fix, a red gate with advice, a follow-up and a green regate) and `golden_layer_events(tmp_path) -> tuple[list[dict], list[dict], list[dict]]` returning the story events, the studio run's events and the ledger rows, neutralised with `tests/unit/test_story_flow.py`'s `neutral`. Later tasks assert against the recorded lists.

- [ ] **Step 1: Write the recording helper and the test**

Build the story with the driver (`pipeline(..., composer=False)` helper from `tests/unit/test_story_flow.py`, the supervisor enabled with the fake triage from `tests/unit/test_story_supervisor_flow.py`). Record the three sequences on the current code and paste them into the test as constants:

```python
def test_a_layer_build_emits_the_recorded_events_state_and_ledger_rows(tmp_path: Path) -> None:
    story, studio, ledger = golden_layer_events(tmp_path)
    assert [e["kind"] for e in story] == GOLDEN_STORY_KINDS
    assert [e["kind"] for e in studio] == GOLDEN_STUDIO_KINDS  # run_start … gate_start, gate_failed, follow_up, subtask_done, handoff, gate_start, gate_passed, run_done
    assert [(r.trigger, r.outcome) for r in ledger] == GOLDEN_LEDGER
    assert neutral_state(tmp_path) == GOLDEN_STATE  # built.01 keys, attempt None, runs length
```

Also one assertion on the prompts: the `nudge` of each continuation turn (`trace/build/round-N/prompt.json` last message) equals the recorded text for the done-check, the fix and the follow-up.

- [ ] **Step 2: Run it; it passes on the current code**

Run: `FORCE_COLOR= uv run pytest tests/unit/test_studio_split.py -v`
Expected: PASS (this is the baseline every later task keeps green).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_studio_split.py
git commit -m "flow: golden events, state, prompts and ledger rows of a layer build before the studio split"
```

### Task 2: The studio split

**Files:**
- Modify: `src/airlie/topology/studio.py:497-537, 549-681, 851-941, 982-1054`
- Test: `tests/unit/test_studio_split.py`, `tests/unit/test_studio_orchestrator.py` (unchanged, must pass)

**Interfaces:**
- Produces, on `StudioOrchestrator` (all state stays on `self`):
  - `async def open_worktree(self, repo: Path, *, base: str | None, branch: str | None, detach: bool, test_cmd: str | None, test_steps: Sequence[str], scope_tests: bool, checkpoints: Mapping[str, Callable[[], Awaitable[bool]]] | None, max_steps: int | None, goal: str, plan: Plan | None) -> Path` — phases 1–3 (`check_plan`, `run_start`, worktree, base commit). Returns the worktree.
  - `async def place(self, prepare: Callable[[Path], Awaitable[None]] | None) -> None` — phase 4.
  - `async def plan_phase(self, query_text: str, *, plan: Plan | None, repo_map: bool, map_focus: Sequence[str], map_chars: int | None, images: Sequence[Path]) -> Plan | None` — phases 5–6 (map, given plan or chief).
  - `async def coder_turn(self, subtask: Subtask, role: Role) -> SubtaskRun` — `_run_subtask` up to and including the first `team.run` (route, skill, handoffs, `subtask_start`, the turn under `write_lock`/`sem`); an exception becomes the `engine_error` result as today.
  - `async def done_check(self, run: SubtaskRun) -> None` — today's `_done_check`.
  - `async def review_round(self, run: SubtaskRun, round_no: int) -> Review | None` — one round: commit, handoff, review, trace, `review` event, handoff; `None` when review is off for the role.
  - `def blocking(self, review: Review | None) -> bool` — request_changes with a blocking finding.
  - `async def fix_turn(self, run: SubtaskRun, review: Review, round_no: int) -> None` — `fix_rounds += 1`, `fix_round` event, the turn.
  - `def close_subtask(self, run: SubtaskRun) -> None` — `subtask_done` and the result handoff.
  - `async def gate_once(self) -> bool | None` — `_tests_pass`; stores `gate_output`.
  - `def gate_tail(self, follow_up: str) -> str` — `gate_failed` event and the `gate-failed` steer text with the tail (today's lines before the advice).
  - `async def gate_advice(self, follow_up: str, text: str) -> str` — the advisor call; on `WRONG_TEST` sets `test_wrong`, folds the advice into `gate_output` and raises `WrongTest` (new `class WrongTest(Exception)` in studio.py); else returns `with_advice(text, advice)` (or `text` with no advisor or no advice).
  - `async def follow_up(self, subtask_id: str, nudge: str, *, max_steps: int | None = None) -> None` — exists today, unchanged.
  - `async def settle_gate(self, passed: bool | None, follow_up: str) -> None` — `advisor.settle("gate_retry", …)` with the suppressed exception.
  - `def finish(self, *, final: str, passed: bool | None, started: float) -> RunReport` — report and `run_done`.
- `execute_task` keeps its signature and calls these in today's order; `_gate(follow_up)` is rewritten as `gate_once → (passed? return) → gate_tail → gate_advice (WrongTest → return False) → follow_up → gate_once → settle_gate`, with the `_tests_pass` early-return rules kept.

- [ ] **Step 1: Write the step-sequence test**

```python
async def test_the_steps_in_order_emit_what_execute_task_emits(tmp_path: Path) -> None:
    a = await run_execute_task(tmp_path / "a", SCRIPT_LAYER)       # events.jsonl of execute_task
    b = await run_steps(tmp_path / "b", SCRIPT_LAYER)              # open_worktree, place, plan_phase, coder_turn, done_check, review loop, close_subtask, gate chain, finish
    assert [neutral(e) for e in a] == [neutral(e) for e in b]
```

- [ ] **Step 2: Run it; fails with `AttributeError: open_worktree`**

- [ ] **Step 3: Split `execute_task` into the methods above**, moving each phase's body verbatim; `_run_subtask`, `_done_check`, `_review_and_fix`, `_gate` are deleted or reduced to the wrappers that call the steps.

- [ ] **Step 4: Run the gate**

Run: `FORCE_COLOR= uv run pytest tests/unit/test_studio_split.py tests/unit/test_studio_orchestrator.py tests/unit/test_story_supervisor_flow.py tests/unit/test_story_pipeline.py -q`
Expected: all pass, Task 1's golden test included.

- [ ] **Step 5: Commit** — `flow: the studio's execute_task is a sequence of step methods sharing the orchestrator's state`

### Task 3: `LayerStudio` and `_build_once` as a wrapper

**Files:**
- Create: `src/airlie/story/layer_build.py`
- Modify: `src/airlie/story/pipeline.py:1598-1725`
- Test: `tests/unit/test_layer_build.py`

**Interfaces:**
- Produces, in `layer_build.py`:
  - `@dataclass class LayerStudio: orch: StudioOrchestrator; sup: LayerSupervisor | None; nn: str; i: int; branch: str; base_sha: str; package: Package; brief: Brief; plan: Plan; run: SubtaskRun | None = None; review: Review | None = None; passed: bool | None = None; follow_up_text: str | None = None; started: float`
  - `async def open_layer(pipe: StoryPipeline, st: StoryState, card: Card, story: Story, layers: Layers, i: int, package: Package, attempt: int) -> LayerStudio` — today's lines 1598–1646 (orch, `st.runs`, `st.attempt`, `layer_start`, supervisor, brief + `brief` event, plan) then `orch.open_worktree(...)` and `orch.place(pipe._place(st, orch, package, nn))` with the arguments `_build_once` passes today; enters `decoding(...)`.
  - `async def layer_outcome(pipe, ls: LayerStudio, st, card, story, layers, attempt: int) -> str | None` — `orch.finish(...)`, then today's lines 1664–1724: final commit, cut-off, kept, failure → feedback or `StoryBlocked`; success → `squash_layer`. Always `ls.sup.close()` in a `finally` (together with leaving `decoding`).
  - `def squash_layer(pipe, ls, st, ...) -> None` — `squash_since`, `remove_worktree`, `st.built[nn]`, `layer_built`, board comment.
  - `class LayerDied(RuntimeError)` — raised by `layer_outcome` when `ls.run is None` (a node order error).
- `_build_once` = `ls = await open_layer(...)`; `ls.run = await orch.coder_turn(...)`; `await orch.done_check(ls.run)`; the review loop with `review_round`/`blocking`/`fix_turn` (bounded by `review.rounds` as `_review_and_fix` was); `orch.close_subtask(ls.run)`; the gate chain (`gate_once`, `gate_tail`, `gate_advice` with `WrongTest` caught, `follow_up`, `gate_once`, `settle_gate`); `return await layer_outcome(...)`.

- [ ] **Step 1: Write the tests**

```python
def test_build_once_through_the_wrapper_matches_the_golden_run(tmp_path): ...  # Task 1's constants, driver path
async def test_a_raise_inside_the_coder_turn_closes_the_supervisors_rows_unknown_and_keeps_the_worktree(tmp_path): ...
```

- [ ] **Step 2: Run; the first fails only once `_build_once` changes — write Step 3 and keep the golden test green**

- [ ] **Step 3: Implement `layer_build.py` and rewrite `_build_once`**

- [ ] **Step 4: Run the gate** (full pytest). Expected: pass; the golden story, studio, ledger and prompt assertions unchanged.

- [ ] **Step 5: Commit** — `flow: the layer build's glue lives in layer_build; _build_once is a wrapper over the studio's steps`

### Task 4: Write runner, sessions and the `build.*` memory head

**Files:**
- Modify: `src/airlie/flow/store.py:58`, `src/airlie/flow/composer.py:57-68, 336-399`, `src/airlie/flow/runners.py`, `src/airlie/flow/spec.py` (advisor documented)
- Test: `tests/unit/test_flow_store.py`, `tests/unit/test_flow_composer.py`

**Interfaces:**
- `StateStore.MEMORY = ("feedback", "package", "build")`: `build.*` names are in memory, never saved; `has` treats them like the others.
- `RunContext` gains `session: Any | None` (the object `ctx.sessions.get(node.session)` when the node names one) and `advisor: Any | None` (the registry entry `node.advisor` resolves to, called with `ctx`, when the node names one).
- `async def write_runner(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]` in `runners.py`: raises `MissingInput(f"workflow {ctx.workflow}: node {node.name}: session '{node.session}' is not held")` when the named session is absent; otherwise `return await ctx.registry[node.run](node, ctx)`. (The ReAct loop itself is `NativeTeam.run`; the registry body builds the turn.)
- The composer dispatches `write` to `write_runner`; `session` inputs declared in `inputs` are still checked in `ctx.sessions` as today.
- A failed node (FAIL_KEY) with a `loop` to an earlier node also pops every session the bundle's nodes output and clears `build.*` for the item, so the chain re-runs from the loop target (a jump forces only its target today; the rest would be skipped on stale sessions).

- [ ] **Step 1: Tests**

```python
def test_build_names_are_memory_only(tmp_path): ...               # put build.review; not in state.json; has/get work
async def test_a_write_node_without_its_session_is_a_missing_input(): ...
async def test_a_loop_jump_clears_the_bundles_sessions_and_build_memory(): ...
```

- [ ] **Step 2: Run; fail** — [ ] **Step 3: Implement** — [ ] **Step 4: Gate** — [ ] **Step 5: Commit** — `flow: write runner over held sessions; build.* in memory; a loop jump clears the bundle's sessions`

### Task 5: The dissolved layer bundle

**Files:**
- Modify: `config/workflows/layer.yaml`, `src/airlie/story/flow.py` (registry + `CATALOG`), `src/airlie/story/layer_build.py`
- Test: `tests/unit/test_story_flow.py` (side-by-side), `tests/unit/test_layer_build.py`

**Interfaces:**
- Registry ids (all in `story_registry`, bodies in `layer_build.py`), with the node they serve:
  - `story.place_layer` (check `place`): `open_layer(...)` → `{"attempt": st.attempt, "studio": ls}`; holds `ls` on `ctx.held` so a run that ends closes the supervisor.
  - `story.layer_brief` (prompt id on the write nodes): returns `ls.brief.text`.
  - `story.coder_turn` (write `build`): `ls.run = await orch.coder_turn(ls.plan.subtasks[0], role)` → `{"coder": ls.run}`.
  - `story.done_check` (write `done_check`, `session: coder`): `await orch.done_check(ls.run)` → `{"coder": ls.run}`.
  - `story.review_round` (ask `review`): `ls.review = await orch.review_round(ls.run, round_no)` where `round_no` is the loop count for this item → `{"build.review": {"verdict", "blocking"}}`.
  - `story.blocking_findings` (predicate): `orch.blocking(ls.review)`.
  - `story.fix_turn` (write `fix`, `session: coder`): `await orch.fix_turn(ls.run, ls.review, round_no)` → `{"coder": ls.run}`.
  - `story.final_gate` (check `final_gate`): `orch.close_subtask(ls.run)` then `ls.passed = await orch.gate_once()` → `{"build.gate": "green" | "red" | "none"}`.
  - `story.gate_advice` (ask `gate_advice`): `text = orch.gate_tail("build")`; `try: ls.follow_up_text = await orch.gate_advice("build", text)` → `{"build.advice": "go"}`; `except WrongTest` → `{"build.advice": "wrong_test"}`.
  - `story.follow_up_turn` (write `follow_up`, `session: coder`): `await orch.follow_up("build", ls.follow_up_text)` → `{"coder": ls.run}`.
  - `story.regate` (check `regate`): `ls.passed = await orch.gate_once(); await orch.settle_gate(ls.passed, "build")` → `{"build.regate": "green" | "red"}`.
  - `story.layer_outcome` (check `outcome`): `feedback = await layer_outcome(...)`; `None` → `{"build.outcome": "ok"}`; a string → `{FAIL_KEY: "layer red", "feedback": feedback}` (the loop to `package` carries it); `StoryBlocked` propagates as today.
  - `story.squash_layer` (check `squash`): `squash_layer(...)` → `{"built.{item}": st.built[nn]}`.
  - `story.supervisor` (advisor id): `ls.sup`.
- `layer.yaml` after `package`:

```yaml
place:       {type: check, run: story.place_layer, inputs: ["package.{item}", story, layers], outputs: {attempt: json, studio: session}, when: story.has_package}
build:       {type: write, role: coder, prompt: story.layer_brief, run: story.coder_turn, inputs: [studio, "package.{item}"], outputs: {coder: session}, advisor: story.supervisor}
done_check:  {type: write, role: coder, prompt: story.layer_brief, run: story.done_check, session: coder, steps: studio.done_check_steps, outputs: {coder: session}, advisor: story.supervisor}
review:      {type: ask, role: reviewer, run: story.review_round, inputs: [coder], outputs: {"build.review": json}, loop: {to: fix, max: review.rounds, when: story.blocking_findings}, on_fail: continue, next: final_gate}
fix:         {type: write, role: coder, prompt: story.layer_brief, run: story.fix_turn, session: coder, inputs: ["build.review"], outputs: {coder: session}, advisor: story.supervisor, next: review}
final_gate:  {type: check, run: story.final_gate, inputs: [coder], outputs: {"build.gate": json}, on: "build.gate", branch: {red: gate_advice, "*": outcome}}
gate_advice: {type: ask, role: supervisor, run: story.gate_advice, inputs: [coder], outputs: {"build.advice": json}, on: "build.advice", branch: {wrong_test: outcome, go: follow_up}}
follow_up:   {type: write, role: coder, prompt: story.layer_brief, run: story.follow_up_turn, session: coder, outputs: {coder: session}, advisor: story.supervisor, next: regate}
regate:      {type: check, run: story.regate, inputs: [coder], outputs: {"build.regate": json}, next: outcome}
outcome:     {type: check, run: story.layer_outcome, inputs: [studio], outputs: {"build.outcome": json}, loop: {to: package, max: story.layer_attempts, carry: feedback}, next: squash}
squash:      {type: check, run: story.squash_layer, inputs: [studio, "build.outcome"], outputs: {"built.{item}": json}}
```

The `build` node of today and `story.build_once` are removed from the YAML and the registry; `CATALOG` lists every new id.

- [ ] **Step 1: Side-by-side tests** (driver vs composer, `built_run` style), one per script:

```python
test_the_dissolved_layer_runs_the_golden_script_the_same_either_way       # Task 1's script: story, studio events, state, ledger rows, prompts
test_review_rounds_running_out_still_reach_the_gate_either_way            # rounds=1, two blocking reviews
test_a_wrong_test_verdict_at_the_gate_skips_the_follow_up_either_way      # no follow_up event; package-test-wrong steer first
test_a_layer_that_changed_nothing_is_replanned_either_way                 # gate green, no edits
test_a_run_killed_after_the_coder_turn_resumes_from_place_either_way      # second build(): place re-runs, branch reused, no skipped node after it
test_the_last_attempt_blocks_either_way                                   # StoryBlocked, worktree kept
```

- [ ] **Step 2: Run; fail (missing registry ids)** — [ ] **Step 3: Implement** — [ ] **Step 4: Full gate incl. `task web:gate`** (the builder's tests read `config/workflows/*.yaml`) — [ ] **Step 5: Commit** — `flow: the layer bundle runs the build as place, coder, done-check, review/fix, gate, advice, follow-up, regate, outcome and squash nodes`

### Task 6: Trace diff

**Files:**
- Create: `src/airlie/bench/trace_diff.py`, `tests/unit/test_trace_diff.py`
- Modify: `src/airlie/cli.py` (under `bench`), `Taskfile.yml`

**Interfaces:**
- `def diff_story_runs(a: Path, b: Path, *, prompts: bool = True) -> TraceDiff` where `a`, `b` are story directories (`stories/<key>/`); `@dataclass class TraceDiff: events: list[str]; prompts: list[str]; ledger: list[str]` of human-readable lines, empty when equal; `TraceDiff.clean -> bool`.
- Events: the story `events.jsonl` kinds and non-volatile fields in order (volatile = timestamps, ids, ms, run ids, paths, shas; node events dropped from the composer side), then each studio run's events in `runs/*/events.jsonl` paired by order. Prompts: for each paired studio run, every `trace/**/prompt.json` message text in order, as a unified diff capped at 40 lines per prompt. Ledger: rows for the story's key from `supervisor.sqlite` next to the state dir, as (trigger, category, outcome).
- CLI: `airlie bench trace-diff A B [--no-prompts]` prints the lines and exits 1 when not clean. Taskfile: `bench:trace-diff` with `A`/`B` vars.

- [ ] **Step 1: Tests** — two synthetic story dirs: identical → clean; a changed field, an extra event, a changed prompt line → one line each naming the first divergence.
- [ ] **Step 2–5:** fail, implement, gate, commit — `flow: airlie bench trace-diff compares two story runs' events, prompts and ledger rows`

### Task 7: Bench: a driver run and three composer runs on roman (controller-run)

Not a subagent task. With `airlie.bench.yaml` (flag on) and a sibling config with the flag off: one driver run and three composer runs of the roman story on the bench, `task bench:trace-diff` of each composer run against the driver run. Clean means only node events differ. Record in `docs/bench/2026-11-08-part2-roman.md` (wall clock, tokens, diff summary). The feedback memory applies: check in with Noah every 30 minutes while runs are live. A run that is not clean stops the task; the divergence goes back to Task 5 as a fix round.

### Task 8: Flip the flag and write up

**Files:**
- Modify: `airlie.yaml` (`story.composer: true`), `docs/superpowers/specs/2026-11-06-generic-nodes-design.md` ("Part 2 as built"), `AGENTS.md` (the workflows line: the composer runs every story)
- Test: `tests/unit/test_flow_spec.py` (the default stays `False` in the schema; the committed config turns it on)

- [ ] **Step 1:** test that the committed `airlie.yaml` loads with `story.composer` true.
- [ ] **Step 2–3:** flip, write the as-built section (what differs from the design's layer table: `regate` as its own node, `outcome` before `squash`, `build.*` memory head, `LayerStudio` as the `studio` session, `WrongTest` as the branch signal, `execute_task` kept as the wrapper), gate, commit — `flow: story.composer on; Part 2 as built`.

The driver's deletion (`_build_once`, `_build`, the `_methods`) is a later PR after a week of stories, per the design's Migration section.

---

## Self-review

- Spec coverage: layer table rows → Task 5 (covered/drafts/package unchanged; place; write; done_check; review; fix; final_gate; gate_advice; follow_up; outcome loop; squash); supervisor as advisor + ask → Tasks 4–5; sessions → Tasks 3–5; "Running the same" → Tasks 1, 5, 6, 7; Migration → Tasks 7–8; D15 studio split → Tasks 2–3. The design's `readiness`, `unproven`, `revise_*` rows were folded into `package` in Part 1 and stay there.
- Type consistency: `SubtaskRun`, `Review`, `Plan`, `Subtask`, `Role`, `RunReport`, `Brief`, `Package` are today's classes; `LayerStudio`, `WrongTest`, `LayerDied`, `TraceDiff` are defined where first named.
- Review Focus 1–5 pinned in Tasks 4 and 5.
