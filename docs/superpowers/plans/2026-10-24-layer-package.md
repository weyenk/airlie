# The Layer Package and Its Executors (P1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A story layer is built from a *package* the architect writes just before the layer runs: the layer's
tests as code, the files, the interfaces, the steps and what covers which criterion. The pipeline writes the
tests into the worktree, checks them mechanically, locks them, and the coder builds against them. No tester
agent runs.

**Architecture:** A new `airlie.story.package` module holds the package contract and its parser; a new
`airlie.story.package_checks` module places a package into a worktree and runs the mechanical readiness
checks (parse, lint with fixer, covers, imports, red on the base) in a scratch worktree, so a rejected package
costs no branch. `StoryPipeline._build` asks the architect for the package (a read-only studio ask at the
layer's real base, retried with findings), places it through a new `prepare` hook of
`StudioOrchestrator.execute_task`, runs the build with one `build` subtask, and on a red gate re-plans the
layer from the gate's output up to a budget. AIR-22's lesson store is absorbed and records every return and
re-plan. The tester seat as an author, `story.tester_role`, the pre-code tester retry and the tester's mend
are retired.

**Tech Stack:** Python 3.12, uv, pydantic v2, pytest (asyncio auto), git worktrees, the prompt hub
(`src/airlie/prompts/`), vllm-mlx unchanged.

**Spec:** `docs/superpowers/specs/2026-10-24-planning-line-design.md`, sub-plan P1 (decisions D1–D4, D2a, D7,
D8, N2, N5). Read it first.

## Global Constraints

- Every change passes, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- uv only; every action is a Taskfile task; unknown config keys are errors (`Strict` models); a removed key
  gets a `REMOVED` message in `src/airlie/config/load.py`.
- Model-facing prose is a prompt hub file (`src/airlie/prompts/rules/*.md`, `steering/*.md`) with front
  matter (`kind`, `roles`, `fields`, `governs`, `why`); output-format text (the package contract) stays in
  Python beside its parser. After any hub change: `AIRLIE_WRITE_PROMPT_TEXTS=1 uv run pytest
  tests/unit/test_prompt_texts.py`, then rerun without the flag and review the JSON diff.
- Model output and issue text are data: parsed and validated, never executed; a package's paths are checked
  against the worktree before anything is written; the architect's test text reaches the disk only inside
  the worktree.
- Only a human merges; the lock on a layer's tests stays (`StudioOrchestrator.lock`, by definition for
  Python files).
- Test first: every task's test fails before its change. Dense docstrings that cite the incident and date,
  as the repository writes them. Commit after each task.
- Do not restart the engine (`task up/restart`): a story may be running on this Mac. Task 10 needs the
  engine idle; say so to Noah before running it.

## Review Focus

1. **A package that modifies an existing test module and reproduces its old tests verbatim** must lock only
   the definitions that are new or changed, so the coder can still fix a fake in that file (AIR-9 layer 05).
   Pinned in Task 7 (`test_a_package_that_adds_to_an_existing_test_module_locks_only_its_own_definitions`).
2. **A package reply whose prose holds a ```json example before the real one, or whose test file holds a
   fenced block,** must parse the last ```json fence as the package and only `### test file:` headings as test
   files. Pinned in Task 2 (`test_the_package_is_the_last_json_fence_and_test_files_follow_their_headings`).
3. **The lint fixer changes a test file in the scratch worktree:** the package carried into the real worktree
   must hold the fixed text, or the lock commit differs from what was checked. Pinned in Task 3
   (`test_what_ruff_fixes_is_carried_into_the_package`).
4. **A third-party import (`pytest`, `httpx`) in a test** must not be an unresolved import; only modules whose
   root package lives in the worktree (or is created by the package) are resolved. Pinned in Task 3
   (`test_third_party_imports_are_not_checked`).
5. **A re-plan after a red gate reuses the layer's branch name:** the worktree and branch of the failed build
   must be removed before the next `execute_task`, or `git worktree add -b` refuses. Pinned in Task 7
   (`test_a_gate_still_red_after_the_coders_follow_up_replans_the_layer_once_on_the_same_branch`).

---

### Task 1: Absorb AIR-22's lesson store and sizing rule

AIR-22 was killed on 2026-10-24 (spec N5). Its two built layers are single commits on local branches.

**Files:**
- Create (by cherry-pick): `src/airlie/story/lessons.py`, `src/airlie/prompts/rules/brief-layers-sizing.md`,
  `src/airlie/prompts/rules/brief-layers-lessons.md`, `tests/unit/test_story_lessons.py`
- Modify (by cherry-pick): `src/airlie/story/briefs.py` (`layers_brief` gains `lessons`, `max_steps`,
  `context_budget`), `src/airlie/story/pipeline.py` (`self.lessons = LessonsStore(...)` in `__init__`),
  `tests/unit/test_story_briefs.py`, `tests/unit/test_prompt_texts.py`, `tests/unit/data/prompt_texts.json`
- Modify after: `src/airlie/story/lessons.py`, `tests/unit/test_story_lessons.py`

