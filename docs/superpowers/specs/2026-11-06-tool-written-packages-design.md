# Tool-written packages: the architect writes its tests as files, its reply is a short JSON

2026-11-06. Direction approved by Noah; this spec covers how it fits the story pipeline. Not built.

## Why

Today the architect writes a layer's whole package in one reply: every test file in fenced blocks
(`### test file:` / `### test additions:`), then the package JSON (`PACKAGE_CONTRACT`,
`src/airlie/story/package.py`). From 2026-10-31 to 11-05, 48% of package rounds blocked after 2 tries, and 56% of
returned tries had no usable package: cut off at max_tokens (10), max_steps (7), no JSON block (7),
repeated_action (4), parse errors. About 20.6 h lost. The reply is 40k+ characters for an 18-test layer. Thinking
is cut at 2048 tokens while the architect designs all of it at once. #235/#239 (answer_after_cuts, plain-text
answer turn) and #240 (a soft cap on layer size) treat symptoms. A failed try also loses everything it wrote.

## Goals

1. No reply holds test code. The reply is the package JSON, a few thousand characters at most.
2. A test is written with `write_file` / `edit_file`, one file (or one test) per call, into a scratch checkout of
   the layer's base. A try that fails keeps its files, and the next try edits them.
3. `Package` stays as it is (path -> whole text), so the checks, the lock, the coder's brief, state.json and PR
   provenance do not change.

## Non-goals

- Changing what the checks check (`PackageChecker.check`), the critic's verdict, or the coder's build.
- The retry carrying the previous package's JSON. That is being fixed separately; this design gives it the files.
- Letting the architect write anything other than test files.

## Design

### The checkout and the tools

The architect's ask already runs in a detached worktree at a commit (`Asker.ask` / `Asker.conversation` ->
`execute_task(detach=True)`, `run_dir/studio`). That worktree is the scratch checkout. No new worktree is needed.

- **Tools for the package subtask only.** The architect role keeps `[read_file, list_dir, grep, compute]` for
  survey, layers and the meeting. The package asks (`_package`, `_draft_packages`) pin a subtask that also gets
  `write_file` and `edit_file`. This needs a new `Subtask.tools` field (extra tool names), which
  `StudioOrchestrator._tools` adds. The architect is not in `studio.review.roles`, so writing triggers no review or
  done-check. It does turn on `native_team`'s write nudges (`write-now` at 40% of steps, then the write-only
  hold). Here that is what we want.
- **Confinement.** Add `ToolContext.write_scope: Literal["any", "tests"]`. With `"tests"`, `write_file` and
  `edit_file` refuse any path that is not `is_test_file` (pytest `test_*.py` / `*_test.py`, Vitest
  `*.test.ts` and kin), with a one-line error naming the rule. `resolve` already refuses paths that leave the
  worktree. This is the same rule `parse_package` applies today: a package cannot carry a non-test file.
- **Size per call.** Add `ToolContext.write_max_chars` (story setting `package_write_chars`, default 12 000,
  about 3k tokens). A `write_file` or `edit_file` whose new text is longer is refused: "write the file with its
  first tests, add the rest with edit_file". This keeps each completion well under max_tokens.
  `write_file_max_overwrite_lines` (200) already sends a long existing file to `edit_file`, which is the old
  "test additions" case.

### Harvest: the tests come from the checkout, not the reply

After each package turn, `harvest(worktree, since)` returns the test files changed or added since `since`. It
uses `scope.changed_test_files`, plus a check that no test function that existed at `since` was deleted
(`deleted_test` is refused as a parse error). `parse_package(text, layer, story, gated, tests=harvested)` reads
the JSON only. Its `covers` and `files` checks run as today, against the harvested texts. Any test-file blocks
in the reply are written into the checkout before the harvest (fallback, one code path), so a reply in the old
form is still usable.

The new contract (`PACKAGE_TOOLS_CONTRACT`, replacing `PACKAGE_CONTRACT` in `package_brief`):

