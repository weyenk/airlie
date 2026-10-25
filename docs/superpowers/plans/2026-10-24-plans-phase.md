# The Plans Phase (P3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An approved epic on the board becomes, with no person in the loop, prioritized story cards under
it: each the smallest unit a reviewer can judge from its own PR stack, with testable criteria, its files,
its boundary interfaces and its dependencies. The tick plans epics, promotes dependents as their dependency
reaches review, stacks dependents on their dependency's branches, replays them when a lower story moves,
resumes a blocked story once, and re-plans the epic when it blocks again.

**Architecture:** A new `airlie.story.epic` module holds the epic contract and `task epic`; `airlie.story.epic_plan`
holds the story-plan contract, its parser and the plan-time checks; `airlie.story.asking` is the read-only
studio ask machinery lifted out of `StoryPipeline` so an `EpicPipeline` (`airlie.story.epic_pipeline`) can
plan with the architect in rounds, check the plan, and make the cards. `StoryPipeline` learns to take a story
from its card's JSON block (no refine), to base itself on a dependency's top branch, to publish a stack on
that branch, and to replay its layers onto a moved base. The tick (`story/factory.py`) gains the epic's
steps, parent gating, promotion, the one resume and the re-plan. The epic has its own state and events.

**Tech Stack:** Python 3.12, uv, pydantic v2, pytest (asyncio auto), git worktrees, `gh` with `gh-stack`,
the prompt hub, the board's SQLite store.

**Spec:** `docs/superpowers/specs/2026-10-24-plans-phase-design.md` (N1, N2, D1–D10), on the planning-line
design and the P1 and P2 plans, which must be built first.

## Global Constraints