**Interfaces:**
- Produces: `Lesson(title, acceptance, source_files, test_files, steps, context_tokens, ending, timestamp,
  detail="")`; `ENDINGS = ("out_of_steps", "out_of_context", "gate_red", "package_rejected")`;
  `LessonsStore(state_dir: Path, repo: Path)` with `record(lesson) -> None` and `recent(n=5) -> list[Lesson]`;
  `layers_brief(..., lessons: Sequence[Lesson] = (), max_steps: int = 40, context_budget: int = 65_536)`.

- [ ] **Step 1: Cherry-pick the two layers**

```bash
git cherry-pick AIR-22-record-size-blocked-layer-less-01-lessons-store AIR-22-record-size-blocked-layer-less-02-brief-sizing-lessons
```

Expected: two commits apply cleanly (they touch `briefs.py` and `pipeline.py` only at the import and
`__init__`). Run the gate; it is green (the branch regenerated the golden snapshot).

- [ ] **Step 2: Write the failing test for the new endings**

In `tests/unit/test_story_lessons.py`:

```python
def test_a_lesson_records_why_it_came_back_and_the_endings_name_the_package_path() -> None:
    assert ENDINGS == ("out_of_steps", "out_of_context", "gate_red", "package_rejected")
    lesson = make_lesson("Store", ending="package_rejected", detail="passes_before_code, lint_fails")
    store = LessonsStore(tmp, repo); store.record(lesson)
    assert store.recent()[0].detail == "passes_before_code, lint_fails"
```

Run: `uv run pytest tests/unit/test_story_lessons.py -q`. Expected: FAIL (`ENDINGS` differs; `detail` unknown).

- [ ] **Step 3: Extend `Lesson` and `ENDINGS` in `src/airlie/story/lessons.py`**

