# Lock the tester's tests, not their files: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The coder can change what was already in a test file the tester added tests to (fakes, helpers, imports), while the tests the tester wrote stay locked. Without this, AIR-9 layer 05 cannot pass its gate.

**Architecture:** One small module, `airlie.tools.test_lock`, works out which top-level definitions in a locked Python file are the tester's: new since the layer's base, or changed. It does this by comparing syntax trees, the way `layer_checks.changed_tests` already does.
- `StudioOrchestrator.lock` records those names per file.
- `write_file` and `edit_file` refuse a change only if it touches one of those definitions.
- The restore before the gate puts a file back only if one of them changed.
- The tests' mend goes to the tester only when the failing tests are the tester's own.
- Anything that cannot be split this way stays locked whole, as today: a non-Python file, or a file that does not parse at lock time.

**Tech Stack:** Python 3.12, uv, stdlib `ast`, pytest with `FakeEngine`.

**Spec:** this conversation's AIR-9 finding (run `20261022-185301-ff0c`, recorded in the shared notes file `context-gathering-build.md`):
- The coder changed `board.comment(card, text)` to `comment(card, text, role)`.
- The fakes `RecordingBoard`, `FlakyBoard` and `DownBoard` in `tests/unit/test_story_pipeline.py` had to follow, but that file was locked whole, because the tester added its tests there.
- 8 of the coder's edits were refused, both coder rounds used all 40 steps, and the gate failed.

## Global Constraints

- The test gate passes after every task: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- Every action has a Taskfile task. uv only.
- **What the tester wrote is locked; what was there before is not.** The tester's definitions are the top-level `def`, `async def` and `class` nodes of a locked `.py` file that are absent from the base or whose `ast.dump` differs from the base's. So a test, a fixture, a helper or a fake the tester added or changed is locked. Module-level statements (imports, constants) are never locked.
- A locked file that is not `.py`, or that does not parse at lock time, stays locked whole, exactly as today.
- A change to a file locked by definition is allowed only when the file still parses afterwards, and every one of the tester's definitions is still there with the same `ast.dump`. Moving a test or reformatting it is not a change.
- The coder still cannot change the tester's tests by any route. The shell route is closed by the restore before the gate.
- No change to the pre-code check, `WRITE_NOW`, `WRITE_ONLY` or the stuck nudges.
- Test fixtures are synthetic (the repo is public).

## Review Focus