- Every change passes, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- uv only; Taskfile tasks; `Strict` config models; hub files for model-facing prose; output contracts
  (`EPIC_CONTRACT`, `STORY_PLAN_CONTRACT`, the story card's block) in Python beside their parsers; regenerate
  the golden snapshot after any hub change.
- Epic text and the architect's plan are data: a path, a branch or a repository name reaches `git` or `gh`
  only after the checks; every branch name goes through `branch_name`'s validation.
- `BoardStore` and the board API are the only writers of cards. Jira stays read-only for the line: with
  `story.board: jira`, the epic steps log one line and do nothing.
- The one-story lock holds for planning an epic as for building a story. Only a human merges.
- Test first; commit per task; dense docstrings with the incident and date. Do not restart the engine.

## Review Focus

1. **A plan whose dependencies form a cycle, or name a story not in the plan,** must be refused as feedback,
   never turned into cards. Pinned in Task 2 (`test_a_cycle_or_an_unknown_dependency_is_refused`).
2. **An epic criterion no story names** must be feedback, not a plan with a hole. Pinned in Task 2
   (`test_an_epic_criterion_no_story_covers_is_refused`).
3. **A dependency's branch moves between the dependent's promotion and its first build** (a reviewer pushed
   to the lower story): the dependent must be replayed onto the new base before it builds. Pinned in Task 6
   (`test_a_dependent_whose_base_moved_is_replayed_before_it_builds`).
4. **A person pausing the epic** (moving it to `blocked`) must hold its waiting and approved stories, and
   releasing it must let them go. Pinned in Task 7 (`test_a_paused_epic_holds_its_stories_until_it_is_released`).
5. **`task epic -- --file` on a spec with no `## Acceptance criteria` section and no JSON block** must refuse
   with a message and make no card. Pinned in Task 1 (`test_a_spec_without_criteria_makes_no_card_and_says_why`).

---

### Task 1: The epic contract and `task epic` (D1)

**Files:**
- Create: `src/airlie/story/epic.py`
- Modify: `src/airlie/cli.py` (an `epic` command group: `airlie epic add`), `Taskfile.yml` (`epic:add`),
  `README.md` (one table row)
- Test: `tests/unit/test_story_epic.py`, `tests/unit/test_cli_epic.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class EpicCriterion: id: str; text: str
  @dataclass(frozen=True)
  class EpicSpec: repositories: tuple[str, ...]; criteria: tuple[EpicCriterion, ...]; constraints: tuple[str, ...]; links: tuple[str, ...]
      def to_dict / from_dict
  EPIC_CONTRACT: str     # the design's D1 JSON, verbatim
  def parse_epic(obj: dict | None) -> EpicSpec
      # StoryError: no JSON; type != "epic"; no repositories; a repository not "owner/name"; no criteria; a
      # criterion without id or text; ids repeated
  def epic_from_markdown(text: str, repo: str | None) -> tuple[str, EpicSpec]
      # the card's description (the text, plus a generated JSON block when the text has none) and the spec;
      # criteria from the bullets under "## Acceptance criteria" as EC1…; StoryError when the text has neither
      # a block nor that section, or `repo` is None and the block names none
  def epic_of(card_description: str) -> EpicSpec | None     # the block at the end of a card, or None
  ```
- CLI: `airlie epic add --file PATH [--repo owner/name] [--title TEXT]` → `BoardStore.create(level="epic",
  title=<--title or the file's first heading>, description=..., labels=[f"repo:{r}" for r in repositories])`,
  prints the key; `task epic:add -- --file spec.md --repo owner/name`.
- [ ] **Step 1: Failing tests:** `test_an_epic_block_parses_and_round_trips`,
  `test_a_markdown_spec_gets_its_criteria_from_the_acceptance_section`,
  `test_a_spec_without_criteria_makes_no_card_and_says_why`, `test_a_flawed_epic_block_is_refused_with_a_reason`
  (parametrised); `test_cli_epic.py`: `test_epic_add_makes_an_epic_card_with_its_repo_label_and_prints_the_key`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: the epic contract and task epic:add`

---

### Task 2: The story plan contract and its parser (D2)

**Files:**
- Create: `src/airlie/story/epic_plan.py`
- Test: `tests/unit/test_story_epic_plan.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class PlannedStory: id: str; title: str; why: str; repository: str; epic_criteria: tuple[str, ...]
      scope: tuple[ScopedPath, ...]; consumes: tuple[str, ...]; produces: tuple[str, ...]
      acceptance: tuple[Criterion, ...]; out_of_scope: tuple[str, ...]; assumptions: tuple[str, ...]
      depends_on: tuple[str, ...]; size: Literal["S", "M"]
      def story(self) -> Story      # title, summary=why, acceptance, out_of_scope, assumptions
  @dataclass(frozen=True)
  class Risk: risk: str; pinned: str      # "S2/AC1"
  @dataclass(frozen=True)
  class StoryPlan: summary: str; constraints: tuple[str, ...]; risks: tuple[Risk, ...]; stories: tuple[PlannedStory, ...]
      def to_dict / from_dict; def order(self) -> tuple[PlannedStory, ...]   # dependency order, ties by position
  STORY_PLAN_CONTRACT: str   # the design's D2 JSON, verbatim, with the sentence about the smallest reviewable unit
  def parse_story_plan(obj: dict | None, epic: EpicSpec, *, max_stories: int, max_scope_files: int) -> StoryPlan
      # StoryError for each refusal the design lists (ids, cycles, coverage of epic criteria, no criterion,
      # repository, counts, size L, a pinned risk naming no story criterion), with a reason the architect can act on
  ```
- [ ] **Step 1: Failing tests:** `test_a_story_plan_parses_in_dependency_order_and_each_story_is_a_story`,
  `test_a_cycle_or_an_unknown_dependency_is_refused`, `test_an_epic_criterion_no_story_covers_is_refused`,
  parametrised `test_a_flawed_plan_is_refused_with_a_reason` (repeated id, no criterion, wrong repository,
  too many stories, too many paths, size L, risk pinned to nothing).
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: the story plan contract and its parser`

---

### Task 3: Plan-time mechanical checks (D4)

**Files:**
- Modify: `src/airlie/story/epic_plan.py` (checks)
- Test: `tests/unit/test_story_epic_plan.py`

**Interfaces:**
- Produces:
  ```python
  def check_story_plan(plan: StoryPlan, *, trunk_of: Callable[[str], tuple[Path, str] | None]) -> list[Finding]
      # trunk_of(repository) -> (checkout, trunk sha) or None when the line has no checkout; findings:
      # file_missing (a scope path not new, not at the trunk, not new in an earlier story of the plan),
      # order_violation (a dependency later in the order), empty_criterion (a Given/When/Then with a blank part)
  def plan_feedback(findings: Sequence[Finding]) -> str    # one line per finding, for brief-reply-again
  ```
- [ ] **Step 1: Failing tests:** `test_a_scope_path_the_trunk_lacks_is_a_finding_unless_an_earlier_story_creates_it`,
  `test_a_repository_without_a_checkout_passes_its_paths_unchecked`, `test_an_empty_criterion_part_is_a_finding`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: a story plan is checked against the trunk before any card`

---

### Task 4: The asking machinery leaves `StoryPipeline`

A behaviour-preserving refactor so the epic pipeline can ask the architect the way the story pipeline does.

**Files:**
- Create: `src/airlie/story/asking.py`
- Modify: `src/airlie/story/pipeline.py` (uses `Asker`)
- Test: `tests/unit/test_story_pipeline.py` (unchanged behaviour; the refactor keeps every test green)

**Interfaces:**
- Produces: `class Asker` with the methods now on `StoryPipeline`: `_start_ask`, `_answer`, `_ask`,
  `_ask_in_rounds`, `_usable` (same signatures; `st` becomes a protocol `Recorder` with `runs`, `save()`,
  `emit()`, `card`, `base`), constructed with `(cfg, studio: StudioFactory, repo: Path)`;
  `StoryPipeline.asker: Asker` and its methods delegate.
- [ ] **Step 1: Run the pipeline tests; green. Step 2: Extract; run them again; green. Step 3: Commit**
  `story: the read-only asks live in asking.py`

---

### Task 5: The epic pipeline plans with the architect (D3, D9)

**Files:**
- Create: `src/airlie/story/epic_pipeline.py`, `src/airlie/story/epic_state.py`,
  `src/airlie/prompts/rules/plan-brief.md`, `src/airlie/prompts/rules/plan-replan.md` (Task 8 uses it)
- Modify: `src/airlie/story/briefs.py` (`plan_brief`), `src/airlie/config/schema.py` (`PlanSpec`),
  `airlie.yaml` (`story.plan`)
- Test: `tests/unit/test_story_epic_pipeline.py`, `tests/unit/test_story_briefs.py`, `tests/unit/test_config.py`

**Interfaces:**
- `StorySpec.plan: PlanSpec` with `max_stories: int = Field(8, ge=1)`, `max_scope_files: int = Field(8, ge=1)`,
  `round_steps: int = Field(5, ge=0)`, `max_rounds: int = Field(3, ge=1)`.
- `EpicState` at `epics/<key>/state.json`: `card`, `repo`, `stage` in `("new", "planned", "building",
  "review", "done", "blocked")`, `plan: dict | None`, `attempts: int`, `stories: dict[str, str]` (plan id →
  card key), `replans: list[dict]`, `blocked: str | None`; `emit` to `epics/<key>/events.jsonl`;
  `open`, `save`, `finish` as `StoryState` has them.
- `plan_brief(epic_text: str, spec: EpicSpec, *, guide: str, conventions: str, lessons: Sequence[Lesson],
  max_stories: int, max_scope_files: int, feedback: str | None, round_steps: int) -> str`: `plan-brief`
  (the hub rule: the writing-plans framing in Noah's words: the smallest unit a reviewer judges from its own
  PR stack; files, interfaces, criteria as the proof; order by dependency; constraints copied; risks pinned),
  the epic's document quoted as data, the guide (`repo_guide` for the paths the epic names, at the trunk),
  the conventions (P2's `conventions_text`), the lessons, `brief-layers-sizing`'s budget line, the contract,
  `brief-layers-rounds` + `REFINE_FIELD` when planning in rounds, `_again(feedback)`.
- `EpicPipeline(cfg, *, board: LocalBoard, studio: StudioFactory, repo_of: Callable[[str], Path | None])`
  with `async def plan(card: Item) -> EpicState`: opens the state; `plan_round_steps`-style rounds through
  `Asker._ask_in_rounds` on a detached worktree at the first repository's trunk (fetched as `_trunk_ref`
  does); `parse_story_plan` then `check_story_plan` per round, findings as feedback, up to
  `story.stage_attempts`; events `epic_start`, `plan_round`, `plan_returned(kinds)`, `plan`; a plan that is
  never usable → `blocked` with the reason on the epic card and the state. Card creation is Task 6's
  `_create_stories`, called at the end of `plan`.
- [ ] **Step 1: Failing tests:** `test_the_plan_brief_frames_the_story_as_the_smallest_reviewable_unit_and_carries_the_epic`,
  `test_an_epic_is_planned_in_rounds_and_its_plan_is_recorded`,
  `test_a_plan_with_findings_is_asked_again_with_them_then_blocks_the_epic`,
  `test_story_plan_has_defaults_and_floors`.
- [ ] **Step 2: Implement; CASES for the hub ids; snapshot. Step 3: Gate; commit**
  `story: an epic is planned into stories by the architect, in rounds`

---

### Task 6: Stories on the board, and a dependent story stacks on its dependency (D5, D6, D7, N2)

**Files:**
- Modify: `src/airlie/story/epic_pipeline.py` (`_create_stories`), `src/airlie/story/model.py`
  (`Story.epic: EpicHeader | None`, `StoryCard` block), `src/airlie/story/pipeline.py` (`_refine` reads the
  card's block; `_base` for a dependent; `_publish` on a base branch; the PR body line), `src/airlie/story/state.py`
  (`base` gains `story`), `src/airlie/story/publish.py` (`stack(wt, base, branches)`), `src/airlie/story/briefs.py`
  (`## The epic this story serves` in the amigos opening and the package brief; `pr_body` line)
- Test: `tests/unit/test_story_epic_pipeline.py`, `tests/unit/test_story_pipeline.py`,
  `tests/unit/test_story_publish.py`, `tests/unit/test_story_briefs.py`

**Interfaces:**
- `EpicHeader(key: str, summary: str, constraints: tuple[str, ...], risk: str | None)`; `Story.epic` defaults
  None; `STORY_CARD_CONTRACT`: the card's closing block `{"type": "story_card", "story": <Story>, "scope":
  [...], "consumes": [...], "produces": [...], "depends_on": [<card keys>], "size": "S" | "M", "epic":
  <EpicHeader>}` (`size` is read later by P4's `StoryCosts`, which prices an epic from finished stories);
  `story_card_of(description) -> StoryCard | None`; `render_story_card(ps, header, depends_keys) -> str`.
- `_create_stories(plan, header)`: in `plan.order()`, `BoardStore.create(level="story", parent=epic key,
  title, description=render_story_card(...), labels=["repo:…", "epic:<key>", "plan:<id>", *("after:<dep key>" …)])`;
  then `update(key, status="approved")` for a story with no dependency; `EpicState.stories` maps ids to keys;
  event `stories_created(keys, order)`; a comment on the epic card listing them.
- `StoryPipeline._refine`: when `story_card_of(card.description)` is not None, `st.story` is its story (with
  `epic`) and refine is skipped (event `refine_skipped(reason="story card")`).
- `StoryPipeline._base`: when the card has `after:` labels, the base is the last dependency's top layer branch:
  `{"ref": <branch>, "sha": rev_parse(refs/heads/<branch>), "story": <dep key>}` read from the dependency's
  `state.json` (`built[last]["branch"]`); a dependency not in review is `StoryBlocked` ("waiting for <key>");
  `_publish` passes `st.base["ref"]` as the stack's base when `story` is set, else the trunk; `pr_body` adds
  "Stacks on <key>'s pull requests; merge those first." under the stack heading.
- [ ] **Step 1: Failing tests:** `test_the_plans_stories_become_ranked_cards_under_the_epic_with_their_labels`,
  `test_a_story_with_no_dependency_is_born_approved_and_a_dependent_waits_with_its_after_label`,
  `test_a_story_card_with_a_block_skips_refine_and_carries_the_epic_header_into_its_briefs`,
  `test_a_dependent_story_bases_on_its_dependencys_top_branch_and_publishes_a_stack_on_it`,
  `test_a_dependent_pr_body_names_the_story_it_stacks_on`, `test_a_dependency_not_yet_in_review_is_a_wait_not_a_build`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: stories land on the board under their epic, dependents stacked`

