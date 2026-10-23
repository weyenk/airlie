# A retry that writes: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A role sent back to fix its own work (the tester's pre-code retry, a coder's fix round) starts from what it already wrote, is held to writing as a first run is, and the tester stops writing tests for behaviour the brief says stays unchanged.

**Architecture:** Three changes.
- A fresh continuation hands the role its own diff, so it does not read its files again.
- The generic write nudge counts a continuation's own writes, not the files of the run it continues.
- The tester's rules say a behaviour the brief keeps as it is gets no new test.

The branch first takes main, where PR #129 moved every prompt text into `src/airlie/prompts/`.

**Tech Stack:** Python 3.12, uv, pytest with `FakeEngine`, the prompt hub (`airlie.prompts.text(id)`, the golden snapshot `tests/unit/data/prompt_texts.json`, `governs` tags).

**Spec:** AIR-9 layer 05, run `20261023-081549-c197` (the shared notes file `test-lock-build.md`).
- The tester added `test_airlies_own_narration_stays_authored_by_airlie`. The brief says the picked-up, resume and block notes "stay role-less", so the test passes before the code, and the pre-code check found `passes_before_code`.
- The retry's stored conversation was 40,960 tokens, over `teams.fresh_continuation_tokens` (32,768), so it started fresh. It spent all 20 steps reading the files it had written and running tests, and never wrote. Deleting one test was the whole fix.
- No write nudge fired. `native_team` sets `wrote = bool(ctx.already_written)`, so a continuation counts as having written before its first step.

## Global Constraints

- The test gate passes after every task: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- Every action has a Taskfile task. uv only.
- Prompt text lives in the hub, never inline:
  - A changed or new text is a `.md` file under `src/airlie/prompts/{rules,steering}/` with its `kind`, `roles`, `when` (for steering), `governs` and `why`.
  - After a text change, regenerate the snapshot with `AIRLIE_WRITE_PROMPT_TEXTS=1 uv run pytest tests/unit/test_prompt_texts.py`, then rerun it without the flag.
  - `tests/unit/test_prompt_contradictions.py` passes.
  - A new `governs` subject is checked against `task prompts:subjects` for a near-duplicate first.
