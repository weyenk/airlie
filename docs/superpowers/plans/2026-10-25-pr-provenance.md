# Provenance in the Pull Request (P6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A layer's pull request carries the why (the initiative and the epic it serves, the story), the what
(the layer's package: files, interfaces, steps, done-when), the contract (the layer's tests, named, written
before the code and red at the base), the checks that passed, who built it on which model at what cost, and
its place in the stack, so a reviewer judges the PR without opening the board; the layer's commit carries
the same provenance as git trailers.

**Architecture:** `airlie.story.provenance` assembles a `Provenance` record from what the pipeline already
stores (the story state's built layers with their packages, the agreement's proof, the subtask rows, the
gate, the runs' events, the diagnoses, the epic and initiative cards) with no model call. `pr_body` is
rewritten to render it in fixed sections under the body limit, with every model-written field neutralised as
today. `squash_since` takes trailers. Two CLI commands print a body before publishing and re-describe a
published story's PRs.

**Tech Stack:** Python 3.12, uv, git, `gh`, pytest.

**Spec:** the planning-line design's P6 paragraph and D1 (`docs/superpowers/specs/2026-10-24-planning-line-design.md`):
"PR bodies that carry the epic, the story, the layer package and the checks that passed, so the human review
has the why beside the diff; the layer's tests named as the contract. Done when: a reviewer can judge a PR
without opening the board." The data comes from P1 (`st.built[nn]["package"]`, `["tests"]`), P2
(`st.agreement` with its proof), P3 (`Story.epic`, the `after:` labels, the "Stacks on" line), P4 (the epic
card's block: `initiative`, `kind`, `estimate`), P5 (`stories/<key>/diagnosis/<n>.json`). Each section
renders when its source exists and is left out when it does not, so the plan is buildable after P1 and
grows as the others land.

## Global Constraints

- Every change passes, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- Everything a model wrote (titles, steps, interfaces, done-when, criteria, findings, diagnoses) is cut to
  its line limit and neutralised by `_model` before it reaches a PR body, as `pr_body` does today; the body
  stays under `BODY_LIMIT` (60,000) with findings cut first, then steps, then interfaces; the stack and the
  merge line always close it. A test id is a path and a name: it is rendered in backticks after `_line`.
- Trailers carry keys, numbers and shas only, never model text.
- Re-describing a published PR edits content on GitHub: it runs only by a person's command (`task
  story:describe`), never from the tick.
- No network or `gh` in tests: the publisher is the recorded fake of `tests/unit/test_story_pipeline.py`;
  git runs for real in `tmp_path`.
- Test first; commit per task; dense docstrings with the incident and date.

## Review Focus

1. **An enabling layer with no criterion** must still name its tests as the contract and say it owns no
   criterion. Pinned in Task 2 (`test_an_enabling_layer_names_its_tests_and_says_it_owns_no_criterion`).
2. **A package whose steps or interfaces are huge or hostile** (headings, mentions, `#123`, HTML) must render
   neutralised and under the limit, stack and merge line intact. Pinned in Task 2
   (`test_a_hostile_oversized_package_renders_neutralised_under_the_limit_with_the_stack_intact`).
3. **A story with no epic** (a written request, a hand-made card) must render with no epic or initiative
   section and no "None" anywhere. Pinned in Task 1 (`test_a_story_without_an_epic_has_no_chain_above_it`) and
   Task 2 (`test_a_body_without_an_epic_has_no_empty_section_and_no_none`).
4. **A dependent story's PR** must name the story it stacks on and the merge order across both stacks.
   Pinned in Task 2 (`test_a_dependent_storys_body_names_its_base_story_and_the_merge_order`).
5. **A layer that blocked and was resumed** must show each block with its class and what released it.
   Pinned in Task 1 (`test_a_resumed_layer_lists_its_blocks_with_their_classes`).

---

### Task 1: The provenance record, from the state alone

**Files:**
- Create: `src/airlie/story/provenance.py`
- Test: `tests/unit/test_story_provenance.py`

**Interfaces:**
- Consumes: `StoryState` (`built[nn]`: `branch`, `sha`, `run_id`, `subtasks`, `gate`, `package`, `tests`;
  `agreement`; `runs`; `base` with `story` for a dependent; `prs`); `Story.epic: EpicHeader | None` (P3);
  `epic_of(description)` and `EpicSpec`'s P4 fields; `BoardStore.get`; `diagnosis.latest`/the
  `diagnosis/*.json` files (P5); the run's `events.jsonl` (`step` events with tokens and `gen_ms`,
  `layer_start`, `layer_built`, `package_ready`, `red_check` events as P1 names them).
- Produces:
  ```python
  @dataclass(frozen=True)
  class Chain: initiative: tuple[str, str] | None; epic: tuple[str, str, str] | None   # (key, title), (key, title, why)
      epic_criteria: tuple[str, ...]      # the epic criterion ids this story names (Story.epic / the story card)
      story: tuple[str, str, str]          # (key or "", title, summary); layer: tuple[str, str]  # ("02/05", title)
  @dataclass(frozen=True)
  class Contract: tests: tuple[str, ...]; by_criterion: Mapping[str, tuple[str, ...]]   # criterion id -> test ids; "" for none
      author_role: str; author_model: str; red_at: str | None; green_at: str   # base sha the tests failed at; the layer's sha
  @dataclass(frozen=True)
  class Checks: red_check: str | None; package_findings: tuple[str, ...]; gate: str | None; review: str | None
      review_findings: tuple[str, ...]; supervisor_interventions: int
  @dataclass(frozen=True)
  class Cost: minutes: float; tokens_in: int; tokens_out: int; attempts: int; blocks: tuple[tuple[str, str, str], ...]   # (class, kind, released_by)
  @dataclass(frozen=True)
  class Builder: subtask: str; role: str; model: str; stop: str; review: str; fix_rounds: int
  @dataclass(frozen=True)
  class Stack: position: int; total: int; base: tuple[str, str]       # ("trunk", "main") or ("story", "AIR-39")
      prs: tuple[tuple[int, str, str], ...]                           # (number, url, title) in merge order
  @dataclass(frozen=True)
  class Provenance:
      chain: Chain; package: Package | None; contract: Contract; checks: Checks; cost: Cost
      builders: tuple[Builder, ...]; stack: Stack; build_commit: str | None; run_ids: tuple[str, ...]; board_url: str | None
  def gather_provenance(st: StoryState, card: Card, story: Story, layers: tuple[Layer, ...], i: int, *,
                        prs: Sequence[tuple[int, str]], cfg: AirlieConfig, store: BoardStore | None) -> Provenance
      # every field from disk and the state; a source that does not exist (no package, no agreement, no epic, no
      # store, no diagnosis) leaves its field None or empty, never raises; the models from cfg.roles[role].model
  def layer_cost(st: StoryState, nn: str) -> Cost
      # minutes from layer_start to layer_built of the last attempt plus earlier attempts; tokens summed from the
      # runs' step events; attempts = 1 + resumes of this layer; blocks from diagnosis/*.json with layer == nn
  ```
  `LineSpec.board_url: str | None` (P5's spec; added here when P5 has not landed) names the web app's
  base URL for card links (`<board_url>/board/<key>`); None links nothing.
- [ ] **Step 1: Failing tests** (a synthetic story directory under `tmp_path` with `state.json`, a run's
  `events.jsonl`, a `diagnosis/1.json`, a `BoardStore` with an initiative, an epic in P4's shape and the story
  card): `test_the_chain_reads_the_initiative_the_epic_and_its_criteria_from_the_board`,
  `test_a_story_without_an_epic_has_no_chain_above_it`,
  `test_the_contract_names_the_packages_tests_under_their_criteria_with_the_red_and_green_shas`,
  `test_the_checks_carry_the_red_check_the_package_findings_the_gate_and_the_review`,
  `test_a_resumed_layer_lists_its_blocks_with_their_classes`,
  `test_a_dependent_storys_stack_base_is_the_story_it_stacks_on`,
  `test_a_state_from_before_the_package_renders_a_contract_from_the_tests_field_or_none`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: a layer's provenance is gathered from its state`

---

### Task 2: The PR body renders the provenance

**Files:**
- Modify: `src/airlie/story/briefs.py` (`pr_body`), `src/airlie/story/pipeline.py` (`_publish` builds the
  provenance and passes it), `docs/integrations/story.md` (*What lands where*, the PRs bullet)
- Test: `tests/unit/test_story_briefs.py`, `tests/unit/test_story_pipeline.py`

**Interfaces:**
- `pr_body(p: Provenance, *, test_cmd: str | None) -> str` replaces today's signature (`_publish` is its
  only caller). Sections, in order, each left out when its source is empty:
  1. the header: `**Initiative:** <key> <title>` · `**Epic:** <key> <title>` · `**Story:** <key> <title>`
     (linked to `board_url` when set) · `**Layer n of N:** <title>`;
  2. `### Why`: the epic's why, two lines at most, then the story's summary, quoted as model text;
  3. `### This layer`: the package's `done_when`, a files table (`path | create or modify | why`), the
     interfaces as `file: signature` lines, the steps as a numbered list (cut after `_fit`);
  4. `### The contract`: "Tests written by the <author_role> on <model> before the code; failing at
     `<red_at>`, passing at `<green_at>`", then each criterion the layer owns (its line, quoted) with its
     test ids beneath, then tests with no criterion under "Enabling tests"; an enabling layer says "This
     layer owns no acceptance criterion; its tests are the contract";
  5. `### Checks that passed`: the red check line, the package checks ("<n> findings returned to the
     architect before the tests were accepted" or "accepted first time"), the gate command, the review
     verdict, the supervisor's interventions count;
  6. `### Reviewer findings to check`: as today;
  7. `### How it was built`: the table gains a `model` column; then one line: "<minutes> min, <tokens in>/
     <tokens out> tokens, <attempts> attempt(s)" and, when any, "blocked <n> time(s): <class>/<kind>,
     released by <fix key or a person>";
  8. `### Stack (review and merge bottom-up)`: as today, plus for a dependent story the line P3 adds,
     "Stacks on <key>'s pull requests; merge those first", and the base named;
  9. the footer: "Built by airlie <build_commit short> on local models; runs <run ids>; a human reviews and
     merges."
  The size discipline: findings are cut first, then the steps, then the interfaces, then the Why; the
  header, the contract's test ids, the stack and the footer are never cut (`BODY_LIMIT` as today).
- `_publish`: `p = gather_provenance(st, card, story, layers, i, prs=prs, cfg=self.cfg, store=self._store)`
  where `_store` is the `LocalBoard`'s `BoardStore` when the board is local, else None; `pr_body(p,
  test_cmd=gate)`.
- [ ] **Step 1: Failing tests:** `test_pr_title_and_body` rewritten for the sections; `test_the_body_leads_with_the_chain_and_the_why`,
  `test_the_contract_lists_each_criterions_tests_and_the_red_and_green_shas`,
  `test_an_enabling_layer_names_its_tests_and_says_it_owns_no_criterion`,
  `test_a_hostile_oversized_package_renders_neutralised_under_the_limit_with_the_stack_intact`,
  `test_a_body_without_an_epic_has_no_empty_section_and_no_none`,
  `test_a_dependent_storys_body_names_its_base_story_and_the_merge_order`,
  `test_the_cost_line_and_the_blocks_render_when_present`; in `test_story_pipeline.py`:
  `test_publish_writes_a_body_with_the_contract_from_the_layers_package`.
- [ ] **Step 2: Implement. Step 3: Docs; gate; commit** `story: the pull request carries the layer's provenance`

---

### Task 3: Provenance in the commit

**Files:**
- Modify: `src/airlie/teams/worktree.py` (`squash_since(..., trailers)`), `src/airlie/story/pipeline.py`
  (`_build` passes them)
- Test: `tests/unit/test_worktree.py`, `tests/unit/test_story_pipeline.py`

**Interfaces:**
- `squash_since(worktree, base_sha, message, *, author=None, trailers: Mapping[str, str] = {}) -> str`: the
  message, a blank line, then `Key: value` lines in the order given; keys match `[A-Za-z-]+`, values are one
  line with no control characters (a bad trailer is a `ValueError` before any git call).
- `_build` passes `{"Airlie-Story": <key or "request">, "Airlie-Layer": "<nn>/<total>", "Airlie-Epic": <epic
  key>}` (the epic only when the story has one), `"Airlie-Run": <run id>`, `"Airlie-Build": <build_commit>`
  (when known), `"Airlie-Tests": "<n> tests in <k> files"` (a count, never ids: the ids are in the PR).
- [ ] **Step 1: Failing tests:** `test_squash_since_appends_trailers_git_can_read` (`git interpret-trailers
  --parse` returns them), `test_a_trailer_with_a_bad_key_or_a_newline_is_refused_before_git_runs`,
  `test_a_layers_commit_carries_the_story_layer_and_run_trailers`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: a layer's commit carries its provenance as trailers`

---

### Task 4: Print a body before publishing; re-describe a published story; one real stack

**Files:**
- Modify: `src/airlie/cli.py` (`story pr-body KEY NN`, `story describe KEY`), `Taskfile.yml`
  (`story:pr-body`, `story:describe`), `src/airlie/story/factory.py` (`describe_one`),
  `docs/integrations/story.md`, `README.md` (the task rows)
- Test: `tests/unit/test_cli_story.py`, `tests/unit/test_story_factory.py`

**Interfaces:**
- `airlie story pr-body KEY NN` prints the body a built layer would get now (no network; the board read
  for the chain); `airlie story describe KEY` rebuilds every published layer's title and body and runs
  `Publisher.describe` for each (`gh pr edit`), printing the PR numbers; refuses a story with no `prs` in
  its state. `describe_one(cfg, *, board, card) -> list[int]`.
- [ ] **Step 1: Failing tests:** `test_story_pr_body_prints_the_body_for_a_built_layer_without_publishing`,
  `test_story_describe_re_describes_every_published_pr_and_refuses_an_unpublished_story`.
- [ ] **Step 2: Implement. Step 3: Docs; gate; commit** `story: print a layer's PR body; re-describe a published story`
- [ ] **Step 4 (with Noah's go: it edits pull requests on GitHub): `task story:describe -- AIR-9` on the open
  stack #131–#135, read one body as the reviewer would, and note in the PR of this plan what the body now
  answers that it did not before.**

---

## Self-review notes

- Spec coverage: the epic and the story (Task 1's chain, Task 2's header and Why); the layer package (Task
  2's This layer); the checks that passed (Tasks 1, 2); the tests named as the contract (Tasks 1, 2); "judge
  without opening the board" is the whole of Task 2 plus the cost and the blocks; the commit trailers are an
  addition in the same spirit, small, and marked as mine (Task 3).
- Every section degrades: built after P1 alone, a body has the story, the package, the contract, the checks,
  the cost and the stack; the epic and initiative lines appear when P3 and P4 land; the blocks' classes when
  P5 does. `gather_provenance` reads what is there.
- `pr_body`'s callers: `_publish` and the two CLI commands; the old positional signature goes.
- `board_url` sits in `LineSpec` because P5 owns the line's config; built before P5, Task 1 adds a one-field
  `LineSpec` that P5 extends.
