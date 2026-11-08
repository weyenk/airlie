# Nodes Are Config, Plan 1: The Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A model node runs from its own fields alone (type, role, prompt text, inputs, outputs with fields, steps, tools), checks come from a declared catalog, wiring is lines that carry artifacts, bundles are steps with ports, a team node spins up workers, and the story workflow runs on this surface with the same events and state as today.

**Architecture:** The schema (`flow/spec.py`) loses every registry id on model nodes and gains outputs with fields, `to` lines with conditions and loops, a `bundle` type with a declared interface and a `team` type. The composer becomes a dataflow scheduler: a node runs when the artifacts it reads exist and every line that could carry them has fired or died; two lines out of a node run their targets at once. Prompts are text rendered from inputs (`flow/render.py`); structured answers are parsed by the output's field list (`flow/outputs.py`); conditions are a small expression language (`flow/conditions.py`); checks are a catalog of declared operations (`flow/catalog.py`). The story and layer workflows are rewritten on this surface with their briefs as prompt files, and the side-by-side tests against the driver stay the contract. Old snapshots and drafts are upgraded structurally; a node that still names code is marked, never run.

**Tech Stack:** Python 3.12, uv, pydantic `Strict` models, pytest with `FakeEngine`, ruff, mypy. No web changes in this plan (plan 2 is the builder).

**Spec:** `docs/superpowers/specs/2026-11-08-nodes-are-config-design.md` (D1–D25). Companion: `docs/superpowers/specs/2026-11-06-generic-nodes-design.md` (Part 1), whose side-by-side tests this plan keeps green.

## Global Constraints

