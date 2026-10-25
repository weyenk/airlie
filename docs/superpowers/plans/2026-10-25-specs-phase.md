# The Specs Phase (P4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An approved initiative card (a person's paragraph, `repo:` labels, an optional budget) becomes,
through product's dialogue with the person and its consultations with the specialists, one core epic and
optional extension epics under it, each a spec a planner can work from, priced in hours from the ledger with
the arithmetic shown, with a viability verdict, and for web work three mockup options the person picks
from; the person's reply runs the next turn at once, the tick is the fallback, and a comment on an epic is one
revision.

**Architecture:** `airlie.story.initiative` holds the initiative read from its card and the contracts of
product's replies (a question, a round of asks, the epics) with their parsers and checks; `airlie.story.costs`
prices a story sketch from the stories the ledger has finished; `airlie.story.mockups` holds the directions
contract, the page check and the files; `airlie.story.initiative_pipeline` runs the stages (questions,
imagining in rounds with side-by-side consultations and the surveys, mockups, the planner's read, the cards)
over P3's `Asker`, and the revision; `airlie.story.initiative_state` is its record. The tick, a serve route
and job, the CLI, two MCP tools and a Chat skill are the doors. P3's `EpicSpec` gains P4's optional fields
and its tick gains two checks (the pick, the held extension).

**Tech Stack:** Python 3.12, uv, pydantic v2, pytest (asyncio auto), FastAPI, the prompt hub, the board's
SQLite store, git worktrees, the studio orchestrator with `FakeEngine` in tests.

**Spec:** `docs/superpowers/specs/2026-10-24-specs-phase-design.md` (N1–N7, D1–D15), on the planning-line
design and the P1, P2 and P3 plans, which must land first (its Constraints: built strictly after P3).

## Global Constraints

- Every change passes, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- uv only; Taskfile tasks; `Strict` config models; hub files for model-facing prose; output contracts in
  Python beside their parsers; regenerate the golden snapshot (`AIRLIE_WRITE_PROMPT_TEXTS=1 uv run pytest
  tests/unit/test_prompt_texts.py -q`, then rerun without it) after any hub change.
- The initiative's text, the person's answers, the specialists' answers, the mockup directions and the
  rendered pages are data: a repository name reaches a checkout only through `repo:` labels and
  `story.repos_root` (`find_checkout`); a mockup page is written and served as a file, never executed by the
  line; every path a route serves is matched to `option-[1-9].html` under the initiative's own directory.
- `BoardStore` and the board API are the only writers of cards. With `story.board: jira` the initiative steps
  log one line and do nothing.
- The one-story lock (`story_lock`) is held for every turn, by the tick and by the serve job alike. Only a
  human approves an epic. Do not restart the engine; a story may be running.
- No live engine or network in tests: the studio runs on `FakeEngine` with scripted replies per role, as
  `tests/unit/test_story_pipeline.py` does; the serve routes on `TestClient` with `MockTransport`.
- The front end is the Vue app's work: nothing under `src/airlie/serve/web` changes; P4 adds routes and rows.
- Test first; commit per task; dense docstrings with the incident and date.

## Review Focus

1. **A reply of "go" or "skip", or an initiative whose text asks for no questions,** must end the questions
   and start the imagining on the same turn, never ask another. Pinned in Task 3
   (`test_a_reply_of_go_ends_the_questions_and_the_imagining_starts_on_the_same_turn`).
2. **A round that names a seat not in `story.specs.consultable`, or more asks than the round allows,** must go
   back to product as feedback and run nothing. Pinned in Task 4
   (`test_an_ask_for_a_seat_not_consultable_is_feedback_and_runs_no_turn`).
3. **A rendered page holding a `<script>`, a `<link>`, an `@import` or a URL that is not `data:`** must never
   be kept on disk or served. Pinned in Task 5 (`test_a_page_with_a_script_or_an_outside_url_is_never_kept`).
4. **The turn route while the lock is held** must answer 409 and leave the comment for the tick, which then
   runs that turn exactly once. Pinned in Task 9
   (`test_the_turn_route_answers_busy_while_the_lock_is_held_and_the_tick_then_runs_the_turn_once`).
5. **An approved epic with mockup options and no pick** must not be planned and must keep `review` with a
   note, however many ticks pass. Pinned in Task 7
   (`test_an_approved_epic_with_options_and_no_pick_is_not_planned_and_stays_in_review`).

---

### Task 1: The config and the initiative read from its card (D1, D10)

**Files:**
- Create: `src/airlie/story/initiative.py`
- Modify: `src/airlie/config/schema.py` (`SpecsSpec`, `StorySpec.specs`, `validate_references`),
  `airlie.yaml` (`story.specs`, product's `max_tokens: 8192` and `max_context: 131072`),
  `src/airlie/story/local_board.py` (`initiative`, `item`, `next_initiative`, `is_person`)
- Test: `tests/unit/test_story_initiative.py`, `tests/unit/test_config.py`, `tests/unit/test_local_board.py`

**Interfaces:**
- Produces:
  ```python
  class SpecsSpec(Strict):
      enabled: bool = True
      product_role: str = "product"
      consultable: list[str] = Field(default_factory=lambda: ["architect", "ux", "design", "security", "docs", "research"])
      max_questions: int = Field(5, ge=0);  max_rounds: int = Field(3, ge=1);  max_asks_per_round: int = Field(4, ge=1)
      max_consultations: int = Field(8, ge=0);  consult_steps: int = Field(8, ge=1);  consult_chars: int = Field(4_000, ge=200)
      product_steps: int = Field(12, ge=1);  read_steps: int = Field(6, ge=1);  mockup_options: int = Field(3, ge=2)
      revisions: int = Field(1, ge=0);  attempts: int = Field(2, ge=1);  min_samples: int = Field(3, ge=1)
      default_hours: dict[str, float] = Field(default_factory=lambda: {"S": 1.0, "M": 5.0, "L": 8.0})
      size_scale: dict[str, float] = Field(default_factory=lambda: {"S": 0.6, "M": 1.0, "L": 1.8})
  StorySpec.specs: SpecsSpec = Field(default_factory=SpecsSpec)
  # validate_references: specs.product_role and every consultable name is in roles; default_hours and
  # size_scale each have exactly the keys S, M, L
  ```
  ```python
  BUDGET_LABEL = re.compile(r"budget:(\d+(?:\.\d+)?)h")      # the label; "budget:20h"
  UPLOAD_LINK = re.compile(r"/v1/airlie/uploads/([A-Za-z0-9_-]+)")
  SKIP_PHRASES = ("no questions", "skip the questions")       # in the text, case-insensitive
  @dataclass(frozen=True)
  class Initiative:
      key: str; title: str; text: str; repositories: tuple[str, ...]; budget_hours: float | None
      skip_questions: bool; attachments: tuple[str, ...]      # upload ids linked in the text
      @classmethod
      def from_item(cls, item: Item) -> Initiative
          # StoryError: level is not "initiative"; no repo: label (repo_labels); a budget label that is not a number
      def card(self) -> Card        # for the briefs that take one (survey_brief): key, ref and title the initiative's, description the text
  def answer_ends_questions(text: str) -> bool               # "go" or "skip" alone, any case, trailing punctuation allowed
  ```
- `LocalBoard.item(key) -> Item` (any level; `BoardError` as `_item`); `LocalBoard.initiative(key) ->
  Initiative` (`Initiative.from_item`, `BoardError` for another level); `LocalBoard.next_initiative() -> Item
  | None` (the oldest `approved` initiative); `LocalBoard.is_person(comment: Comment) -> bool` (`not
  self._is_note(comment.author, comment.text)`), used by every "whose comment" rule below.
- [ ] **Step 1: Failing tests:** `test_story_specs_has_defaults_and_floors_and_names_only_roles`,
  `test_an_initiative_reads_its_repositories_budget_skip_and_attachments_from_its_card`,
  `test_an_initiative_without_a_repo_label_is_refused_with_a_reason`,
  `test_go_or_skip_alone_ends_the_questions_and_a_sentence_does_not` (parametrised),
  `test_local_board.py`: `test_the_oldest_approved_initiative_is_next_and_a_persons_comment_is_told_from_a_roles`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: the specs config and the initiative read from its card`

---

### Task 2: The ledger prices a story sketch (D6)

**Files:**
- Create: `src/airlie/story/costs.py`
- Test: `tests/unit/test_story_costs.py`

**Interfaces:**
- Consumes: `story_card_of(description)` and the story card's `size` (P3 Task 6); `epics/<key>/state.json`
  (P3 Task 5) with `estimate` once Task 6 here writes it.
- Produces:
  ```python
  Size = Literal["S", "M", "L"]
  @dataclass(frozen=True)
  class StorySketch: title: str; size: Size; scope: str
  @dataclass(frozen=True)
  class Estimate:
      hours: float; basis: str; at: str                       # at: ISO 8601 UTC
      revised: float | None = None; actual: float | None = None
      def to_dict / from_dict
  def story_wall_clock(events: Path) -> float | None
      # hours: the sum over runs (split at each story_start) of last ts minus first ts; None when the story's
      # state.json stage is not "review" (never published) or the log is unreadable
  def size_of_story(state_dir: Path, key: str) -> Size | None
      # the card's size when the story's card description (state.json "card") has a story card block with
      # one; else by built layers: 1-2 S, 3-4 M, 5+ L; None with no built layer
  class StoryCosts:
      def __init__(self, state_dir: Path, *, min_samples: int, default_hours: Mapping[str, float],
                   size_scale: Mapping[str, float], now: Callable[[], float] = time.time) -> None
      def samples(self) -> dict[Size, list[float]]              # published stories' hours by size
      def median_hours(self, size: Size) -> tuple[float, str]
          # (hours, how): "median of N S stories" when N >= min_samples; else "overall median x scale" when
          # any story is published; else "default"
      def calibration(self) -> tuple[float, str]
          # ratio of the sum of actuals to the sum of estimates over epics whose estimate has `actual`;
          # (1.0, "uncalibrated") under 3 such epics
      def estimate(self, sketch: Sequence[StorySketch]) -> Estimate
          # hours = calibration x sum(count(size) x median_hours(size)); basis is one line, for example
          # "3 S at 1.1 h (median of 7), 2 M at 5.0 h (default), x1.2 calibration (4 epics)"
  def queue_hours(store: BoardStore, state_dir: Path) -> float
      # for every epic approved or doing: its latest estimate (revised, else hours) x the share of its
      # stories (the epic's children on the board) not done; 0.0 with none
  ```
- [ ] **Step 1: Failing tests** (synthetic `stories/<key>/{state.json,events.jsonl}` under `tmp_path`):
  `test_a_published_storys_wall_clock_sums_its_runs_and_an_unpublished_one_counts_for_nothing`,
  `test_a_storys_size_is_its_cards_or_its_layer_count`,
  `test_the_median_by_size_falls_back_to_the_scaled_overall_median_then_to_the_defaults_and_says_which`,
  `test_an_estimate_is_the_calibrated_sum_with_its_arithmetic_in_the_basis`,
  `test_calibration_is_one_until_three_epics_have_actuals`,
  `test_queue_hours_counts_the_undone_share_of_approved_and_doing_epics`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: a story sketch is priced from the ledger's medians, calibrated`

---

### Task 3: The question stage (D2, D9)

**Files:**
- Create: `src/airlie/story/initiative_state.py`, `src/airlie/story/initiative_pipeline.py`,
  `src/airlie/prompts/rules/specs-questions.md`
- Modify: `src/airlie/story/initiative.py` (the question contract), `src/airlie/story/briefs.py`
  (`questions_brief`), `src/airlie/story/asking.py` (`Asker.conversation`, `images`),
  `tests/unit/test_prompt_texts.py` (CASES)
- Test: `tests/unit/test_story_initiative_pipeline.py`, `tests/unit/test_story_briefs.py`

**Interfaces:**
- Consumes: P3's `Asker(cfg, studio, repo)` with `_ask(st, stage, role, brief, at=None, steps=None)` and the
  `Recorder` protocol (`runs`, `save()`, `emit()`, `card`, `base`); `find_checkout(root, github_repo)`.
- Produces:
  ```python
  QUESTION_CONTRACT: str      # D2: {"type": "question", "text": "..."} or {"type": "questions_done", "assumptions": ["..."]}
  @dataclass(frozen=True)
  class QuestionReply: question: str | None; assumptions: tuple[str, ...]
  def parse_question_reply(obj: dict | None) -> QuestionReply
      # StoryError: no JSON; a type that is neither; a question that is empty, over 600 characters, or holds
      # two question marks (one question at a time); assumptions not strings
  ```
  ```python
  @dataclass
  class InitiativeState:              # initiatives/<key>/state.json, events.jsonl beside it
      path: Path; card: dict[str, Any]; repos: list[str]
      stage: str = "questions"        # questions | imagining | reading | delivered | blocked
      qa: list[dict[str, Any]] = ...  # {"q": str, "asked_at": float, "a": str | None, "answered_at": float | None}
      assumptions: list[str]; consultations: list[dict[str, Any]]; surveys: dict[str, str]
      mockups: dict[str, dict[str, Any]]; epics: list[dict[str, Any]]; revisions: dict[str, int]
      runs: list[str]; base: dict[str, str] | None; blocked: str | None; delivered_at: float | None
      open(path, item: Item, repos) / save() / emit(kind, **fields) as StoryState has them
      def pending_question(self) -> dict | None     # the last qa entry with no answer
  ```
  ```python
  def questions_brief(initiative: Initiative, readmes: Mapping[str, str], qa: Sequence[Mapping[str, Any]],
                      *, max_questions: int, feedback: str | None) -> str
      # specs-questions (the rules: one open question about a real moment; only what the code and README
      # cannot answer; no menus unless the person is stuck; stop when everything that matters is in the
      # request; at the cap, state assumptions and go on), the initiative quoted as data (_quote), each
      # README's first 4,000 characters, the budget line when any, the Q&A so far, the contract, _again(feedback)
  ```
  ```python
  class InitiativePipeline:
      def __init__(self, cfg: AirlieConfig, *, board: LocalBoard, studio: StudioFactory,
                   repo_of: Callable[[str], Path | None], costs: StoryCosts, uploads: UploadStore | None = None) -> None
      async def turn(self, item: Item) -> InitiativeState
          # the phase's next piece for this initiative: Task 3 does the questions stage; Tasks 4-6 add the rest
      async def revise(self, epic: Item) -> InitiativeState    # Task 7
  ```
  `turn` in the questions stage: open the state (`initiatives/<key>`); `repo = repo_of(repositories[0])`,
  none → `blocked` with "no checkout under story.repos_root for owner/name" on the card; when the state has a
  pending question, the newest person comment on the card newer than `asked_at` is its answer (recorded,
  event `answer`), else return (nothing to do); then: `skip_questions`, or the answer ends the questions
  (`answer_ends_questions`), or `len(qa) >= max_questions` → stage `imagining` (event `questions_done`,
  with `reason`); else one `Asker._ask(st, "questions", product_role, questions_brief(...),
  steps=product_steps)` through `_usable` (`attempts`): a question → `qa.append`, comment by `product_role`
  (the first one followed by one line saying "go" skips the rest), card → `review`, event `question(n)`;
  `questions_done` → `assumptions`, stage `imagining`. The initiative's first turn also moves it `doing`.
- `Asker.conversation(st, stage, role, brief, *, steps, at=None, images=()) -> AsyncContextManager[Say]` where
  `Say = Callable[[str | None], Awaitable[str]]`: the first call runs the subtask (`execute_task`, detached,
  `max_steps=steps`), each later call `follow_up(stage, nudge)`; the worktree is removed on exit;
  `_ask(...)` gains `images: Sequence[Path] = ()` passed to `execute_task`. Task 4 uses the conversation.
- [ ] **Step 1: Failing tests:** `test_the_questions_brief_carries_the_initiative_the_readmes_the_budget_and_the_qa`,
  `test_a_question_is_posted_as_products_comment_and_the_card_waits_in_review`,
  `test_an_answer_newer_than_the_question_is_recorded_and_the_next_question_is_asked`,
  `test_a_reply_of_go_ends_the_questions_and_the_imagining_starts_on_the_same_turn`,
  `test_the_cap_ends_the_questions_with_products_assumptions`,
  `test_an_initiative_whose_repository_has_no_checkout_blocks_with_a_note`,
  `test_a_two_question_reply_is_feedback_then_the_turn_stops_with_the_reason`.
  The pipeline test helper `initiative_pipeline(tmp_path, repo, engine, *, board)` mirrors `pipeline(...)` in
  `test_story_pipeline.py` (a `LocalBoard` over a `BoardStore` in `tmp_path`, `FakeEngine` scripts per role).
- [ ] **Step 2: Implement; CASES for `specs-questions` and `questions_brief`; snapshot. Step 3: Gate; commit**
  `story: product asks the initiative's questions one at a time, as card comments`

---

### Task 4: Imagining in rounds, the consultations and the surveys (D3, D5's contract)

**Files:**
- Create: `src/airlie/prompts/rules/specs-imagine.md`, `src/airlie/prompts/rules/specs-consult.md`
- Modify: `src/airlie/story/initiative.py` (the round contracts, `parse_epics`, `check_epics`),
  `src/airlie/story/epic.py` (P3's: `Viability`, `Decision` beside `EpicCriterion`),
  `src/airlie/story/initiative_pipeline.py` (`_imagine`), `src/airlie/story/briefs.py` (`imagine_brief`,
  `consult_brief`), `tests/unit/test_prompt_texts.py`
- Test: `tests/unit/test_story_initiative.py`, `tests/unit/test_story_initiative_pipeline.py`,
  `tests/unit/test_story_briefs.py`

**Interfaces:**
- Consumes: `LessonsStore(state_dir, repo).recent(n=5)` (P1); `survey_brief(card)` and `SURVEY_STEPS`
  (`pipeline.py`); `Asker.conversation` (Task 3).
- Produces:
  ```python
  @dataclass(frozen=True)
  class Ask: seat: str; question: str; kind: Literal["question", "mockups"] = "question"; epic: str | None = None
  @dataclass(frozen=True)
  class Decision: what: str; why: str
  @dataclass(frozen=True)
  class Viability: verdict: Literal["viable", "risky", "not_viable"]; reasons: tuple[str, ...]
  @dataclass(frozen=True)
  class EpicDraft:
      id: str; kind: Literal["core", "extension"]; title: str; why: str
      goals: tuple[str, ...]; non_goals: tuple[str, ...]; anti_goals: tuple[str, ...]
      decisions: tuple[Decision, ...]; constraints: tuple[str, ...]; criteria: tuple[EpicCriterion, ...]
      repositories: tuple[str, ...]; stories: tuple[StorySketch, ...]; risks: tuple[str, ...]
      viability: Viability; needs_mockups: bool; depends_on: tuple[str, ...]; open: tuple[str, ...]
      def to_dict / from_dict
  @dataclass(frozen=True)
  class EpicsReply: summary: str; epics: tuple[EpicDraft, ...]
      def order(self) -> tuple[EpicDraft, ...]             # the core first, then extensions in dependency order
  CONSULT_CONTRACT: str; EPICS_CONTRACT: str                 # D3's and D5's JSON, verbatim; the reply is one or the other
  def parse_imagine_reply(obj: dict | None, *, initiative: Initiative, consultable: Sequence[str],
                          max_asks: int) -> tuple[Ask, ...] | EpicsReply
      # StoryError: no JSON; a type that is neither consult nor epics; a seat not in consultable; more than
      # max_asks asks; a mockups ask without epic or brief; an empty question; else parse_epics
  def parse_epics(obj: dict, *, initiative: Initiative, single: bool = False) -> EpicsReply
      # D5's rules as StoryError text product can act on: exactly one core (any kind alone when single);
      # ids E<n> unique; depends_on names sibling ids, acyclic, never the core on an extension; a criterion
      # each with unique ids; every story has a size in S|M|L; a verdict in the three with a reason; every
      # repository among the initiative's; title, why non-empty
  def check_epics(reply: EpicsReply, *, budget_hours: float | None, estimate: Callable[[Sequence[StorySketch]], Estimate]) -> list[Finding]
      # Finding as P3's check_story_plan returns it; over_budget: the core's estimate over the budget (one
      # finding, the hours and the budget named)
  ```
  ```python
  def imagine_brief(initiative: Initiative, qa, assumptions, *, surveys: Mapping[str, str], lessons: Sequence[Lesson],
                    consultable: Sequence[str], max_asks: int, rounds_left: int, budget_hours: float | None,
                    feedback: str | None, attachments: Sequence[str] = ()) -> str
      # specs-imagine (the seats and when each helps; the consult protocol; think wide, drop the weak; the
      # epic's parts; core and extensions; viability with reasons; the sketch and its sizes; the budget as
      # the core's ceiling; at the last round reply with the epics), the initiative, the Q&A and assumptions,
      # each repository's survey notes, the lessons (brief-layers-lessons' lines), the attachments by name,
      # CONSULT_CONTRACT and EPICS_CONTRACT, _again(feedback)
  def consult_brief(initiative: Initiative, qa, ask: Ask, *, chars: int) -> str
      # specs-consult (answer from the code and your craft; bullets; say what you could not find; the cap),
      # the initiative, the Q&A, product's question quoted as data
  def consult_round_text(answers: Sequence[tuple[Ask, str]], *, chars: int) -> str
      # the follow-up nudge: "## From <seat>" sections, each _clip(answer, chars), then "Reply with the next
      # round or the epics"
  ```
  `_imagine(st, initiative)`: the surveys start first, one `Asker._ask(st, f"survey:{repo}", architect_role,
  survey_brief(initiative.card()), steps=SURVEY_STEPS)` per repository on that repository's `Asker`
  (`repo_of`), gathered with `asyncio.gather(return_exceptions=True)`, a failure recorded as `""` (event
  `survey_failed`); product's conversation opens with `imagine_brief(...)` (the surveys that have finished
  are in it; the rest join the first follow-up); each reply is parsed by `parse_imagine_reply`, a
  `StoryError` goes back as a follow-up with the reason (`attempts` per round); a round of asks runs them
  side by side (`asyncio.gather`, each a `_ask(st, f"consult:{seat}", seat, consult_brief(...),
  steps=consult_steps)`; a `mockups` ask is Task 5's `_mockups`), records each as a consultation (seat,
  question, answer, steps, seconds, round; event `consult`), stops at `max_consultations` (the rest refused
  in the nudge) and follows up with `consult_round_text`; at `max_rounds` the nudge says the epics are due;
  an `EpicsReply` ends the stage with `st.epics = [d.to_dict() ...]` (event `epics_drafted`); `check_epics`
  findings go back once as a follow-up, then the reply stands with the overrun named. The conversation stays
  open for Task 6's read (the pipeline keeps the `Say` until `_deliver`).
- [ ] **Step 1: Failing tests:** `test_a_consult_round_parses_and_a_flawed_one_is_refused_with_a_reason`
  (parametrised: unknown seat, too many asks, mockups without epic, empty question),
  `test_an_epics_reply_parses_in_order_and_the_d5_rules_are_enforced` (parametrised: two cores, no core,
  repeated id, a cycle, the core depending on an extension, no criterion, a story without a size, a bad
  verdict, a repository not the initiative's), `test_a_core_over_the_budget_is_one_finding`,
  `test_the_imagine_brief_carries_the_qa_the_surveys_the_lessons_the_seats_and_both_contracts`,
  `test_the_surveys_run_for_every_repository_beside_the_first_round`,
  `test_a_round_of_asks_runs_the_seats_side_by_side_and_their_answers_come_back_as_one_follow_up`,
  `test_an_ask_for_a_seat_not_consultable_is_feedback_and_runs_no_turn`,
  `test_the_consultation_cap_refuses_the_rest_and_the_last_round_demands_the_epics`,
  `test_two_asks_at_once_leave_no_stray_worktree` (real git in `tmp_path`, `git_helpers`).
- [ ] **Step 2: Implement; CASES for `specs-imagine`, `specs-consult`, `imagine_brief`, `consult_brief`;
  snapshot. Step 3: Gate; commit** `story: product imagines the epics in rounds, consulting the seats side by side`

---

### Task 5: Mockups: three directions, three pages, checked (D4, D13, D14)

**Files:**
- Create: `src/airlie/story/mockups.py`, `src/airlie/prompts/rules/specs-mockup-directions.md`,
  `src/airlie/prompts/rules/specs-mockup-render.md`, `src/airlie/prompts/rules/specs-mockup-look.md`
- Modify: `src/airlie/story/initiative_pipeline.py` (`_mockups`), `src/airlie/story/briefs.py`
  (`directions_brief`, `render_brief`), `tests/unit/test_prompt_texts.py`
- Test: `tests/unit/test_story_mockups.py`, `tests/unit/test_story_initiative_pipeline.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Direction: name: str; idea: str; screen: str; differs: str
  DIRECTIONS_CONTRACT: str      # D4's JSON, verbatim
  def parse_directions(obj: dict | None, *, count: int) -> tuple[Direction, ...]
      # StoryError: not exactly count; a part empty; two names alike (casefold)
  FORBIDDEN = (r"<script\b", r"<link\b", r"@import\b", r"\b(?:https?:)?//[^\s\"')]+", r"<iframe\b", r"<object\b", r"<embed\b")
  def check_page(html: str) -> list[str]           # one finding per forbidden match, with the matched text; empty when clean
  def page_from_reply(text: str) -> str            # the last ```html fence's body, else the text from <!doctype or <html
  def mockup_dir(state_dir: Path, initiative_key: str, epic_id: str) -> Path     # initiatives/<key>/mockups/<epic id>
  def option_name(n: int) -> str                   # "option-<n>.html"; OPTION_FILE = re.compile(r"option-[1-9]\.html")
  def mockup_look(repo: Path | None) -> str        # <repo>/docs/design/look.md when it exists, else hub_text("specs-mockup-look")
  def pick_from_comment(text: str) -> int | None   # "option 2", "Option 2", "#2 please": the first 1-9 after "option"
  ```
  ```python
  def directions_brief(initiative: Initiative, qa, ask: Ask, *, count: int, attachments: Sequence[str] = ()) -> str
      # specs-mockup-directions (count directions that differ in flow or layout, not colour; each a screen
      # top to bottom; name what differs), the initiative, the Q&A, product's brief for the screen, the contract
  def render_brief(direction: Direction, look: str, *, max_chars: int = 12_000) -> str
      # specs-mockup-render (one page, self-contained, inline CSS only, no script, no link, no network, no
      # image but data:, the size cap, reply with the page in one ```html fence), the look, the direction
  ```
  `_mockups(st, initiative, ask)`: ux's directions through `_usable` (`_ask(st, f"directions:{ask.epic}",
  "ux", directions_brief(...), images=<the initiative's picture attachments>)`); then one design
  `_ask(st, f"render:{ask.epic}:{n}", "design", render_brief(d, mockup_look(repo)))` per direction, side by
  side; `check_page` on each: a page with findings goes back once with them (`_again`), a page still failing
  is dropped (event `mockup_dropped(epic, n, findings)`); the pages that pass are written to
  `mockup_dir(...)/option-<n>.html`; `st.mockups[ask.epic] = {"directions": [...], "options": [names]}`;
  event `mockups(epic, options)`; the consultation's answer to product is the directions' names and ideas
  with the count of pages drawn. The `specs-mockup-look` text carries the `:root` tokens of
  `src/airlie/serve/web/app.css` (`--bg #000000`, `--surface #0e1013`, `--ink #f2f4f7`, `--accent #8fb6e0`,
  `--line #2b2f36`, the ok/warn/err pairs) and "dark by default, system type stack".
- [ ] **Step 1: Failing tests:** `test_three_directions_parse_and_fewer_or_alike_ones_are_refused`,
  `test_a_page_with_a_script_or_an_outside_url_is_never_kept` (parametrised over FORBIDDEN; the directory
  holds only the clean pages), `test_a_pages_findings_go_back_to_design_once_then_the_option_is_dropped`,
  `test_the_look_is_the_repositorys_file_when_it_has_one_else_the_hub_text`,
  `test_a_pick_is_read_from_a_comment`, `test_a_mockups_ask_draws_ux_directions_then_one_page_each_side_by_side`.
- [ ] **Step 2: Implement; CASES for the three hub texts and both briefs; snapshot. Step 3: Gate; commit**
  `story: three mockup directions become three checked pages`

---

### Task 6: The planner's read, the price and the cards (D5's card, D6, D7, D8's birth, D9)

**Files:**
- Create: `src/airlie/prompts/rules/specs-read.md`
- Modify: `src/airlie/story/initiative.py` (`ReadReply`, `parse_read`, `render_epic_document`,
  `EPIC_SECTIONS`), `src/airlie/story/epic.py` (`EpicSpec`'s optional fields, `Mockups`),
  `src/airlie/story/initiative_pipeline.py` (`_read`, `_deliver`), `src/airlie/story/briefs.py`
  (`read_brief`, `read_back_text`, `delivery_note`), `tests/unit/test_prompt_texts.py`
- Test: `tests/unit/test_story_initiative.py`, `tests/unit/test_story_epic.py`,
  `tests/unit/test_story_initiative_pipeline.py`

**Interfaces:**
- Consumes: P3's `EpicSpec(repositories, criteria, constraints, links)`, `parse_epic`, `epic_of`;
  `StoryCosts.estimate`, `queue_hours` (Task 2).
- Produces:
  ```python
  # epic.py (P3's), extended; parse_epic keeps every key it does not know and reads these when present
  @dataclass(frozen=True)
  class Mockups: options: tuple[str, ...]; picked: int | None
  class EpicSpec:  # new optional fields, defaults None/()
      initiative: str | None; kind: str | None; depends_on: tuple[str, ...]; stories: tuple[StorySketch, ...]
      estimate: Estimate | None; viability: Viability | None; mockups: Mockups | None
  ```
  ```python
  READ_CONTRACT: str            # D7: {"type": "read", "epic": "E1", "ok": true, "questions": ["..."]}
  @dataclass(frozen=True)
  class ReadReply: epic: str; ok: bool; questions: tuple[str, ...]
  def parse_read(obj: dict | None, *, epic: str) -> ReadReply       # StoryError: wrong epic id; ok false with no question
  EPIC_SECTIONS = ("Why", "Goals", "Non-goals", "Anti-goals", "Decisions", "Constraints", "Acceptance criteria",
                   "Stories, a sketch", "Risks", "Viability", "Estimate", "Consulted", "Questions asked", "Open for you")
  def render_epic_document(draft: EpicDraft, *, estimate: Estimate, consulted: Sequence[Mapping[str, Any]],
                           qa: Sequence[Mapping[str, Any]], option_urls: Sequence[str], initiative_key: str,
                           depends_keys: Sequence[str]) -> str
      # the sections in EPIC_SECTIONS order (a section with nothing says "none"), the estimate's hours and
      # basis, the consultations as "<seat>: <question> → <first line of the answer>", the Q&A verbatim,
      # then the JSON block: EpicSpec(...).to_dict() with P4's fields, links = option_urls
  ```
  ```python
  def read_brief(draft_document: str) -> str       # specs-read (you are the planner who will write stories from this; could you, without the person?), the document as data, READ_CONTRACT
  def read_back_text(reads: Sequence[ReadReply]) -> str      # the nudge to product: each epic's questions; answer into the epic or leave it in `open`; reply with the epics
  def delivery_note(epics: Sequence[tuple[str, EpicDraft, Estimate]], *, queue_hours: float, budget_hours: float | None) -> str
      # one line per epic: key, kind, title, hours, verdict; then "Queued ahead: N h"; the budget line when any
  ```
  `_read(st, reply)`: per draft, `_ask(st, f"read:{id}", architect_role, read_brief(render_epic_document(...
  without key)), steps=read_steps)` side by side, parsed; events `planner_read(epic, questions)`; when any
  questions, one follow-up to product's open conversation with `read_back_text` → a new `EpicsReply`
  (parsed and checked as in Task 4, `attempts`); the conversation then closes. `_deliver(st, reply)`: in
  `reply.order()`, `estimate = costs.estimate(draft.stories)`; `BoardStore.create(level="epic",
  parent=initiative.key, title=draft.title, description=render_epic_document(...), labels=[f"repo:{r}" ...,
  f"initiative:{key}", f"kind:{draft.kind}", *(f"after:{<core's key>}" for each depends_on)])` then
  `update(key, status="review")`; `option_urls` are `/v1/airlie/initiatives/<key>/mockups/<id>/<file>`;
  `st.epics[i]["key"]`, `["estimate"]`; the initiative gets the `delivery_note` comment by `product_role`
  and moves to `review`; `stage = "delivered"`, `delivered_at`; event `epics(keys, kinds, hours)`.
- [ ] **Step 1: Failing tests:** `test_a_p4_epic_block_round_trips_through_epic_spec_and_a_p3_block_still_parses`,
  `test_the_epic_document_has_every_section_in_order_and_closes_with_the_block`,
  `test_the_planners_questions_go_back_to_product_once_and_the_rest_land_in_open`,
  `test_the_epics_land_under_the_initiative_core_first_each_in_review_with_its_hours_and_the_queue_in_the_note`,
  `test_a_not_viable_core_is_written_and_marked_never_hidden`,
  `test_an_extension_carries_an_after_label_naming_the_core`.
- [ ] **Step 2: Implement; CASES for `specs-read` and the three brief functions; snapshot. Step 3: Gate; commit**
  `story: the architect reads each epic as its planner; the epics land priced under the initiative`

---

### Task 7: The person's turn: the pick, the revision, the held extension, the estimate closes (D4, D6, D8, N7)

**Files:**
- Create: `src/airlie/prompts/rules/specs-revise.md`
- Modify: `src/airlie/story/initiative.py` (`REVISION_CONTRACT`, `parse_revision`),
  `src/airlie/story/initiative_pipeline.py` (`revise`), `src/airlie/story/briefs.py` (`revise_brief`),
  `src/airlie/story/mockups.py` (`record_pick`), `src/airlie/story/epic_pipeline.py` (P3's: `plan` writes
  `estimate.revised`; the epic step's two checks), `src/airlie/story/factory.py` (the tick's epic step and
  the estimate's close), `src/airlie/story/local_board.py` (`epics_awaiting_revision`, `children`),
  `tests/unit/test_prompt_texts.py`
- Test: `tests/unit/test_story_initiative_pipeline.py`, `tests/unit/test_story_epic_pipeline.py`,
  `tests/unit/test_story_factory.py`, `tests/unit/test_story_mockups.py`

**Interfaces:**
- Produces:
  ```python
  REVISION_CONTRACT: str        # {"type": "revision", "epic": {<EpicDraft>}, "redraw_mockups": false, "changed": "one line"}
  @dataclass(frozen=True)
  class Revision: epic: EpicDraft; redraw_mockups: bool; changed: str
  def parse_revision(obj: dict | None, *, initiative: Initiative, id: str) -> Revision    # the draft via parse_epics(single=True); the id must match
  def revise_brief(initiative: Initiative, document: str, consulted: Sequence[Mapping[str, Any]], comment: str, *, feedback: str | None) -> str
      # specs-revise (the person commented; change the epic to answer it, nothing else; say in one line what
      # changed; set redraw_mockups when the comment is about the screens), the document, the consultations'
      # answers, the comment quoted as data, the contract, _again(feedback)
  def record_pick(store: BoardStore, state_dir: Path, epic_key: str, option: int) -> EpicSpec
      # rewrites the card's JSON block with mockups.picked, adds the label mockup:<n> (replacing an older one),
      # emits `pick(epic, option)` on the initiative's events; StoryError when the epic has no such option
  ```
  `InitiativePipeline.revise(epic)`: the epic's initiative from its `initiative:` label; the newest person
  comment on the epic newer than the card's last product note; `pick_from_comment` first (a pick is
  `record_pick`, not a revision); `revisions[key] >= revisions` → one note saying the next comment waits for
  P5, done; else `revise_brief` through `_usable` on a fresh `_ask(st, f"revise:{key}", product_role, ...,
  steps=product_steps)`; the draft re-priced and re-rendered (`BoardStore.update(key, title, description)`),
  `redraw_mockups` → `_mockups` again with the comment as the brief and the links replaced; comment "Revised:
  <changed>" by `product_role`; event `revision(epic, changed, redrawn)`; `revisions[key] += 1`.
- `LocalBoard.epics_awaiting_revision() -> list[Item]`: epics in `review` with an `initiative:` label whose
  newest comment is a person's and newer than the newest role comment; `LocalBoard.children(key) -> list[Item]`.
- The tick's epic step (P3 Task 7's `next_epic`) refuses two cases before planning, each with one note on the
  epic and no status change: `EpicSpec.mockups` with options and `picked is None` ("pick a mockup first");
  an `after:` label whose epic is not `review` or `done` ("waits for <key>", N7). `EpicPipeline.__init__`
  gains `costs: StoryCosts | None = None`, and `plan` writes `estimate.revised = costs.estimate(<planned
  stories' sizes>).hours` into the card's block when the epic has an estimate and `costs` is given. The tick's bookkeeping, when it moves an epic to `done` (P3 Task 7): `estimate.actual =
  sum(story_wall_clock(...) for its stories)`; when actual is over 1.5x or under 0.5x `hours`, event
  `estimate_missed` on the initiative and a comment there naming both numbers.
- [ ] **Step 1: Failing tests:** `test_a_persons_comment_on_an_epic_is_one_revision_then_a_note_says_the_next_waits`,
  `test_a_revision_about_the_screens_redraws_three_options`, `test_an_option_comment_is_a_pick_not_a_revision`,
  `test_record_pick_writes_the_block_the_label_and_the_event`,
  `test_an_approved_epic_with_options_and_no_pick_is_not_planned_and_stays_in_review`,
  `test_an_approved_extension_waits_with_a_note_until_its_core_is_in_review`,
  `test_planning_an_epic_revises_its_estimate_and_finishing_it_closes_it_and_flags_a_miss`.
- [ ] **Step 2: Implement; CASES for `specs-revise` and `revise_brief`; snapshot. Step 3: Gate; commit**
  `story: a comment on an epic is one revision, a pick is recorded, a held extension waits, the estimate closes`

---

### Task 8: Attachments reach the briefs (D12)

**Files:**
- Modify: `src/airlie/story/initiative_pipeline.py` (`_attachments`), `src/airlie/story/briefs.py`
  (`questions_brief`, `imagine_brief`, `directions_brief` take `attachments: Sequence[Attachment]`),
  `src/airlie/story/initiative.py` (`Attachment`)
- Test: `tests/unit/test_story_initiative_pipeline.py`, `tests/unit/test_story_briefs.py`

**Interfaces:**
- Consumes: `UploadStore.get(id) -> Upload | None`, `UploadStore.path(upload)`, `as_parts(store, upload,
  media=...)`, `Upload.kind`, `MEDIA` (`serve/uploads.py`).
- Produces:
  ```python
  @dataclass(frozen=True)
  class Attachment: name: str; kind: str; text: str | None; path: Path | None
      # text: a text or PDF attachment's text as as_parts renders it, clipped to 6,000 characters; path: a picture's file
  def _attachments(self, initiative: Initiative) -> tuple[Attachment, ...]
      # each upload id in the initiative through self.uploads (none → ()); an id the store lacks is an
      # Attachment with text "[attachment is no longer available]"; the initiative's uploads are the person's
      # own, so there is no owner check here (the card was theirs to write)
  ```
  The briefs list attachments by name; text attachments are quoted as data in product's briefs; pictures go
  as `images=` to ux's and design's asks (Task 5) and are named, not shown, in product's.
- [ ] **Step 1: Failing tests:** `test_a_text_attachment_is_quoted_in_products_brief_and_a_picture_is_named`,
  `test_a_picture_attachment_reaches_ux_as_an_image`, `test_a_missing_upload_is_a_placeholder_not_a_stop`.
- [ ] **Step 2: Implement; snapshot (the briefs' CASES gain one attachment). Step 3: Gate; commit**
  `story: an initiative's attachments are read into the briefs`

---

### Task 9: The turn route and job, the tick's fallback, the CLI, the chief, the Chat skill (D1, D10, D15, N6)

**Files:**
- Create: `skills/chat/initiative.md`
- Modify: `src/airlie/story/factory.py` (`initiative_job`, `tick`), `src/airlie/story/state.py`
  (`lock_held`), `src/airlie/serve/app.py` (`POST /v1/airlie/initiatives`, `POST
  /v1/airlie/initiatives/{key}/turn`), `src/airlie/serve/mcp_server.py` (`initiative`, `answer`),
  `src/airlie/cli.py` (`initiative` group: `add`, `turn`), `Taskfile.yml` (`initiative`, `initiative:turn`),
  `docs/integrations/opencode/agent/chief.md` (the two tools), `prompts/roles/product.md`, `prompts/roles/ux.md`,
  `prompts/roles/design.md`, `prompts/roles/architect.md` (a paragraph each on the specs phase)
- Test: `tests/unit/test_story_factory.py`, `tests/unit/test_serve.py`, `tests/unit/test_mcp_server.py`,
  `tests/unit/test_cli_story.py`, `tests/unit/test_skills.py`

**Interfaces:**
- Produces:
  ```python
  def lock_held(stories_dir: Path) -> bool          # state.py: a non-blocking probe of story_lock's file
  async def initiative_job(cfg, *, key: str, running: Callable[[str], bool] | None = None
                           ) -> tuple[str, Path, Coroutine[Any, Any, dict[str, Any]]]
      # job id "initiative-<key>"; the events path initiatives/<key>/events.jsonl; StoryBusy when lock_held;
      # StorySetupError for story.board: jira, a key that is not an initiative, or nothing to do (no pending
      # answer, no new card); the work takes story_lock (StoryBusy again when lost to the tick meanwhile),
      # runs InitiativePipeline.turn and returns {"stage", "waiting_on": "person"|"line"|None, "question", "epics": [keys], "blocked"}
  ```
  Routes: `POST /v1/airlie/initiatives` `{title, text, repos: [owner/name], budget_hours?}` → `BoardStore.create(level="initiative",
  labels=[repo:..., budget:<h>h], created_by=<principal>)`, `update(status="approved")`, then the turn as
  below → 202 `{"key", "job_id"}`; `POST /v1/airlie/initiatives/{key}/turn` → 202 `{"job_id"}`, 409 with
  `{"error": "busy: a story is in flight; the tick picks AIR-40 up within 15 minutes"}` on `StoryBusy`, 400 on
  `StorySetupError`. The jobs registry keys the job as `story_job` does.
- `tick`, before P3's epic step and after the engine and GitHub checks: (a) an initiative in `review` with
  `stage == "questions"` and a pending question whose card's newest comment is a person's newer than it →
  `InitiativePipeline.turn`; (b) `epics_awaiting_revision()` → `revise` on the first; (c)
  `next_initiative()` with no state → `turn`. Each is the tick's one piece of work; the log line names it
  (`AIR-40 asked question 3`, `AIR-41 revised`, `AIR-40 delivered 3 epics (14 h)`). Bookkeeping on every
  tick, beside P3's: an initiative in `review` with `stage == "delivered"` whose every child epic is `done`
  or `todo` (parked) is moved to `done` (D8); one with no child epic is left alone.
- CLI: `airlie initiative add --text TEXT | --file PATH --repo owner/name [--repo ...] [--budget HOURS]
  [--title TEXT]` creates and approves the card and runs the first turn in-process under the lock (as `task
  story -- KEY` runs a story), printing the key and the question or the delivery; `airlie initiative turn
  KEY` runs the next turn (for an answer given on the board while serve is down); `task initiative -- ...`,
  `task initiative:turn -- KEY`.
- MCP: `initiative({text, repo, budget})` → `POST /v1/airlie/initiatives` (title = the first line) →
  `_started(job_id)`; `answer({card, text})` → `POST /v1/board/items/{key}/comments` `{author: <the
  principal's name>, text}` then `POST /v1/airlie/initiatives/{key}/turn`, the 409 line returned as the
  tool's text; `status` follows the job; the chief's prompt gains the two tools and when to use them.
- `skills/chat/initiative.md` (`summary`, `action: initiative`): the twin of `/story`: turn the conversation
  into an initiative text (first line the title; the ask; the repositories; the budget when the person named
  one), ask one question only when no outcome is named, say the person edits and presses Start.
- [ ] **Step 1: Failing tests:** `test_post_initiatives_makes_an_approved_card_and_starts_its_first_turn`,
  `test_the_turn_route_answers_busy_while_the_lock_is_held_and_the_tick_then_runs_the_turn_once`,
  `test_the_tick_resumes_an_answered_question_revises_a_commented_epic_and_starts_a_new_initiative_in_that_order`,
  `test_the_tick_ignores_a_persons_comment_on_a_delivered_initiative`,
  `test_an_initiative_is_done_when_its_epics_are_done_or_parked`,
  `test_the_server_lists_seven_tools_with_these_arguments` (replacing the five-tool test),
  `test_answer_posts_the_comment_then_runs_the_turn_and_relays_busy`,
  `test_initiative_add_creates_approves_and_asks_the_first_question`, `test_the_initiative_skill_loads_with_its_action`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story, serve: an initiative's turn runs at once through a route, the tick is the fallback`

---

### Task 10: The flow API, the mockup and pick routes, the docs, one real initiative (D4, D9, D13)

**Files:**
- Modify: `src/airlie/serve/flow.py` (`initiative_flow`), `src/airlie/serve/listing.py` (`initiatives`),
  `src/airlie/serve/app.py` (`GET /v1/airlie/initiatives`, `GET /v1/airlie/initiatives/{key}/flow`, `GET
  /v1/airlie/initiatives/{key}/mockups/{epic}/{file}`, `POST /v1/airlie/epics/{key}/mockup`),
  `docs/integrations/story.md` (an *Initiatives* section: the card, the questions, the seats, the mockups,
  the price and its arithmetic, the routes, the config table), `README.md` (the task rows), the design's
  Status line
- Test: `tests/unit/test_story_flow.py`, `tests/unit/test_serve.py`, `tests/unit/test_serve_listing.py`

**Interfaces:**
- Produces:
  ```python
  def initiative_flow(state_dir: Path, store: BoardStore, key: str) -> dict[str, Any]
      # {"key", "title", "stage", "blocked", "waiting_on": "person" | "line" | None, "question": str | None,
      #  "budget_hours", "qa": [{"q", "a", "asked_at", "answered_at"}], "assumptions": [...],
      #  "consultations": [{"seat", "question", "answer", "seconds", "round"}],
      #  "epics": [{"key", "id", "kind", "title", "status", "hours", "basis", "revised", "actual", "verdict",
      #             "options": [urls], "picked", "depends_on": [keys]}],
      #  "queue_hours": float}
  def initiatives(state_dir: Path, store: BoardStore) -> list[dict[str, Any]]   # key, title, stage, epics count, hours, waiting_on
  ```
  The mockup route: `file` must match `OPTION_FILE` and `epic` the id pattern `E[0-9]+`, else 404; the file
  under `mockup_dir(...)`; `FileResponse(media_type="text/html", headers={"Content-Security-Policy":
  "default-src 'none'; style-src 'unsafe-inline'; img-src data:", "X-Content-Type-Options": "nosniff"})`. The
  pick route: `{"option": int}` → `record_pick` → the `EpicSpec` as JSON; 404 for an unknown epic, 400 for
  an option it has not got.
- [ ] **Step 1: Failing tests:** `test_the_initiative_flow_shows_the_dialogue_the_consultations_the_epics_and_the_queue`,
  `test_a_mockup_page_is_served_locked_down_and_an_odd_path_is_not_served`,
  `test_a_pick_is_recorded_through_the_route`, `test_initiatives_are_listed`.
- [ ] **Step 2: Implement. Step 3: Docs; gate; commit** `serve, docs: initiatives in the flow API, the mockup and pick routes`
- [ ] **Step 4 (with Noah's go, engine idle): `task initiative -- --text "<a real ask>" --repo weyenk/airlie`;
  answer its questions on the board; let it deliver; pick a mockup if it drew any; record the questions,
  the consultations, each epic's hours and basis, the wall-clock and tokens per stage in
  `docs/bench/2026-10-25-specs-phase.md`; open the PR with the numbers in its body.**

---

## Self-review notes

- Spec coverage: D1 Tasks 1, 9 (the card, the doors); D2 Task 3; D3 Task 4; D4 Tasks 5, 7, 10; D5 Tasks 4,
  6; D6 Tasks 2, 7; D7 Task 6; D8 Tasks 6, 7; D9 Tasks 3, 10; D10 Tasks 7, 9; D11 Tasks 3–7 (eight hub
  texts: `specs-questions`, `specs-imagine`, `specs-consult`, `specs-mockup-directions`,
  `specs-mockup-render`, `specs-mockup-look`, `specs-read`, `specs-revise`; the design's `specs-document`
  is `EPIC_SECTIONS` in code, read by the renderer, and the imagine brief lists the parts through
  `EPICS_CONTRACT`); D12 Task 8; D13 Tasks 5, 10; D14 Task 5; D15 and N6 Task 9; N7 Task 7.
- The design's `initiative_flow(state_dir, key)` takes the store too (the epics' statuses live on the
  board); the route constructs both.
- `Asker.conversation` and `_ask(images=)` are additions to P3's `asking.py`; nothing else of P1–P3 changes
  except `EpicSpec`'s optional fields, `parse_epic` keeping unknown keys, `EpicPipeline.plan` writing
  `estimate.revised`, and the two checks before the epic step.
- The product role's `max_tokens` and `max_context` change in `airlie.yaml` (Task 1) apply to every stage
  product serves; the story refine stage gains headroom it did not need, which is harmless.
