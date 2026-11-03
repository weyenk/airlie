# Story pipeline screen matches the pipeline: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The story workflow screen (`/workflows/<key>`) gives every pipeline step a box that is lit while it runs,
with a now bar naming the running step, hover cards for the detail, an exit row, and a phone list with a bottom
sheet.

**Architecture:** The pipeline and the studio log a few more events (starts that are missing today, a `layer` on
layer asks, a corrected run name). `src/airlie/serve/flow.py` is split: shared helpers in `flow_parts.py`, the
plan lane in `flow_plan.py`, the build lane in `flow_build.py`, the now bar, the exit row and liveness in
`flow_now.py`; `story_flow` assembles them and keeps its response shape, adding optional fields. The Vue
pipeline (`web/src/screens/workflow/`) draws the new fields: option C on a laptop, a folding list on a phone.

**Tech Stack:** Python 3.12 (uv, pytest, mypy, ruff), Vue 3.5 + TypeScript + Tailwind, reka-ui 2.11 (`HoverCard*`),
the app's `ui/dialog`, vitest, Playwright e2e (`task test:e2e`).

**Spec:** `docs/superpowers/specs/2026-11-02-pipeline-matches-diagram-design.md`; mockups beside it
(`2026-11-02-pipeline-matches-diagram-mockups.html`, option C and "On a phone").

## Global Constraints

- Every task passes, from the repo root, in order: `uv sync --frozen`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`.
- No stall warning: no color change or threshold for quiet time. `last event Ns ago` is plain muted text.
- New events and fields change no pipeline behaviour. Stories logged before them still draw, with the
  fallbacks the spec's table gives.
- `story_flow` keeps `key`, `title`, `summary`, `stage`, `blocked`, `diagnosis`, `budgets`, `acceptance` and
  `nodes`. Layer nodes keep `id: "layer-NN"`, `kind: "layer"` and a node `status`
  (`web/src/screens/board/StoryFlow.vue` and `web/src/screens/chat/JobCard.vue` read them). New fields are optional
  in the TypeScript types so the run flow (`run_flow.py`, `RunWorkflowScreen.vue`) is untouched.
- Six box shapes only: model, code, same-time bracket, milestone, squares (card only), steps inside (card only).
- Web: tokens from `web/src/styles/tokens.css`, both themes, `BOX`/`DOT` from `style.ts`, `data-*` hooks for
  tests, no pulse under `prefers-reduced-motion` (the `motion-safe:` prefix).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A killed story** (last event over an hour old, no `blocked`/`cancelled`/`story_done`) should draw its open box
   as `stopped` and the now bar as `Stopped at …`, never `running`. Test in Task 5.
2. **A story logged before these events** (AIR-47 as of 2026-11-02: no `package_draft_start`, no `gate_start`, no
   `layer` on `run`) should still light the drafting turn after the last `package_drafted`, and the gate between
   the coder's `subtask_done` and `run_done`. Tests in Task 4.
3. **Resumed tries:** `package_start` attempts restarting at 1 after a resume, and a proof revision repeating an
   attempt number, should number tries by position in the cycle and not merge cycles. Test in Task 4.
4. **A story with no survey and no meeting** (`story.survey` off, `story.amigos.enabled` false) should draw no
   Survey box and no amigos box, with PLANNED still shown once the layers are agreed. Test in Task 3.
5. **Keyboard and touch:** a box's card should open on keyboard focus on a laptop; on a phone (no hover) a tap
   should open the bottom sheet with the card, never a hover card. Tests in Tasks 6 and 8.

---

### Task 1: The pipeline logs the starts and fields the screen needs

**Files:**
- Modify: `src/airlie/story/asking.py` (`start_ask`, a new `ASK_LAYER`)
- Modify: `src/airlie/story/pipeline.py` (`StoryWaiting`, `_stacked_base`, `_drive`, `_wait`, `_agree`,
  `_draft_each`, `_resumed`, `_ready_package`)
- Test: `tests/unit/test_story_pipeline.py`

**Interfaces:**
- Produces, in `airlie.story.asking`: `ASK_LAYER: contextvars.ContextVar[str | None]` (default `None`).
  `start_ask` adds `layer=<value>` to the `run` event when it is set.
- Produces events (all on the story's `events.jsonl`):
  - `run` with `layer` for stages `package`, `critic`, `proof`, `criteria`
  - `package_draft_start {layer}`
  - `package_checks_start {layer, attempt}`
  - `amigos_start {resumed: bool}`
  - `waiting {reason, on}`
- `StoryWaiting(message: str, *, on: str)` carries `.on`, the key waited for.

- [ ] **Step 1: Write the failing tests**

In `test_an_issue_keeps_its_key_in_branch_names_and_its_trail_and_findings_reach_the_prs` (its event-order
assertion at `tests/unit/test_story_pipeline.py:937`), change the build part of the expected kinds to:

```python
*("packages_drafting", "run", "package_draft_start", "package_drafted", "package_draft_start", "package_drafted"),
*("test_checks_skipped", "package_start", "package_checks_start", "package_ready"),
*("layer_start", "brief", "package_placed", "layer_built"),
*("test_checks_skipped", "package_start", "package_checks_start", "package_ready"),
*("layer_start", "brief", "package_placed", "layer_built"),
```

Add these tests:

```python
async def test_a_layers_asks_name_the_layer_on_their_run_event(tmp_path: Path) -> None:
    # a story whose layer 02 has a proof and whose draft is unusable, so layer 02 asks the architect
    # and the critic itself (reuse the setup of the existing unusable-draft test)
    ...
    runs = [e for e in kinds(st.path / "events.jsonl", "run") if e["stage"] in ("package", "critic")]
    assert runs and all(e["layer"] == "02" for e in runs)
    assert all("layer" not in e for e in kinds(st.path / "events.jsonl", "run") if e["stage"] in ("refine", "layers"))