`detail: str = ""` (the finding kinds, or the gate's first failing step), `ENDINGS` as above
(`gate_red_at_step_end` becomes `gate_red`: the gate is red after the build, step end or not). Update the
module docstring: a lesson is recorded when a package is rejected, when a layer is re-planned after a red
gate, and when the coder ran out of steps or context.

- [ ] **Step 4: Run the lessons tests; run the gate; commit**

```bash
git commit -am "story: lessons record why a layer came back (absorbed from AIR-22)"
```

---

### Task 2: The package contract and its parser

**Files:**
- Create: `src/airlie/story/package.py`
- Test: `tests/unit/test_story_package.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class PackageFile: path: str; action: Literal["create", "modify"]; why: str = ""
  @dataclass(frozen=True)
  class Interface: file: str; signature: str; behaviour: str = ""
  @dataclass(frozen=True)
  class Package:
      layer: str                                   # the layer's slug
      tests: Mapping[str, str]                     # test path -> the file's whole text
      files: tuple[PackageFile, ...]
      interfaces: tuple[Interface, ...]
      steps: tuple[str, ...]
      covers: Mapping[str, tuple[str, ...]]        # criterion id -> test ids "path::name"
      done_when: str
      def to_dict(self) -> dict[str, Any]; @classmethod def from_dict(cls, d) -> Package
      def with_tests(self, tests: Mapping[str, str]) -> Package   # the same package, other test text
      @property def test_ids(self) -> tuple[str, ...]            # every "path::name" in covers, sorted
      @property def source_files(self) -> tuple[str, ...]        # files' paths, tests excluded
  PACKAGE_CONTRACT: str      # the output format, in Python beside the parser (spec: output formats stay inline)
  def test_files_in(text: str) -> dict[str, str]
  def parse_package(text: str, layer: Layer, story: Story) -> Package   # StoryError with a reason for the retry
  ```
- `PACKAGE_CONTRACT` text (exact):
  ```
  Reply with, for each test file, a line `### test file: <path from the repository root>` followed by one fenced ```python block holding that file's whole text (the file as it must be after this layer: new, or the existing file with your tests added). Then end your reply with one ```json block holding exactly one object:
  {"type": "package",
   "files": [{"path": "src/pkg/mod.py", "action": "create" | "modify", "why": "one line"}],
   "interfaces": [{"file": "src/pkg/mod.py", "signature": "def name(arg: T) -> R", "behaviour": "one line: what it does, the exact message or value the tests assert"}],
   "steps": ["ordered, one change each, naming the file and the function"],
   "covers": {"AC1": ["tests/unit/test_x.py::test_name"]},
   "done_when": "one sentence"}
  Every criterion this layer owns appears in covers with at least one test; every test id names a test in one of your test files.
  ```

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_story_package.py`, with a `STORY` of AC1/AC2 and a `LAYER` owning `("AC1",)`:

```python
def test_a_package_reply_parses_into_tests_files_interfaces_steps_and_covers() -> None:
    text = reply(tests={"tests/unit/test_store.py": "def test_a():\n    assert store() == 1\n"},
                 files=[{"path": "src/store.py", "action": "create"}],
                 interfaces=[{"file": "src/store.py", "signature": "def store() -> int"}],
                 steps=["create src/store.py with store() returning 1"], covers={"AC1": ["tests/unit/test_store.py::test_a"]})
    p = parse_package(text, LAYER, STORY)
    assert p.tests == {...} and p.files[0].action == "create" and p.test_ids == ("tests/unit/test_store.py::test_a",)
    assert p.source_files == ("src/store.py",) and Package.from_dict(p.to_dict()) == p

def test_the_package_is_the_last_json_fence_and_test_files_follow_their_headings() -> None:
    # a ```json example in the prose and a fenced block inside the test text (a docstring with ```) are not the package

@pytest.mark.parametrize("flaw, says", [
    ("no_json", "no JSON object"), ("wrong_type", '"type": "package"'), ("no_test_file", "no test file"),
    ("not_a_test_module", "not a pytest test module"), ("path_escapes", "inside the repository"),
    ("empty_steps", "steps"), ("covers_unknown_criterion", "AC9"), ("criterion_uncovered", "AC1"),
    ("test_id_not_in_a_test_file", "names a test in no test file"), ("bad_action", "create or modify"),
])
def test_a_flawed_package_is_refused_with_a_reason_the_architect_can_act_on(flaw, says) -> None:
    with pytest.raises(StoryError, match=says): parse_package(flawed(flaw), LAYER, STORY)

def test_a_layer_with_no_criteria_needs_one_test_and_no_covers() -> None: ...
def test_a_test_path_is_normalised_without_a_leading_dot_slash() -> None: ...
```

Run: `uv run pytest tests/unit/test_story_package.py -q`. Expected: FAIL, `airlie.story.package` missing.

- [ ] **Step 2: Implement `src/airlie/story/package.py`**

`test_files_in`: a heading line matching `^### test file: (?P<path>\S+)\s*$`, then the first fence line
(`^(?P<fence>`{3,})(python|py)?\s*$`) after it, then every line up to the next line equal to that fence;
the content is the lines joined with `\n` plus one trailing newline. Headings with no block are a
`StoryError`. The JSON is `extract_json_block(text)` from `airlie.topology.studio_plan` (the last ```json
fence). Validation uses `model._text`, `_strs`, `_encodable` and `tools.scope.is_test_module`; a path is
normalised with `removeprefix("./")` and refused when it is absolute or has a `..` part. `layer.acceptance`
empty: `covers` may be empty but `tests` may not.

- [ ] **Step 3: Run the tests; they pass. Run the gate. Commit**

```bash
git add src/airlie/story/package.py tests/unit/test_story_package.py
git commit -m "story: the layer package contract and its parser"
```

---

### Task 3: Placing a package and the mechanical readiness checks

**Files:**
- Create: `src/airlie/story/package_checks.py`
- Test: `tests/unit/test_story_package_checks.py`
- Reuses: `layer_checks.PytestChecks`, `layer_checks.changed_tests`, `layer_checks.unparseable_tests`,
  `layer_checks.Finding`, `layer_checks.feedback_said`'s `_Said` pattern, `tester_lint.lint_for`,
  `teams.worktree.add_worktree/commit_wip/remove_worktree`.

**Interfaces:**
- Produces:
  ```python
  def place_package(worktree: Path, package: Package) -> list[str]
      # writes every test file (parents made), returns their paths, sorted; writes nothing else
  def imports_unresolved(worktree: Path, package: Package) -> list[Finding]
      # Finding("import_unresolved", "<test path>: <module>.<name>", why, "")
  class PackageChecker:
      def __init__(self, commands: Sequence[str], *, timeout_s: float, confinement: dict[str, Any] | None = None) -> None
      async def check(self, package: Package, repo: Path, base: str, scratch: Path) -> tuple[Package, list[Finding]]
          # a detached worktree at `base` under `scratch` (removed after), the package placed and committed;
          # returns the package with ruff's fixes applied to its test text, and the findings
  def package_feedback_said(findings: Sequence[Finding]) -> tuple[str, tuple[str, ...]]
      # the architect's retry text and the hub ids in it: `checks-found`, a line per finding (with
      # `passes-before-code-choice` and `loose-match` as feedback_said adds them), then `package-fix`
  ```
- New finding kinds added to `layer_checks.Kind`: `"file_exists"`, `"file_missing"`, `"covers_missing"`,
  `"import_unresolved"`.
- New steering text `src/airlie/prompts/steering/package-fix.md` (roles `[architect]`, `when`: a layer's
  package failed the readiness check, `governs: package.fix: whole-reply`): *"Reply with the whole package
  again: every test file in full and the JSON object, with these findings fixed. A test that passes before
  the code does not test this layer: make it fail for the right reason or drop it. Do not write the
  feature's code."*

**Check order** (each step's findings are collected; a later step runs unless an earlier one makes it
meaningless, as noted):

1. Paths: a `files` entry with `action: create` whose path exists → `file_exists`; `modify` whose path does
   not → `file_missing`. Test paths: none of these checks (the test file may be new or existing).
2. `place_package`, then `commit_wip(scratch, "package check")`.
3. Lint, when the gate has ruff steps: `lint.fix(scratch, base)`; when it changed files, re-read those test
   files from the scratch tree into the returned package (`with_tests`); then `lint.check` → `lint_fails`.
4. `unparseable_tests` → `unparseable_test`; stop here when any (nothing below is reliable).
5. `changed_tests` → `assertion_free_test` for a test without a check; `covers_missing` for a covered test
   id whose name is not a new or changed test of that file.
6. `imports_unresolved` (algorithm below).
7. Red, when the gate runs pytest (`checks_for(commands)` is not None): `PytestChecks.run(scratch, base)` →
   `passes_before_code`, `existing_test_changed`, `existing_test_fails`, `no_new_test`, `suite_did_not_run`.
   When the gate runs no pytest, no red check, and the caller logs `test_checks_skipped` as today.

**Import resolution** (the one algorithm the signature does not determine): for each test file, parse it;
for each `import a.b` take module `a.b`; for each `from a.b import c, d` take module `a.b` and names
`c, d`. Roots are the worktree and `worktree/src`. A module `a.b` is *in scope* when `a/` or `a.py` exists
under a root, or when a package file with `action: create` has path `<root>/a/b.py` or `<root>/a/b/__init__.py`;
a module not in scope (pytest, httpx) is skipped. In scope, the module *resolves* when `a/b.py` or
`a/b/__init__.py` exists under a root or is a created package file; else `import_unresolved` with why
"the module is not in the worktree and the package creates no such file". A name resolves when the module's
file exists and its AST has a top-level `def`, `async def`, `class`, or assignment target of that name, or when
an `interfaces` entry for that file has a `signature` that starts with `def <name>(`, `async def <name>(`,
`class <name>`, or `<name> =` / `<name>:`; else `import_unresolved` with why "the name is defined neither in
the file nor in the package's interfaces for it". `from a.b import *` resolves the module only.

- [ ] **Step 1: Write the failing tests**

Use `tests/unit/test_story_layer_checks.py`'s `repo(tmp_path, files)` and `commit` helpers (import them)
and the real `PYTEST` gate of `tests/unit/test_story_layer_checks_flow.py` (`sys.executable -m pytest -q`):

```python
async def test_a_red_package_against_a_pytest_gate_has_no_findings_and_leaves_no_worktree(tmp_path) -> None
async def test_a_test_that_passes_before_the_code_is_a_finding(tmp_path) -> None
async def test_a_covered_test_id_that_is_not_in_the_file_is_a_finding(tmp_path) -> None
async def test_an_import_of_a_name_the_package_neither_defines_nor_declares_is_a_finding(tmp_path) -> None
async def test_an_import_of_a_name_declared_in_the_packages_interfaces_for_a_created_file_resolves(tmp_path) -> None
async def test_third_party_imports_are_not_checked(tmp_path) -> None
async def test_what_ruff_fixes_is_carried_into_the_package(tmp_path) -> None   # unsorted imports in the test; the returned package's text is sorted
async def test_a_created_file_that_exists_and_a_modified_file_that_does_not_are_findings(tmp_path) -> None
async def test_a_gate_without_pytest_skips_the_red_check_and_still_parses_and_lints(tmp_path) -> None
def test_place_package_writes_only_the_test_files_and_returns_their_paths(tmp_path) -> None
def test_package_feedback_names_each_finding_and_ends_with_package_fix() -> None
```

Run: `uv run pytest tests/unit/test_story_package_checks.py -q`. Expected: FAIL, module missing.

- [ ] **Step 2: Add the four kinds to `layer_checks.Kind` and the `package-fix` steering file; implement
  `src/airlie/story/package_checks.py`**

Regenerate the golden snapshot (Global Constraints) with a `"package-fix"` case added to
`tests/unit/test_prompt_texts.py` `CASES`.

- [ ] **Step 3: Run the tests; they pass. Run the gate. Commit**

```bash
git commit -m "story: a package is placed in a scratch worktree and checked before anyone codes against it"
```

---

### Task 4: The architect's package brief and the coder's build brief

**Files:**
- Modify: `src/airlie/story/briefs.py` (`layer_plan` signature, new `package_brief`, new `build_brief`)
- Create: `src/airlie/prompts/rules/brief-package-task.md`, `src/airlie/prompts/rules/package-tests-rules.md`,
  `src/airlie/prompts/rules/package-contract.md`
- Test: `tests/unit/test_story_briefs.py`, `tests/unit/test_prompt_texts.py` (+ golden)

**Interfaces:**
- Produces:
  ```python
  def package_brief(story: Story, layers: tuple[Layer, ...], i: int, *, guide: str = "", built: str = "",
                    lessons: Sequence[Lesson] = (), max_steps: int, context_budget: int,
                    feedback: str | None = None) -> str
  def build_brief(story: Story, layers: tuple[Layer, ...], i: int, package: Package, *, guide: str = "",
                  built: str = "") -> str
  def layer_plan(story: Story, layers: tuple[Layer, ...], i: int, package: Package, *, build_role: str,
                 guide: str = "", built: str = "") -> Plan
      # one Subtask("build", build_brief(...), build_role); tester_role is gone
  ```
- `package_brief` order: `brief-package-task`; the layer context (`story_text`, "This is layer i of n:
  title", scope, the criteria it owns as `Criterion.line()` lines or `brief-layer-plan-no-criteria`, the
  later layers); the layer's own brief under `## What this layer builds`; `built`; `## {GUIDE_HEADING}` +
  guide when given; `brief-layers-lessons` + `_lesson_line`s when any; `package-tests-rules`; `test-rules`;
  `brief-layers-sizing` (with `max_steps`, `context_budget`); `PACKAGE_CONTRACT`; `_again(feedback)`.
- `build_brief` order: the layer context as `layer_plan` builds it today (story, layer, scope, later layers,
  `built`, guide); `## The tests you make pass` (each test id under its criterion, from `covers`; the files are
  in the worktree, locked); `## Files` (`path (action): why`); `## Interfaces` (`file: signature` and
  behaviour); `## Steps` (numbered); `## Done when`; then `package-contract`, `brief-layer-plan-agents-md`,
  `brief-layer-plan-edit-file`, `brief-layer-plan-build-stop`.
- Hub texts (exact bodies):
  - `brief-package-task` (roles `[architect]`, governs `package.form: tests-and-steps`): *"Write this layer's
    package: its tests as code, and the steps a coder follows to make them pass without further design.
    Read the test module nearest the feature first; its fixtures and fakes are yours to use, and a new test
    file is named for the feature. Each test calls the code the way its users will once the layer is built,
    with the exact names, messages and values the coder must produce, and fails now because the feature is
    missing. airlie runs the whole suite on your tests before the coder starts; a test that passes there, an
    import it cannot resolve, or a criterion with no test comes back to you. Do not write the feature's code."*
  - `package-tests-rules` (roles `[architect]`, governs `tests.criteria: each`, `tests.fail-first:
    required`, `tests.negative: inside-trigger`): *"One or more tests per criterion this layer owns (for a
    layer with none, per behaviour its brief adds). A check that something does NOT happen lives inside a
    test that also makes it happen, before the trigger. A test file you add to must keep every test that
    was there; a reformat is not a change. Every name a test imports from this repository exists already or
    is one your interfaces declare."*
  - `package-contract` (roles `[coder]`, governs `gate-failure.fix: code`, `tests.acceptance: unchanged`):
    *"The tests for this layer were written before you by the architect and are in the worktree; airlie has
    checked that each fails without the code. They are the contract, and you cannot change them: follow the
    steps and make them pass. What else is in their files was there before (fakes, helpers, imports): change
    that where your change needs it. If a test is wrong about a name or a signature the steps fix, say which
    and why when you stop. Add tests in a new file for anything else you change. Your run_tests runs this
    layer's tests; once they pass, you are done: airlie runs the whole suite after review."*

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_story_briefs.py`, replacing
  `test_with_a_tester_role_a_layer_with_criteria_is_the_tests_then_the_build_against_them`,
  `test_with_a_tester_role_a_foundation_layer_gets_its_tests_from_its_brief_first_too`,
  `test_the_coder_after_a_tester_is_not_told_to_put_tests_in_the_testers_files`,
  `test_a_layer_with_criteria_is_one_coder_subtask_that_writes_the_tests_first`,
  `test_an_enabling_layer_is_one_build_subtask`:

```python
def test_the_package_brief_asks_for_tests_as_code_with_the_contract_and_the_coders_budget() -> None
def test_the_package_brief_carries_the_guide_the_built_digest_and_the_lessons_only_when_given() -> None
def test_the_package_brief_carries_the_rejection_feedback_when_asked_again() -> None
def test_the_build_brief_lists_the_tests_per_criterion_the_files_the_interfaces_and_the_steps() -> None
def test_a_layer_plan_is_one_build_subtask_from_its_package() -> None
```

Run: `uv run pytest tests/unit/test_story_briefs.py -q`. Expected: FAIL (names missing; the old tester tests
fail on the removed parameter).

- [ ] **Step 2: Add the three hub files; implement `package_brief`, `build_brief`; change `layer_plan`**

Add CASES for the three ids and `"fn:briefs.package_brief"`, `"fn:briefs.build_brief"` (with a fixed sample
package) to `tests/unit/test_prompt_texts.py`; regenerate the golden snapshot; review the diff: only the new
keys and the removed tester keys change.

- [ ] **Step 3: Run the briefs and prompt tests; they pass. Run the gate (the pipeline tests fail on the
  `layer_plan` signature until Task 7: note them, do not fix them here). Commit**

```bash
git commit -m "story: the architect's package brief and the coder's build brief"
```

---

### Task 5: The studio's `prepare` hook

**Files:**
- Modify: `src/airlie/topology/studio.py` (`execute_task`)
- Test: `tests/unit/test_studio_orchestrator.py`

**Interfaces:**
- Changes: `execute_task(..., prepare: Callable[[Path], Awaitable[None]] | None = None)`; the `amend_tests`
  parameter is removed (Task 7 removes its caller; the gate's `amend_tests` branch in `_gate` goes with it).
- `prepare(worktree)` runs after `self.base_sha = commit_wip(...)` and before `self._prepare(...)` (the repo
  map), so the map shows what it placed and `base_sha` is the commit before it. What it writes is committed
  by the caller inside the hook (the pipeline commits and locks there).

- [ ] **Step 1: Write the failing test**

```python
async def test_prepare_runs_on_the_worktree_after_the_base_commit_and_before_the_plan(tmp_path) -> None:
    # prepare writes tests/test_given.py and commits it; the coder's first ctx.already_written holds it,
    # base_sha is the commit before it, and the repo map event lists the file
