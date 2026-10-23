# Prompt Hub Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move airlie's inline rule and steering prose out of the Python into Markdown files under `src/airlie/prompts/`,
served by one loader. Add a deterministic contradiction check that runs in the test gate, a `steer` event, and an
advisory model audit. The text models receive must not change.

**Architecture:** A small package, `airlie.prompts`, loads `rules/*.md` and `steering/*.md` with
`importlib.resources` once and caches them. Callers ask for `text(id, **fields)`. `airlie.prompts.check` compares
the `governs` tags of the texts that can reach each role. A golden snapshot, captured before anything moves,
proves that every moved text is byte-identical.

**Tech Stack:** Python 3.12, uv, PyYAML (already a dependency), pytest, Typer, Taskfile.

**Spec:** `docs/superpowers/specs/2026-10-22-prompt-hub-design.md` (approved by Noah 2026-10-22)

## Global Constraints

- Every change passes, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- uv only; every new action is a Taskfile task (`task prompts:audit`).
- Byte-identical text to models, with one intended exception: role prompts lose their front matter in
  `orchestrator.py` and `diversify.py` (Task 4).
- Output-format lines stay inline next to their parsers: `STORY_CONTRACT` and `LAYERS_CONTRACT` (`briefs.py`),
  `REFINE_FIELD` (`rounds.py`), the verdict JSON (`review.py`), plan and synthesis JSON (`studio.py`), `WINNER:`
  (`teams/aggregate.py`), and the YES/NO and key prompts in `lts/summarizer.py` and `lts/controller/judge.py`.
- These also stay in Python: `teams/protocol.cut_off_text`, tool descriptions, CLI and config help text, and the
  short finding labels in `story/layer_checks.py` (`PASSES_BEFORE_CODE`, `EXISTING_FAILS`, `ASSERTION_FREE`,
  `NO_NEW_TEST`).
- No Jinja and no logic in files: `{name}` placeholders in `str.format` style, with literal braces doubled.
- The incident and date notes in a moved constant's docstring go into that file's `why`; none is dropped.
- Match the repository's comment style: dense docstrings that cite the incident and date.
- **PR #124 (AIR-30) changes `native_team.py`.** Before Task 5, rebase onto `main`. If #124 has merged, its
  carried-reasoning message moves too, as steering `reasoning-carried`.

## Review Focus

1. **A body with literal braces** (JSON-ish examples) must render with single braces. Pinned in Task 2
   (`test_doubled_braces_render_single`) and by the snapshot.
2. **Whitespace at a file's edges** must not leak into the text. The body is everything after the closing `---`
   line, minus exactly one trailing newline. Pinned in Task 2 (`test_body_keeps_inner_whitespace_and_drops_one_final_newline`).
3. **A resumed run whose history holds end-of-budget messages** under today's wording must still have them
   dropped. Pinned in Task 5 (`test_a_resumed_run_drops_hub_end_of_budget_messages`).
4. **The `.md` files must ship with the package.** If they are missing from an install, every run fails at the
   first nudge. Pinned in Task 2 (`test_the_hub_loads_from_the_installed_package`). Task 10 checks the wheel.
5. **A typo in a file's `roles`** would quietly exclude that text from the check. Pinned in Task 8
   (`test_every_role_named_by_a_hub_text_exists`).

---

### Task 1: Golden snapshot of every model-facing text that will move

Captured from today's code before anything moves. Every later task must keep it green without editing the JSON.

**Files:**
- Create: `tests/unit/test_prompt_texts.py`
- Create: `tests/unit/data/prompt_texts.json` (generated)

**Interfaces:**
- Produces: `CASES: dict[str, Callable[[], str]]` in `tests/unit/test_prompt_texts.py`. Each key is the hub
  id the text will get (ids listed in Tasks 5–7). Each value renders the text with fixed sample fields, today
  through the old constant or function. Later tasks change only the *value* to `text(id, ...)` and leave the key
  and the JSON alone.

- [ ] **Step 1: Write the snapshot test and its cases**

```python
GOLDEN = Path(__file__).parent / "data" / "prompt_texts.json"

def test_every_text_is_byte_identical_to_the_golden_snapshot() -> None:
    rendered = {k: f() for k, f in sorted(CASES.items())}
    if os.environ.get("AIRLIE_WRITE_PROMPT_TEXTS") == "1":
        GOLDEN.write_text(json.dumps(rendered, indent=1, ensure_ascii=False) + "\n")
    assert rendered == json.loads(GOLDEN.read_text())
```