- Every config model extends `Strict` (`src/airlie/config/schema.py:15`). New keys are declared fields.
- With `story.composer: false` and no `workflow:` label a story's `events.jsonl` and `state.json` are byte-identical to the base commit; `StoryState.save()` omits the new `done` field when empty, as it omits `artifacts`.
- The side-by-side tests in `tests/unit/test_story_flow.py` (driver against composer: `EXPECTED_PLAN_KINDS`, `EXPECTED_RUN_KINDS`, `plan_events`, `plan_state`, `built_run`) stay green after every task that touches the story; no prompt text a model receives changes (the equivalence tests are the proof; the Python briefs in `pipeline.py`/`briefs.py` stay for the driver, D12).
- Nothing merged into the repository ever comes from the board database; sessions never reach `state.json`.
- A node of a model type (`ask`, `conversation`, `write`, `meeting`, `team`) carries no registry id: `check_workflows` refuses `run`, `parse`, `after`, `when`, `done`, `next`, `branch`, `loop`, `nodes`, `over` on it (D1).
- Test gate from `AGENTS.md` after every task: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `FORCE_COLOR= uv run pytest -q -n auto --maxprocesses 8`; `task web:gate` only when `config/workflows/*.yaml` changed (the builder's tests read them).
- Commit after every task. Subjects start with `flow:`.

## Review Focus

1. **A prompt names `{sources}` but the node's inputs lack it.** Loading the workflow must fail naming the workflow, node and placeholder, before any model runs. Pinned in Task 3 and Task 1.
2. **A loop line fires after downstream nodes have already produced outputs.** The target and everything that reads its outputs must run again; stale artifacts must not survive the loop. Pinned in Task 5.
3. **Two alternative producers of `story`.** The node after them runs once the taken branch completes and never waits on the branch whose line died. Pinned in Task 5.
4. **A board draft from the old schema names `run: story.intake` on an ask.** Loading must mark the node with a problem and keep the draft unrunnable, never raise. Pinned in Task 9.
5. **A team supervisor asks for twelve workers with a cap of five.** Five run, the supervisor is told the rest were not started, and the node finishes. Pinned in Task 7.

---

## File Structure

**Create:**
- `src/airlie/flow/conditions.py` — the condition expression: parse, free names, evaluate.
- `src/airlie/flow/render.py` — prompt text rendering from inputs, config and feedback.
- `src/airlie/flow/outputs.py` — `OutputSpec`, field kinds, `parse_output`.
- `src/airlie/flow/catalog.py` — `Check` declarations, the `check` decorator, `describe()`.
- `src/airlie/flow/upgrade.py` — `upgrade_v1(mapping) -> mapping`: old wiring to lines; code ids left as problems.
- `src/airlie/flow/team.py` — the team runner.
- `src/airlie/prompts/rules/story-*.md` — the story's briefs as prompt files (Task 8).
- `tests/unit/test_flow_conditions.py`, `test_flow_render.py`, `test_flow_outputs.py`, `test_flow_catalog.py`, `test_flow_upgrade.py`, `test_flow_team.py`.

**Modify:**
- `src/airlie/flow/spec.py` — schema v2 and `check_workflows`.
- `src/airlie/flow/composer.py` — the dataflow scheduler.
- `src/airlie/flow/runners.py` — generic ask, conversation, write, meeting runners.
- `src/airlie/flow/store.py` — `done` set; `MEMORY` unchanged.
- `src/airlie/story/state.py` — `done: list[str]` field, omitted when empty.
- `src/airlie/story/flow.py` — registry trimmed to declared checks; `CATALOG` replaced by `catalog.describe()`.
- `src/airlie/story/workflows.py` — snapshot upgrade, `adopt_current`, use tracking.
- `src/airlie/serve/workflows_api.py`, `src/airlie/serve/app.py`, `src/airlie/cli.py` — catalog shape, `used_by`, adopt route and command.
- `src/airlie/serve/flow_nodes.py` — the run view reads lines and bundles.
- `config/workflows/story.yaml`, `config/workflows/layer.yaml` — rewritten.
- `tests/unit/test_flow_spec.py`, `test_flow_composer.py`, `test_story_flow.py`, `test_story_workflows.py`, `test_flow_nodes.py`, `test_prompt_texts.py` (golden snapshot).

---

### Task 1: Schema v2

**Files:**
- Modify: `src/airlie/flow/spec.py`
- Create: `src/airlie/flow/outputs.py` (the `OutputSpec` model only; parsing comes in Task 3)
- Test: `tests/unit/test_flow_spec.py`

**Interfaces:**
- Produces, in `spec.py`:
  - `NodeType = Literal["ask", "conversation", "write", "meeting", "team", "check", "gate", "bundle"]`.
  - `class Line(Strict): node: str; when: str | None = None; loop: LoopBound | None = None` and `class LoopBound(Strict): max: int | str; carry: str | None = None`.
  - `class TeamRole(Strict): supervisor: str; worker: str`.
  - `NodeSpec` fields: `type`, `role: str | list[str] | TeamRole | None`, `prompt: str | None` (text or `prompt:<id>`), `turn: str | None` (text), `worker: str | None` (text), `inputs: list[str]`, `outputs: dict[str, OutputSpec]`, `steps: int | str | None`, `tools: list[str] | None`, `on_fail: Literal["block", "continue"]`, `events: dict[Literal["done", "failed"], str]`, `session: str | None`, `advisor: str | None`, `workers: int | None`, `run: str | None` (check/gate only), `args: dict[str, Any]` (check/gate only), `workflow: str | None` (bundle), `each: str | None` (bundle: the list input run per item), `to: list[Line]`.
  - `WorkflowSpec` fields: `type: str | None`, `inputs: list[str]` (replaces `loose`), `outputs: list[str]`, `nodes`, `layout`.
  - `check_workflows(workflows, *, prompts=(), catalog: Catalog | None = None) -> list[str]` with these rules, each a message `workflow <w>: node <n>: <what>`:
    1. per type: ask/conversation/write need `role` (str) and `prompt`; meeting needs `role` (list) and `prompt`; team needs `role` (TeamRole), `prompt` and `worker`; check/gate need `run`; bundle needs `workflow`.
    2. a model node with `run`, `args`, `workflow` or `each`; a check/gate with `prompt`, `turn`, `worker`, `session`, `workers`.
    3. an output name reserved (`RESERVED` unchanged); an output's fields grammar invalid (Task 3's `OutputSpec` validator).
    4. a prompt placeholder (`render.names(text)`, Task 3) that is not an input, `feedback`, `item` or `config.*`.
    5. a `to` line to an unknown node; a line `when` whose free names (Task 2) are not a produced output head, a workflow input or `config`.
    6. an input nothing produces: not a workflow input, not an output of any node, not `feedback`.
    7. a bundle's `inputs` not all carried by lines into it, and its `each` not among its inputs; the named workflow's `inputs` not all among the bundle node's inputs.
    8. a `session` input whose producer has a different `role` than this node (D25).
    9. a check's `run` unknown to the catalog, or its `inputs`/`outputs` not matching the declaration (when a catalog is given).
    10. unknown `start`-less cycles: a line forms a cycle without a `loop` bound.
  - `upgrade` is Task 9; this task only defines the new shapes and refuses the old field names with a message that names the upgrade.