---

### Task 7: The tick learns the epic, gates on it, promotes and replays (D10, D5)

**Files:**
- Modify: `src/airlie/story/factory.py` (`tick`), `src/airlie/story/local_board.py` (`next_card` parent
  filter, `next_epic`, `waiting_stories`, `epic_of`), `src/airlie/story/pipeline.py` (`restack(card, onto)`
  generalised from the trunk to any base)
- Test: `tests/unit/test_story_factory.py`, `tests/unit/test_story_local_board.py`

**Interfaces:**
- `LocalBoard.next_epic() -> Card | None` (the oldest `approved` epic whose `EpicState` has no plan);
  `LocalBoard.next_card()` returns only a story whose parent epic, when it has one, is `approved` or `doing`;
  `LocalBoard.waiting_stories() -> list[Item]` (`todo` with `after:` labels, parent not held);
  `LocalBoard.set_epic_status(key, status)`.
- `tick` order: (1) bookkeeping on every tick: promote each waiting story whose dependencies' stories are in
  `review` (`update(status="approved")`, event `story_promoted` on the epic); for each `approved`/`doing`
  dependent whose recorded base sha differs from its base branch's sha, or whose dependency's PRs are merged
  (`gh pr view --json state,mergedAt`), `restack(card, onto=<new base>)`, which replays its built layers
  (`restack.replay`) and moves its branches, then updates `state.base`; move each epic's status with its
  stories (`doing`, `review`, `done`); (2) one piece of work: plan the next epic if one is waiting, else build
  the top approved story as today. `story:tick`'s log line names which it did.