1. **The tester changed an existing test** (PR 123's case). That test's definition differs from the base, so it is locked like a new one. *Test in Task 1.*
2. **The tester added a method to an existing `class TestX`.** The whole class is the tester's, and the coder cannot edit any part of it. *Test in Task 1.*
3. **The coder's edit leaves the file unparseable** (half an edit). It is refused with a message saying so, and the file is unchanged. *Test in Task 2.*
4. **A shell command changes a fake.** That change survives the restore before the gate. A shell command that changes a tester's test is put back. *Test in Task 3.*
5. **The gate fails only on an old test in a locked file** (one that uses a fake the coder has not fixed yet). The tester is not sent back to mend it; the gate's follow-up goes to the coder, as for any other red test. *Test in Task 4.*

---

### Task 1: `airlie.tools.test_lock`

**Files:**
- Create: `src/airlie/tools/test_lock.py`
- Test: `tests/unit/test_test_lock.py`

**Interfaces:**
- Produces:
  - `tester_definitions(base: str | None, locked: str) -> frozenset[str] | None`: the names of the top-level `FunctionDef`, `AsyncFunctionDef` and `ClassDef` nodes in `locked` that are absent from `base` or whose `ast.dump` differs. `base=None` means the file is new, so every definition is the tester's. Returns None when `locked` does not parse, which means lock the whole file. A `base` that does not parse counts as None.
  - `touched(names: Collection[str], before: str, after: str) -> list[str] | None`: which of `names` are missing from `after`, or have a different `ast.dump` there than in `before`, sorted. Returns None when `after` does not parse.

- [ ] **Step 1: Write the failing tests**
  - `test_new_and_changed_definitions_are_the_testers`: the base has `class FakeBoard`, `def test_old` and `def helper`. The locked file adds `def test_new` and changes `test_old`'s body. Result: `{"test_new", "test_old"}`.
  - `test_a_reformat_or_a_move_is_no_change`: the same definitions, reordered and with different blank lines and comments → `frozenset()`.
  - `test_a_new_file_is_all_the_testers`: `base=None` → every top-level definition.
  - `test_a_method_added_to_an_existing_class_locks_the_class`: `class TestX` gains a method → `{"TestX"}`.
  - `test_imports_and_constants_are_never_locked`: the locked file only adds `import os` and `X = 1` → `frozenset()`.
  - `test_an_unparseable_locked_file_is_locked_whole`: → `None`.
  - `test_touched_names_a_changed_or_removed_definition`: with `names={"test_new"}`, editing `FakeBoard` gives `[]`; changing `test_new` gives `["test_new"]`; deleting it gives `["test_new"]`; an unparseable `after` gives `None`.

- [ ] **Step 2: Run them** with `uv run pytest tests/unit/test_test_lock.py -q`. Expected: ImportError.
- [ ] **Step 3: Implement both functions.** Compare with `ast.dump(node)`, which leaves out line numbers, so a move is no change.
- [ ] **Step 4: Run the tests, then the gate.**
- [ ] **Step 5: Commit** with the message `tools: which definitions in a locked test file are the tester's`.

### Task 2: The file tools lock definitions

**Files:**
- Modify: `src/airlie/tools/protocol.py` (`ToolContext`), `src/airlie/tools/fs.py` (`_locked`, `LOCKED`, `WriteFile.run`, `EditFile.run`)
- Test: `tests/unit/test_fs_tools.py`

**Interfaces:**
- Consumes: `touched` from Task 1.
- Produces: `ToolContext.locked_tests: Mapping[str, frozenset[str]] = field(default_factory=dict)`. This maps a path from the worktree's root to the tester's definitions in that file. `read_only` keeps its meaning: files locked whole.
- `write_file` and `edit_file` on a path in `locked_tests` work out the new text first, then call `touched(names, current_text, new_text)`:
  - `None` → `error: {path} would not parse after this change, so airlie cannot tell that the tests written before you are untouched. Change it in steps that each leave it whole.`
  - non-empty → `LOCKED_TESTS.format(path=..., names=", ".join(t))`, which says: `error: this change touches {names} in {path}: tests written before you for this layer, your contract, and you cannot change them. Make them pass with the code. What else is in the file was there before them (fakes, helpers, imports): you may change that.`
  - empty → the write goes ahead as today, including `edit_file`'s loose-span match and `write_file`'s overwrite limit.

- [ ] **Step 1: Write the failing tests**
  - `test_the_coder_may_change_a_fake_in_a_file_the_tester_added_tests_to`: `locked_tests={"tests/test_p.py": frozenset({"test_new"})}`. An `edit_file` that adds a `role` parameter to `FakeBoard.comment` → ok, and the file changed.
  - `test_an_edit_that_touches_the_testers_test_is_refused_and_names_it`: an edit inside `test_new` → not ok, the output contains `test_new`, and the file is unchanged.
  - `test_write_file_that_drops_the_testers_test_is_refused`.
  - `test_an_edit_that_leaves_the_file_unparseable_is_refused`.
  - The existing `test_write_file_and_edit_file_refuse_a_locked_file_and_say_why` stays green unchanged (`read_only`).
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the tests, then the gate.**
- [ ] **Step 5: Commit** with the message `tools: write_file and edit_file refuse only the tester's tests in a locked file`.

### Task 3: Studio locks definitions and restores only them

**Files:**
- Modify: `src/airlie/topology/studio.py` (`lock`, `_restore_locked`, `_tools`)
- Test: `tests/unit/test_studio_orchestrator.py`

**Interfaces:**
- Consumes: `tester_definitions` from Task 1, `ToolContext.locked_tests` from Task 2.
- Produces:
  - `StudioOrchestrator.locked_tests: dict[str, frozenset[str]]`, filled by `lock`. For each locked path ending in `.py`, `tester_definitions` is given the text at `self.base_sha` (None when the base does not have the file) and the text at the lock commit. A result of None, and every non-`.py` path, goes in the whole-file set.
  - `self.locked` stays the set of all locked paths, so the pipeline's existing uses still hold. A new `self.locked_whole: frozenset[str]` is what `_tools` passes as `read_only`, and `_tools` passes `locked_tests` as well, to every role but `lock_owner`.
  - The `locked` event gains `tests: {path: sorted names}`.
  - `_restore_locked`:
    - Files locked whole: as today.
    - Files locked by definition: put back whole when `touched(names, lock_text, now_text)` is not `[]`, meaning a definition changed or the file no longer parses. Otherwise leave the file as it is.
    - `locked_restored` names only the files put back.

- [ ] **Step 1: Write the failing tests** (model them on `test_locked_files_are_refused_to_other_roles_and_put_back_before_the_gate`)
  - `test_lock_records_the_testers_definitions`: the base has `tests/test_p.py` with `class FakeBoard` and `test_old`. The tester's checkpoint adds `test_new`, then `lock`. Assert that the `locked` event's `tests` is `{"tests/test_p.py": ["test_new"]}`.
  - `test_a_shell_change_to_a_fake_survives_the_gate_and_one_to_a_test_is_put_back`: run two variants of a coder step that edits the file through `shell`. When it changes `FakeBoard`, the change is still there after the gate. When it changes `test_new`, the file is back as locked and `locked_restored` names it.
  - The existing locked-file test (`note.txt`, not Python) stays green unchanged.
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it.** Read the texts with `teams.worktree.find_file` and `read_blob`, as `story.context` does.
- [ ] **Step 4: Run the tests, then the gate.**
- [ ] **Step 5: Commit** with the message `studio: a locked test file keeps the tester's tests, the rest is the coder's`.

### Task 4: The tests' mend is for the tester's tests, and the coder is told

**Files:**
- Modify: `src/airlie/story/layer_checks.py` (a new `testers_test`), `src/airlie/story/pipeline.py` (`_amend_tests`), `src/airlie/story/briefs.py` (`CONTRACT`)
- Test: `tests/unit/test_story_layer_checks.py` (`testers_test`), `tests/unit/test_story_layer_checks_flow.py` (the mend; `tests/unit/test_story_supervisor_flow.py` also drives it), `tests/unit/test_story_briefs.py`

**Interfaces:**
- Consumes: `StudioOrchestrator.locked_whole` and `locked_tests` from Task 3.
- Produces:
  - `testers_test(test_id: str, whole: Collection[str], by_definition: Mapping[str, Collection[str]]) -> bool`. It is True when the test's file is in `whole` (as `in_files` decides today), or when the first name after `::` (a class or a function, with any `[params]` removed) is one of that file's definitions in `by_definition`.
  - `_amend_tests` sends the tester back only when every failing test is `testers_test(...)`. In the AIR-9 case (old tests failing on a fake) it returns False, and the gate's follow-up goes to the coder.
  - `CONTRACT` becomes: `"The tests for this layer were written before you (the result of 'tests' below lists them and what each asserts), and airlie has checked that each fails without the code. They are the contract, and you cannot change them: make them pass with the code. What else is in their files was there before them (fakes, helpers, imports): change that where your change needs it, a signature a fake copies say. If a test is wrong about a name or a signature the brief fixes, say which and why when you stop. Add tests in a new file for anything else you change. Your run_tests runs your tests; once they pass, you are done: airlie runs the whole suite after review."`

- [ ] **Step 1: Write the failing tests**
  - `test_testers_test`: a whole-file path → True. `"tests/test_p.py::test_new"` with `by_definition={"tests/test_p.py": {"test_new"}}` → True. `"tests/test_p.py::test_old"` → False. `"tests/test_p.py::TestX::test_a[1]"` with `{"TestX"}` → True.
  - In `test_story_layer_checks_flow.py`, `test_an_old_test_failing_in_a_locked_file_goes_to_the_coder_not_the_tester`: the gate fails only on `test_old` in a file locked by definition. Assert no `locked_tests_fail` event and no follow-up to `tests`.
  - The existing mend tests stay green. Update any whose fixture locks a `.py` file and expects the mend for a test the tester did not write.
  - `test_story_briefs.py`: the coder's brief contains `"What else is in their files was there before them"`.
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the tests, then the gate.**
- [ ] **Step 5: Commit** with the message `story: the tests' mend is for the tester's own tests; the coder may fix what was there before`.

### Task 5: AIR-9 again

- [ ] **Step 1:** Resume AIR-9 with `uv run airlie story run AIR-9 --repo /Users/noahkruswicki/git/airlie` from this worktree. The engine must be up. If layer 05 passes its gate, the story opens its stacked draft PRs.
- [ ] **Step 2:** Run `task story:steps -- AIR-9`. Read the layer 05 run's events for `locked` (it should show the tester's definitions), refused edits, and the gate.
- [ ] **Step 3:** Report to Noah: did the gate pass, how many coder steps were used, and whether any edit was refused.

## Not in this plan

- Making the tester put new tests in a new file. That is a prompt rule; this plan makes it unnecessary.
- The map balance and the tester paging big files despite their outlines (from the last run). Those are separate decisions.