- `OutputSpec(Strict): format: Format; fields: dict[str, str] | None = None`; a bare `Format` string in YAML is accepted (a `model_validator(mode="before")` wraps it).

- [ ] **Step 1: Write the failing tests** (replace the old wiring tests; keep the type-and-layout test):

```python
def test_each_type_requires_its_fields():  # ask/conversation/write/meeting/team/check/gate/bundle, one bad case each
def test_a_model_node_may_name_no_code():  # run on an ask → "names code (run) …"
def test_lines_name_known_nodes_and_conditions_name_known_artifacts():
def test_an_input_nothing_produces_is_refused_unless_the_workflow_declares_it():
def test_a_bundle_box_needs_its_lines_and_the_bundles_inputs_must_be_its_inputs():
def test_a_session_line_is_only_between_two_nodes_of_the_same_role():
def test_a_bare_format_is_an_output_spec_without_fields():
def test_a_cycle_without_a_loop_bound_is_refused():
```

- [ ] **Step 2: Run them; fail on the missing types.**
- [ ] **Step 3: Implement the models and `check_workflows`.** `_produced` now includes workflow `inputs` and `feedback.<node>` for every loop line.
- [ ] **Step 4: Gate** (the old composer tests will fail until Task 5: mark them `xfail(strict=True, reason="Task 5")` in this task, remove the marks in Task 5).
- [ ] **Step 5: Commit** — `flow: schema v2: outputs with fields, lines, bundle and team types, no code ids on model nodes`

### Task 2: Conditions

**Files:**
- Create: `src/airlie/flow/conditions.py`, `tests/unit/test_flow_conditions.py`

**Interfaces:**
- `def parse(text: str) -> Condition` raising `ConditionError(ValueError)` with the position and what was expected.
- `Condition.names -> frozenset[str]` (the heads of every path: `verdict`, `card`, `config`).
- `Condition.evaluate(lookup: Callable[[str], Any]) -> bool` where `lookup(path)` returns the value of a dotted path or raises `KeyError`; a missing path makes `exists` false and any comparison false.
- Grammar: `expr := term (("and" | "or") term)*; term := "not" term | "(" expr ")" | atom; atom := path "exists" | path ("==" | "!=") literal | path`. A bare path is truthy. Literals: a quoted string, a number, `true`, `false`, or a bare word (a string). `and` binds tighter than `or`.

- [ ] **Step 1: Tests** — one per grammar rule with exact values: `verdict.decision == object`, `survey exists`, `not survey exists`, `config.amigos.enabled`, `card.level == story and not built exists`, a missing path, a malformed expression's message.
- [ ] **Step 2–5:** fail, implement (a hand-written tokenizer and recursive descent; no library), gate, commit — `flow: a line's condition is a small expression over artifacts, the card and config`

### Task 3: Prompt rendering and output parsing

**Files:**
- Create: `src/airlie/flow/render.py`, `tests/unit/test_flow_render.py`, `tests/unit/test_flow_outputs.py`
- Modify: `src/airlie/flow/outputs.py` (parsing), `src/airlie/flow/prompt_files.py` (`prompt_text` reused)