- [ ] **Step 1: Failing tests:** `test_the_tick_plans_the_top_approved_epic_before_it_builds_a_story`,
  `test_a_waiting_story_is_promoted_when_its_dependency_reaches_review`,
  `test_a_dependent_whose_base_moved_is_replayed_before_it_builds`,
  `test_a_paused_epic_holds_its_stories_until_it_is_released`, `test_an_epics_status_follows_its_stories`,
  `test_an_approved_epic_with_no_block_is_set_aside_with_a_note`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: the tick plans epics, gates on them, promotes and replays dependents`

---

### Task 8: One resume, then the re-plan (D8)

**Files:**
- Modify: `src/airlie/story/factory.py` (the one resume), `src/airlie/story/epic_pipeline.py` (`replan`),
  `src/airlie/cli.py` + `Taskfile.yml` (`epic:replan`), `src/airlie/config/schema.py`
  (`StorySpec.resume_attempts: int = Field(1, ge=0)`)
- Test: `tests/unit/test_story_factory.py`, `tests/unit/test_story_epic_pipeline.py`

**Interfaces:**
- The tick: a story the line blocked (its state's `blocked` set, card in `blocked`) with fewer than
  `resume_attempts` `resumed:N` labels is moved back to `approved` with the next label; one that has them all
  triggers `EpicPipeline.replan(epic)` once (`replanned_for:<story key>` label on the epic) when it belongs to
  an epic.
- `EpicPipeline.replan(card: Item, *, reason: str) -> EpicState`: the architect's ask with `plan-replan`
  (the current plan as JSON, each story marked not started / started / blocked (with its lesson and layers
  built) / in review / merged, the epic card's comments since the plan, the lesson store), parsed and checked
  as a plan; applies it: unstarted cards changed in place (title, description, rank), new cards made, dropped
  cards → `done` with a note; a blocked story with no built layer may be retired (card → `done` with the note
  naming its replacement; `_clear_attempt` on its state; its state directory kept) and a blocked story with a
  built layer is kept untouched; started, in-review and merged stories untouched; events `replan`, a comment
  on the epic. The brief forbids a story that redoes a kept story's work.
- [ ] **Step 1: Failing tests:** `test_a_blocked_story_is_resumed_once_then_its_epic_is_replanned`,
  `test_a_replan_changes_only_unstarted_cards_and_may_retire_a_blocked_story_with_no_layer`,
  `test_a_replan_never_touches_a_story_in_review_or_with_a_built_layer`, `test_epic_replan_is_a_task`.
- [ ] **Step 2: Implement; CASES for `plan-replan`; snapshot. Step 3: Gate; commit**
  `story: a blocked story is resumed once, then its epic is re-planned`

---

### Task 9: The epic in the flow API, the docs, and one real epic

**Files:**
- Modify: `src/airlie/serve/flow.py` (an `epic_flow(state_dir, key)` with the plan, the stories and their
  states), `src/airlie/serve/listing.py` (`epics`), `src/airlie/serve/app.py` (`GET /v1/airlie/epics`,
  `/v1/airlie/epics/{key}/flow`), `tests/unit/test_story_flow.py`, `tests/unit/test_serve.py`,
  `docs/integrations/story.md` (an *Epics* section: the contract, the plan, stacking and replay, the tick,
  the config table), `README.md` (the task rows), the two design documents' Status lines
- [ ] **Step 1: Failing API tests. Step 2: Implement. Step 3: Docs; gate; commit**
  `serve, docs: epics in the flow API and the story docs`
- [ ] **Step 4 (with Noah's go, engine idle): `task epic:add` with a small real spec, one tick to plan it,
  one tick to build its first story; record the plan's rounds, tokens and wall-clock and the cards it made in
  `docs/bench/2026-10-24-plans-phase.md`; open the PR with the numbers in its body.**

---

## Self-review notes

- Spec coverage: D1 Task 1; D2 Task 2; D3, D9 Task 5; D4 Task 3; D5, D6, D7, N2 Task 6 (stacking, the
  base, the PR line) and Task 7 (promotion, replay on a moved base); D8 Task 8; D10 Task 7. The React
  screens are not touched (the flow API only).
- The design's `scope_widened` event (D4) is P2's meeting to emit once the agreement's layers are known; it is
  noted in the P2 plan's Task 6 as a one-line addition and not repeated here.
- `restack` today refuses a story that has published; stacking across stories means a published dependent
  may be replayed when its base moves (Task 7). The refusal becomes "a story whose PRs are merged is never
  replayed".
