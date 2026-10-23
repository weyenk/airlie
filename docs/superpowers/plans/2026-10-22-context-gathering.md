# Context gathering: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Story roles get the code they need in their first message, so they stop spending steps re-reading it. Most of all the tester, which uses all 16 of its steps in 11 of 18 runs and writes first at a median of step 11.

**Architecture:** Five changes, plus a report that measures them.
- A step report reads the run traces and prints, per role, the steps spent before the first write. It is the before/after measure.
- A code outline (defs and classes with line numbers) is shared by the repo map and the brief.
- The brief's "Already read for you" block becomes budgeted. A file goes in whole when it fits the budget, as an outline when it does not, and never as a 5000-char head.
- The repo map lists the layer's files and the code directories first, instead of being cut off alphabetically inside `docs/`.
- `read_file` takes several paths in one call, under the same output cap.
- The tester gets 20 steps.

**Tech Stack:** Python 3.12, uv, stdlib `ast`, pydantic config, typer CLI, pytest.

**Spec:** the findings note `context-gathering-findings.md` in the session notes folder (2026-10-22). Its numbers are copied into "Evidence" below, so this plan stands on its own.

## Evidence (83 runs: AIR-7, AIR-28, AIR-30, AIR-31, AIR-16, AIR-17, AIR-9)

- The tester spends 72% of its steps before its first write, and 185 of those 186 steps are reads.
- 45% of the tester's reads before its first write (64 of 142) are of files the brief had already "read for you". For the coder it is 44% (110 of 252).
- 74% of those files are longer than the brief's 5000-char clip. 32 of the tester's 40 first re-reads start at line 1.
- Repeated reads before the first write are paging past the 20k output cap, not true repeats: 31 of 32 for the tester, 48 of 52 for the coder.
- The repo map for airlie renders 216k chars and is cut at 6000. It shows docs/, scripts/ and root files, with 0 lines under src/ or tests/.
- The full size of the files a layer names is a median of 55k chars and at most 178k chars, so loading them whole is not affordable. The tester reaches 36k of its 65k tokens by its first write, and got the 75% context warning in 5 runs.
- Models make about one tool call per turn: 1 parallel pair in 18 tester runs.
- 61% of the coder's steps come after its first write. That is the fix loop, which is **not in this plan** (see "Not in this plan").

## Global Constraints