**Interfaces:**
- `render.names(text: str) -> frozenset[str]`: the placeholder heads in `text`: `{survey}`, `{story.title}`, `{config.story.max_layers}`, `{feedback}`, `{item}`, and the names of `{?name}…{/name}` blocks.
- `render.render(text: str, lookup: Callable[[str], Any], *, feedback: str | None, item: str | None) -> str`: a placeholder is `render_value(lookup(path))` (from `prompt_files`); `{feedback}` is `_feedback(feedback, carried)` as today or empty; a `{?name}…{/name}` block is kept only when `lookup(name)` is present (non-empty), with the block's own placeholders rendered; literal braces are `{{` and `}}`.
- `render.prompt_source(node_prompt: str, texts: Mapping[str, PromptText]) -> str`: a `prompt:<id>` is the hub rule's or the draft's body (`prompt_files.prompt_text`); anything else is the text itself.
- `outputs.Kind` grammar for a field: `text | number | bool | list of <Kind> | one of a | b | c`, optional with a trailing `?`. `outputs.parse_kind(s) -> Kind`, `ValueError` with the text when invalid; `OutputSpec` validates its fields with it.
- `outputs.parse_output(text: str, spec: OutputSpec) -> Any`: `json` with fields: `extract_json_block` (existing, `airlie.story.model`) then every non-optional field present with the kind (a `StoryError` naming the field and what it is); `json` without fields: the block as parsed; `markdown`/`text`: the whole reply stripped; `bool`: yes/no/true/false; `list`: a JSON list.
- `outputs.describe(spec) -> str`: the sentence the ask appends to the brief ("Reply with one ```json block with fields: decision (one of agree | object), reasons (list of text).") — the one fixed text a generic ask adds; it is a hub rule `output-format` so the text is in the snapshot.

- [ ] **Step 1: Tests** — names (incl. a conditional block), render with a missing input raising `PromptError`, a Card rendering as title + description, config paths, feedback with carried text, `{{`; parse_kind valid/invalid; parse_output per format and the field errors' wording.
- [ ] **Step 2–5:** fail, implement, gate, commit — `flow: prompts render from inputs and config; outputs parse by their field list`

### Task 4: The catalog of checks

**Files:**
- Create: `src/airlie/flow/catalog.py`, `tests/unit/test_flow_catalog.py`
- Modify: `src/airlie/story/flow.py` (declare every check), `src/airlie/serve/workflows_api.py` (`/catalog` shape)

**Interfaces:**
- `@dataclass(frozen=True) class Check: name: str; inputs: tuple[str, ...]; outputs: tuple[str, ...]; title: str; fn: Callable[..., Awaitable[Mapping[str, Any]] | Mapping[str, Any]]`.
- `class Catalog: def add(self, check: Check) -> None; def __getitem__(self, name) -> Check; def names(self) -> list[str]; def describe(self) -> list[dict]` (name, title, inputs, outputs).
- `def check(name: str, *, inputs: Sequence[str] = (), outputs: Sequence[str] = (), title: str) -> Callable[[F], F]`: registers on the module-level `STORY_CATALOG` used by `story_registry`; the function's signature stays `(ctx, **args)`.
- Every check `story_registry` registers today is declared (the list in `CATALOG["checks"]` plus the predicates that become bool-output checks: `to_build`, `survey_on`, `is_story_card`, `amigos_on`, `open_layer`, `not_built`, `has_package` as `story.<name>` with `outputs=("<name>",)` of format bool).
- New computed-input checks (D13), each `story.<name>` with its output: `repo_guide → guide`, `lessons → lessons`, `dossier → dossier`, `coder_budget → budget` (dict: max_steps, context_budget), `layer_sources → sources.{item}`, `layer_proof → proof.{item}`, `lint_note → lint`, `survey_note` (emits `survey_done`/`survey_failed` and the comment, inputs `survey`), `refined_note` (the product comment + `finish("refine")`, inputs `story`), `layers_note` (the architect comment, inputs `layers`).
- `GET /v1/workflows/catalog` returns `{roles, types, formats, kinds: ["text","number","bool","list of …","one of …"], checks: catalog.describe(), prompt_files}` and drops `prompts`, `parsers`, `predicates`, `afters`, `turns`, `meetings`.

- [ ] **Step 1: Tests** — a declared check's inputs/outputs are read back; `describe()` lists them; `check_workflows(..., catalog=)` refuses a `run` the catalog lacks and an output the declaration does not give; every id the old `CATALOG["checks"]` named is declared; the catalog route's shape.
- [ ] **Step 2–5:** fail, implement, gate, commit — `flow: checks are a catalog of declared operations; the computed inputs of the briefs are checks`