async def test_the_meeting_logs_its_start(tmp_path: Path) -> None:
    # the existing meeting test's setup
    ...
    [start] = kinds(st.path / "events.jsonl", "amigos_start")
    assert start["resumed"] is False
    log = [e["kind"] for e in kinds_all(st.path / "events.jsonl")]
    assert log.index("amigos_start") < log.index("amigos_turn")
```

In `test_a_dependency_not_yet_in_review_is_a_wait_not_a_build` (line 2558) add
`assert waiting["on"] == "AIR-4"`.

Add `test_a_returned_resumed_package_names_the_drafting_run`: drafting held, layer 02's resumed turn returns
an unusable reply, and the `package_returned` for layer 02 has `studio_run` equal to the `packages` run, not
layer 01's build run.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run pytest tests/unit/test_story_pipeline.py -q -k "keeps_its_key or name_the_layer or meeting_logs or not_yet_in_review or resumed_package"`
Expected: FAIL. The kinds are missing, there's a KeyError on `layer`/`on`, and the run is wrong.

- [ ] **Step 3: Implement**

- `ASK_LAYER` in `asking.py`. `start_ask` passes `**({"layer": v} if (v := ASK_LAYER.get()) else {})` to the
  `run` emit.
- `_ready_package`: set `ASK_LAYER` to `nn` for the whole body (`token = ASK_LAYER.set(nn)` … `finally:
  ASK_LAYER.reset(token)`). `_draft_each`: the same around each layer's iteration.
- `_draft_each`: emit `package_draft_start(layer=nn)` before the first `say` of each layer.
- `_ready_package`: emit `package_checks_start(layer=nn, attempt=attempt)` just before `await checker.check(`.
  Set `self._package_run = None` at the top of each try. In the StoryError branch, log
  `studio_run=self._package_run or st.runs[-1]`.
- `_resumed`: set `self._package_run = run` *before* `await say(...)`.
- `_agree`: emit `amigos_start(resumed=st.meeting is not None)` just before the meeting's worktree is made.
- `StoryWaiting.__init__(self, message: str, *, on: str)`. Both raises in `_stacked_base` pass `on=dep`.
  `_drive` calls `self._wait(st, card, str(e), e.on)`. `_wait(..., on: str)` emits `waiting(reason=reason, on=on)`.

- [ ] **Step 4: Run them to see them pass**

Run: `uv run pytest tests/unit/test_story_pipeline.py -q`
Expected: PASS.

- [ ] **Step 5: Gate and commit**

Run the Global Constraints gate. Commit: `story: log the starts and the layer the workflow screen needs`.

---

### Task 2: The build run logs both gate runs

**Files:**
- Modify: `src/airlie/topology/studio.py` (`_tests_pass`)
- Test: `tests/unit/test_studio_orchestrator.py`

**Interfaces:**
- Produces, on the studio run's `events.jsonl`: `gate_start {n}` (1 for the first gate run, 2 for the run after
  the follow-up), then `gate_passed {n}` when it passes. A failure keeps today's `gate_failed` (first run) or
  `run_done ok:false` (second run).

- [ ] **Step 1: Write the failing test**

In `test_a_failing_final_gate_sends_the_named_subtask_its_output_once` (line 1395) add:

```python
gates = [(e["kind"], e["n"]) for e in events(report) if e["kind"] in ("gate_start", "gate_passed")]
assert gates == [("gate_start", 1), ("gate_start", 2), ("gate_passed", 2)]
```