- `teams.fresh_continuation_tokens` and when a fresh start happens are unchanged (HDS-5: resending 58–72k tokens cut the coder's replies off). Only what the fresh start carries changes.
- The coder's write schedule (`write_schedule`) and its continuation deadline are unchanged.
- Test fixtures are synthetic.
- Fixed values: `FRESH_DIFF_CHARS = 12_000`.

## Review Focus

1. **A coder's fix round whose diff is far over 12k chars** (a big layer). It gets the per-file list (`path (+a −b)`) instead of the diff, never a cut-off diff. *Test in Task 2.*
2. **A continuation that writes on its first step.** No nudge fires, as for a first run. *Test in Task 3.*
3. **A continuation of a role without write tools** (a reviewer, the architect). No write nudge, as today. *Test in Task 3.*
4. **The tester's retry for a test of behaviour that stays unchanged.** The findings text tells it to delete that test, and the test's name is in the message. *Test in Task 4.*
5. **A short conversation continued as it is** (under the threshold). It gets no diff and no fresh text: the conversation already holds its work. *Test in Task 2.*

---

### Task 1: Take main (PR #129, the prompt hub)

**Files:** whatever the merge touches. Predicted conflicts are in `src/airlie/story/briefs.py`, `src/airlie/story/pipeline.py`, `tests/unit/test_config.py` and `tests/unit/test_studio_orchestrator.py`.

- [ ] **Step 1:** Bring the branch up to date with `main` through the app's `sync_with_base_branch` (ccd_host), not a hand `git merge`.
- [ ] **Step 2:** Resolve each conflict so both sides' intent holds.
  - This branch's `CONTRACT` wording ("What else is in their files was there before them …") goes into `src/airlie/prompts/rules/tests-contract.md`. Its `governs` gets a subject for "the coder may change what was there before" (check `task prompts:subjects` for an existing one first).
  - `briefs.py` reads the text through the hub.
  - Any other inline text this branch changed moves the same way. The `LOCKED`/`LOCKED_TESTS`/`UNPARSEABLE` refusals in `fs.py` stay where they are, unless main moved the old `LOCKED` into the hub: then the new texts follow it there.
- [ ] **Step 3:** Regenerate the prompt snapshot. Then run the full gate, `tests/unit/test_prompt_texts.py` and `tests/unit/test_prompt_contradictions.py`.
- [ ] **Step 4:** Commit the merge, with a message naming the conflicts and how each was resolved.

### Task 2: A fresh continuation carries the role's own diff

**Files:**
- Modify: `src/airlie/topology/studio.py` (`_continuation`), `src/airlie/prompts/steering/fresh-continuation.md`
- Test: `tests/unit/test_studio_orchestrator.py`

**Interfaces:**
- Produces:
  - `own_changes(worktree: Path, since: str, limit: int = FRESH_DIFF_CHARS) -> str` in `src/airlie/teams/worktree.py`. It returns the `git diff <since>` of the working tree (untracked files included, as `changed_in_tree` finds them) as one fenced `diff` block. When that is over `limit`, it returns one line per file instead, `path (+added −removed)`, under the heading `Too long to show; the files you changed:`. When nothing changed, it returns `""`.
  - `_continuation`, on a fresh start, builds the goal as `{brief}\n\n{fresh-continuation}\n\n{own_changes(...)}\n\n{nudge}`. `since` is the commit the subtask started from (`run.before`). With `run.before` unset, it gives no diff.
- `fresh-continuation.md` becomes: `You have already worked on this subtask, and your earlier conversation is not repeated here. What you changed so far is below: it is in the worktree as shown, so do not read those files again to find it. Pick up from there and deal with what follows.` Its `governs` keeps `history: worktree` and adds a subject for "the diff is given" (check `task prompts:subjects`).

- [ ] **Step 1: Write the failing tests**
  - `test_a_fresh_continuation_hands_the_role_its_own_diff`: a subtask writes `tests/test_x.py`, and its stored state is forced over the threshold (a small `fresh_continuation_tokens` in the test config). Then `follow_up`. The first user message of the new conversation contains `+def test_x` inside a ```` ```diff ```` block, and the hub's fresh-continuation text.
  - `test_a_diff_over_the_limit_is_listed_by_file`: `own_changes(wt, since, limit=100)` on a change of a few hundred chars gives the heading and a `tests/test_x.py (+N −0)` line, and no `@@`.
  - `test_a_short_conversation_continues_without_a_diff`: under the threshold, the conversation continues, and no ```` ```diff ```` block is added.
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it, edit the steering file, and regenerate the snapshot.**
- [ ] **Step 4: Run the tests, then the gate.**
- [ ] **Step 5: Commit** with the message `studio: a fresh continuation hands the role the diff it already made`.

### Task 3: A continuation owes a write of its own

**Files:**
- Modify: `src/airlie/teams/native_team.py` (the generic `write_by` nudge and hold), plus a new `src/airlie/prompts/steering/write-now-continuing.md`
- Test: `tests/unit/test_native_team.py`

**Interfaces:**
- In a continuation (`ctx.already_written` non-empty), the generic nudge and hold key on this run's own landed write (`changed`), not on `wrote`.
  - At `write_by` steps without one, the role is told `write-now-continuing` (fields `n`, `total`): `You have used {n} of your {total} steps on this round and changed nothing yet. You were sent back to change something: make that change now with edit_file or write_file, then run run_tests.`
  - At `write_by + WRITE_ONLY_AFTER`, the hold (`write-only`, with `what="nothing this round"`) starts, exactly as for a first run.
  - A first run behaves as today. A role with a `write_schedule` keeps its own continuation deadline (unchanged).
- `write-now-continuing.md`: `kind: steering`, `roles: [tester]` (the same roles as `write-now.md`), with a `when` and a `why` citing AIR-9 layer 05, 2026-10-23. Its `governs` reuses `write-now.md`'s subjects (`reading: stop`, `writing: now`).

- [ ] **Step 1: Write the failing tests**
  - `test_a_continuation_that_only_reads_is_nudged_then_held`: `already_written=("tests/test_x.py",)`, `max_steps=20`, and a FakeEngine that only calls `read_file`. The write-now-continuing text arrives after step 8, and the hold after step 12 (`read_refused` follows once the hold's reads run out).
  - `test_a_continuation_that_writes_first_is_not_nudged`.
  - `test_a_continuation_without_write_tools_is_not_nudged`.
  - The existing write-nudge tests stay green.
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it, add the steering file, and regenerate the snapshot.**
- [ ] **Step 4: Run the tests, then the gate.**
- [ ] **Step 5: Commit** with the message `team: a continuation is nudged to write its own change, as a first run is`.

### Task 4: Behaviour that stays unchanged gets no new test

**Files:**
- Modify: `src/airlie/prompts/rules/tests-only.md`, `src/airlie/prompts/steering/checks-fix-before-code.md`, `src/airlie/story/layer_checks.py` (`feedback`)
- Test: `tests/unit/test_story_layer_checks.py`, the prompt snapshot

**Interfaces:**
- `tests-only.md` gains, after the sentence on "does NOT happen": `A behaviour the brief says stays as it is (leave it, keep it, unchanged) is not this layer's to test: it passes now and will pass after, and the tests already there guard it. Write no test for it.`
- `checks-fix-before-code.md` gains, after the "nothing happens" sentence: `A test of something the brief keeps as it is passes now and always will: delete it.`
- `feedback(...)`: for each `passes_before_code` finding, the line after `- {test}: {why}` reads `  if it checks something the brief keeps unchanged, delete it (edit_file with the whole test as old and nothing as new); else make it fail now.`. This is a new steering text, `passes-before-code-choice.md`, with field `test`, kept beside the `LOOSE_MATCH` line logic.
- Both rule files gain a `governs` subject for "unchanged behaviour gets no test", for example `tests.unchanged: none` (check `task prompts:subjects`). The contradiction check passes: nothing else governs that subject differently.

- [ ] **Step 1: Write the failing tests**
  - `test_a_test_that_passes_before_code_is_told_it_may_be_deleted`: `feedback([Finding("passes_before_code", "tests/test_p.py::test_keeps", PASSES_BEFORE_CODE, "...")])` contains `test_keeps` and `delete it (edit_file with the whole test as old and nothing as new)`.
  - The snapshot test fails until it is regenerated: regenerate it, and check the diff shows only the three intended texts.
- [ ] **Step 2: Run them and check they fail.**
- [ ] **Step 3: Implement it.**
- [ ] **Step 4: Run the tests, the prompt tests and the gate.**
- [ ] **Step 5: Commit** with the message `tester: behaviour the brief keeps unchanged gets no new test; a retry may delete one`.

### Task 5: AIR-9 again

- [ ] **Step 1:** Resume AIR-9 from this worktree with `uv run airlie story run AIR-9 --repo /Users/noahkruswicki/git/airlie`. The engine must be up. If layer 05 passes, the story opens its stacked draft PRs.
- [ ] **Step 2:** Run `task story:steps -- AIR-9`, and read the layer 05 run's events. Check:
  - whether the tester wrote a test for unchanged behaviour;
  - on a retry, whether it got the diff and the nudge, and when it wrote;
  - whether the coder reached the gate, and whether the by-definition lock let it fix the fakes.
- [ ] **Step 3:** Report to Noah with the numbers.

## Not in this plan

- Changing `fresh_continuation_tokens`, or summarising a long conversation instead of starting fresh.
- Checking after the gate that every one of the tester's tests passed. That is the lock follow-up from the last plan, still Noah's call.
- An architect rule against "leave X unchanged" lines in briefs. Those lines are right for the coder; it is the tester that must not test them.