### Task 5: The dataflow scheduler

**Files:**
- Modify: `src/airlie/flow/composer.py`, `src/airlie/flow/store.py`, `src/airlie/story/state.py`
- Test: `tests/unit/test_flow_composer.py` (rewritten), `tests/unit/test_flow_store.py`

**Interfaces:**
- `StoryState.done: list[str] = field(default_factory=list)`; `save()` omits it when empty. `StateStore.done(name: str) -> bool` and `StateStore.mark_done(name: str) -> None` (`<node>` or `<node>@<item>`); memory heads unchanged.
- `Composer.run(workflow, *, until=None, item=None) -> str` keeps its signature. Scheduling:
  - A node is **runnable** when it is not done, every input is present (`_present`) or every line that could carry it is dead, every incoming ordering line (a line from a node with no output the target reads) has fired or died, and, if it has incoming lines, at least one fired. A line **fires** when its source finished and its `when` is absent or true; it **dies** when its source finished and `when` is false, or its source died (every line into the source died and it has incoming lines). Start nodes are the nodes with no incoming lines.
  - All runnable nodes run at once (`asyncio.gather`); a failure cancels the rest, as `_parallel` does today.
  - A node whose outputs are all present at the start of a run is marked done and `node_skipped` is emitted (resume); a node with no outputs is skipped when `store.done(name)`.
  - A `loop` line fires when its `when` holds (or, with no `when`, when the source failed) and `_again` allows; firing marks the target and every node downstream of it (through lines) not done, clears their non-memory outputs from the store (`store.put(name, None)` for each declared output), and puts `carry` under `feedback.<target>`.
  - A failed node (`FAIL_KEY`) with no loop line and `on_fail: block` raises `StoryBlocked`; with `continue` its outgoing non-loop lines fire.
  - A `bundle` node: for each item of `store.get(each)` (or once, without `each`), run the named workflow's scheduler with `item`, its inputs read from the parent store by name, its declared `outputs` the only names written back; `node_started`/`node_finished` for the bundle node, per item events from inside as today.
  - `until`: the run returns after the named node finishes.
  - A run with no runnable node and unfinished nodes raises `Stalled(f"workflow {w}: nothing can run; waiting: {node}: {missing inputs}")`.
  - `MissingInput` is gone (a schema-valid workflow cannot start a node without its inputs); `EndRun` and `FAIL_KEY` stay.
- Node events unchanged: `node_started {node, type, workflow, item}`, `node_finished {node, type, outcome, ms}`, `node_skipped`.

- [ ] **Step 1: Tests** (the fixture `composer(...)` kept; `MemoryStore` gains `done`):

```python
def test_two_lines_out_run_both_targets_at_once():            # order of node_started shows both before either finishes
def test_a_node_waits_for_every_line_in_and_runs_once():
def test_a_dead_line_counts_as_satisfied():                   # refine | take_card alternatives → layers runs once
def test_a_loop_line_reruns_the_target_and_everything_downstream_and_clears_their_outputs():
def test_a_loop_bound_spent_blocks_or_continues_by_on_fail():
def test_a_bundle_runs_once_per_item_and_writes_back_only_its_declared_outputs():
def test_a_node_done_by_its_artifacts_is_skipped_on_resume_and_an_outputless_node_by_the_done_set():
def test_until_returns_after_the_named_node():
def test_a_stalled_workflow_names_what_it_waits_for():
def test_a_condition_reads_outputs_the_card_and_config():
def test_feedback_carried_in_one_item_is_not_read_by_the_next():  # kept from today
```