- The test gate passes after every task: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- Every action has a Taskfile task. uv only.
- The guide stays inside the layer's shared `context` block (`briefs.layer_plan`), so the tester and the coder keep one common prompt prefix and the engine's prefix cache serves the coder's.
- The repo map stays the preamble of every role in a run. It is the same text for every role in one studio run.
- No change to `WRITE_NOW`, `WRITE_ONLY`, `READS_UNDER_HOLD`, `write_by` or the stuck nudges in `native_team.py`.
- No real trace excerpts are committed (the repo is public). Test fixtures are synthetic.
- Fixed values: `GUIDE_SHARE = 0.2` (the share of the smallest reader's `max_context`), `CHARS_PER_TOKEN = 3`, `READ_PATHS_MAX = 5`, tester `max_steps: 20`.

## Review Focus

1. **A named file that is not Python** (YAML, Markdown, JS) and too big for the budget. There is no ast outline for it, so it gets its line count and its first lines up to `FILE_HEAD_CHARS` = 2000, with the read-on line. *Test in Task 3.*
2. **A Python file that does not parse** (a syntax error at the base, or a template). Its outline falls back to the non-Python form, and the guide does not raise. *Test in Task 2.*
3. **Named files whose outlines together exceed the budget** (AIR-9 layer 04: 6 files, 178k chars). Files are taken in order of mention. Once even an outline does not fit, the rest are listed as `path (N lines)` only, and the guide never goes over the budget. *Test in Task 3.*
4. **`read_file` with `paths` where one path is missing or outside the worktree.** That entry says so in its own section, the other files are still shown, and the result is `ok`. *Test in Task 5.*
5. **A focus path in the repo map that is not tracked** (a file the layer creates). It is left out without an error, and the map still lists src/ and tests/. *Test in Task 4.*

---

### Task 1: Step report (`task story:steps`)

**Files:**
- Create: `src/airlie/obs/step_report.py`
- Modify: `src/airlie/cli.py` (a `steps` command on `story_app`), `Taskfile.yml` (`story:steps`)
- Test: `tests/unit/test_step_report.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Segment`: `story: str`, `run: str`, `layer: str | None`, `seg: str` (the trace folder under `trace/`, e.g. `tests` or `build/round-1`), `role: str`, `steps: int`, `first_write: int | None` (1-based), `reads_before_write: int` (read_file, list_dir, grep), `pages: int`, `repeats: int`, `brief_file_reads: int`, `max_steps_hit: bool`.
  - `story_segments(state_dir: Path, story: str, repo: Path) -> list[Segment]`
  - `render(segments: Sequence[Segment]) -> str`: a Markdown table per role, first conversations and fix rounds as separate rows.
- How the data is read:
  - Runs come from `stories/<story>/state.json["runs"]`, and layers from that story's `layer_start` events (`studio_run`, `layer`, `base_sha`).
  - Roles come from each run's `subtask_start` events, and steps from `runs/<id>/trace/**/step-NNN.json` (`u_t` holds the tool name and its JSON arguments).
  - A *page* is a `read_file` of a path already read, with an `offset` past every earlier offset for that path. Any other re-read is a *repeat*.
  - `brief_file_reads` counts reads before the first write of a path that `story.context.named_paths(layer.scope + "\n" + layer.brief)` returns and that exists at `base_sha`.
  - `max_steps_hit` is true when `steps` equals the role's `max_steps`, falling back to `teams.max_steps`.

- [ ] **Step 1: Write the failing tests**

  Build a synthetic state dir with one story, one run and one layer. Two trace folders: `tests`, with steps `read_file a.py`, `read_file a.py offset=400`, `read_file a.py`, `write_file t.py`; and `build`, with `edit_file x.py` first.
  - `test_segment_counts_reads_pages_and_repeats_before_the_first_write`: for `tests`, `first_write == 4`, `reads_before_write == 3`, `pages == 1`, `repeats == 1`.
  - `test_brief_file_reads_count_named_files_present_at_the_base`: the layer brief names `src/a.py` (committed) and `src/new.py` (not committed). Reads of both give `brief_file_reads == 1`.
  - `test_a_role_that_writes_first_has_no_reads_before_it`: for `build`, `first_write == 1` and `reads_before_write == 0`.
  - `test_render_has_one_row_per_role`: `render` output contains `| tester |` and `| coder |`.

- [ ] **Step 2: Run them and check they fail**

  Run `uv run pytest tests/unit/test_step_report.py -q`. Expected: ImportError, because `airlie.obs.step_report` does not exist yet.

- [ ] **Step 3: Implement `story_segments` and `render`, then add `airlie story steps KEY... [--repo PATH]`**

  The command prints `render(...)` over every story given. Add the Taskfile entry:
  ```yaml
  story:steps:
    desc: "Steps each story role spent before its first write: reads, pages, repeats, brief-file re-reads (task story:steps -- AIR-9 AIR-31)"
    cmds:
      - '{{.RUN}} airlie story steps {{.CLI_ARGS}}'
  ```

- [ ] **Step 4: Run the tests and the gate, then record the baseline**

  All tests pass. Then run `task story:steps -- AIR-7 AIR-28 AIR-30 AIR-31 AIR-16 AIR-17 AIR-9`. The tester's first conversations should show a median first write of 11 and 11 of 18 at the cap; that matches the Evidence section. Paste the table into the PR description as the baseline.

- [ ] **Step 5: Commit** with the message `obs: step report, the steps each story role spends before its first write`.

### Task 2: Code outline

**Files:**
- Create: `src/airlie/outline.py`
- Modify: `src/airlie/repo_map.py` (`_signatures` builds on `outline_entries`)
- Test: `tests/unit/test_outline.py`; `tests/unit/test_repo_map.py` stays green unchanged.

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Entry`: `line: int`, `depth: int` (0 for top level, 1 for a method), `text: str` (`class Name`, `def name(args) -> ret`, or `async def …`).
  - `outline_entries(source: str) -> list[Entry] | None`: top-level defs and classes, and the methods of top-level classes. Returns None when `source` does not parse.
  - `outline(path: str, source: str, *, head_chars: int) -> str`. The first line is `{path}: {n} lines`. For Python, one line per entry follows, `{line}: {"    " * depth}{text}`. For anything else (or Python that does not parse), the first lines up to `head_chars` follow, cut on a line boundary.
- `repo_map._signatures` keeps its current output (top-level only, no line numbers) by filtering `depth == 0` from `outline_entries`.

- [ ] **Step 1: Write the failing tests**
  - `test_outline_lists_classes_methods_and_functions_with_lines`: the source `"import x\n\nclass A:\n    def m(self, k: int) -> str:\n        return ''\n\nasync def f() -> None:\n    pass\n"` gives exactly the lines `"3: class A"`, `"4:     def m(self, k: int) -> str"` and `"7: async def f() -> None"` after the header `"src/a.py: 8 lines"`.
  - `test_a_file_that_does_not_parse_falls_back_to_its_head`: `outline("src/b.py", "def oops(:\n" + "x\n" * 5000, head_chars=100)` starts with `"src/b.py: 5001 lines"` and is at most `100 + len(header) + 1` chars long.
  - `test_non_python_gets_line_count_and_head`: a YAML source gives a header and its first lines, never past `head_chars`.

- [ ] **Step 2: Run them and check they fail** with ImportError.

- [ ] **Step 3: Implement `outline.py` and rebase `repo_map._signatures` on it.**

- [ ] **Step 4: Run** `uv run pytest tests/unit/test_outline.py tests/unit/test_repo_map.py -q`, then the gate. Expected: all pass.

- [ ] **Step 5: Commit** with the message `outline: defs and classes with line numbers, shared by the repo map`.

### Task 3: The budgeted guide

**Files:**
- Modify: `src/airlie/story/context.py`, `src/airlie/story/briefs.py` (the heading text, both places), `src/airlie/story/pipeline.py:557`, `:718`
- Test: `tests/unit/test_story_context.py`, `tests/unit/test_story_briefs.py`

**Interfaces:**
- Consumes: `outline(path, source, *, head_chars)` from Task 2.
- Produces:
  - `repo_guide(repo: Path, commit: str, text: str, *, budget_chars: int) -> str`
  - `guide_budget(cfg: AirlieConfig, roles: Sequence[str]) -> int`, equal to `int(min(cfg.roles[r].max_context for r in roles) * GUIDE_SHARE * CHARS_PER_TOKEN)`. For the tester and coder (65536 each) that is 39321 chars. For the architect (131072) it is 78643.
  - Constants: `GUIDE_SHARE = 0.2`, `CHARS_PER_TOKEN = 3`, `FILE_HEAD_CHARS = 2000`. `GUIDE_CHARS` (AGENTS.md) stays. `FILE_CHARS` and `MAX_FILES` go.
- The guide takes named files in order of mention and fits each one in the first form that still fits the budget:
  1. Whole: `### {path} (whole, {n} lines)` followed by the fenced file.
  2. Outline: `### {path} (outline: read a span with read_file offset and limit)` followed by the fenced outline.
  3. Listed: `- {path} ({n} lines)`, collected under `### Also named (not shown)`.

  AGENTS.md comes first and counts against the budget.
- In `briefs.py`, the heading `## Already read for you (do not read these again)` becomes `## Read for you: whole files are complete, do not read them again; outlines give the line to read from`.
- The pipeline passes `budget_chars=guide_budget(self.cfg, [r for r in (self.s.tester_role, self.s.build_role) if r])` at :718, and `guide_budget(self.cfg, [self.s.architect_role])` at :557.

- [ ] **Step 1: Rewrite the clip test and add the new ones**
  - `test_a_file_that_fits_goes_in_whole`: a 50-line `src/m.py` with `budget_chars=10_000` gives `"### src/m.py (whole, 50 lines)"`, and its last line is in the guide.
  - `test_a_file_over_the_budget_goes_in_as_an_outline`: a 3000-line `src/big.py` with defs at lines 10 and 2900 and `budget_chars=5_000` gives the `(outline:` heading, and contains `"2900: def"`.
  - `test_the_guide_never_exceeds_its_budget`: seven 40k-char Python files named, `budget_chars=20_000` → `len(guide) <= 20_000`, and the later files appear under `### Also named (not shown)` as `(N lines)` entries.
  - `test_a_big_non_python_file_gets_its_head`: a 100k-char `docs/x.md` with `budget_chars=8_000` gives the outline heading and at most `FILE_HEAD_CHARS` of its text.
  - `test_guide_budget_is_a_share_of_the_smallest_readers_context`: from the repo's `airlie.yaml`, `guide_budget(cfg, ["tester", "coder"]) == 39321`.
  - In `test_story_briefs.py` (lines 500–527), the five `Already read for you` assertions check the new heading instead.
  - The existing tests for missing files, the empty guide and the commit-not-worktree read keep their assertions. Pass them `budget_chars=10_000`.

- [ ] **Step 2: Run them and check they fail.** Expected: TypeError on `budget_chars`, and failures on the new headings.

- [ ] **Step 3: Implement `repo_guide` and `guide_budget`, change the heading, and wire both pipeline calls.**

- [ ] **Step 4: Run** `uv run pytest tests/unit/test_story_context.py tests/unit/test_story_briefs.py tests/unit/test_story_pipeline.py -q`, then the gate. Expected: all pass.

- [ ] **Step 5: Commit** with the message `story: the guide hands whole files or outlines within a context budget, never a 5000-char head`.

### Task 4: A repo map that shows the code

**Files:**
- Modify: `src/airlie/repo_map.py` (`_render`, `build_repo_map`), `src/airlie/topology/studio.py` (`execute_task`, `plan`, `_prepare`), `src/airlie/story/pipeline.py` (the `execute_task` call at :752)
- Test: `tests/unit/test_repo_map.py`, `tests/unit/test_studio_orchestrator.py`

**Interfaces:**
- Produces: `build_repo_map(repo, *, test_cmd, max_chars, depth, cache_dir, focus: Sequence[str] = ()) -> tuple[str, bool]`. `focus` goes into the cache fingerprint.
- Consumes: `StudioOrchestrator.execute_task(..., map_focus: Sequence[str] = ())` passes `focus=map_focus` through `_prepare`. The pipeline passes `map_focus=named_paths(layer.scope)`.
- Order of the rendered map, with the budget filled in this order:
  1. The `[Repository map]` header and the test command.
  2. `## In scope`: each tracked focus file with its full outline (Task 2's `outline_entries`, with line numbers).
  3. Every top-level directory as one line, `{dir}/ ({n} files)`, which is always present.
  4. The directories expanded, in this order: those holding a focus file, then those holding files with a code suffix (`.py .js .ts .tsx .go .rs .java .kt .swift .c .h .cpp`), then those named `tests` or `test`. Root files come after these, and other directories last. Each expanded file line keeps today's form, `path (size): signatures`.
  5. Expansion stops at the first directory that does not fit, and the map ends with `[map truncated]`, as today.

- [ ] **Step 1: Write the failing tests**
  - `test_code_and_tests_come_before_docs_that_outweigh_the_budget`: a repo with 200 large `docs/*.md` files, plus `src/pkg/a.py` and `tests/test_a.py`, at `max_chars=3000`. Assert `"src/pkg/a.py"` and `"tests/test_a.py"` are in the map, and `"docs/ (200 files)"` is a line.
  - `test_focus_files_come_first_with_their_outline`: `focus=["src/pkg/a.py"]` → `"## In scope"` comes before the first directory line and contains `"1: def"`.
  - `test_an_untracked_focus_path_is_left_out`: `focus=["src/pkg/new.py"]` → no error, and `"new.py"` is not in the map.
  - `test_every_top_level_directory_is_named_even_when_truncated`: at `max_chars=400` each top-level directory still appears as a `/ (` line.
  - Studio: `test_the_story_build_maps_the_layers_files`. Using `FakeEngine`, `execute_task(..., map_focus=["src/m.py"])` → the first user message of the `build` subtask contains `"## In scope"`.
  - The existing repo-map tests stay green unchanged.

- [ ] **Step 2: Run them and check they fail.**

- [ ] **Step 3: Implement the ordering and `focus`, then plumb `map_focus` through studio and the pipeline.**

- [ ] **Step 4: Run the tests and the gate.** Then check by hand, as a user would: run `uv run python -c "from pathlib import Path; from airlie.repo_map import build_repo_map; print(build_repo_map(Path('.'), test_cmd='task test', max_chars=6000, depth=3, cache_dir=None)[0])"` on this repo. It prints `src/` and `tests/` lines.

- [ ] **Step 5: Commit** with the message `repo map: the layer's files and the code directories first, every top-level directory named`.

### Task 5: `read_file` with several paths

**Files:**
- Modify: `src/airlie/tools/fs.py` (`ReadFile`)
- Test: `tests/unit/test_fs_tools.py`

**Interfaces:**
- Produces: the `read_file` parameters gain `paths: {"type": "array", "items": {"type": "string"}, "maxItems": READ_PATHS_MAX}`, with `READ_PATHS_MAX = 5`. `required` becomes `[]`, and the call needs exactly one of `path` or `paths`.
  - Each file is shown under `=== {path} ({n} lines) ===`, numbered from line 1.
  - `offset` and `limit` work only with `path`. With `paths` they give `error: offset and limit read one file: use path`.
  - The whole result is capped at `ctx.output_cap_chars`, so batching never adds more context per step than one read does today.
  - Each file cut by the cap ends with the same `read on with offset=` notice as `_cap_lines`. A file that gets no room at all is listed as `{path}: not shown, over the output cap; read it with path`.
- The description gains one sentence: `"Several small files at once: paths (up to 5), each shown in full up to the output cap."`

- [ ] **Step 1: Write the failing tests**
  - `test_read_file_paths_shows_each_file_with_its_header`: two small files → both headers, both bodies, `ok`.
  - `test_read_file_paths_stay_under_the_output_cap`: three 15k-char files with `output_cap_chars=20_000` → `len(output) <= 20_000 + len(notices)`, and the third file's `not shown` line is present.
  - `test_read_file_paths_missing_one_still_shows_the_others`: one missing path → `error:` in its section, the other file shown, `ok` true.
  - `test_read_file_paths_with_offset_is_refused`.
  - `test_read_file_needs_path_or_paths`: no argument, or both → an error naming both.

- [ ] **Step 2: Run them and check they fail.**

- [ ] **Step 3: Implement it in `ReadFile.run`.** The single-`path` behaviour stays exactly as it is.

- [ ] **Step 4: Run the tests and the gate.**

- [ ] **Step 5: Commit** with the message `tools: read_file takes up to five paths in one call, under the same output cap`.

### Task 6: Tester steps 16 → 20

**Files:**
- Modify: `airlie.yaml` (`roles.tester.max_steps: 20`)
- Test: `tests/unit/test_config.py`

The tester's work before it is done is at least 3 to 5 reads, then writing its tests, running them, and one or two fixes. Even with the reads saved by Tasks 3 and 4 that is close to 16. `write_by` follows the budget (`max(4, 20 * 2 // 5) = 8`), so the nudges move with it unchanged.

- [ ] **Step 1: Write the failing test.** `test_the_tester_has_twenty_steps`: load `airlie.yaml` and assert `cfg.roles["tester"].max_steps == 20`.
- [ ] **Step 2: Run it and check it fails** (`16 != 20`).
- [ ] **Step 3: Set `max_steps: 20`** under `roles.tester` in `airlie.yaml`.
- [ ] **Step 4: Run the test and the gate.**
- [ ] **Step 5: Commit** with the message `config: the tester gets 20 steps`.

### Task 7: A real story run, measured

- [ ] **Step 1:** With the engine up (`task up`), resume AIR-9 with `task story -- AIR-9`. Its layer 05 tester used all 16 steps in both of its runs.
- [ ] **Step 2:** Run `task story:steps -- AIR-9` and compare its tester and coder rows with the Task 1 baseline. Both targets must hold:
  - the tester's median first write is at step 8 or earlier (from 11);
  - `brief_file_reads` per tester run is at most half the baseline (from about 3.6).
- [ ] **Step 3:** Put both tables in the PR description. If a target is missed, report it to Noah with the table and stop. Do not cut a task.

## Not in this plan

- **The coder's fix loop.** 61% of coder steps come after the first write. In the 10 runs that hit 40 steps, 281 of 400 steps did. That needs its own investigation now that AIR-30 (carry thinking, PR #124) has landed: measure it with Task 1's report first, then fix.
- Counting a step per generation rather than per call. Models rarely batch, so it buys little until Task 5 shows they do.