> Write each test file with write_file, one file per call; add tests to a file that exists with edit_file, one
> or a few tests per call. Write no test code in your reply. When the tests are written, end your reply with
> one ```json block: {"type": "package", "files": [...], "interfaces": [...], "steps": [...],
> "covers": {...}, "done_when": "..."}.

The JSON shape does not change. "Guards" are not a field today. If they mean the existing tests a change
breaks (`brief-package-existing-tests`), the architect now edits those tests in place (see Q1), so no new field
is needed.

### The single ask (`_package`) and a retry

- **First try:** pinned subtask with write tools at `base_sha`, harvest since `base_sha`, parse.
- **Retry while the drafting conversation is held (`_resumed`):** the checkout is committed after every turn
  (`commit_wip`), so `detach_at(base_sha)` finds a clean tree. Then the pipeline places the last package's tests
  (rebased, with ruff's fixes, `place_package`) and commits them as "previous try". The architect is told that
  the files are in place and to change what the findings name with `edit_file`, not write them again. The
  harvest is still taken since `base_sha`.
- **Cold retry (no held conversation, e.g. after a restart):** `Asker.ask` gains a `prepare` passthrough to
  `execute_task(prepare=...)`, which places the previous tests the same way.
- **An unusable try keeps its files.** A try that is cut off, hits max_steps or has no JSON is harvested anyway.
  `st.drafts[nn] = {"unusable": why, "tests": harvested}` and the next try starts from those files. A missing
  JSON becomes a cheap answer turn ("reply with the package JSON only; your tests are written"), not a rewrite.

### Drafting several layers in one conversation (`_draft_each`)

The conversation's checkout is at the story's base. After layer NN's turn, its files are committed as snapshot
`S_NN`, kept under the ref `refs/airlie/<story>/draft-NN` so `git gc` cannot drop it. Layer NN's tests are those
harvested since `S_(NN-1)` (or the base). Layer NN+1 finds layer NN's tests already in the files and edits
them. This replaces "when a later package changes it, write it whole" in `brief-package-drafts`. The draft is
stored as today (`{"package": to_dict(), "run": run}`) plus `"at": S_(NN-1)`, the commit its edits were made on.

**Rebase onto the real base.** Today `Package.rebased` re-joins `added` text. A tool-written draft can also
change a file in place, so it is rebased with a three-way merge per file: `git merge-file` with ancestor = the
file at `at`, ours = the file at the layer's real base, theirs = the draft. If the real base equals the ancestor,
the draft is taken as it is. A conflict is a `draft_conflict` finding. The layer then retries in the
conversation with the files placed at its real base, as above.

### The critic

`_objections` no longer needs `_with_tests` to build a commit. The critic reads at the draft's snapshot (or the
single ask's checkout committed after the turn). `critic_brief` still inlines the test files as data.
`package-critic.md` loses its now-false "not in the repository yet" paragraph and says they are at the paths
shown. An objection is one more architect turn that edits the files in place, followed by a new harvest.

### The coder

No change. `_place` writes `package.tests` (whole texts) into the layer's worktree, commits and locks them.
Locking by definition (`studio.lock`) covers tests that were changed in place as well as new ones.

## Compatibility

- **`Package`, `to_dict`, `from_dict`, state.json:** unchanged. A draft gains an optional `"at"`; the unusable
  record gains an optional `"tests"`.
- **Resumed stories with old drafts:** `"package"` without `"at"` is rebased with `added` and `joined_tests`, as
  today. `joined_tests` and `_test_blocks` stay; the blocks become the fallback path above.
- **Flag:** `story.package_tools: bool`. It is on in committed `airlie.yaml` from the PR that ships step 3. With
  it off, the old contract and reply parsing run unchanged. The old path is removed once two real stories have
  run on the new one.

## How the checks map

| Check | Today | Tool-written |
|---|---|---|
| Parse / heading / fence | `_test_blocks` on the reply | gone (files on disk); JSON parse only |
| covers -> named test exists | in reply blocks | in harvested files |
| ruff fix, then check | scratch `package-NN`, fixes carried by `with_tests` | same; on a retry the fixed text is what is placed back, so the architect edits the fixed file |
| unparseable, assertion-free, covers_missing (new or changed test) | scratch at base | same, unchanged |
| imports, unknown methods, sweeps | scratch | same |
| red on base (pytest), Vitest red + `npm run typecheck` | scratch | same |
| file_exists / file_missing | JSON `files` vs base | same |

`PackageChecker` keeps its own scratch worktree (`st.path/package-NN`) placed from `Package.tests`. That gives the
red check a clean base whatever else the architect's checkout holds, and it is code that already works.

**Failure classes:**

- **Removed or much reduced:** cut_off at max_tokens (the reply is JSON only, and each write is capped), no JSON
  block (the files survive and the answer turn is small), repeated whole-package rewrites, and heading, fence and
  parse errors of test blocks. The loss of a whole try goes too, since a failed try keeps its files.
- **Not removed:** max_steps (writes use steps), repeated `edit_file` misses (`_not_found` and `nudge_stuck`
  help), all content findings (lint, red, imports, covers), and thinking cut at budget. That last one is helped,
  because each step designs one file or test, not the layer.

## Risks and open questions (with recommendations)

1. **Editing existing tests in place versus only adding tests.** In-place edits let the architect change an
   existing test's expectation itself. That makes it part of the contract instead of a step the coder meets
   after the code. The risk is weakening tests to make the layer easy. *Recommend:* allow edits. Refuse deleting
   a test function. List "changed existing tests" in `package_ready`, in the critic's brief (as a before/after
   pair; `Finding.before` exists) and in the PR provenance.
2. **Helpers, `conftest.py`, fixtures.** These are not writable, as today. *Recommend:* keep it that way for v1.
   Revisit if the critic or the checks show packages working around it.
3. **Step budget.** 18 tests one at a time is 18 or more steps. *Recommend:* one call per new file under the
   12k cap, one call per test only for additions to an existing file. Raise `package_steps` from 24 to 32.
4. **answer_after_cuts with write tools.** Its answer turn offers no tools. If it fires before anything is
   written, the architect can only reply in text. *Recommend:* when the role can write and has not written this
   turn, answer_after_cuts starts the write-only hold (`write-only`, existing) instead of the tool-less turn. The
   tool-less JSON turn follows a write. The block fallback above covers a reply that writes tests anyway.
5. **Tool-call arguments holding code.** Quotes, backslashes and fences inside `content` and `old`/`new`. The
   coder already writes files this way on the same engine (`qwen3_xml_raw`, raw parameter values).
   *Recommend:* no extra handling. Watch `malformed` events on the first runs.
6. **Review of what it wrote.** *Recommend:* no new review step. The critic (proof), the mechanical checks and
   the human PR review stay as they are. Add a `package_written` event (paths, chars, write and edit counts)
   so the pipeline screen shows the step.
7. **Snapshot refs piling up.** *Recommend:* delete `refs/airlie/<story>/draft-*` when the story publishes or is
   discarded.
8. **Separate role versus `Subtask.tools`.** A role `architect-package` in `roles.yaml` would need no studio
   change, but it splits lessons, steering ids and sampling across two names. *Recommend:* `Subtask.tools`.

## Decisions (Noah, 2026-11-06)

1. **Editing existing tests in place:** allowed. Deleting a test is refused. Edited existing tests are shown to
   the critic and in the PR.
2. **Helpers and `conftest.py`:** not writable in v1.
3. **Step budget:** `package_steps` goes from 24 to 32.
4. **answer_after_cuts:** a writer whose thinking keeps getting cut before it has written anything gets the
   write-only hold, not the tool-less answer turn (build step 5).
5. **"Guards":** these are the regression guards of PR #244 (`Package.guards`, `"guards": {"<test id>":
   "<reason>"}` in the package JSON, and the `satisfied` reply). The JSON-only contract keeps `guards` and the
   `satisfied` reply. #244 merges before build step 3; steps 1 and 2 do not depend on it.

## Build plan (independent PRs, each through the gate)

1. **Tools.** `ToolContext.write_scope` and `write_max_chars`; `Subtask.tools` added in `_tools`. Tests:
   `write_file`/`edit_file` refuse a non-test path and an over-cap text; a pinned subtask gets the extra tools and
   a non-pinned one does not; no review for a writing role outside `review.roles`.
2. **Harvest and contract.** `harvest()` (deleted-test refusal); `parse_package(..., tests=)` JSON-only, with
   reply blocks placed first; `PACKAGE_TOOLS_CONTRACT`, the `brief-package-task`, `brief-package-next` and
   `brief-package-drafts` wording, and the `story.package_tools` flag (off). Tests: covers resolved against
   harvested files; an old-form reply still parses; the brief carries the new contract only with the flag on.
3. **Single ask and retries.** Write tools on `_package`; `Asker.ask(prepare=)`; `_resumed` commit, detach and
   place; unusable try keeps `tests`; JSON-only answer turn. Turn the flag on. Tests (fake engine): a cut-off
   try's files are placed for try 2; a retry's checkout holds ruff's fixes; the harvest is since `base_sha`.
4. **Drafting and critic.** Per-layer snapshots and refs; harvest since the previous snapshot; `"at"` on drafts;
   `merge-file` rebase with `draft_conflict`; the critic reads at the snapshot; `package-critic.md` wording;
   ref cleanup. Tests: layer 02 edits layer 01's file and the rebase onto a real base with a coder-changed fake
   merges cleanly; a conflicting one is a finding; old drafts with `added` still rebase.
5. **Cuts and the real run.** answer_after_cuts starts the write-only hold for a writer that has not written.
   Then roman and one real AIR story from a clean state. Success is measured against the numbers in "Why":
   blocked package rounds well under 48%, unusable tries well under 56%, and no `cut_off` or no-JSON try.