- [ ] **Step 2–5:** fail, implement, gate (remove Task 1's xfails), commit — `flow: the composer is a dataflow scheduler over lines; bundles are steps; loops clear what they re-run`

### Task 6: Generic runners

**Files:**
- Modify: `src/airlie/flow/runners.py`, `src/airlie/story/flow.py` (`story_runners`)
- Test: `tests/unit/test_story_flow.py` (runner tests), new cases in `tests/unit/test_flow_composer.py`

**Interfaces:**
- `ask_runner(node, ctx, *, asker, st, seat=str, ask=None)`: brief = `render(prompt_source(node.prompt, ctx.texts), ctx.lookup, feedback=..., item=ctx.item)` + `outputs.describe(spec)` for the structured output (the first output with fields; a node may have one); with fields → `asker.usable(..., parse=lambda text: parse_output(text, spec))`; without → one ask; the result stored under the output name; `events["done"]` emitted with `chars`, `events["failed"]` with `reason` on `continue`.
- `conversation_runner`: turn 0 sends the opening (or the brief when continuing a session); turn `n` sends `render(node.turn, ..., feedback)`; each reply is parsed by the structured output's spec and the conversation ends when it parses; `steps` bounds the turns; the session output as today.
- `write_runner(node, ctx, *, asker, st, seat=str)`: one `Asker.conversation` turn with `Writes(tools=node.tools or role write tools, scope=..., prepare=...)` on the worktree the `ref:worktree` input names (or detached at `base`); outputs: `diff` (text, `git diff` since the base), `account` (the reply) and the session. Continuing a session (`node.session`) sends the prompt as the nudge.
- `meeting_runner`: the amigos protocol (`pipe._agree`) with the node's `prompt`/`turn` texts handed as the opening and turn texts when set (today's hub texts when absent); outputs `agreement`, `story`, `layers` declared.
- `gate_runner`: unchanged. `check_runner`: `catalog[node.run].fn(ctx, **node.args)`.
- `RunContext` gains `texts: Mapping[str, PromptText]` and `lookup(path) -> Any` (store, then `card.*` on the loose card, then `config.*`).

- [ ] **Step 1: Tests** with `FakeEngine`: an ask with fields retries until the fields parse; a conversation ends on the parsed turn; a write node yields a diff and a session; the legacy `events` emit; a meeting node's prompt text reaches the opening.
- [ ] **Step 2–5:** fail, implement, gate, commit — `flow: model nodes run from their own fields: rendered prompts, parsed outputs, legacy events by name`

### Task 7: The team runner

**Files:**
- Create: `src/airlie/flow/team.py`, `tests/unit/test_flow_team.py`
- Modify: `src/airlie/flow/runners.py` (dispatch), `src/airlie/prompts/rules/team-assign.md`, `team-handback.md` (the two fixed texts)