Sample fields: `n=7, total=40, k=3, what="no test", tool="read_file", p=2, label="b", width=3`, plus
`worktree_hint="/tmp/wt"`. Cover:
- every constant in Tasks 5–7, under its future id;
- `native_team._goal_message` with tools and without, with and without `team_id` and preamble;
- `diversify.nudge_text` with `repeated`, `test_tail` and `hypothesis` both set and unset (4 cases);
- `layer_checks.feedback` (before_code True and False, with and without advice), `amend_feedback` and
  `findings_text` on two `Finding`s;
- `briefs.refine_brief`, `survey_brief`, `layers_brief`, and `layer_plan` for every layer of the `STORY` and
  `LAYERS` fixtures in `tests/unit/test_story_briefs.py`, with and without a tester role. Import those fixtures;
  don't copy them.

For `serve/app.py`, whose bridge text is inline in a closure, add a case for the literal string now. Task 6
switches it to the hub.

- [ ] **Step 2: Generate the golden file**

Run: `AIRLIE_WRITE_PROMPT_TEXTS=1 uv run pytest tests/unit/test_prompt_texts.py -q`
Expected: 1 passed. `tests/unit/data/prompt_texts.json` now exists.

- [ ] **Step 3: Run without the flag**

Run: `uv run pytest tests/unit/test_prompt_texts.py -q`
Expected: 1 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_prompt_texts.py tests/unit/data/prompt_texts.json
git commit -m "prompts: a golden snapshot of every model-facing text the hub will move"
```

---

### Task 2: The hub loader

**Files:**
- Create: `src/airlie/prompts/__init__.py`, `src/airlie/prompts/hub.py`
- Create: `src/airlie/prompts/rules/.gitkeep`, `src/airlie/prompts/steering/.gitkeep`
- Test: `tests/unit/test_prompt_hub.py`

**Interfaces:**
- Produces, in `airlie.prompts.hub`, re-exported from `airlie.prompts`:
  - `Kind = Literal["rule", "steering"]`
  - `@dataclass(frozen=True) class PromptText`, with fields `id: str`, `kind: Kind`, `roles: tuple[str, ...]`,
    `fields: tuple[str, ...]`, `when: str | None`, `governs: Mapping[str, str]`, `yields_to: tuple[str, ...]`,
    `why: str`, `body: str`, `path: str`
  - `class PromptError(ValueError)`
  - `def parse(name: str, folder: Kind, raw: str) -> PromptText`: validates one file
  - `def load() -> Mapping[str, PromptText]`: `functools.cache`d; every file in both folders
  - `def entry(id: str) -> PromptText`
  - `def text(id: str, /, **fields: object) -> str`

The file format: YAML front matter between a first line `---` and the next line `---`, parsed with
`yaml.safe_load`. Keys: `kind` (required, must equal the folder: `rules/` holds `rule`), `roles` (required
list), `fields` (list, default empty), `when` (required for steering, absent for rules), `governs` (mapping,
default empty), `yields_to` (list, default empty), `why` (required string). The id is the file stem, in
kebab-case. The body is everything after the closing `---\n`, with exactly one trailing `\n` removed. A body is
always a format string: `text()` calls `body.format_map(fields)`.

- [ ] **Step 1: Write the failing tests**

`parse` tests use raw strings; nothing reads a file except the last two.
- `test_parse_reads_front_matter_and_body`: all fields round-trip; the id comes from `name`.
- `test_body_keeps_inner_whitespace_and_drops_one_final_newline`: `"a\n\nb\n\n"` gives the body `"a\n\nb\n"`.
- `test_doubled_braces_render_single`: the body `'{{"x": {n}}}'` with `fields: [n]` gives
  `text(...) == '{"x": 3}'` for `n=3`. Test through `parse` plus a small helper `render(entry, **fields)` that
  `text` also uses.
- `test_placeholders_must_match_fields`: a body using `{n}` with `fields: [n, total]` raises `PromptError`
  naming the file; so does a `{k}` that is not declared.
- `test_kind_must_match_folder`: `kind: steering` parsed for folder `rule` raises `PromptError`.
- `test_steering_needs_when_and_rules_have_none`: both mismatches raise.
- `test_text_rejects_missing_and_extra_fields`: each raises `PromptError`.
- `test_unknown_id_raises`: `text("no-such-id")` raises `PromptError`.
- `test_the_hub_loads_from_the_installed_package`: `load()` returns a mapping and reads through
  `importlib.resources.files("airlie.prompts")`. After Task 5 it also asserts `"last-step" in load()`. Add that
  line in Task 5.
- `test_ids_are_unique_across_folders`: two files with the same stem raise. Test through
  `_load_from(root: Traversable)` on a `tmp_path`; `load()` calls it on the package root.

- [ ] **Step 2: Run them; they fail**

Run: `uv run pytest tests/unit/test_prompt_hub.py -q`
Expected: FAIL with `ModuleNotFoundError: airlie.prompts`.

- [ ] **Step 3: Implement `airlie/prompts/hub.py` to the interface above**

Find placeholders with `string.Formatter().parse`. Ignore `.gitkeep`. Docstrings in the repository's style
should point at the spec.

- [ ] **Step 4: Run the tests and the gate**

Run: `uv run pytest tests/unit/test_prompt_hub.py -q && uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/airlie/prompts tests/unit/test_prompt_hub.py
git commit -m "prompts: the hub loader, rule and steering files by id with checked placeholders"
```

---

### Task 3: The contradiction check, and its gate test on the repository

**Files:**
- Create: `src/airlie/prompts/check.py`
- Test: `tests/unit/test_prompt_check.py` (synthetic), `tests/unit/test_prompt_contradictions.py` (the repo)

**Interfaces:**
- Consumes: `PromptText`, `load()` (Task 2); `airlie.skills.library.split_frontmatter`; `load_config`.
- Produces, in `airlie.prompts.check`:
  - `@dataclass(frozen=True) class Conflict`, with fields `role: str`, `subject: str`, `a: str`, `a_value: str`,
    `b: str`, `b_value: str`. `a` and `b` are sources: a hub id, or `role:<name>` for a role prompt.
  - `def role_tags(prompt: Path) -> tuple[dict[str, str], tuple[str, ...]]`: reads the *flat* front matter of a
    role prompt. Every key `governs.<subject>` gives a subject and value. `yields_to: a, b` is split on commas.
    This keeps `split_frontmatter` and the `summary:` line unchanged.
  - `def conflicts(texts: Iterable[PromptText], role_prompts: Mapping[str, Path]) -> list[Conflict]`
  - `def subjects(texts: Iterable[PromptText], role_prompts: Mapping[str, Path]) -> dict[str, list[str]]`:
    each subject, sorted, mapped to its sources.

Rules for `conflicts`, from the spec:
- A text reaches role R when its `roles` contains R or `any`. A role prompt reaches only its own role.
- Compare rules (role prompts included) with rules, and steering with steering. Never compare steering with a
  rule.
- Two sources conflict on a subject when their values differ and neither lists the other in `yields_to`.
- Report each conflicting pair once per role, sorted by (role, subject, a, b).

- [ ] **Step 1: Write the failing synthetic tests**

Build `PromptText`s directly, and write role prompts into `tmp_path`.
- `test_two_rules_that_disagree_conflict`: rule `x` and role prompt `role:coder` both govern `tests.location`,
  as `new-file` and `named-file`; `conflicts` returns one `Conflict` with role `coder`.
- `test_yields_to_settles_it`: the same, with `x.yields_to = ("role:coder",)`, gives `[]`.
- `test_steering_never_conflicts_with_a_rule`: steering `reading: stop` against rule `reading: first` gives `[]`.
- `test_two_steering_texts_that_disagree_conflict`.
- `test_any_reaches_every_role_in_role_prompts`.
- `test_texts_for_different_roles_do_not_conflict`.
- `test_role_tags_reads_flat_governs_keys_and_keeps_the_summary`: a role prompt with `summary: a: b`,
  `governs.tests.location: new-file` and `yields_to: brief-named-test-file` gives the right tags, and
  `split_frontmatter(...)[0]["summary"] == "a: b"`.

- [ ] **Step 2: Run them; they fail**

Run: `uv run pytest tests/unit/test_prompt_check.py -q`
Expected: FAIL with `ModuleNotFoundError: airlie.prompts.check`.

- [ ] **Step 3: Implement `airlie/prompts/check.py` to the interface**

- [ ] **Step 4: Add the repository gate test**

`tests/unit/test_prompt_contradictions.py`:
- `test_the_repository_prompts_do_not_contradict`: loads the repository's `airlie.yaml` with `load_config`,
  maps `{name: role.prompt for name, role in cfg.roles.items()}`, and asserts `conflicts(load().values(),
  that) == []`. On failure it prints each conflict on one line.
- `test_subjects_in_use` prints `subjects(...)` with `-s` and asserts nothing. It exists so a reviewer can
  see near-duplicate subjects. Mark it `@pytest.mark.skip(reason="run with -s to list subjects")` only if the
  repo's pytest config rejects tests without asserts. Otherwise keep it as a no-assert test.

- [ ] **Step 5: Run and commit**

Run: `uv run pytest tests/unit/test_prompt_check.py tests/unit/test_prompt_contradictions.py -q`
Expected: all pass. The repository check passes trivially until Task 8 adds tags.

```bash
git add src/airlie/prompts/check.py tests/unit/test_prompt_check.py tests/unit/test_prompt_contradictions.py
git commit -m "prompts: the contradiction check (rule vs rule, steering vs steering) and its repository gate test"
```

---

### Task 4: Role prompts reach the model without their front matter everywhere

This is the one intended text change.

**Files:**
- Modify: `src/airlie/orchestrator.py:209` and `src/airlie/topology/diversify.py:40`, changing
  `role.prompt.read_text()` to `prompt_text(role.prompt)` from `airlie.skills.library`.
- Test: `tests/unit/test_fork_diversify.py`, plus the orchestrator's existing test module (find it with
  `grep -l "orchestrator" tests/unit`).

- [ ] **Step 1: Write failing tests**

`test_<topology>_system_message_has_no_front_matter`: give the role a prompt file
`"---\nsummary: s\n---\n# coder\nbody\n"`, capture the first `GenRequest`'s system message with the fake engine
these modules already use in their tests, and assert it equals `"# coder\nbody\n"`. Write one test per module.

- [ ] **Step 2: Run them; they fail** (the content starts with `---`).

- [ ] **Step 3: Make the two one-line changes.**

- [ ] **Step 4: Run both test modules; they pass.**

- [ ] **Step 5: Commit**

```bash
git commit -am "single and diversify send a role prompt without its front matter, as studio and review do"
```

---

### Task 5: Steering and goal rules for the team loop, with `steer` events

**Files:**
- Create `src/airlie/prompts/steering/`: `malformed-call.md`, `no-tools.md`, `last-step.md`, `write-now.md`,
  `write-only.md`, `tests-now.md`, `code-now.md`, `change-now.md`, `read-refused.md`, `read-left.md`,
  `repeated.md`, `cycled.md`, `repeat-write-only.md`, `act-first.md`, `still-red.md`, `last-turn-retry.md`,
  `one-step-left.md`, `fresh-run.md`, and `reasoning-carried.md` if #124 has merged.
- Create `src/airlie/prompts/rules/`: `goal-tools.md` (fields `worktree_hint`) and `goal-no-tools.md`. These
  hold the fixed sentences of `_goal_message`; the `Goal:`, `who` and preamble assembly stays in Python.
- Modify: `src/airlie/teams/native_team.py`
- Test: `tests/unit/test_native_team.py`, `tests/unit/test_prompt_texts.py` (switch these cases to
  `text(...)`), `tests/unit/test_prompt_hub.py` (add the `"last-step"` assertion)

**Interfaces:**
- Consumes: `text`, `entry` (Task 2).
- Produces: `native_team._steer(ctx: TeamContext, step: int, id: str, /, **fields: object) -> str`. It returns
  `text(id, **fields)` and, when `ctx.emit` is set, emits `"steer"` with `id`, `team_id`, `role` and `step`.
  Every place that today appends one of the constants above calls `_steer` instead. Where `on_stuck` rewrites
  the text (`REPEATED`/`CYCLED`), the event carries the hub id of the text before it was advised.
- Each file's front matter:
  - `roles: [any]`;
  - `when:` from the constant's docstring, as one sentence;
  - `why:` carries the docstring's incident and date notes word for word;
  - `governs:` stays empty until Task 8.
- `read-left`'s body has no leading blank lines. The code keeps prepending `"\n\n"`.
- Module names that tests import (`MALFORMED`, `LAST_STEP`, `LAST_TURN_RETRY`, `ONE_STEP_LEFT`, `REPEATED`,
  `REPEAT_WRITE_ONLY`) are removed. Tests switch to `text(...)` or `entry(...).body`. *(Plan's call: one source
  of each text.)*
- `_STALE_BUDGET` is built from `entry(i).body` for `("last-step", "one-step-left", "last-turn-retry",
  "no-tools")`. The regex construction is unchanged.

- [ ] **Step 1: Rebase on `main`, and check #124**

Run: `git fetch origin && git rebase origin/main && gh pr view 124 --json state -q .state`
Expected: a clean rebase. If the state is `MERGED`, include `reasoning-carried`, and add its case to Task 1's
`CASES` and JSON first, from the merged constant, before moving it.

- [ ] **Step 2: Write failing tests**

- `test_a_nudge_emits_a_steer_event_with_its_id`: drive the fake engine until `write-now` fires, then assert
  one `steer` event with `id == "write-now"`, `role`, `team_id` and `step`.
- `test_an_advised_repeat_still_reports_the_hub_id`: give an `on_stuck` that rewrites the text; the event's id
  is `repeated`.
- `test_a_resumed_run_drops_hub_end_of_budget_messages`: a dumped history holding
  `text("last-step", n=40)` and `entry("one-step-left").body` loses both through `_without_stale_budget`.
- Switch the Task 1 `CASES` for these ids to `text(id, ...)`.

- [ ] **Step 3: Run; the new tests fail** (no `steer` event, no hub files).

- [ ] **Step 4: Create the files, add `_steer`, and replace the constants and the `_goal_message` sentences.**

- [ ] **Step 5: Run the module, the snapshot and the hub tests**

Run: `uv run pytest tests/unit/test_native_team.py tests/unit/test_prompt_texts.py tests/unit/test_prompt_hub.py -q`
Expected: all pass, with the golden JSON untouched (`git diff --exit-code tests/unit/data`).

- [ ] **Step 6: Commit**

```bash
git add -A src/airlie/prompts src/airlie/teams/native_team.py tests/unit
git commit -m "team loop: its nudges and goal sentences come from the prompt hub; each nudge is a steer event"
```

---

### Task 6: The other loops: tool loop, diversify, studio, serve

**Files and ids:**
- `src/airlie/tools/loop.py`: `NO_MORE_TOOLS` becomes steering `no-more-tools`. This path has no emitter, so no
  event. Add a comment saying so.
- `src/airlie/topology/diversify.py` `nudge_text`: the fixed sentences become steering `branch-start` (`label`,
  `width`), `branch-stalled` (`name`, `args`, `out`), `branch-different` and `branch-finish`. The `Last test
  output:` and `Pursue this hypothesis:` labels, with their fences, stay in Python as framing.
- `src/airlie/topology/studio.py`: `FRESH_CONTINUATION`, `DONE_CHECK` and `GATE_FAILED` become steering
  `fresh-continuation`, `done-check` and `gate-failed`. Each emits `steer` where studio already emits events,
  with `id`, `role` and `subtask`. `SKILL_QUESTION` and the JSON formats stay.
- `src/airlie/serve/app.py`: the bridge sentence becomes rule `client-bridge`, `roles: [any]`.
- Tests: `test_tool_loop.py`, `test_fork_diversify.py`, `test_studio_orchestrator.py`, and the snapshot cases.

- [ ] **Step 1: Write the failing test** `test_studio_done_check_emits_a_steer_event`, in
  `test_studio_orchestrator.py`, with its existing fakes. Switch these ids' `CASES` to the hub.
- [ ] **Step 2: Run; the new test fails.**
- [ ] **Step 3: Move the texts and wire the studio events.**
- [ ] **Step 4: Run the four modules and the snapshot.** Expected: pass, and `git diff --exit-code tests/unit/data`.
- [ ] **Step 5: Commit** `"tool loop, diversify, studio and serve: their steering and rules come from the prompt hub"`

---

### Task 7: Story briefs, rounds and layer checks

**Files and ids:**
- `src/airlie/story/briefs.py`:
  - `TEST_RULES` → rule `test-rules`, `roles: [tester, coder]`
  - `PRE_CODE_CHECK` → rule `pre-code-check`, `roles: [tester]`
  - `STATIC_CHECK` → rule `static-check`, `roles: [coder]`
  - `TESTS_ONLY` → rule `tests-only`, `roles: [tester]`
  - `CONTRACT` → rule `tests-contract`, `roles: [coder]`
  - Each fixed paragraph inside `refine_brief`, `survey_brief`, `layers_brief` and `layer_plan` becomes a rule
    named `brief-<function>-<what>`, for example `brief-layer-plan-tests-where`. Put the target role in
    `roles`. Computed parts, clipping and joins stay in Python.
  - `STORY_CONTRACT` and `LAYERS_CONTRACT` stay inline.
- `src/airlie/story/rounds.py`:
  - `ROUND_NUDGE` → steering `plan-round`
  - `FIX_NUDGE` → `plan-fix` (`error`)
  - `UNFINISHED_NUDGE` → `plan-unfinished`
  - All three have `roles: [architect]`. Each sends `emit("steer", id=..., role="architect", round=n)` through
    the `emit` that `rounds` already takes.
  - `REFINE_FIELD` stays inline.
- `src/airlie/story/layer_checks.py`:
  - `AMEND` → steering `amend`
  - `LOOSE_MATCH` → steering `loose-match`
  - The fixed sentences of `feedback` and `amend_feedback` → steering `checks-<what>`, `roles: [tester]` or
    `[coder]` to match who receives them.
  - The finding labels stay.
  - These texts are returned, not sent, so `layer_checks` emits nothing. Wherever the story pipeline sends one
    of them to a role and already has an `emit`, it emits `steer` with the id. The functions return the ids
    they used alongside the text: `feedback(...) -> tuple[str, tuple[str, ...]]` and the same for
    `amend_feedback`. Update their callers.
- Tests: `test_story_briefs.py`, `test_story_rounds.py`, `test_story_layer_checks.py`,
  `test_story_layer_checks_flow.py`, and the snapshot cases.

- [ ] **Step 1: Write the failing test** `test_a_plan_fix_emits_a_steer_event`, in `test_story_rounds.py`, with
  `id == "plan-fix"`. Switch the `CASES` for the moved constants to the hub. Function cases (`layer_plan`
  and others) stay as calls.
- [ ] **Step 2: Run; the new test fails.**
- [ ] **Step 3: Move the texts.** Move `briefs.py` first, then `rounds.py`, then `layer_checks.py`, running the
  snapshot after each file.
- [ ] **Step 4: Run the four story modules and the snapshot.** Expected: pass, and `git diff --exit-code tests/unit/data`.
- [ ] **Step 5: Confirm nothing is left behind**

Rerun the long-literal sweep from the spec (non-docstring `str` constants over 150 characters with more than 20
spaces, under `src/`). Every hit must be either moved or in a Global Constraints keep-inline list. List any other
hit in the PR description with a reason.

- [ ] **Step 6: Commit** `"story: briefs, planning rounds and layer-check feedback come from the prompt hub"`

---

### Task 8: Tags: `governs` and `yields_to` on every text and role prompt

This is where the check starts doing real work.

**Files:**
- Modify: every file under `src/airlie/prompts/rules/` and `steering/`, and every `prompts/roles/*.md`
  (flat `governs.<subject>: value` keys).
- Test: `tests/unit/test_prompt_contradictions.py`

**Values the spec pins:**
- `prompts/roles/coder.md`: `governs.tests.location: new-file`.
- The rule that says a brief-named test file wins (from `briefs._tests_where` and `layer_plan`, PR 57):
  `governs: {tests.location: named-file}`, `yields_to` empty.
- `coder.md`: `yields_to: <that rule's id>`.
- Every other subject and value is the tagger's call: a short dotted subject, and a one- or two-word value.

- [ ] **Step 1: Write failing tests**

- `test_every_hub_text_governs_something`: every `PromptText.governs` is non-empty.
- `test_every_role_named_by_a_hub_text_exists`: every name in any `roles` is `any` or a key of the repository
  config's `roles`.
- `test_the_tests_location_rules_are_settled`: `conflicts` over the repository is `[]`, and removing
  `coder.md`'s `yields_to` (by rebuilding the role map with a temp copy) yields exactly one conflict on
  `tests.location`.

- [ ] **Step 2: Run; they fail** (no text has tags yet).
- [ ] **Step 3: Tag every text.** Read each body and state its instruction. Run
  `uv run pytest tests/unit/test_prompt_contradictions.py -s -q` and read the subject list for near-duplicates.
- [ ] **Step 4: Resolve any real conflicts the check reports.** Settle each with `yields_to`, or fix the
  wording. A wording fix changes the snapshot, so stop and list it for Noah in the PR instead of changing the
  text. Use `yields_to` here.
- [ ] **Step 5: Run the gate test.** Expected: pass.
- [ ] **Step 6: Commit** `"prompts: every rule, nudge and role prompt says what it governs; the check runs on them"`

---

### Task 9: `task prompts:audit`, the advisory model audit

**Files:**
- Create: `src/airlie/prompts/audit.py`
- Create: `src/airlie/prompts/rules/audit.md`: the audit's instruction, `roles: [reviewer]`, with a `governs`
  tag.
- Modify: `src/airlie/cli.py` (a `prompts` Typer group with an `audit` command, like `supervisor_app`) and
  `Taskfile.yml` (`prompts:audit` → `'{{.RUN}} airlie prompts audit {{.CLI_ARGS}}'`).
- Test: `tests/unit/test_prompt_audit.py`

**Interfaces:**
- `def instruction_sets(texts: Iterable[PromptText], role_prompts: Mapping[str, Path]) -> dict[str, str]`: per
  role, the role prompt body (front matter stripped), then every rule and steering body that reaches it, each
  headed `## <source>`, placeholders left as `{name}`.
- `async def audit(cfg: AirlieConfig, engine: Engine) -> str`: one `GenRequest` per role, using the
  `reviewer` role's sampling, `max_tokens` and thinking settings. The messages are a system message of
  `text("audit")` and a user message of that role's instruction set. It returns a Markdown report, one
  section per role holding the model's reply. The output format (quote both sides, name both sources) is inline
  in `audit.py`.
- CLI: `airlie prompts audit [--config] [--role NAME]...` builds the engine with
  `_http_engine(cfg, cfg.engine_for_model(cfg.roles["reviewer"].model))`. It writes
  `prompts-audit-<UTC yyyymmdd-HHMMSS>.md` under `cfg`'s `state_dir / "runs"`, then prints the path and each
  role's first line. Its exit code is always 0 unless the engine is unreachable.

- [ ] **Step 1: Write failing tests**
  - `test_instruction_sets_hold_every_text_that_reaches_a_role`, including an `any` text;
  - `test_audit_asks_once_per_role_and_reports_each_reply`, with a fake engine returning `"none found"`;
  - `test_cli_audit_writes_the_report`, with Typer's `CliRunner` and `_http_engine` patched, as the CLI tests
    already do for other commands.
- [ ] **Step 2: Run; they fail.**
- [ ] **Step 3: Implement it, and add the Taskfile entry.**
- [ ] **Step 4: Run the tests and check `task --list`.** Expected: pass, and `prompts:audit` is listed with a
  `desc` in the style of its neighbours.
- [ ] **Step 5: Commit** `"prompts: task prompts:audit, Muse reads each role's full instructions for contradictions"`

---

### Task 10: Docs, packaging check and the full gate

**Files:**
- Modify: `README.md`. Add a short "Prompts" section covering:
  - the layout: `prompts/roles` set in `airlie.yaml`; rules and steering in `src/airlie/prompts`;
  - the file format and `governs`/`yields_to`;
  - that the contradiction check runs in `pytest`;
  - `task prompts:audit`;
  - the `steer` event.

  Point at the spec.
- Modify: the spec's status line, to "built in PR #N".

- [ ] **Step 1: Check the wheel carries the files**

Run: `uv build --wheel -o /tmp/airlie-wheel && unzip -l /tmp/airlie-wheel/*.whl | grep -c 'airlie/prompts/.*\.md'`
Expected: the count equals the number of `.md` files under `src/airlie/prompts`. If it is short, add a
`[tool.hatch.build.targets.wheel]` include for `src/airlie/prompts/**/*.md` and rebuild.

- [ ] **Step 2: Run the whole gate from `AGENTS.md`, in order.** Expected: all five pass.

- [ ] **Step 3: Commit, push, and open the PR**

The PR description covers:
- the one intended text change (Task 4);
- the snapshot as proof that nothing else changed;
- any wording conflicts left for Noah (Task 8, step 4);
- any sweep hits kept inline (Task 7, step 5).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