```

Run: `uv run pytest tests/unit/test_studio_orchestrator.py -k prepare -q`. Expected: FAIL (unexpected
keyword).

- [ ] **Step 2: Implement; remove `amend_tests` from `execute_task`, `__init__` and `_gate`**

- [ ] **Step 3: Run the studio tests; run the gate (pipeline tests still red until Task 7). Commit**

```bash
git commit -m "studio: a prepare hook places files in the worktree before the plan runs"
```

---

### Task 6: Configuration

**Files:**
- Modify: `src/airlie/config/schema.py` (`StorySpec`), `src/airlie/config/load.py` (`REMOVED`),
  `airlie.yaml` (`story:`), `docs/integrations/story.md` (the config table rows)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `StorySpec.package_attempts: int = Field(default=2, ge=1)` ("tries the architect gets at one
  layer's package before the story stops"); `StorySpec.layer_attempts: int = Field(default=2, ge=1)`
  ("builds of one layer, each re-planned from the last gate's output, before the story stops");
  `StorySpec.package_steps: int = Field(default=12, ge=1)` ("steps the architect gets to read before it
  writes a layer's package"). `tester_role` is removed; `REMOVED["story.tester_role"]` says: *"removed on
  2026-10-24: the architect writes every layer's tests as code in the layer's package
  (docs/superpowers/specs/2026-10-24-planning-line-design.md); delete story.tester_role from your config"*.
- `airlie.yaml`: delete `tester_role: tester` and its comment from `story:`; `roles.tester` stays (the
  critic seat of P2).

- [ ] **Step 1: Write the failing tests**

```python
def test_story_tester_role_is_a_removed_key_with_the_reason() -> None   # load_config refuses it, naming the design
def test_the_package_budgets_have_defaults_and_floors() -> None          # 2, 2, 12; ge=1 refused with the path
```

- [ ] **Step 2: Implement; update `airlie.yaml`; update the two table rows in `docs/integrations/story.md`**
  (`story.tester_role` row replaced by `story.package_attempts`, `story.layer_attempts`, `story.package_steps`).
  The `pipeline()` helper in `tests/unit/test_story_pipeline.py` and `TESTER_ROLES` there set
  `tester_role`; drop the key from the helper (Task 7 rewrites the tests that used `TESTER_ROLES`).

- [ ] **Step 3: Tests pass; commit**

```bash
git commit -m "config: story.package_attempts, layer_attempts, package_steps; story.tester_role removed"
```

---

### Task 7: The pipeline builds a layer from its package

**Files:**
- Modify: `src/airlie/story/pipeline.py` (`_build`; remove `_check_tests`, `_amend_tests`, `_check_static`,
  `_settle_amend`, `CHECK_ATTEMPTS`; add `_package`, `_place`, `_lesson`), `src/airlie/supervisor/ledger.py`
  (`Trigger` gains `"package_retry"`, `"replan"`; the old names stay for old rows)
- Test: `tests/unit/test_story_pipeline.py`, `tests/unit/test_story_layer_checks_flow.py`

**Interfaces:**
- Produces (methods of `StoryPipeline`):
  ```python
  async def _package(self, st, story, layers, i, base_sha: str, built: str, guide: str,
                     feedback: str | None) -> Package
      # one architect ask (`_ask(st, "package", architect_role, package_brief(...), at=base_sha,
      # steps=self.s.package_steps)`), parsed; a StoryError is the feedback of the next try. The brief gets
      # lessons=self.lessons.recent(), max_steps=cfg.roles[build_role].max_steps or cfg.teams.max_steps,
      # context_budget=cfg.roles[build_role].max_context; `_layers` passes the same three to layers_brief
      # (AIR-22's unbuilt wiring)
  async def _ready_package(self, st, story, layers, i, base_sha, built, guide, *, feedback=None) -> Package
      # up to story.package_attempts: _package, then PackageChecker.check in st.path / f"package-{nn}";
      # findings → package_feedback_said → feedback (steer events per id, trigger "package_retry");
      # events: package_start(layer, attempt, studio_run), package_returned(layer, attempt, kinds),
      # package_ready(layer, attempt, tests, files); the findings as events via _emit_findings;
      # exhausted → _lesson("package_rejected", kinds) and StoryBlocked("layer NN (title): its package
      # still fails the readiness check after N tries (<kinds>)")
  def _place(self, orch, package, nn) -> Callable[[Path], Awaitable[None]]
      # the prepare hook: place_package, commit_wip("studio: layer NN (package)"), orch.lock(paths,
      # self.s.architect_role), event package_placed(layer, studio_run, paths)
  def _lesson(self, layer, package, result, ending, detail) -> None
      # LessonsStore.record(Lesson(title=layer.title, acceptance=list(layer.acceptance),
      # source_files=len(package.source_files), test_files=len(package.tests), steps=result.steps,
      # context_tokens=result.prompt_tokens, ending=ending, detail=detail)); event lesson_recorded
  ```
- `_build` flow:
  1. as today up to `plan =`: author, `_clear_attempt`, base, branch, guide, built.
  2. `package = await self._ready_package(...)`.
  3. for attempt in `range(story.layer_attempts)`: a fresh `orch` (`st.runs.append`, `st.attempt`,
     `layer_start` with `attempt`); `plan = layer_plan(story, layers, i, package, build_role=..., guide=guide,
     built=built)`; `report = await orch.execute_task(layer_goal(...), repo, test_cmd, test_steps, plan,
     base=base_sha, branch=branch, synthesise=False, scope_tests=True, gate_follow_up="build",
     map_focus=named_paths(layer.scope) + package.source_files, prepare=self._place(orch, package, nn))`;
     `commit_wip(wt, final)`. Then:
     - changed nothing → `_lesson("gate_red", "changed nothing")`; re-plan or block as below.
     - `report.tests.get("studio") is not True` → the coder's result `r = report.teams[0]`; ending
       `out_of_steps` when `r.stop_reason == "max_steps"`, `out_of_context` when it is `cut_off` or
       `context_full`, else `gate_red`; `_lesson(...)`; if another attempt remains: event
       `layer_replanned(layer, attempt, why)`, `remove_worktree(repo, wt)`, `delete_branch(repo, branch)`,
       `package = await self._ready_package(..., feedback=<the coder's final_text and the gate's tail>)`,
       continue; else `StoryBlocked` with today's wording plus "after N builds".
     - green → `squash_since`, `remove_worktree`, `st.built[nn]` (add `"package": package.to_dict()` and
       `"tests": list(package.test_ids)`), note, as today.
  4. The supervisor stays on the build (`gate_retry`, `repeat_guard`, `block`); `precode_retry` and `amend`
     are no longer emitted.
- The stop reasons keep `_cut_off` and `_kept`.

- [ ] **Step 1: Write the failing pipeline tests**

In `tests/unit/test_story_pipeline.py`: a helper `package_reply(tests: dict[str, str], covers: dict,
files=..., steps=...) -> FakeReply` that renders the headings, fences and JSON; `happy_engine` gives the
architect `[LAYERS, PKG1, PKG2]`. Replace `test_with_a_tester_each_layer_gets_its_tests_written_before_the_coder_builds`
with:

```python
async def test_each_layer_is_built_from_the_architects_package_and_the_coder_sees_its_tests(tmp_path) -> None:
    # st.built["01"]["subtasks"] ids == ["build"]; the build goal holds "## The tests you make pass" and the test id;
    # the test file is in the layer's commit; st.built["01"]["tests"] names it; events package_start/package_ready/package_placed per layer