**Interfaces:**
- `team_runner(node, ctx, *, asker, st, seat=str)`: the supervisor is one held conversation (`Asker.conversation`) opened with `render(node.prompt)` + the `team-assign` rule (reply with one ```json block `{"assignments": [{"id": str, "task": str, "files": [str]}]}`); more than `node.workers or 5` assignments → the first N run and the supervisor is told the rest were not started; workers run concurrently, each a `write_runner` turn (write tools) or an `ask` (read tools) with `render(node.worker, lookup + {"assignment": a})`; as each finishes, the supervisor gets a turn with the `team-handback` rule and the result (a diff is applied to the supervisor's worktree with `git apply --3way`; a failure comes back as a failed hand-back); after the last, the supervisor's final reply is parsed by the structured output's spec.
- Outputs: `results` (list of `{id, ok, text}`), the synthesis under the structured output's name, and `branch` (ref:branch) when any worker wrote.

- [ ] **Step 1: Tests** with `FakeEngine`: three assignments → three workers, hand-backs in finish order, synthesis stored; a cap of two with five asked → two run and the supervisor's next turn names the three left; a diff that does not apply → a failed hand-back and the node still finishes; a reader team writes no branch.
- [ ] **Step 2–5:** fail, implement, gate, commit — `flow: a team node: the supervisor delegates, workers hand back, the supervisor assembles as they finish`

### Task 8: The story and layer workflows rewritten

**Files:**
- Modify: `config/workflows/story.yaml`, `config/workflows/layer.yaml`, `src/airlie/story/flow.py` (registry: checks only), `tests/unit/test_story_flow.py`, `tests/unit/test_prompt_texts.py` golden (`AIRLIE_WRITE_PROMPT_TEXTS=1`)
- Create: `src/airlie/prompts/rules/story-survey.md`, `story-refine.md`, `story-layers.md`, `story-layers-turn.md`, `story-drafts.md`, `story-package.md` (frontmatter `kind: rule`, `fields:` the placeholders)

**Interfaces:**
- The prompt files render, from the same inputs, the same text the Python briefs produce (`survey_brief`, `refine_brief`, `layers_brief`, `drafts_brief`, the package brief), section by section, using `{?name}` blocks where a brief has an optional section. The side-by-side tests prove equality; `test_prompt_texts.py` records them.
- `story.yaml` nodes (names kept so the screen's readers match): `intake` (check) → `base` → `gate` (when `config.story.build … ` as today's `to_build` check output) → lines to `take_card_as_story` (when `is_story_card`) and to `survey` and `refine` (when `not is_story_card`), the computed-input checks (`repo_guide`, `lessons`, `dossier`, `coder_budget`) before `layers`, `survey_note` after `survey`, `refined_note` after `refine`, `costed_refine`, `layers` (conversation: prompt `prompt:story-layers`, turn `prompt:story-layers-turn`, output `layers: {format: json, fields: …}`), `plan_check` (check: today's `parse_layers` validation beyond fields, loop line back to `layers` with the reason, `max: story.stage_attempts`), `size_review` (ask reviewer, line back when oversized), `layers_note`, `costed_plan`, `agree_plain` | `amigos` (lines with `config.story.amigos.enabled`), `costed_amigos`, `planned`, `build_layers` (bundle `layer`, `each: layers`), `built`, `publish`, `costed_publish`, `review` (gate).
- `layer.yaml`: `inputs: [card, repo, story, layers, base, gate]`, `outputs: ["built.{item}", "satisfied.{item}"]`; nodes `covered`, `layer_sources`, `layer_proof`, `lint_note`, `drafts` (conversation with `prompt:story-drafts`, session output), `critic` (ask tester, loop line back to `drafts` once), `package` (check `story.ready_package` until Part 2), `build` (check `story.build_once` until Part 2, loop line to `package` with `story.layer_attempts`), `costed_layer`.
- `story_registry` keeps only catalog checks; the brief, parser, predicate, after, turns and meeting ids are removed from the registry (their functions stay in `pipeline.py`/`briefs.py` for the driver).

- [ ] **Step 1: Run the side-by-side tests; they fail on the first unknown id.**
- [ ] **Step 2: Rewrite both files and write the six prompt files**, one brief at a time, running `test_the_composer_plans_with_the_drivers_events_fields_and_state` after each.
- [ ] **Step 3: Full gate incl. `task web:gate`**; refresh the prompt golden; `test_every_registry_id_the_workflows_name_resolves` and `test_the_catalog_names_every_registered_id` rewritten for the catalog.
- [ ] **Step 4: Commit** — `flow: the story and layer workflows run from prompt files, declared checks and lines; no code ids`

### Task 9: Upgrade, adopt the current workflow, use tracking

**Files:**
- Create: `src/airlie/flow/upgrade.py`, `tests/unit/test_flow_upgrade.py`
- Modify: `src/airlie/story/workflows.py`, `src/airlie/story/restart.py` (no change to `_cleared`), `src/airlie/serve/app.py`, `src/airlie/cli.py`, `src/airlie/serve/workflows_api.py`, `src/airlie/board/store.py` (drafts loaded through `upgrade_v1`)
- Test: `tests/unit/test_story_workflows.py`, `tests/unit/test_board_api.py`

**Interfaces:**
- `upgrade_v1(data: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]`: an old-schema workflow mapping to v2 plus problems: `next`/`branch`/`loop`/`on_fail` loop → `to` lines (a branch value → `when: "<on> == <value>"`, `*` → a line with no condition last); `parallel` → the children get the parent's incoming lines and its outgoing lines; `each` → a `bundle` node with `each: <over>`; `loose` → `inputs`; `when: <id>` on a node → the problem `node <n>: condition '<id>' was code; write it as an expression`; `prompt`/`parse`/`after`/`done`/`turn` ids on a model node → `node <n>: write this node's prompt` (the id dropped, the node unrunnable through `workflow_problems`); a v2 mapping passes through unchanged (detected by the absence of every v1-only key).
- `from_snapshot` runs `upgrade_v1` and raises `WorkflowError` listing the problems when any; `resolve_workflows` runs it on drafts and files alike (files are v2 after Task 8; the function is a no-op on them).
- `adopt_current(pipe, st, card) -> Snapshot`: replaces `st.workflow` with `snapshot(resolve_workflows(...))` for `card_workflow(card)`, keeps `artifacts`, `done` and every state field, saves, emits `workflow_adopted {name, from, to}` (the old and new spec hashes), posts a card note; refused with `StoryRunning` while the story runs. Route `POST /v1/airlie/stories/{key}/adopt-workflow` and `airlie story adopt-workflow KEY`; the card's next run continues by the artifacts present (Task 5's resume rule).
- `GET /v1/workflows` rows gain `used_by: list[str]` (workflows whose bundle nodes name it); `DELETE /v1/workflows/drafts/{name}` and a save that renames are refused 409 with `used_by` when non-empty.

