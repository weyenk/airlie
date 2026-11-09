# Mutation testing: a small PR gate, a nightly sweep, survivors as fix cards — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep a PR's mutation gate to the functions the change touched, move every whole-module run to a nightly
sweep of `main`, and turn the sweep's surviving mutants into fix cards on airlie's board that the line builds
into draft PRs once a person approves them.

**Architecture:** `scripts/mutate_changed.py` gains three things: `--defer-setup` (a setup-file change mutates
nothing on a PR), an `equivalent` list in `pyproject.toml` (a listed mutant that no test can catch stops
failing runs), and `--survivors` on `--merge` (a JSON file of the gating survivors and their diffs). A new
`mutation-nightly.yml` replaces `mutation-weekly.yml`. In airlie, a new `airlie.story.mutation_cards` module,
called from the line's bookkeeping `bookkeep()`, downloads the newest nightly's survivors with `gh` and files
one `fix` card per function, deduplicated by signature, in `todo`.

**Tech Stack:** Python 3.12 + uv, mutmut, GitHub Actions, `gh`, airlie's `BoardStore`, pytest.

**Spec:** this plan's Design section (the decisions made with Noah in chat, 2026-11-09). There is no separate spec.

## Design

Data from the 388 pipeline runs between 2026-10-21 and 2026-11-09 (gh API, mutation jobs only):

- 274 runner-hours. 293 runs took under 5 minutes (16 h total); 29 took 60 minutes or more (153 h, 56%). The
  long runs are mostly `mutation setup changed (...): every enrolled module` (`impacted_sources`, a change to
  `pyproject.toml` or `tests/unit/conftest.py`).