async def test_a_package_the_parser_refuses_is_asked_again_with_the_reason_then_stops_the_story(tmp_path) -> None
async def test_a_rejected_package_records_a_lesson_and_the_next_layers_brief_carries_it(tmp_path) -> None
```

In `tests/unit/test_story_layer_checks_flow.py` (its `calc_repo`, `PYTEST`, `RUFF_GATE`, `write` helpers
stay; the tester scripts become package replies):

```python
async def test_a_red_package_is_placed_locked_and_the_coder_builds_against_it(tmp_path) -> None
async def test_a_test_that_passes_before_the_code_sends_the_package_back_to_the_architect_once(tmp_path) -> None
async def test_the_architects_retry_is_a_steer_event_per_hub_text_it_holds(tmp_path) -> None   # checks-found, passes-before-code-choice, package-fix
async def test_a_package_still_green_before_the_code_after_the_retry_stops_the_layer_before_the_coder(tmp_path) -> None
async def test_the_coder_cannot_change_the_packages_tests_with_the_file_tools(tmp_path) -> None
async def test_a_package_that_adds_to_an_existing_test_module_locks_only_its_own_definitions(tmp_path) -> None
async def test_what_ruff_can_fix_in_the_package_is_fixed_before_it_is_placed(tmp_path) -> None
async def test_what_ruff_cannot_fix_goes_back_to_the_architect_with_the_rule(tmp_path) -> None
async def test_a_failing_final_gate_sends_the_coder_its_output_once(tmp_path) -> None   # stays
async def test_a_gate_still_red_after_the_coders_follow_up_replans_the_layer_once_on_the_same_branch(tmp_path) -> None
    # the architect's second package is asked with the coder's account and the gate tail; the layer builds on the second try;
    # one lesson "gate_red"; one layer_replanned event; the branch name is the same