- [ ] **Step 1: Tests** — each upgrade rule with a small v1 mapping and its v2 result (incl. Review Focus 4: an old draft with `run` on an ask → problem, no raise, `workflow_problems` lists it); a snapshot from before this plan upgraded; `adopt_current` replaces the snapshot and the next `build()` skips nodes whose artifacts exist; the route and command; `used_by` and the 409.
- [ ] **Step 2–5:** fail, implement, gate, commit — `flow: old workflows upgrade to lines; a card can adopt the current workflow; bundles know where they are used`

### Task 10: The run view and the docs

**Files:**
- Modify: `src/airlie/serve/flow_nodes.py`, `tests/unit/test_flow_nodes.py`, `AGENTS.md`, `README.md`, `docs/superpowers/specs/2026-11-08-nodes-are-config-design.md` (as-built section)

**Interfaces:**
- `node_flow(spec)` reads `to` lines and `bundle` nodes (the children of a bundle are the named workflow's nodes, drawn flat as today); lanes split at `planned` as before; a bundle's `each` draws the per-item lanes for reached items.
- `AGENTS.md`: the configuration section says a workflow is nodes and lines, prompts are text or `prompt:<id>` files, checks come from the catalog (`airlie workflows catalog` prints it; add that CLI command: `cli.py`, `catalog.describe()` as a table).
- As-built section: what differs from the design text (the `bundle` type with `each`, the condition grammar, the field-kind grammar, the `done` set, `Stalled`, the `output-format` rule, the two team rules).

- [ ] **Step 1: Tests** — the run view from the rewritten story spec lights the same boxes as before (names unchanged); the CLI prints every catalog entry.
- [ ] **Step 2–5:** fail, implement, gate, commit — `flow: the run view reads lines and bundles; the catalog has a CLI; as-built notes`

---

## Self-review

- **Spec coverage:** The node by field → T1, T6; Outputs → T3; Checks → T4; The canvas (engine side: lines, parallel, join, condition, loop, per-item) → T1, T2, T5; Editing → plan 2; Team node → T7; Story rewritten → T8; D12 driver stays → T8 keeps the Python copies; D13 computed inputs → T4, T8; D14 events → T6 (`events`), T4 (mark checks); D15 alternatives → T5; D16 conditions → T2; D17 ordering lines and the done set → T5; D18 full catalog → T4; D19 validation checks with loop lines, early end on parse, size review, critic → T8; D20 meeting protocol in code → T6; D21 upgrade → T9; D22 writing workers → T7; D23 bundle interface → T1, T5; D24 adopt → T9; D25 same-role sessions → T1.
- **Type consistency:** `Line`, `LoopBound`, `TeamRole`, `OutputSpec`, `Catalog`, `Check`, `Condition`, `Stalled`, `upgrade_v1`, `adopt_current` are defined where first named and used by those names after.
- **Review Focus:** 1 → T1 rule 4 and T3; 2 → T5; 3 → T5; 4 → T9; 5 → T7.
- **Proportion:** the plan carries signatures, rules and test names; the one algorithm spelled out is the scheduler's runnable rule, which the tests alone would not pin.