Add `test_a_gate_that_passes_at_once_logs_its_start_and_its_pass`, which asserts
`[("gate_start", 1), ("gate_passed", 1)]` for a run whose gate passes first time (reuse any one-build passing
test's setup).

- [ ] **Step 2: Run to see it fail**

Run: `uv run pytest tests/unit/test_studio_orchestrator.py -q -k gate`
Expected: FAIL (no `gate_start`).

- [ ] **Step 3: Implement**

`_tests_pass` keeps a counter `self._gate_runs` (initialised to 0 in `__init__`). After the early `return None, ""`,
it adds 1, emits `gate_start(n=self._gate_runs)`, and emits `gate_passed(n=...)` before returning a pass. This
includes the `gate_memo` shortcut.

- [ ] **Step 4: Run to see it pass**

Run: `uv run pytest tests/unit/test_studio_orchestrator.py -q`
Expected: PASS.

- [ ] **Step 5: Gate and commit**

Commit: `studio: log each gate run's start and pass`.

---

### Task 3: The flow's plan lane

**Files:**
- Create: `src/airlie/serve/flow_parts.py`. Move into it, unchanged except for dropping the leading
  underscore: `_job`, `_tokens`, `_run_events`, `_detail`, `_timing`, `_status_of`, `_safe`, `_name`, `_load`,
  `StoryNotFound`.
- Create: `src/airlie/serve/flow_plan.py`
- Modify: `src/airlie/serve/flow.py` (import from the two; `_stage_node` and `_meeting_job` move to
  `flow_plan.py`)
- Test: `tests/unit/test_story_flow_plan.py` (new); update `tests/unit/test_story_flow.py`

**Interfaces:**
- `flow_parts`:
  - `tag(text: str, tone: Literal["warn","info","ok","danger","chip"]) -> dict`
  - `card(title: str, rows: list[dict], *, tags: Sequence[dict] = (), foot: str | None = None) -> dict`
  - Row builders:
    - `squares(label: str, marks: Sequence[str], text: str | None = None) -> dict`. Each mark is a status, or
      `"warn"`, or `"code-passed"` for a checker turn.
    - `step_row(status: str, text: str, seconds: float | None = None) -> dict`
    - `text_row(label: str, text: str) -> dict`
    - `votes(rows: Sequence[dict]) -> dict`, where each row is `{role, requirements, changes, proof}`.
  - The rows' wire shape is the `CardRow` type in Task 6.
- `flow_plan.plan_nodes(state_dir: Path, st: dict, events: list[dict]) -> list[dict]` returns, in order, the
  nodes `intake`, `base`, `gate`, `survey`, `refine`, `layers`, `amigos`, `planned`:
  - Every node has `lane: "plan"`.
  - Survey and Refine have `group: "same-time"`.
  - Every job has `shape`: `"code"` for intake, base and gate; `"milestone"` for planned; `"model"` for the rest.
  - Every job has `live: str | None`, `tags: list`, `card: dict | None`.
- A node with no jobs (survey off, no meeting) stays in the list with `jobs: []`, as `survey` does today.
- Statuses here may be `running`; Task 5 settles them against liveness.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_story_flow_plan.py`, built with
  `test_story_flow.write_events`)

```python
def test_the_plan_lane_runs_intake_base_gate_survey_refine_layers_amigos_planned(tmp_path): ...
    assert [n["id"] for n in flow["nodes"]][:8] == ["intake", "base", "gate", "survey", "refine", "layers", "amigos", "planned"]
    assert {n["id"]: n["lane"] for n in flow["nodes"][:8]} == dict.fromkeys(ids, "plan")
    assert by_id(nodes, "survey")["group"] == by_id(nodes, "refine")["group"] == "same-time"
    assert [by_id(nodes, i)["jobs"][0]["shape"] for i in ("intake", "base", "gate")] == ["code"] * 3
    assert by_id(nodes, "planned")["jobs"][0]["shape"] == "milestone"

def test_the_gate_card_lists_its_commands_and_the_agents_md_commit(tmp_path): ...
    # state.gate = {"commands": ["uv sync --frozen", "uv run mypy"], "agents_md": "29021b8"}; base sha e8146e0…
    job = by_id(nodes, "gate")["jobs"][0]
    assert job["status"] == "passed" and job["card"]["title"] == "Gate"
    assert [r["text"] for r in job["card"]["rows"] if r["kind"] == "step"] == ["uv sync --frozen", "uv run mypy"]

def test_plan_layers_card_shows_rounds_fixes_and_over_budget(tmp_path): ...
    # AIR-47's events: 2 plan_fix, 1 retry stage layers, plan_round 1, plan_round_unusable 2, plan_done, budget_exceeded stage plan
    job = by_id(nodes, "layers")["jobs"][0]
    rounds = next(r for r in job["card"]["rows"] if r["kind"] == "squares" and r["label"] == "rounds")
    assert rounds["marks"] == ["passed", "failed"]
    fixes = next(r for r in job["card"]["rows"] if r["label"] == "fixes")
    assert fixes["marks"] == ["warn", "warn", "warn"]
    assert {"text": "over budget", "tone": "warn"} in job["tags"]

def test_the_meeting_is_its_own_box_running_from_its_start(tmp_path): ...
    # amigos_start, then one amigos_turn (product), no end
    job = by_id(nodes, "amigos")["jobs"][0]
    assert job["id"] == "meeting" and job["status"] == "running" and job["live"] == "architect"  # next in the round
    # then amigos_agreed: passed, card votes are each role's last verdict
    assert votes_row["votes"][0] == {"role": "product", "requirements": "agree", "changes": "agree", "proof": "agree"}

def test_an_old_meeting_without_a_start_runs_from_its_skill_pick(tmp_path): ...
    # skill {subtask: amigos}, no amigos_start, no turns yet: running

def test_no_survey_and_no_meeting_draw_no_boxes_but_planned_still_shows(tmp_path): ...
    # survey never ran, no amigos_* events, state.agreement set, stage "layers"
    assert by_id(nodes, "survey")["jobs"] == [] and by_id(nodes, "amigos")["jobs"] == []
    assert by_id(nodes, "planned")["jobs"][0]["status"] == "passed"
```

In `tests/unit/test_story_flow.py`, update `test_the_flow_lists_the_stages_in_order_with_their_status` to the new
id order (`intake, base, gate, survey, refine, layers, amigos, planned, packages, layer-01, layer-02, publish,
review`). Move the meeting tests (lines 682 to 736) from node `layers` to node `amigos`.

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/unit/test_story_flow_plan.py tests/unit/test_story_flow.py -q`
Expected: FAIL (no such nodes, no `lane`).

- [ ] **Step 3: Implement** `flow_parts.py`, then `plan_nodes`, following the spec's table:
- **intake:** passed at stage ≥ intake.
- **base:** passed when `st["base"]`; its sub line is `ref` plus a 7-character sha.
- **gate:** passed when `st["gate"]` or a `gate` event exists. It failed when the story is blocked with no
  `st["gate"]` and stage `intake`. Card: commands and AGENTS.md commit.
- **survey and refine:** as today's `_stage_node`.
- **layers:** today's job, plus the rounds/fixes card and the over-budget tag.
- **amigos:** today's `_meeting_job`. It runs from `amigos_start` (or a `skill` event with `subtask: "amigos"` when
  there's no start). `live` is the role after the last turn's role in `product → architect → tester → airlie`,
  and `"product"` before any turn. The card has the turn squares (airlie's turns as `"code-passed"`), each role's
  last `verdict`, `objections`/`resolved`, the end, and a skill line `text_row("skill", chosen or "none")`.
- **planned:** passed on `story_planned`, or when `st["agreement"]` is set and stage ≥ layers. Its sub line is
  the time.

`story_flow` uses `plan_nodes` in place of the first four `_stage_node` calls. Today's `layers` node no longer
carries the meeting.

- [ ] **Step 4: Run to see them pass.** Same command. Expected: PASS.

- [ ] **Step 5: Gate and commit.** Commit: `serve: the story flow's plan lane, box per step`.

---

### Task 4: The flow's build lane

**Files:**
- Create: `src/airlie/serve/flow_build.py`. Today's `_layer_node`, `_package_job` and the legacy `tests` path move
  here and are rewritten as described below.
- Create: `tests/fixtures/flow/AIR-48/` (`state.json` from `state.pre-drop-layer02-2026-11-01.json`, the story's
  `events.jsonl` trimmed to layer 02's events plus the stops and resumes, and run `20261101-140822-8efc/events.jsonl`
  without its `step` lines) and `tests/fixtures/flow/AIR-47/` (state and events as of 16:12 on 2026-11-02).
- Modify: `src/airlie/serve/flow.py`
- Test: `tests/unit/test_story_flow_build.py` (new); update the layer tests in `tests/unit/test_story_flow.py`
  (lines 213 to 434)

**Interfaces:**
- `flow_build.build_nodes(state_dir: Path, st: dict, events: list[dict]) -> list[dict]` returns `packages`, then
  `layer-NN` for each `st["layers"]`, then `publish` and `review`. All have `lane: "build"`.
- The `packages` node has one job `packages`, label `Draft packages`, shape model.
  - Its card has one `step_row` per layer turn: `"L01 · package → critic agree"`.
  - `live` is `"L03 · package"` or `"L03 · critic"`.
- A layer node has:
  - `rows: list[dict]`: `{"kind": "cycle", "id": "c1", "label": str, "status": str, "jobs": [...]}` or
    `{"kind": "divider", "text": str, "card": dict | None}`, in log order.
  - `jobs`: the last cycle's jobs (pending jobs when no cycle yet). The last cycle's job ids are plain; earlier
    cycles' ids are `c{n}-{job}`.
  - `attempts`: the number of builds.
  - `tags`: `re-planned` when any `layer_replanned`.
- Each cycle's jobs are `package` (model), `checks` (code), `place` (code), `build` (model) and `squash` (code).
- `cycles(nn: str, events: list[dict]) -> list[dict]` is the pure grouping function (exported for its tests).

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_story_flow_build.py`)

```python
def test_drafting_lights_the_running_turn(fixture AIR-47):
    job = by_id(nodes, "packages")["jobs"][0]
    assert job["status"] == "running"
    assert [r["status"] for r in job["card"]["rows"] if r["kind"] == "step"] == ["passed", "passed", "running", "pending", "pending"]
    assert job["live"] == "L03 · package"  # an old log: no package_draft_start, the turn after the last drafted

def test_a_drafting_turn_with_its_start_logged_is_the_running_one(tmp_path):
    # packages_drafting, package_draft_start 01: L01 running, live "L01 · package"; then run stage critic layer 01: live "L01 · critic"

def test_a_drafting_stopped_by_an_unusable_reply_marks_that_turn_failed(tmp_path):
    # state.drafts["02"] = {"unusable": "..."}: L02 failed, L03..: pending, the job passed (drafting ended)

def test_a_resumed_layer_has_one_row_per_try_cycle_with_dividers(fixture AIR-48):
    rows = by_id(nodes, "layer-02")["rows"]
    assert [r["kind"] for r in rows] == ["cycle", "divider", "cycle", "cycle", "divider", "cycle"]
    assert [r["label"] for r in rows if r["kind"] == "cycle"] == ["package · stopped", "build 1 · red → re-planned", "build 2 · red", "package · stopped"]
    assert rows[1]["text"].startswith("blocked · resumed ")

def test_tries_are_numbered_by_their_place_in_the_cycle(tmp_path):
    # package_start attempt 1, package_returned, package_start attempt 2, amigos_proof_revised, package_start attempt 2 (repeat), package_ready
    (cycle,) = cycles("01", events)
    package = cycle["jobs"][0]
    assert package["attempts"] == 3
    assert [r["text"] for r in package["card"]["rows"] if r["kind"] == "step"][:3] == ["try 1 · returned: lint_fails", "try 2 · proof revised", "try 3 · ready"]

def test_the_build_card_lists_coder_reviewer_gate_and_follow_up(fixture AIR-48, run 8efc):
    build = next(r for r in by_id(nodes, "layer-02")["rows"] if r.get("label") == "build 2 · red")["jobs"][3]
    steps = [(r["text"], r["status"]) for r in build["card"]["rows"] if r["kind"] == "step"]
    assert steps == [("coder", "passed"), ("reviewer", "passed"), ("gate", "failed"), ("coder · gate follow-up", "passed"), ("gate", "failed")]

def test_a_build_is_not_done_while_its_gate_follow_up_runs(tmp_path):
    # run: subtask_start build, subtask_done build ok, gate_failed, follow_up (no end, no layer_built)
    assert build["status"] == "running" and build["live"] == "coder · gate follow-up"

def test_an_old_run_without_gate_events_shows_the_gate_running_after_the_coder(tmp_path):
    # run: subtask_start build, subtask_done build ok; no gate_*, no run_done
    assert build["live"] == "gate"

def test_supervisor_triages_go_to_the_build_they_happened_in_by_time(tmp_path):
    # a supervisor_triage with layer "01" whose ts is inside layer 02's build window counts for layer 02's build
    assert "supervisor: 1 triage" in [r["text"] for r in build["card"]["rows"] if r["kind"] == "text"]

def test_publish_and_review_are_code_steps(tmp_path): ...
```

In `tests/unit/test_story_flow.py`, the layer jobs become `["package", "checks", "place", "build", "squash"]`
(the legacy `tests` job stays for a story recorded before P1). The `checks` job label changes from `Test checks`
to `checks`.

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/unit/test_story_flow_build.py tests/unit/test_story_flow.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement `cycles`.** It walks the story's events in order and is not derived from the
  signatures, so the algorithm is pinned here:

```text
open = None; out = []
for e in events:
    if e.kind in ("blocked", "cancelled") and open is not None:
        open.end = "stopped"; out.append(open); open = None; pending_divider = e
    elif e.kind == "story_start" and e.resumed and pending_divider:
        out.append(divider(pending_divider, resumed_at=e.ts)); pending_divider = None
    elif e.layer == nn:
        if open is None:
            if pending_divider: out.append(divider(pending_divider, resumed_at=None)); pending_divider = None
            open = new cycle
        open.events.append(e)
        if e.kind in ("layer_built", "layer_replanned"): open.end = e.kind; out.append(open); open = None
if open: out.append(open)   # the running or last cycle
```

A divider's card holds the block's reason. It also lists the `fix_filed` / `fix_linked` / `fix_noted` events and
the `supervisor_triage` with `trigger: "block"` that fall between the block and the resume.

Then build each cycle's five jobs from the spec's table:
- `package_checks_start` starts the checks, falling back to the try's `package_start`.
- Critic and proof are `run` events with this layer. For old logs, pair a `run` event with the next event naming
  a layer.
- The build's inner steps come from the run named by the cycle's `layer_start`.
- The gate is `gate_start` / `gate_passed` / `gate_failed`. With no `gate_*` in a run, it is running between the
  last coder `subtask_done` and `run_done`.
- `done_check` gets a row only when logged.
- Triages count for a build when their `ts` falls between its `layer_start` and its end.
- Cycle labels are `build k · built`, `build k · red → re-planned`, `build k · red`, `package · stopped`, or
  `package` while running.
- `packages` turns use `package_draft_start`, falling back to the layer after the last `package_drafted` while
  drafting runs. They use `amigos_critic` per layer and `st["drafts"]` for an unusable reply or a proof change.

Check kinds are `CHECK_KINDS` plus the older `vacuous_test`, `unparseable_test` and `test_checks_red_base`. Any
other kind on a layer's event between a try's start and its outcome counts as a finding. The retired mutation
events (`mutation_gate*`, `run` stage `mutation_review`) are ignored. Lanes come only from `st["layers"]`: events
for a layer dropped from the state are not drawn.

`story_flow` uses `build_nodes` in place of today's layer, publish and review nodes.

- [ ] **Step 4: Run to see them pass.** Same command. Expected: PASS.

- [ ] **Step 5: Gate and commit.** Commit: `serve: the story flow's build lane, try cycles and inner steps`.

---

### Task 5: The now bar, the exit row, liveness and job detail

**Files:**
- Create: `src/airlie/serve/flow_now.py`
- Modify: `src/airlie/serve/flow.py` (`story_flow`, `job_detail`), `src/airlie/serve/app.py`
  (`story_flow_get`, `story_job_get`)
- Test: `tests/unit/test_story_flow_now.py` (new); `tests/unit/test_serve.py` for the route

**Interfaces:**
- `flow_now.story_live(events: list[dict], run_last_ts: float | None, *, held: bool, now: float) -> bool`
  - True when `held`.
  - False when the last event is `story_done`, `blocked`, `waiting` or `cancelled`.
  - Otherwise True when `now - max(last event ts, run_last_ts) <= ACTIVE_S` (import from `airlie.serve.running`).
- `flow_now.settle(nodes: list[dict], *, live: bool, blocked: bool) -> None` turns every `running` job, cycle and
  node into `failed` when `blocked`, or `stopped` when not live.
- `flow_now.story_now(nodes, st, events, *, live: bool) -> dict` returns `{status: "running"|"stopped"|"idle",
  path: list[str], node, job, inner, role, steps, started, last_event, text}`.
  - `path` is lane label (`Plan lane` / `Build lane`), node label, then for a layer the latest cycle label.
  - `inner` is the job's `live`.
  - `text` when idle: `Blocked at <path>`, `Waiting on <on>`, `Cancelled during <step>`, `In review · N PRs`,
    `Planned · waiting for the build lane`, `Not started`.
  - When stopped: `Stopped at <path> · no event since HH:MM`.
- `flow_now.story_exit(state_dir: Path, key: str, st: dict, events: list[dict], nodes: list[dict]) -> dict | None`
  returns `{kind, where, times, at, reason, on, jobs}` or None. Its `jobs` are `diagnose` (model, role from the
  diagnosis or `"muse"`), `fix` (code, sub the fix key) and `card` (code, `→ blocked`, or `→ To Do` for
  waiting/cancelled).
  - Present when `st["blocked"]` is set, or the last of `waiting` / `cancelled` / `story_start` is a `waiting` or
    `cancelled`.
- `story_flow(state_dir: Path, key: str, *, held: bool = False, now: float | None = None) -> dict` adds `now` and
  `exit`.
- `app.story_flow_get` passes `held=(j := jobs.get(f"story-{key}")) is not None and j.state == "running"`.
- `job_detail` also finds jobs in a layer's `rows` and in `exit.jobs`:
  - team is the job id without its `c{n}-` prefix; for node `packages` it is `packages`.
  - code-shaped jobs get `input_note` "airlie's own check, no model".
  - node `amigos` / job `meeting` keeps today's output and note.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_story_quiet_for_over_an_hour_with_no_stop_is_stopped_not_running(fixture AIR-47, now=last_ts + 3601):
    flow = story_flow(state_dir, "AIR-47", now=...)
    assert by_id(flow["nodes"], "packages")["jobs"][0]["status"] == "stopped"
    assert flow["now"]["status"] == "stopped" and flow["now"]["text"].startswith("Stopped at Build lane › Draft packages")

def test_a_quiet_story_this_server_holds_is_still_running(...held=True...):
    assert flow["now"]["status"] == "running" and flow["now"]["path"] == ["Build lane", "Draft packages"]
    assert flow["now"]["inner"] == "L03 · package"

def test_a_planned_story_waiting_for_the_build_lane_says_so(...):  # last events story_planned, story_done
    assert flow["now"] == {**flow["now"], "status": "idle", "text": "Planned · waiting for the build lane"}

def test_a_blocked_story_has_an_exit_row_with_its_diagnosis_and_fix_card(fixture AIR-48):
    ex = flow["exit"]
    assert ex["kind"] == "blocked" and ex["times"] == 3
    assert [j["id"] for j in ex["jobs"]] == ["diagnose", "fix", "card"]
    assert [j["shape"] for j in ex["jobs"]] == ["model", "code", "code"]

def test_a_waiting_story_names_what_it_waits_on(...):  # waiting {on: "AIR-4"}
    assert flow["exit"]["kind"] == "waiting" and flow["exit"]["on"] == "AIR-4"

def test_a_story_that_resumed_after_a_block_has_no_exit_row(...):  # blocked, then story_start resumed

def test_job_detail_opens_an_earlier_cycles_build_with_its_run_trace(fixture AIR-48):
    d = job_detail(state_dir, "AIR-48", "layer-02", "c2-build")
    assert d["job"]["studio_run"] and d["input_note"] != "airlie's own check, no model"

def test_job_detail_for_a_code_step_says_no_model(...):  # layer-01 / place
```

The route test in `tests/unit/test_serve.py`: with a running job `story-AIR-1` registered in the app's job store, the
flow's `now.status` is `running` even when the log is older than an hour.

- [ ] **Step 2: Run to see them fail.** Run: `uv run pytest tests/unit/test_story_flow_now.py tests/unit/test_serve.py -q`. Expected: FAIL.

- [ ] **Step 3: Implement** the interfaces above.
  - `run_last_ts` is the last event's `ts` in the run named by the deepest running job's `studio_run`, or None.
  - `story_flow` builds the nodes, computes `live`, calls `settle`, then `story_now` and `story_exit`.

- [ ] **Step 4: Run to see them pass.** Run: `uv run pytest tests/unit -q -k "flow or serve"`. Expected: PASS.

- [ ] **Step 5: Gate and commit.** Commit: `serve: the story flow's now bar, exit row and liveness`.

---

### Task 6: Web types, the code box and the hover card

**Files:**
- Modify: `web/src/api/workflows.ts`, `web/src/test-utils/workflow-fixtures.ts`,
  `web/src/screens/workflow/style.ts`, `web/src/screens/workflow/JobBox.vue`
- Create: `web/src/screens/workflow/CodeBox.vue`, `MilestoneMark.vue`, `FlowCardView.vue`, `JobHover.vue`
  (each with a `.test.ts`)

**Interfaces** (TypeScript, `workflows.ts`):

```ts
export type FlowStatus = "pending" | "running" | "passed" | "failed" | "skipped" | "stopped";
export type FlowTag = { text: string; tone: "warn" | "info" | "ok" | "danger" | "chip" };
export type CardRow =
  | { kind: "squares"; label: string; marks: string[]; text: string | null }
  | { kind: "step"; status: FlowStatus; text: string; seconds: number | null }
  | { kind: "text"; label: string; text: string }
  | { kind: "votes"; votes: { role: string; requirements: string | null; changes: string | null; proof: string | null }[] };
export type FlowCard = { title: string; tags: FlowTag[]; rows: CardRow[]; foot: string | null };
// FlowJob gains: shape?: "model" | "code" | "milestone"; live?: string | null; tags?: FlowTag[]; card?: FlowCard | null;
export type LaneRow =
  | { kind: "cycle"; id: string; label: string; status: FlowStatus; jobs: FlowJob[] }
  | { kind: "divider"; text: string; card: FlowCard | null };
// FlowNode gains: lane?: "plan" | "build"; group?: string | null; rows?: LaneRow[]; tags?: FlowTag[];
export type FlowNow = { status: "running" | "stopped" | "idle"; path: string[]; node: string | null; job: string | null; inner: string | null; role: string | null; steps: number | null; started: number | null; last_event: number | null; text: string | null };
export type FlowExit = { kind: "blocked" | "waiting" | "cancelled"; where: string | null; times: number; at: number | null; reason: string | null; on: string | null; jobs: FlowJob[] };
// Flow gains: now?: FlowNow; exit?: FlowExit | null;
```

- `style.ts`:
  - `BOX.stopped = "border-dashed border-warn-foreground"`; `DOT.stopped = "bg-warn-foreground"`.
  - Add `MARK: Record<string, string>` for squares: statuses, plus `warn` → `bg-warn-foreground` and
    `code-passed` → a round `bg-ok-foreground`.
- `CodeBox.vue` has the same props and emits as `JobBox`. Square corners (`rounded-[4px]`), `font-mono`,
  `bg-[var(--code)]`, a 7px status-colored square before the label. `data-shape="code"`.
- `JobBox.vue`:
  - The second line shows `job.live` while `status === "running"` and `live` is set, else today's `sub`.
  - Shows each `job.tags` entry as a chip.
  - Gets `data-shape="model"`.
- `FlowCardView.vue` takes `{ card: FlowCard }` and renders the mockups' hover card. It has `data-card`, and each
  row has `data-row=<kind>`.
- `JobHover.vue` takes `{ card: FlowCard | null | undefined }` and wraps its default slot.
  - With a card, it's reka-ui `HoverCardRoot` (`openDelay` 250, `closeDelay` 100) / `HoverCardTrigger as-child` /
    `HoverCardPortal` / `HoverCardContent` (side bottom, collision padding 16) holding `FlowCardView`.
  - Without a card, it renders only the slot.
  - On a phone (`useMediaQuery(PHONE_QUERY)`), it renders only the slot.

- [ ] **Step 1: Write the failing tests**

```ts
test("a code step draws square with a mono label and its status", () => { /* CodeBox: data-shape=code, data-status=passed, text "gate" */ });
test("a running box says what it is on right now", () => {
  const w = mount(JobBox, { props: { nodeId: "layer-02", job: job({ status: "running", live: "coder · gate follow-up" }) } });
  expect(w.text()).toContain("coder · gate follow-up");
});
test("a stopped box is dashed amber", () => { /* BOX.stopped classes on data-status=stopped */ });
test("the card lists its squares, steps, text and votes", () => { /* FlowCardView with one row of each kind: 4 [data-row] */ });
test("focusing a box with a card opens its card", async () => {
  // mount JobHover around a button, attachTo document.body; trigger focus; await flushPromises
  expect(document.body.querySelector("[data-card]")).not.toBeNull();
});
test("on a phone a box never opens a hover card", async () => { /* phone(true); hover: no [data-card] */ });
```

- [ ] **Step 2: Run to see them fail.** Run: `cd web && npx vitest run src/screens/workflow`. Expected: FAIL.

- [ ] **Step 3: Implement** the interfaces above.
- [ ] **Step 4: Run to see them pass.** Same command. Expected: PASS.
- [ ] **Step 5: Gate and commit.** Commit: `web: code boxes, the stopped status and hover cards`.

---

### Task 7: The laptop pipeline (option C)

**Files:**
- Modify: `web/src/screens/workflow/Pipeline.vue` (desktop branch), `LayerLane.vue`,
  `web/src/screens/WorkflowScreen.vue`
- Create: `web/src/screens/workflow/NowBar.vue`, `ExitRow.vue` (each with a `.test.ts`)
- Test: `web/src/screens/workflow/Pipeline.test.ts`, `LayerLane.test.ts`

**Interfaces:**
- `Pipeline.vue` props add `now?: FlowNow` and `exit?: FlowExit | null`. `WorkflowScreen.vue` passes
  `flow.data.value.now` and `.exit`.
- A box renders through `JobHover`, wrapping `CodeBox` when `shape === "code"`, `MilestoneMark` when
  `"milestone"`, else `JobBox`.
- Consecutive stage nodes with the same `group` render as one bracket (`data-group`, label "same time").
- `NowBar.vue` takes `{ now: FlowNow }` and has `data-now` and `data-now-status`.
  - Running shows a dot, `Running`, the path joined by ` › `, `inner`, `step N · <elapsed>` and muted
    `last event Ns ago`.
  - Otherwise it shows `text`.
  - No color change for quiet time.
- `ExitRow.vue` takes `{ exit: FlowExit }` and has `data-exit=<kind>`. It shows the kind label, `where`, a
  `blocked N times` tag when `times > 1`, then its jobs as boxes (clickable, same `pick` emit).
- `LayerLane.vue`:
  - Shows the header and nothing else when `rows` is empty, or when every row's jobs are pending (a layer not
    started).
  - Otherwise each `cycle` is a job row (with its label as a prefix when there is more than one row), and each
    `divider` is a thin row with `data-divider`, wrapped in `JobHover` with its card.
  - Shows node `tags` in the header.
  - Clicks on earlier cycles emit their own job ids (`c1-build`).

- [ ] **Step 1: Write the failing tests**

```ts
test("the row runs intake, base, gate, the same-time pair, plan layers, amigos, planned, drafting, the lanes, publish, review", () => {
  // nodes from fixtures in that order; assert data-node order of top-level items and one [data-group] holding survey and refine
});
test("the now bar names the running step and its inner step", () => {
  // now {status:"running", path:["Build lane","Draft packages"], inner:"L03 · package", steps: 6, ...}
  expect(w.get("[data-now]").text()).toContain("Build lane › Draft packages");
  expect(w.get("[data-now]").text()).toContain("L03 · package");
});
test("a stopped story's now bar says where it stopped", () => { /* status stopped, text "Stopped at …" */ });
test("a layer that has not started is only its header", () => { /* rows: [], no [data-job] inside */ });
test("a resumed layer shows each try cycle and a divider between", () => {
  // rows cycle, divider, cycle: two job rows, one [data-divider]; the first row's build emits pick("layer-02","c1-build")
});
test("a blocked story ends with its exit row", () => { /* [data-exit=blocked], jobs diagnose, fix, card; "blocked 3 times" */ });
```

Update the existing `Pipeline.test.ts` tests to the new order and jobs.

- [ ] **Step 2: Run to see them fail.** Run: `cd web && npx vitest run src/screens/workflow src/screens/WorkflowScreen.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces above, matching the mockups' option C.
- [ ] **Step 4: Run to see them pass.** Same command. Expected: PASS.
- [ ] **Step 5: Gate and commit.** Commit: `web: the story pipeline draws every step (option C)`.

---

### Task 8: The phone list and its bottom sheet

**Files:**
- Create: `web/src/screens/workflow/PhoneList.vue`, `JobSheet.vue` (each with a `.test.ts`)
- Modify: `web/src/screens/workflow/Pipeline.vue` (phone branch uses `PhoneList`),
  `web/src/screens/WorkflowScreen.vue` (on a phone, the selected job opens in `JobSheet`, not below)

**Interfaces:**
- `PhoneList.vue` takes `{ nodes: FlowNode[]; now?: FlowNow; exit?: FlowExit | null; selected: [string, string] | null }`
  and emits `pick: [node, job]`.
  - The now bar is `sticky top-[env(safe-area-inset-top,0px)]` (`data-now`).
  - The rows sit under `PLAN LANE` / `BUILD LANE` labels (`data-lane-label`).
  - **Fold rule:** a lane whose nodes are all `passed` or skipped is one row (`data-folded=plan`) with a chip per
    step. Tapping it opens the lane (local `ref`), and tapping again folds it.
  - A built layer is one row. A pending layer is one row.
  - The node holding the running (or last failed) job is expanded (`data-open`). Its jobs are listed, and the
    running job's card `step` rows are listed under it. The running inner step is outlined
    (`data-current`).
  - In an expanded layer, earlier cycles are folded rows (`build 1 · red → re-planned`) and dividers are small
    rows.
  - The exit row is last.
  - Rows are one line and truncate (`truncate`, `min-w-0`).
- `JobSheet.vue` takes `{ open: boolean; job: FlowJob | null; detail: JobDetail | null; nodeLabel?: string }`
  and emits `close`.
  - It's the app's `ui/dialog`, styled as a bottom sheet (fixed to the bottom, full width, rounded top, a grab
    bar, max height 85vh, scrolls inside).
  - It shows `FlowCardView` when `job.card` is set, then `JobDetailView`. `data-sheet`.
- `WorkflowScreen.vue`: on a phone, `JobSheet` is open while a job is selected, and closing it clears the
  selection (`router.push` to the story path). On a laptop, nothing changes.

- [ ] **Step 1: Write the failing tests**

```ts
test("a finished plan lane folds to one row of chips and opens on tap", async () => { /* [data-folded=plan] with 8 chips; click → its rows appear */ });
test("the running step is open with its inner steps and the current one outlined", () => {
  // build lane: packages running with card rows; expect [data-open][data-node=packages] and one [data-current]
});
test("a built layer and a pending layer are one row each", () => { ... });
test("the now bar stays pinned at the top", () => { /* [data-now] has class sticky */ });
test("an 8-layer story with 120-character titles keeps one line per layer", () => { /* every [data-node^=layer] row has truncate */ });
test("tapping a row on a phone opens the sheet with the card and the detail", async () => {
  // WorkflowScreen with phone(true), select layer-02/build: [data-sheet] contains [data-card] and section[aria-label=Job]
});
test("closing the sheet clears the selection", async () => { ... });
```

- [ ] **Step 2: Run to see them fail.** Run: `cd web && npx vitest run src/screens/workflow src/screens/WorkflowScreen.test.ts`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces above, matching the mockups' "On a phone" section.
- [ ] **Step 4: Run to see them pass.** Same command. Expected: PASS.
- [ ] **Step 5: Gate and commit.** Commit: `web: the phone pipeline folds what is done and opens the sheet on tap`.

---

### Task 9: Browser checks and the real stories

**Files:**
- Modify: `tests/e2e/test_workflow_story.py`, and the shared story in `tests/e2e/conftest.py` if its events lack
  what a check needs
- Create: `docs/bench/2026-11-02-pipeline-screen-check.md` (screenshots linked, what was seen)

- [ ] **Step 1: Update the e2e checks.**
  - Rewrite the existing assertions for the new structure: `[data-lane]` rows, `[data-shape]`, the jobs
    `package/checks/place/build/squash`, the folded phone list in place of the old phone layout.
  - Add these checks, each with `evidence.expect` and `evidence.shot`:
    - the now bar names the running or stopped step;
    - hovering the Build box opens a card listing coder, reviewer and gate;
    - keyboard focus on a box opens its card;
    - the exit row shows for the blocked story;
    - at 375 px, tapping layer 02's Build opens the sheet with the card and the trace, and closing it returns to
      the list.
- [ ] **Step 2: Run them.** Run: `task test:e2e -- -k workflow_story`. Expected: PASS, with screenshots in
  `test-results/e2e/evidence/`.
- [ ] **Step 3: Check the real stories as Noah would.**
  - `task web:build`, then run this worktree's server on a free port against the real state directory:
    `uv run airlie serve --port 8091`. It only reads the stories.
  - Open `/workflows/AIR-47` and `/workflows/AIR-48` at 1280 px and 375 px.
  - Confirm:
    - AIR-47: the now bar and the lit box name the drafting turn, or the build if it has moved on.
    - AIR-48: layer 02 shows its try cycles with dividers and its exit row with the diagnosis and fix card.
  - Save the screenshots and write what was seen in `docs/bench/2026-11-02-pipeline-screen-check.md`. Note any
    box that is dark while its step runs as a defect to fix before finishing.
- [ ] **Step 4: Gate and commit.** Commit: `e2e: the story pipeline's every step, now bar, cards and sheet`.