async def test_a_layer_red_after_every_build_stops_the_story_naming_the_builds(tmp_path) -> None
async def test_a_gate_without_pytest_skips_the_red_check_and_still_locks_the_packages_files(tmp_path) -> None
```

Delete the tester-only scenarios (`test_the_testers_red_tests_are_checked_locked_and_the_coder_builds_against_them`,
the `DownForTester` test, the `*_mend*` tests, `test_without_a_tester_*`, `test_an_old_test_failing_in_a_locked_file_goes_to_the_coder_not_the_tester`).
Run: `uv run pytest tests/unit/test_story_pipeline.py tests/unit/test_story_layer_checks_flow.py -q`.
Expected: FAIL.

- [ ] **Step 2: Implement `_build` and its helpers; remove the retired methods; extend `Trigger`**

- [ ] **Step 3: Run both files; they pass. Run the whole gate; it is green (the other suites were already
  adjusted in Tasks 4–6). Commit**

```bash
git commit -m "story: a layer is built from the architect's package; a red gate re-plans it"
```

---

### Task 8: Retire the tester's texts and code paths

**Files:**
- Delete: `src/airlie/prompts/rules/tests-only.md`, `pre-code-check.md`, `tests-contract.md`,
  `static-check.md`, `brief-layer-plan-tests-first.md`, `brief-layer-plan-add-tests.md`,
  `brief-layer-plan-tests-where.md`, `brief-layer-plan-tests-new-file.md`;
  `src/airlie/prompts/steering/checks-fix-before-code.md`, `checks-fix-with-code.md`, `amend.md`
- Modify: `src/airlie/story/layer_checks.py` (remove `is_testers_test`, `amend_feedback_said`,
  `amend_feedback`, `static_findings`, `feedback_said`'s `before_code` parameter and the rule it appended;
  the module docstring), `src/airlie/story/briefs.py` (remove `_tests_where`, `named_test_files` if unused),
  `tests/unit/test_prompt_texts.py` (CASES), `tests/unit/test_prompt_contradictions.py` (`ACCEPTED` entries
  naming the deleted ids), `tests/unit/test_story_layer_checks.py` (`test_is_testers_test`,
  `test_amend_feedback_said_is_the_text_and_the_amend_id`, `test_static_findings_read_the_tests_and_run_none`,
  the `before_code=False` cases of `test_feedback_said_is_the_text_and_the_hub_ids_in_it_in_order`)

- [ ] **Step 1: Grep first.** `grep -rn "<id>" src tests` for each id and name above; a hit outside this list
  stays and its text is kept. Keep `checks-found`, `passes-before-code-choice`, `loose-match`, `test-rules`,
  `brief-layer-plan-edit-file`, `brief-layer-plan-agents-md`, `brief-layer-plan-build-stop`,
  `brief-layer-plan-no-criteria`.

- [ ] **Step 2: Write the failing test**: `test_the_hub_has_no_tester_authoring_texts` in
  `tests/unit/test_prompt_hub.py` asserting none of the deleted ids loads (`entry(id)` raises).

- [ ] **Step 3: Delete and modify; regenerate the golden snapshot; run the contradiction check**

- [ ] **Step 4: Gate green; commit**

```bash
git commit -m "prompts: the tester's authoring texts and the pre-code retry are retired"
```

---

### Task 9: The web app's layer lane shows the package

**Files:**
- Modify: `src/airlie/serve/flow.py` (`STAGE_ROLES["package"] = "architect"`; the lane's first job becomes
  `package` from the story's `package_start`/`package_returned`/`package_ready` events, role `architect`,
  status `passed` on `package_ready`, `failed` on a block after `package_returned`, `running` between;
  `CHECK_KINDS` gains `file_exists`, `file_missing`, `covers_missing`, `import_unresolved`,
  `package_returned`, `package_ready`, `package_placed`)
- Test: `tests/unit/test_story_flow.py` (`test_a_layer_is_a_lane_of_jobs_with_tester_and_coder_stats` →
  `..._with_package_and_coder_stats`: ids `["package", "checks", "build"]`; a story recorded before the
  package (a `tests` subtask in its run events) still renders, as `tests`)

- [ ] **Step 1: Write the failing tests. Step 2: Implement. Step 3: Gate green; commit**

```bash
git commit -m "serve: a layer's lane starts with the architect's package"
```

---

### Task 10: Documentation, the design's status, and one real story

**Files:**
- Modify: `README.md` (the `task story` row: "each layer built from the architect's package: its tests as
  code, placed and checked before the coder builds"), `docs/integrations/story.md` (the stage table's build
  row; *Each layer is test-first…* paragraph; *The pre-code check* section becomes *The package check*,
  listing the check order and finding kinds of Task 3 and the re-plan of Task 7; the supervisor's triggers),
  `docs/superpowers/specs/2026-10-24-planning-line-design.md` (Status: "P1 built in PR #…, 2026-10-…"),
  `docs/superpowers/specs/2026-10-16-architect-test-specs-design.md` (one line under the supersession note:
  taken up by the 2026-10-24 design)

- [ ] **Step 1: Write the docs; gate green; commit**

- [ ] **Step 2: With Noah's go and the engine idle, run one story on this repository:**
  `task story -- --text "<a one-layer request Noah names>" --repo .`, and record in
  `docs/bench/2026-10-24-layer-package.md`: the package call's wall-clock and tokens, the checks' findings,
  the coder's steps, and the layer's wall-clock to built, beside the 2026-10-23 numbers (tester 25–32 min of
  a 31–37 min layer).

- [ ] **Step 3: Commit; open the PR with the measurement in its body**

---

## Self-review notes

- Spec coverage: D1 (the card's document) is the state's `built[nn]["package"]` for now; the board card's
  description is P5's. D3 (steps, interfaces, non-thinking coder) Task 4 and the existing `thinking: false`.
  D4's second moment: Task 3; its first moment (the split check, sizing) is P2's and stays prose in the
  layers brief (Task 1). D2a: Task 7. D7: Tasks 1 and 7. D8: no code; `roles.coder.model` is the seat. N5:
  Task 1.
- Out of scope, on purpose: size enforcement of a package (P2), the supervisor on the package loop (its
  evidence is the layer run's trace, which does not exist yet at package time), Jira, parallel layers.