- 25 runs had surviving mutants. About 8 PRs fixed real gaps in their own new code with test-only commits
  (#127, #137, #152, #179, #201, #202, #212, #237). The whole-module runs mostly surfaced the same old
  mutants again (`_read_yaml_with_includes__mutmut_27` five times, `validate_references__mutmut_4/6` five
  times), and three PRs merged while the gate was red.
- `mutation-weekly.yml` has never finished: the 11-02 run's shards ran into their 4-hour limit on macOS; the
  11-09 run went red on two `test_story_publish.py` tests before mutmut started (#269 changed that file).

Decisions:

1. **PR gate:** changed functions only. A setup-file change defers the whole-module run to the nightly sweep
   (`--defer-setup`, in CI only; `task mutate:changed` on a laptop keeps today's behaviour). `mutation-mac`'s
   limit goes back from 180 to 90 minutes.
2. **Nightly sweep:** `--scope enrolled` on `main` every day, on 8 Linux shards plus one macOS job for the
   mac-only sources, merged into one verdict. The whole-project backlog run (`--scope project`) leaves CI
   and runs weekly on Noah's Mac instead (decision 5).
3. **Equivalent mutants:** `[[tool.airlie.mutation.equivalent]]` entries in `pyproject.toml` (`mutant`,
   `line`, `reason`). A listed mutant is skipped only while its diff's `+` line still equals `line`, so a
   renumbered mutant is never skipped by mistake; an entry that no longer matches is reported as stale.
4. **Fixer (Noah's pick):** airlie fix cards. The line's bookkeeping checks at most once an hour for a newer
   completed nightly run on `main`. It downloads the run's `mutation-survivors` artifact and files one card
   per function in `todo`, labelled `fix`, `mutation`, `repo:<home>` and `signature:mutation:<function glob>`.
   A person approves the card, the line builds it into a draft PR, and a person merges. The existing
   `release_after_fix` marks the card done once its PR is merged and deployed.
5. **Whole project, locally (Noah, 2026-11-09, after #272 merged):** a launchd agent runs `--scope project`
   every Sunday at 01:00 in a throwaway worktree of `origin/main`, never the home checkout. On the Mac,
   mlx is installed, so the mac-only modules (those that import `mlx`; `HAS_MLX` and `MAC_IMPORT` after #270,
   which removes vllm-mlx) run with the rest, and there is no 4-hour limit. It waits
   while a lane lock is held, keeps the Mac awake with `caffeinate -i`, and writes the score table to
   `~/.local/state/airlie/mutation/<date>.md`. Its survivors are backlog, not cards: the enrolled modules
   already get cards from the nightly sweep. (Task 6.)

## Global Constraints

- uv only; every action is a Taskfile task; no Docker.
- The line never merges (N4). Mutation cards are created in `todo` and wait for a person to approve them.
- A card's text is data from a CI artifact: stored on the board, never passed to a shell.
- Unit tests stay under 500 ms and use no wall clock: the hourly throttle takes an injected `now`.
- Every change passes the AGENTS.md gate: `uv sync --frozen`, `ruff check`, `ruff format --check`, `mypy`,
  `pytest -q -n auto --maxprocesses 8`, `task web:gate`.
- The weekly local run never writes in the home checkout (`~/git/airlie`): mutmut's `mutants/` tree lives in
  its own worktree, and the line keeps running from the home checkout.
- `--defer-setup` and the nightly sweep must not change what `task mutate:changed` does locally without the flag.
- Build on top of #270 (mlx-vlm becomes the only local engine; vllm-mlx and llama-server leave). It edits
  `.github/workflows/pipeline.yml` (the 180-minute `mutation-mac` limit) and `scripts/mutate_changed.py`
  (`HAS_VLLM` becomes `HAS_MLX`; the mac-only rule is files that import `mlx`). Use its names, and never
  reintroduce vllm-mlx. If #270 has not merged when the build starts, rebase onto it once it does.

## Review Focus

1. **The artifact is missing or expired, or `gh` is not signed in:** the beat says why in one line, keeps
   going, and does not record the run as harvested, so the next hour tries again. (Task 4: test
   `test_a_download_that_fails_is_retried_next_hour`.)
2. **The nightly could not run** (a red unit test, a shard missing): no cards; one line naming the run, said
   once. (Task 4: `test_a_nightly_that_could_not_run_files_nothing_and_says_so_once`.)
3. **The same function survives night after night while its card is open:** no second card; one comment only
   when the set of mutants changes. (Task 4: `test_an_open_card_is_commented_only_when_its_mutants_change`.)
4. **A code edit renumbers the mutants:** an `equivalent` entry whose `line` no longer matches is not skipped
   and shows as stale. (Task 1: `test_an_equivalent_entry_whose_line_changed_is_stale_and_still_gates`.)
5. **A card is done but its mutant survives the next night:** a new card is filed, because `find_open_fix`
   ignores done cards. (Task 4: `test_a_done_card_does_not_stop_a_new_one`.)

---

### Task 1: The equivalent list, and shard records that carry diffs

**Files:**
- Modify: `scripts/mutate_changed.py` (`write_results`, `verdict`, `merge`, `main`; docstring "Enrolment")
- Modify: `pyproject.toml` (`[tool.airlie.mutation]`: an empty `equivalent = []` with a comment)
- Test: `tests/unit/test_mutate_changed.py`

**Interfaces:**
- Produces:
  - `Equivalent = TypedDict("Equivalent", {"mutant": str, "line": str, "reason": str})`
  - `equivalents() -> dict[str, Equivalent]` reads `[tool.airlie.mutation].equivalent`, keyed by mutant.
  - `mutated_line(diff: str) -> str | None` returns the first `+` line of a `mutmut show` diff (not `+++`),
    with the `+` and surrounding whitespace stripped.
  - `write_results(path, status, results, shard, scope, shown: dict[str, str] | None = None)`. The record
    gains `"shown": {mutant: diff}` for every gating uncaught mutant of the shard, with no 20-mutant cap.
  - `verdict(results, enrolled_modules, scope, shown: dict[str, str] | None = None, listed: dict[str, Equivalent] | None = None) -> tuple[list[str], int]`.
    A gating mutant whose `mutated_line(shown[m])` equals `listed[m]["line"]` is reported under
    "equivalent (listed in pyproject.toml):" and does not gate. A listed mutant that is caught, or whose line
    differs, goes under "stale equivalent entries:".

- [ ] **Step 1: Write the failing tests**

```python
def test_mutated_line_is_the_plus_line_of_the_diff() -> None:
    diff = '--- src/a.py\n+++ src/a.py\n@@ -1 +1 @@\n-    if c in "*?[":\n+    if c in "XX*?[XX":\n'
    assert mc.mutated_line(diff) == 'if c in "XX*?[XX":'

def test_a_listed_equivalent_mutant_does_not_gate() -> None:
    m = "airlie.engine.sse.x_parse__mutmut_3"
    shown = {m: "+++ x\n+    return  None\n"}
    listed = {m: {"mutant": m, "line": "return  None", "reason": "same value"}}
    report, code = mc.verdict({m: "survived"}, {"airlie.engine.sse"}, "changed", shown, listed)
    assert code == 0 and any("equivalent (listed" in r for r in report)

def test_an_equivalent_entry_whose_line_changed_is_stale_and_still_gates() -> None:
    m = "airlie.engine.sse.x_parse__mutmut_3"
    listed = {m: {"mutant": m, "line": "return  None", "reason": "same value"}}
    report, code = mc.verdict({m: "survived"}, {"airlie.engine.sse"}, "changed", {m: "+    x = 2\n"}, listed)
    assert code == 1 and any("stale equivalent" in r for r in report)

def test_merge_applies_the_equivalent_list_to_the_shards_diffs(tmp_path: Path, monkeypatch) -> None:
    # shard-1.json: {"status": "ran", "results": {m: "survived"}, "shown": {m: "+    return  None\n"}};
    # monkeypatch mc.equivalents to {m: {"mutant": m, "line": "return  None", "reason": "same value"}}
    # -> exit 0, and the report lists the mutant under "equivalent (listed in pyproject.toml)"

def test_the_committed_equivalent_list_parses() -> None:
    assert all({"mutant", "line", "reason"} <= e.keys() for e in mc.equivalents().values())
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest tests/unit/test_mutate_changed.py -q -k "equivalent or mutated_line"`
Expected: FAIL (`AttributeError: module 'mutate_changed' has no attribute 'mutated_line'`)

- [ ] **Step 3: Implement**

In `main`, collect `shown` with `mutmut_out("show", name)` for every gating mutant before `write_results`,
and print the first `SHOW_LIMIT` as today. `merge` unions every record's `shown` and passes `equivalents()`
to `verdict`.

- [ ] **Step 4: Run the file's tests and the gate's ruff and mypy**

Run: `uv run pytest tests/unit/test_mutate_changed.py -q && uv run ruff check scripts && uv run mypy`
Expected: PASS

- [ ] **Step 5: Commit** — `mutation: an equivalent list in pyproject.toml; shard records carry the survivors' diffs`

### Task 2: `--survivors` on merge

**Files:**
- Modify: `scripts/mutate_changed.py` (`merge`, argparse, module docstring "Sharding")
- Test: `tests/unit/test_mutate_changed.py`

**Interfaces:**
- Consumes: the Task 1 records (`shown`), `verdict`'s split.
- Produces:
  - `function_of(mutant: str) -> str`: `"airlie.a.x_f__mutmut_12"` becomes `"airlie.a.x_f__mutmut_*"`.
  - `merge(where, expect, report, enrolled_modules, survivors: Path | None = None) -> int` writes
    `{"status": "ran" | "could_not_run", "survivors": [{"mutant", "status", "function", "diff"}]}`, sorted by
    mutant, gating and not listed as equivalent. It writes the file on every path, including
    `could_not_run` with an empty list, so the nightly's artifact always exists.

- [ ] **Step 1: Write the failing tests**

```python
def test_function_of_a_mutant_is_its_functions_glob() -> None:
    assert mc.function_of("airlie.a.xǁCǁm__mutmut_4") == "airlie.a.xǁCǁm__mutmut_*"

def test_merge_writes_the_gating_survivors_with_their_diffs(tmp_path: Path) -> None:
    # two shards: one survivor of an enrolled module, one of a module that is not enrolled
    # -> survivors.json lists only the enrolled one, with function and diff; status "ran"

def test_merge_writes_a_survivors_file_when_a_shard_could_not_run(tmp_path: Path) -> None:
    # expect=2 with one shard -> exit 2 and survivors.json == {"status": "could_not_run", "survivors": []}
```

- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/unit/test_mutate_changed.py -q -k "survivors or function_of"`
- [ ] **Step 3: Implement** — `--survivors PATH` (with `--merge` only; an error otherwise).
- [ ] **Step 4: Run the file's tests** — PASS
- [ ] **Step 5: Commit** — `mutation: --survivors writes the gating survivors of a merged run as JSON`

### Task 3: The PR gate defers setup changes; the nightly workflow

**Files:**
- Modify: `scripts/mutate_changed.py` (`main`, argparse, docstring "Scopes")
- Modify: `.github/workflows/pipeline.yml` (`mutation` and `mutation-mac` pass `--defer-setup`;
  `mutation-mac` `timeout-minutes: 90`; header and job comments say "the nightly run")
- Delete: `.github/workflows/mutation-weekly.yml`
- Create: `.github/workflows/mutation-nightly.yml`
- Modify: `Taskfile.yml` (`mutate:project` desc: "the whole project; weekly on the Mac (Task 6); CI runs `--scope enrolled` nightly")
- Test: `tests/unit/test_mutate_changed.py`, `tests/unit/test_pipeline_release.py` (the file that already reads `pipeline.yml`)

**Interfaces:**
- Produces: `--defer-setup` (changed scope). When `changed & SETUP_FILES` is not empty, the run says
  `mutation setup changed (<files>): every enrolled module is left to the nightly run`, writes a `"nothing"`
  record and exits 0. The functions the change touched in enrolled sources are still mutated: the flag
  drops only the whole-module widening, so `whole` stays False.
- Produces: the artifact `mutation-survivors` (holding `survivors.json`) from `mutation-nightly.yml`'s
  `merge` job; Task 4 reads it by that name.

`mutation-nightly.yml`:
- Triggers: `schedule: cron "0 6 * * *"` and `workflow_dispatch`.
- Permissions: `contents: read`.
- Jobs: `mutation` on `ubuntu-latest`, 8 shards (`task mutate -- --max-children 3 --shard I/8 --results mutation/shard-I.json`, after `task test:unit`), `timeout-minutes: 120`.
  `mutation-mac` on `macos-15` (`task mutate -- --max-children 3 --mac-only --results mutation/shard-mac.json`), `timeout-minutes: 120`.
  `merge` (`needs: [mutation, mutation-mac]`, `if: always()`) runs `task mutate:merge -- --merge shards --expect 9 --report mutation-report.md --survivors survivors.json`.
  It uploads `mutation-report` and `mutation-survivors` with `if: always()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_defer_setup_mutates_the_touched_functions_but_not_every_module(monkeypatch, capsys) -> None:
    # changed = {"tests/unit/conftest.py", <one enrolled file>}; --defer-setup --list
    # -> output names "left to the nightly run" and only that file's touched functions

def test_without_defer_setup_a_setup_change_still_mutates_every_enrolled_module(monkeypatch, capsys) -> None:
    # today's behaviour for `task mutate:changed` on a laptop

def test_the_pipelines_mutation_jobs_defer_setup_changes() -> None:
    jobs = yaml.safe_load(PIPELINE.read_text())["jobs"]
    for name in ("mutation", "mutation-mac"):
        assert "--defer-setup" in " ".join(s.get("run", "") for s in jobs[name]["steps"])
    assert jobs["mutation-mac"]["timeout-minutes"] == 90

def test_the_nightly_merges_nine_shards_and_uploads_the_survivors() -> None:
    wf = yaml.safe_load(NIGHTLY.read_text())
    run = " ".join(s.get("run", "") for s in wf["jobs"]["merge"]["steps"])
    assert "--expect 9" in run and "--survivors survivors.json" in run
    assert any(s.get("with", {}).get("name") == "mutation-survivors" for s in wf["jobs"]["merge"]["steps"])
```

- [ ] **Step 2: Run them and see them fail**
- [ ] **Step 3: Implement the flag, the workflow edits and the new workflow**
- [ ] **Step 4: Run the tests and lint the workflows** — `uv run pytest tests/unit/test_mutate_changed.py tests/unit/test_pipeline_release.py -q`; PASS
- [ ] **Step 5: Commit** — `ci: a PR's mutation gate defers setup changes to a nightly sweep of the enrolled modules`

### Task 4: Survivors become fix cards (`airlie.story.mutation_cards`)

**Files:**
- Create: `src/airlie/story/mutation_cards.py`
- Modify: `src/airlie/story/fixes.py` (`bookkeep` calls `harvest` last, inside its own `try`)
- Modify: `src/airlie/config/schema.py` (`LineSpec.mutation_cards: bool = True`)
- Modify: `airlie.yaml` (`line.mutation_cards: true` with a comment)
- Modify: `docs/integrations/story.md` (a "Mutation fix cards" section: what files them, the labels, how to
  approve one, how to stop them with `line.mutation_cards: false`)
- Test: `tests/unit/test_story_mutation_cards.py`

**Interfaces:**
- Consumes: `survivors.json` (Task 2) in the artifact `mutation-survivors` (Task 3); `fixes.find_open_fix`,
  `fixes.signature_label`, `fixes.FIX`, `fixes.FIX_AUTHOR`, `fixes.GhRun`, `fixes.home_repo`, `fixes.line_event`.
- Produces:
  - `NIGHTLY = "mutation-nightly.yml"`, `ARTIFACT = "mutation-survivors"`, `MUTATION = "mutation"` (label),
    `CHECK_EVERY_S = 3600`.
  - `@dataclass(frozen=True) class Survivor: mutant: str; status: str; function: str; diff: str`
  - `read_survivors(path: Path) -> tuple[str, list[Survivor]]`: the file's status and its survivors.
  - `render_mutation_card(function: str, survivors: list[Survivor], run_url: str) -> tuple[str, str]`.
    The title is `Mutation: tests miss {n} mutant(s) of {function without __mutmut_*}`. The body has the
    sections Why, Evidence (each diff in a fenced block), Reproduce
    (`uv run python scripts/mutate_changed.py --scope enrolled --list`, then `uv run mutmut run '<glob>'`),
    Acceptance criteria and Out of scope ("changes to the code under test"). The acceptance criteria say:
    with each mutant under Evidence applied, a unit test fails; tests assert what a caller sees, not the
    mutated literal; a mutant no test can catch gets an `equivalent` entry with its reason instead.
  - `file_mutation_cards(store: BoardStore, survivors: list[Survivor], *, home: str | None, run_url: str, reported: dict[str, list[str]]) -> list[str]`.
    One card per `function`; the signature is `mutation:<function>`. When the card is open, it comments only
    if the sorted mutant names differ from `reported[sig]`. It updates `reported` and returns one log line
    per card created or commented.
  - `async harvest(cfg: AirlieConfig, store: BoardStore, *, gh: GhRun, now: Callable[[], float]) -> list[str]`.
    State lives in `<state_dir>/mutation-harvest.json` as `{"checked_at", "run_id", "reported", "said"}`. It
    returns `[]` without a gh call within `CHECK_EVERY_S` of `checked_at`. Otherwise it runs
    `gh run list --workflow mutation-nightly.yml --branch main --status completed --limit 1 --json databaseId,url,conclusion`.
    It downloads only when the newest `databaseId` is not `run_id`, into a temporary directory, with
    `gh run download <id> --name mutation-survivors --dir <tmp>`. `run_id` is recorded only after the cards
    are filed, or after a `could_not_run` was said once (tracked in `said`).
    It returns `[]` when `cfg.line.mutation_cards` is false or `fixes.home_checkout()` is None.

- [ ] **Step 1: Write the failing tests** (a `BoardStore` on `tmp_path`, a recorded-fake `gh` like `test_story_fixes.py`'s, `now` a counter)

```python
async def test_survivors_of_one_function_file_one_todo_card_with_its_labels(...)
    # labels == ["fix", "repo:weyenk/airlie", "signature:mutation:airlie.a.x_f__mutmut_*", "mutation"]; status "todo"
async def test_an_open_card_is_commented_only_when_its_mutants_change(...)
async def test_a_done_card_does_not_stop_a_new_one(...)
async def test_harvest_asks_github_at_most_once_an_hour(...)          # two calls 59 min apart -> one gh call
async def test_a_run_already_harvested_is_not_downloaded_again(...)
async def test_a_download_that_fails_is_retried_next_hour(...)        # gh raises -> one line, run_id unchanged
async def test_a_nightly_that_could_not_run_files_nothing_and_says_so_once(...)
async def test_mutation_cards_off_asks_nothing(...)                     # line.mutation_cards false -> gh never called
async def test_bookkeep_goes_on_when_the_harvest_fails(...)             # harvest raises -> a line, the other notes kept
def test_the_card_body_carries_each_diff_and_the_equivalent_way_out() -> None
```

- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/unit/test_story_mutation_cards.py -q`
- [ ] **Step 3: Implement the module, the `bookkeep` call and the config field**
- [ ] **Step 4: Run the new tests, `test_story_fixes.py`, `test_story_line.py` and `test_config.py`** — PASS
- [ ] **Step 5: Commit** — `line: the nightly mutation sweep's survivors become fix cards`

### Task 5: Prove it end to end

- [ ] **Step 1:** Run the full AGENTS.md gate; all green.
- [ ] **Step 2:** Push the branch and open a draft PR. Check that its `mutation` job's log shows the
  setup-change line (this branch edits `pyproject.toml`) and finishes in minutes.
- [ ] **Step 3:** After merging (a person merges), run `gh workflow run mutation-nightly.yml --ref main`.
  Wait for every job, then check the `mutation-survivors` artifact with `gh run download`.
- [ ] **Step 4:** On the home checkout, `task line:deploy`, then `task line:tick`. If the nightly had
  survivors, the beat's line names the cards filed; check one in the web board (labels, Evidence diffs).
  Report the card keys to Noah; approving them is his call.
- [ ] **Step 5:** For `_read_yaml_with_includes__mutmut_27` and `validate_references__mutmut_4/6`, the repeat
  offenders: if the sweep still reports them, decide each one (a test, or an `equivalent` entry with a reason)
  and put the decisions on the PR for Noah.

### Task 6: The whole-project run, weekly on the Mac

**Files:**
- Create: `scripts/mutate_local.py`
- Create: `docs/integrations/mutation-weekly.plist` (label `dev.airlie.mutation-weekly`)
- Modify: `Taskfile.yml` (`mutate:local`, `mutate:launchd:install`, `mutate:launchd:uninstall`)
- Modify: `docs/integrations/story.md` (in Task 4's "Mutation fix cards" section: the weekly local run, where
  its report lands, how to install and remove it)
- Test: `tests/unit/test_mutate_local.py`

**Interfaces:**
- Consumes: `airlie.story.state.lock_held(stories_dir, lane)`, `airlie.story.factory.stories_dir(cfg)`,
  `airlie.story.fixes.home_checkout()`, `airlie.story.fixes.line_event`, `airlie.config.load_config`;
  `scripts/mutate_changed.py --scope project --max-children N --report PATH`.
- Produces:
  - `WAIT_UNTIL_H = 6`, `POLL_S = 600`, `KEEP_REPORTS = 12`.
  - `lanes_busy(stories: Path) -> list[str]`: the lanes whose lock another holder has.
  - `wait_for_lanes(stories: Path, *, now: Callable[[], datetime], sleep: Callable[[float], None]) -> bool`.
    True once both lanes are free. It polls every `POLL_S`; False when the local time reaches
    `WAIT_UNTIL_H`:00 first.
  - `run_project(home: Path, out: Path, *, run: Callable[[list[str], Path], int], max_children: int, day: str) -> int`.
    It fetches `origin main` in `home`. It removes `<out>/worktree` if left over from an interrupted run
    (`worktree remove --force`, then `worktree prune`), then adds `<out>/worktree` detached at `origin/main`.
    In that worktree it runs `uv sync --frozen`, then
    `uv run python scripts/mutate_changed.py --scope project --max-children <n> --report <out>/<day>.md`.
    It removes the worktree in a `finally` and returns mutmut's exit code. `run` is the seam tests replace.
  - `prune_reports(out: Path, keep: int = KEEP_REPORTS) -> None`: removes all but the newest `keep` `*.md` files.
  - `main() -> int`, with `out = ~/.local/state/airlie/mutation`. Without a home checkout it says so and
    returns 2. If the lanes stay busy until 06:00, it records the line event `mutation_project_skipped` with
    the busy lanes and returns 0. Otherwise it records `mutation_project` with `code` and `report` and
    returns 0 whatever mutmut found, so launchd never respawns it. `--max-children` defaults to
    `max(1, os.cpu_count() // 2)`.
- The plist uses `StartCalendarInterval` `{Weekday: 0, Hour: 1, Minute: 0}` (launchd runs a missed slot on
  wake). `ProgramArguments`:
  `/bin/zsh -lc 'cd "__AIRLIE_DIR__" && exec caffeinate -i uv run --quiet python scripts/mutate_local.py'`.
  The log is `__HOME__/.local/state/airlie/mutation-weekly.log`. Its comment says why it does not run `task`,
  as `line-tick.plist`'s does.
- The `Taskfile.yml` tasks copy `line:launchd:install`/`uninstall` (sed fill-in, `plutil -lint`, bootout then
  bootstrap). `mutate:local` runs the script now, by hand.

- [ ] **Step 1: Write the failing tests** (a `tmp_path` stories directory; a lane is held by entering
  `lane_lock` in the test; `now` and `sleep` are fakes, with no real sleeping)

```python
def test_a_free_line_runs_at_once(tmp_path: Path) -> None:
    assert ml.wait_for_lanes(tmp_path, now=clock(hour=1), sleep=no_sleep) is True

def test_a_busy_lane_is_waited_for_until_it_frees(tmp_path: Path) -> None:
    # the build lane is held for the first two polls -> True, after two sleeps of POLL_S

def test_a_lane_still_busy_at_six_skips_the_week(tmp_path: Path) -> None:
    # held throughout while the clock passes 06:00 -> False

def test_the_run_uses_a_fresh_worktree_of_origin_main_and_always_removes_it(tmp_path: Path) -> None:
    # the fake run records (args, cwd) and returns 1 for the mutate_changed.py call
    assert ml.run_project(home, out, run=fake, max_children=4, day="2026-11-15") == 1
    # recorded: worktree add --detach <out>/worktree origin/main before the mutmut call;
    # the mutmut call has --scope project --max-children 4 --report <out>/2026-11-15.md, cwd <out>/worktree;
    # the last call is the worktree remove

def test_a_worktree_left_by_an_interrupted_run_is_removed_first(tmp_path: Path) -> None

def test_reports_beyond_the_newest_twelve_are_pruned(tmp_path: Path) -> None

def test_the_plist_runs_weekly_on_sunday_at_one_through_caffeinate() -> None:
    plist = plistlib.loads(PLIST.read_bytes())
    assert plist["Label"] == "dev.airlie.mutation-weekly"
    assert plist["StartCalendarInterval"] == {"Weekday": 0, "Hour": 1, "Minute": 0}
    assert "exec caffeinate -i uv run" in plist["ProgramArguments"][-1]
```

- [ ] **Step 2: Run them and see them fail** — `uv run pytest tests/unit/test_mutate_local.py -q`
- [ ] **Step 3: Implement the script, the plist and the tasks**
- [ ] **Step 4: Run the tests and the AGENTS.md gate** — PASS
- [ ] **Step 5: Try it as the user:** run `task mutate:launchd:install`, then
  `launchctl kickstart gui/$(id -u)/dev.airlie.mutation-weekly`. Watch
  `~/.local/state/airlie/mutation-weekly.log` until mutmut starts, then
  `launchctl kill TERM gui/$(id -u)/dev.airlie.mutation-weekly`. Check that `~/.local/state/airlie/mutation/worktree`
  is gone and that the home checkout's worktree list no longer names it. Leave the agent installed only with
  Noah's go; otherwise run `task mutate:launchd:uninstall`.
- [ ] **Step 6: Commit** — `mutation: the whole-project run moves to a weekly launchd agent on the Mac`
