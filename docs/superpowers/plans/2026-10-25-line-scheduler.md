# The Line's Scheduler and Control Surface (P5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A story that blocks writes its own diagnosis from disk, classed by rules before Muse reads it,
files a fix card on airlie's board for airlie's own failures and resumes once the fix is in the code the
line runs; and one scheduler, `task line:tick`, runs every level in two lanes, build and planning, with
planning's long turns in the build's gaps, a pause switch, budgets recorded per stage, the stats that judge
whether stories go through unattended, and the rows the Vue app renders.

**Architecture:** P5a (Tasks 1–5) adds `airlie.story.diagnosis` (the evidence pack and the class, computable
from `state.json`, `events.jsonl` and the run directory alone, calibrated on the recorded blocks) and
`airlie.story.fixes` (the fix card, dedup by signature, the release after deploy, transient retries), wired
into `StoryPipeline._block`, the supervisor's ledger, a `line` CLI group, a route and a Chat skill. P5b
(Tasks 6–13) adds `airlie.story.line` (two lane locks, the beat, the build phase file, the gap wait, the
fan-out, the pause switch), splits `StoryPipeline.run` into `plan` and `build`, ticks from `task serve`,
records budgets (`airlie.story.budgets`), audits board moves and computes the stats (`airlie.story.stats`),
gates on the engine's profile in the doctor, and serves the line's rows.

**Tech Stack:** Python 3.12, uv, pydantic v2, FastAPI, SQLite (the board and the supervisor's ledger),
`fcntl` locks, git and `gh`, the prompt hub, pytest with `FakeEngine` and a fake board.

**Spec:** `docs/superpowers/specs/2026-10-25-line-scheduler-design.md` (N1–N5, D1–D12). P5a is built right
after P1 lands, on P1's block path (D12); P5b after P3 and P4, whose tick steps it consolidates. Task 6
reuses what the P3 and P4 plans named: `EpicPipeline.plan/replan`, `InitiativePipeline.turn/revise`,
`LocalBoard.next_epic`, `epics_awaiting_revision`, `next_initiative`, the pick and hold checks.

## Global Constraints

- Every change passes, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- uv only; Taskfile tasks; `Strict` config models; hub files for model-facing prose; the golden snapshot
  regenerated after any hub change (`AIRLIE_WRITE_PROMPT_TEXTS=1 uv run pytest tests/unit/test_prompt_texts.py
  -q`, then without).
- A diagnosis quotes traces and events as data; a fix card's text never reaches a shell; the home repository
  is `line.home_repo` or the checkout's remote, never a card's text; a path a route serves is under the
  state directory and matched by name.
- `BoardStore` and the board API are the only writers of cards. With `story.board: jira` the fix card is a
  note on the blocked issue and no label is written.
- The supervisor's ledger stays append-only: new columns arrive by `ALTER TABLE ... ADD COLUMN` on open.
- The two lane locks are the only global locks; a job holds exactly one. Only a human merges; the line
  never pulls its own checkout and never restarts the engine (the gate names the command).
- Event kinds are read as emitted today (`blocked`, `error`, `engine_error`, `fetch_failed`, `read_refused`,
  `write_deadline`, `repeated`, `fresh_continuation`, `subtask_done` with `stop_reason`, `step` with
  `prompt_tokens`, `completion_tokens`, `prefill_ms`, `gen_ms`); Task 1's first step lists them from
  `grep -rn "emit(" src/airlie` and the plan's names are corrected to match before any rule is written.
- No live engine, network or `gh` in tests; the lane jobs run on `FakeEngine`, the board is a `BoardStore`
  in `tmp_path`, `gh` is a recorded fake as `tests/unit/test_story_pipeline.py` has it.
- Nothing under `src/airlie/serve/web` changes; P5 adds routes and rows.
- Test first; commit per task; dense docstrings with the incident and date.

## Review Focus

1. **A traceback through airlie's code when Muse blames the tester** must class as `process`, Muse's reading
   kept beside it. Pinned in Task 2 (`test_a_traceback_in_airlie_outranks_muses_category`).
2. **The same bug after a fix attempt that moved lines** must link the open card, not file a second. Pinned
   in Task 2 (`test_a_signature_does_not_change_when_lines_move`) and Task 4
   (`test_a_second_block_with_the_same_signature_links_the_open_card_instead_of_a_new_one`).
3. **A fix merged on GitHub but not in the checkout** must not resume the story; one note asks for the
   deploy. Pinned in Task 5 (`test_a_merged_fix_not_yet_in_the_checkout_gets_one_awaiting_deploy_note`).
4. **A long planning turn while the build lane is decoding** must wait; one already running finishes.
   Pinned in Task 7 (`test_a_long_turn_waits_for_the_build_lanes_gap_and_a_started_one_finishes`).
5. **The serve ticker and the launchd tick at the same moment** must never run one lane twice. Pinned in
   Task 8 (`test_a_beat_that_finds_a_lane_held_by_another_process_logs_busy_and_runs_the_other_lane`).

---

## P5a: the diagnosis and the fix cards (after P1)

### Task 1: The evidence pack, from disk (D4)

**Files:**
- Create: `src/airlie/story/diagnosis.py`, `scripts/block_fixture.py`, `tests/unit/data/blocks/<case>/`
  (five cases copied and redacted from `~/.local/state/airlie/stories`: AIR-17 layer 02's hold trap, AIR-9
  layer 05's pre-code block, AIR-31 layer 02's "changed nothing", HDS-5's product `max_steps`, and a
  `fetch_failed` stop)
- Test: `tests/unit/test_story_diagnosis.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Paths: events: Path; run_dir: Path | None; trace: Path | None; steps: tuple[Path, ...]   # the last five step files
  @dataclass(frozen=True)
  class Pack:
      key: str; n: int; reason: str; stage: str; layer: str | None; attempt: int
      run_id: str | None; subtask: str | None; role: str | None; stop_reason: str | None
      refused_calls: int; holds: tuple[str, ...]; repeats: int; continuations: int
      steps: int; tokens: int; minutes: float
      gate_tail: str; tracebacks: tuple[str, ...]; failures: tuple[str, ...]   # failures: the error/engine_error/fetch_failed event lines
      base_gate_failed: bool | None; diff_stat: str; paths: Paths
      def to_dict / from_dict
  def render_pack(pack: Pack) -> str        # the pack as Markdown data for Muse and the diagnosis: counts, the gate tail, the tracebacks, the paths
  def gather(state_dir: Path, key: str, *, n: int | None = None) -> Pack
      # the nth `blocked` event of the story (default the latest); the run is the last `run` event before it;
      # the subtask and stop_reason from that run's last `subtask_done`; counts from the run's events; the
      # gate tail from the last `gate_failed` event's output (60 lines); tracebacks from `error` events;
      # the diff stat from `git diff --stat <base sha>..<attempt branch>` when the branch exists, else "";
      # a missing piece is "", 0 or None, never a raise; StoryError when the story has no blocked event
  ```
- `scripts/block_fixture.py KEY N OUT` copies the story's `state.json`, `events.jsonl` and the run's trace
  for block N into OUT with URLs, home paths and tokens redacted (`_redact_urls`, `~` for the home).
- [ ] **Step 1: List today's event kinds** (`grep -rn "emit(" src/airlie | sed ...`) and correct the kinds
  this plan names where they differ; write the five fixtures with the script.
- [ ] **Step 2: Failing tests:** `test_the_pack_of_a_hold_trap_counts_the_refused_calls_and_names_the_hold`,
  `test_the_pack_of_a_gate_block_carries_the_gate_tail_and_the_diff_stat`,
  `test_a_story_with_no_block_is_refused_and_a_missing_trace_is_an_empty_field`,
  `test_the_nth_block_is_gathered_when_asked`.
- [ ] **Step 3: Implement. Step 4: Gate; commit** `story: a block's evidence pack is gathered from disk`

---

### Task 2: The class, the signature, and the calibration (D4)

**Files:**
- Modify: `src/airlie/story/diagnosis.py`
- Create: `scripts/calibrate_diagnosis.py`, `docs/bench/2026-10-25-diagnosis-calibration.md`
- Test: `tests/unit/test_story_diagnosis.py`

**Interfaces:**
- Produces:
  ```python
  Class = Literal["transient", "environment", "content", "process", "config"]
  @dataclass(frozen=True)
  class Classified: class_: Class; kind: str; where: str; signature: str; evidence: tuple[str, ...]; rule: str
  def classify(pack: Pack, *, muse_category: str | None = None) -> Classified
      # rules in order, each naming the evidence it matched:
      # transient: a failures line of engine_error or fetch_failed, or the reason says the trunk could not be fetched
      # process:  a traceback whose innermost airlie frame exists -> kind = the exception type, where = module:function;
      #           or stop_reason == "repeated_action" and refused_calls >= 3 -> kind "hold_trap", where = the hold's name;
      #           or an `error` event with no StoryBlocked -> kind "unhandled", where = module:function
      # config:   stop_reason == "cut_off" -> kind "cut_off", where = role; or stop_reason == "max_steps" and the
      #           stage is a planning stage -> kind "max_steps", where = role
      # environment: base_gate_failed is True -> kind "gate_on_base", where = the first failing command
      # content:  else; kind = muse_category or ("gate_failed" | "precode_failed" | "changed_nothing" from the reason),
      #           where = the failing test id in the gate tail or the reason, else the stage
      # signature = f"{class_}:{kind}:{where}"; never a line number
  ```
- `scripts/calibrate_diagnosis.py [STATE_DIR]` runs `gather` and `classify` over every block of every story
  and prints the distribution by class, kind and story, and each block's one-line evidence.
- [ ] **Step 1: Failing tests:** parametrised `test_each_fixture_classes_as_expected` (hold trap → process,
  hold_trap; pre-code → content; changed nothing → content, changed_nothing; product max_steps → config;
  fetch → transient), `test_a_traceback_in_airlie_outranks_muses_category`,
  `test_a_signature_does_not_change_when_lines_move` (two tracebacks of the same exception in the same
  function at different lines share one signature), `test_an_unclassifiable_block_is_content_with_the_stage_as_where`.
- [ ] **Step 2: Implement. Step 3: Calibrate:** run the script over this Mac's state directory; compare with
  the performance review's table (27 gate, 14 pre-code, 9 changed nothing, 5 roles unfinished, 2 GitHub);
  adjust a rule only with a fixture that pins the change; record the distribution and the misreads in
  `docs/bench/2026-10-25-diagnosis-calibration.md`.
- [ ] **Step 4: Gate; commit** `story: a block is classed by rules, with a signature that survives edits`

---

### Task 3: The diagnosis written at block time; Muse reads the pack; the card, `line:why` and `/why` (D4)

**Files:**
- Modify: `src/airlie/story/diagnosis.py` (`render_diagnosis`, `write_diagnosis`, `latest`),
  `src/airlie/story/pipeline.py` (`_block`; the `_at_block` calls leave `_build`), `src/airlie/supervisor/ledger.py`
  (columns, migration), `src/airlie/supervisor/answer.py` (`class_agrees`, `class_reason`),
  `src/airlie/supervisor/layer.py` (`at_block(pack)`), `prompts/roles/supervisor.md` (the pack section and
  the two fields), `src/airlie/cli.py` (`line` group: `why`), `Taskfile.yml` (`line:why`),
  `src/airlie/serve/app.py` (`GET /v1/airlie/stories/{key}/diagnosis`), `tests/unit/test_prompt_texts.py`
- Create: `skills/chat/why.md`
- Test: `tests/unit/test_story_pipeline.py`, `tests/unit/test_supervisor_ledger.py`,
  `tests/unit/test_cli_line.py`, `tests/unit/test_serve.py`, `tests/unit/test_skills.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Diagnosis: pack: Pack; classified: Classified; muse: Answer | None; next: str   # next: filled by Task 4 ("a retry", "fix card AIR-57", "waits for a person")
  def render_diagnosis(d: Diagnosis) -> str          # sections: The class (one line + evidence), Muse's reading, What happens next, Evidence, Paths
  def write_diagnosis(state_dir: Path, d: Diagnosis) -> Path   # stories/<key>/diagnosis/<n>.md and .json
  def latest(state_dir: Path, key: str) -> Diagnosis | None
  def diagnosis_note(d: Diagnosis) -> str           # the card's block note: class line, Muse's sentence, next, "Resume: ..."
  ```
- `Answer` gains `class_agrees: bool | None` and `class_reason: str | None` (optional in the JSON; the prompt
  asks for them at `block`); `Intervention` gains `class_: str | None`, `signature: str | None`,
  `class_agrees: bool | None`; `Ledger.__init__` adds missing columns by `PRAGMA table_info` then `ALTER
  TABLE interventions ADD COLUMN ...`; `Row` reads them (null for old rows).
- `LayerSupervisor.at_block(subtask, role, reason, *, pack: Pack, classified: Classified) -> Answer | None`
  (the evidence is `render_pack(pack)` plus the class line); `StoryPipeline._block(st, card, reason)`:
  `pack = gather(...)`, `classified = classify(pack)`, `muse = await self._muse_at_block(...)` (a `Triage` of
  its own since the layer's supervisor is closed; `None` when `story.supervisor.enabled` is off), `d =
  Diagnosis(...)`, `write_diagnosis`, the `blocked` event gains `class`, `kind`, `signature`, `n`; the note is
  `diagnosis_note(d)`; any failure in gathering or writing is a `diagnosis_failed` event and today's plain
  note. The `_at_block` calls in `_build` are removed (the block triage happens once, here).
- CLI: `airlie line why KEY [--n N] [--paths]`; `task line:why -- KEY`. Route: `GET
  /v1/airlie/stories/{key}/diagnosis[?n=]` → `{"n", "class", "kind", "signature", "muse": {...} | null,
  "next", "markdown", "paths": [...]}`, 404 with no diagnosis. Skill `skills/chat/why.md` (`summary`,
  `action: why`): the app fetches the route for the key in the message and prepends the Markdown as data;
  the skill tells the chat role to answer from it and say when it has none.
- [ ] **Step 1: Failing tests:** `test_a_block_writes_its_diagnosis_and_the_note_leads_with_the_class`,
  `test_muse_reads_the_pack_at_block_and_its_agreement_is_recorded`,
  `test_a_pack_that_cannot_be_gathered_is_an_event_and_the_plain_note`,
  `test_the_ledger_adds_the_class_columns_to_an_old_file_and_old_rows_read_null`,
  `test_line_why_prints_the_latest_diagnosis_and_only_the_paths_with_the_flag`,
  `test_the_diagnosis_route_returns_the_json_and_the_markdown`, `test_the_why_skill_loads_with_its_action`.
- [ ] **Step 2: Implement; CASES for the supervisor prompt; snapshot. Step 3: Gate; commit**
  `story: a block writes its diagnosis; Muse reads the pack; task line:why and /why show it`

---

### Task 4: The fix card (D5)

**Files:**
- Create: `src/airlie/story/fixes.py`
- Modify: `src/airlie/config/schema.py` (`LineSpec` with `home_repo`, `transient_retries`), `airlie.yaml`
  (`line:`), `src/airlie/story/pipeline.py` (`_block` files the fix), `src/airlie/story/diagnosis.py`
  (`next` from the fix)
- Test: `tests/unit/test_story_fixes.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  ```python
  class LineSpec(Strict):
      home_repo: str | None = Field(None, description="owner/name of airlie's own repository, where fix cards are built; None: the remote of the checkout the line runs from")
      transient_retries: int = Field(2, ge=0)
      # Task 6 adds tick_s, serve_ticks, fanout, decode_floor_tps; Task 10 adds budgets; Task 12 adds consult_context
  AirlieConfig.line: LineSpec
  ```
  ```python
  FIX = "fix"
  def signature_label(sig: str) -> str      # "signature:<sig>"
  def blocks_label(key: str) -> str; def blocked_by_label(fix_key: str) -> str
  def home_repo(cfg: AirlieConfig, checkout: Path) -> str          # line.home_repo or Publisher.github_repo_of(checkout)
  def find_open_fix(store: BoardStore, signature: str) -> Item | None   # a card with FIX and the signature label whose status is not done
  def render_fix_card(d: Diagnosis, story_key: str) -> tuple[str, str]
      # title "Line: <KEY> blocked at <stage or layer NN>: <kind>"; sections Why, Evidence, Hypothesis (the rule,
      # Muse's diagnosis and fix_if_attempt), Reproduce (`task line:why -- KEY --paths`, the run id, the step
      # files, the failed command), Acceptance criteria (Given/When/Then by kind: traceback, hold_trap, cut_off,
      # max_steps, unhandled), Out of scope (the blocked story)
  def file_fix(store: BoardStore, *, d: Diagnosis, story_key: str, home: str) -> tuple[Item, bool]
      # (the card, created); creates level story, status todo, labels [FIX, f"repo:{home}", signature, blocks],
      # created_by "line", or comments the open one "also blocks <KEY>"; labels the story blocked_by:<fix key>
  ```
- `_block`: for class `process` or `config`, `file_fix(...)` (a `LocalBoard` only; a Jira board gets the
  card's text as a note on the issue and no labels); `d.next` names the card; events `fix_filed(key, fix,
  signature)` or `fix_linked`.
- [ ] **Step 1: Failing tests:** `test_a_process_block_files_a_fix_card_with_its_sections_and_labels`,
  `test_a_second_block_with_the_same_signature_links_the_open_card_instead_of_a_new_one`,
  `test_a_content_or_transient_block_files_nothing`, `test_a_jira_board_gets_the_text_as_a_note_and_no_labels`,
  `test_line_config_has_defaults_and_the_home_repo_falls_back_to_the_checkouts_remote`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: airlie's own failures become fix cards, one per signature`

---

### Task 5: After the fix, the release; transient retries; `task line:deploy` (D6)

**Files:**
- Modify: `src/airlie/story/fixes.py`, `src/airlie/story/factory.py` (bookkeeping in `tick`, moved by Task 6),
  `src/airlie/story/pipeline.py` (`_block`'s same-signature stop), `src/airlie/cli.py` (`line deploy`),
  `Taskfile.yml` (`line:deploy`), `src/airlie/serve/app.py` (`/health` gains `commit`)
- Test: `tests/unit/test_story_fixes.py`, `tests/unit/test_story_factory.py`, `tests/unit/test_serve.py`

**Interfaces:**
- Produces:
  ```python
  def fix_prs_merged(store: BoardStore, state_dir: Path, fix: Item, *, gh: Callable[..., Awaitable[str]]) -> str | None
      # the merge commit sha when every PR in the fix story's state.json is merged (gh pr view --json state,mergeCommit), else None
  def deployed(merge_sha: str, *, home: Path, serve_commit: str | None) -> bool
      # git merge-base --is-ancestor <sha> <home trunk HEAD>, and the same for serve_commit when given
  async def release_after_fix(store, state_dir, fix: Item, *, home: Path, serve_commit: str | None, gh) -> list[str]
      # merged but not deployed: one comment "merged; run `task line:deploy` ..." (label awaiting_deploy, event
      # fix_awaiting_deploy) and []; deployed: the fix -> done, each story with blocked_by:<fix> -> approved with
      # the label resumed_after:<fix> and a note, events resumed_after_fix; returns the released keys
  def transient_retry(store, state_dir, story: Item, *, retries: int) -> bool
      # a blocked story whose latest diagnosis is transient and whose retried:<sig>:<n> count is under retries
      # -> approved with the next label (event transient_retry); else False
  ```
- `_block`: when the story carries `resumed_after:<fix>` and the new signature equals that card's, the note
  says the fix did not take and the story waits for a person; the fix card gets a comment; no new card.
- Bookkeeping, on every beat (in `factory.tick` until Task 6 moves it): every open fix card through
  `release_after_fix`; every blocked story through `transient_retry`.
- `airlie line deploy`: `git -C <home checkout> pull --ff-only <remote> <trunk>`, then reads `/health`;
  when its `commit` is behind HEAD it prints "restart serve: task serve"; `task line:deploy`. `/health`
  gains `commit: build_commit()`.
- [ ] **Step 1: Failing tests:** `test_a_merged_fix_not_yet_in_the_checkout_gets_one_awaiting_deploy_note`,
  `test_a_fix_in_the_running_code_releases_the_stories_it_blocked_once_and_closes_the_card`,
  `test_a_story_that_blocks_again_on_the_same_signature_after_a_fix_waits_for_a_person`,
  `test_a_transient_block_is_retried_twice_then_waits`, `test_health_reports_the_serve_commit`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: a deployed fix releases the stories it blocked; transient blocks retry twice`

---

## P5b: the scheduler, the budgets, the stats, the rows (after P3 and P4)

### Task 6: Two lanes, two locks, and `plan` apart from `build` (D1)

**Files:**
- Create: `src/airlie/story/line.py`
- Modify: `src/airlie/story/state.py` (`story_lock` → `lane_lock`), `src/airlie/story/factory.py` (`tick`
  delegates to `Line.beat`; `run_one` takes both lanes in turn), `src/airlie/story/pipeline.py` (`plan`,
  `build`, `run`), `src/airlie/story/local_board.py` (`next_planned_story`, `next_unplanned_story`),
  `src/airlie/config/schema.py` (`LineSpec.tick_s`, `serve_ticks`, `fanout`, `decode_floor_tps`),
  `src/airlie/cli.py` (`line tick`; `story tick` prints a deprecation line and calls it), `Taskfile.yml`
  (`line:tick`), `docs/integrations/story-tick.plist` (`airlie line tick`)
- Test: `tests/unit/test_story_line.py`, `tests/unit/test_story_pipeline.py`, `tests/unit/test_story_factory.py`

**Interfaces:**
- Consumes: P3's `EpicPipeline.plan/replan`, `LocalBoard.next_epic`, the promotion, replay and epic-status
  bookkeeping (P3 Task 7); P4's `InitiativePipeline.turn/revise`, `next_initiative`,
  `epics_awaiting_revision`, the pick and the held-extension checks (P4 Tasks 7, 9); Task 5's bookkeeping.
- Produces:
  ```python
  Lane = Literal["build", "plan"]
  def lane_lock(stories_dir: Path, lane: Lane) -> Iterator[bool]     # build.lock (today's .lock, renamed on first open), plan.lock
  def lock_held(stories_dir: Path, lane: Lane) -> bool
  @dataclass(frozen=True)
  class Pipelines:   # the factories the lanes run; the tests pass fakes
      story: Callable[[Path], StoryPipeline]      # by repository checkout
      epic: EpicPipeline; initiative: InitiativePipeline
  class Line:
      def __init__(self, cfg: AirlieConfig, *, board: LocalBoard, state_dir: Path, pipelines: Pipelines,
                   gh: Callable[..., Awaitable[str]], now: Callable[[], float] = time.time) -> None
      async def beat(self) -> str
          # gates (engine up; profile match, Task 12; GitHub answers; not paused, Task 9) -> bookkeeping
          # (P3's, P4's, Task 5's) -> planning lane: under plan.lock, the first ready of: an initiative's turn
          # or an epic's revision, an epic to plan or re-plan, a story to plan -> build lane: under build.lock,
          # the top planned story, a replay/restack first when one is due -> one log line naming both lanes
  LineSpec: tick_s: int = Field(60, ge=5); serve_ticks: bool = True; fanout: int = Field(4, ge=1); decode_floor_tps: float = Field(12.0, gt=0)
  ```
- `StoryPipeline.plan(card) -> StoryState` runs intake, base, gate, refine or the story card, the amigos
  and the layer split, ending at the boundary stage (P2's landed name for today's `layers`);
  `build(card)` runs the layers, publish and finish from that boundary; `run(card)` is `plan` then `build`
  (`task story -- KEY`, the tests); each resumes at the boundary as today's stages resume.
  `LocalBoard.next_unplanned_story()`: the oldest `approved` story whose state has not reached the boundary;
  `next_planned_story()`: the oldest `approved` story that has. Both honour a held ancestor (Task 9).
- `run_one` takes `plan.lock` for `plan` then `build.lock` for `build`; `StoryBusy` names the lane held.
- [ ] **Step 1: Failing tests:** `test_two_lanes_take_two_locks_and_a_job_holds_exactly_one`,
  `test_the_build_lane_takes_only_a_planned_story_and_the_planning_lane_plans_the_unplanned_one`,
  `test_the_planning_lanes_order_is_initiative_epic_revision_epic_plan_then_story`,
  `test_run_is_plan_then_build_and_each_resumes_at_the_boundary`, `test_story_tick_still_works_and_says_it_is_line_tick_now`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `line: one scheduler, two lanes; a story is planned, then built`

---

### Task 7: The build phase, the gap wait, the fan-out, the engine flags (D2, N5)

**Files:**
- Modify: `src/airlie/story/line.py` (`write_phase`, `read_phase`, `wait_for_gap`, `fanout`),
  `src/airlie/story/pipeline.py` (`build` writes the phase), `src/airlie/story/asking.py` (`long`, `fanout`),
  `airlie.yaml` (the studio profile's engine flags)
- Test: `tests/unit/test_story_line.py`, `tests/unit/test_story_asking.py`

**Interfaces:**
- Produces:
  ```python
  Phase = Literal["decoding", "gap", "idle"]
  def write_phase(stories_dir: Path, phase: Phase, *, run_dir: Path | None) -> None     # stories/build.phase: {"phase", "ts", "run_dir"}
  def read_phase(stories_dir: Path, *, stale_s: float = 600.0) -> tuple[Phase, float]  # ("idle", age) when missing or stale
  async def wait_for_gap(stories_dir: Path, *, poll_s: float = 2.0, emit: Emit) -> float   # seconds waited; emits lane_wait once with them
  def fanout(stories_dir: Path, *, width: int, floor_tps: float) -> int
      # the build run's last five step events (completion_tokens / gen_ms): under the floor 1, else width; idle -> width
  ```
- `StoryPipeline.build`: `decoding` when a layer's subtask starts, `gap` at the gate, the review, the publish
  and between layers, `idle` when the build ends or blocks. `Asker._ask`/`conversation` gain `long: bool =
  True`: a long turn calls `wait_for_gap` first; consultations, surveys, reads and renders pass `long=False`;
  the gather in P4's rounds and P2's checker takes `asyncio.Semaphore(fanout())` re-read each round.
- `airlie.yaml`, the studio profile's engine: `kv_cache_quantization: true` and `extra_args:
  ["--chunked-prefill-tokens", "2048", "--cache-memory-mb", "<the doctor's budget minus weights and live KV,
  computed in Step 2 and written with the date>"]`; the flags take effect at the next `task restart`, which
  this plan never runs.
- [ ] **Step 1: Failing tests:** `test_a_long_turn_waits_for_the_build_lanes_gap_and_a_started_one_finishes`,
  `test_short_asks_run_four_wide_and_one_wide_under_the_decode_floor`, `test_a_stale_phase_file_reads_as_idle`,
  `test_the_build_writes_decoding_gap_and_idle_at_its_boundaries`.
- [ ] **Step 2: Implement; compute the cache memory figure with `task doctor`. Step 3: Gate; commit**
  `line: planning's long turns run in the build's gaps; short asks fan out under the decode floor`

---

### Task 8: The serve ticker and the jobs registry (D3)

**Files:**
- Modify: `src/airlie/serve/app.py` (the ticker, `Jobs` registration, `/health`'s `line`), `src/airlie/story/line.py`
  (`beat` reports the jobs it started)
- Test: `tests/unit/test_serve.py`, `tests/unit/test_story_line.py`

**Interfaces:**
- `create_app` starts, when `cfg.line.serve_ticks`, a background task that awaits `Line.beat()` every
  `line.tick_s` seconds (first beat after one interval), each lane job registered in `Jobs` as kind `line`
  with id `line-<lane>-<key>` and its events path, so `/v1/airlie/jobs` and the Workflows screen follow it;
  blocking helpers (git, gh, SQLite) run through `asyncio.to_thread`; the ticker is cancelled on shutdown
  and a running job finishes or is cancelled as POST-started jobs are today. `/health` gains `line:
  {last_tick, paused, lanes: {build: key | null, plan: key | null}}`.
- A beat that finds a lane's lock held by another process (the launchd tick, a `task story`) logs `busy` for
  that lane and runs the other.
- [ ] **Step 1: Failing tests:** `test_serve_ticks_on_its_timer_and_registers_the_lane_jobs`,
  `test_a_beat_that_finds_a_lane_held_by_another_process_logs_busy_and_runs_the_other_lane`,
  `test_health_reports_the_line`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `serve: the line ticks every minute from serve; launchd is the belt`

---

### Task 9: The pause switch and the statuses as controls (D7)

**Files:**
- Modify: `src/airlie/story/line.py` (`paused`, `set_paused`), `src/airlie/story/local_board.py` (held
  ancestors), `src/airlie/cli.py` (`line pause`, `line resume`), `Taskfile.yml`, `src/airlie/serve/app.py`
  (`POST /v1/airlie/line`)
- Test: `tests/unit/test_story_line.py`, `tests/unit/test_local_board.py`, `tests/unit/test_serve.py`

**Interfaces:**
- `paused(state_dir) -> bool` reads `<state_dir>/line.paused`; `set_paused(state_dir, bool)`; the beat
  does gates and bookkeeping and starts no job when paused, logging `paused`; `POST /v1/airlie/line
  {"paused": true|false}` → the line row. `LocalBoard.held(item) -> bool`: any ancestor `blocked` or `todo`;
  every `next_*` skips held cards.
- [ ] **Step 1: Failing tests:** `test_a_paused_line_does_bookkeeping_and_starts_no_job`,
  `test_a_blocked_ancestor_holds_every_card_under_it_and_release_lets_them_go`, `test_the_pause_route_and_tasks`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `line: one switch pauses the line; a held ancestor holds its cards`

---

### Task 10: Budgets, recorded and warned (D8)

**Files:**
- Create: `src/airlie/story/budgets.py`, `scripts/stage_medians.py`
- Modify: `src/airlie/config/schema.py` (`BudgetSpec`, `LineSpec.budgets`), `airlie.yaml`,
  `src/airlie/story/pipeline.py`, `src/airlie/story/epic_pipeline.py`, `src/airlie/story/initiative_pipeline.py`
  (`meter.observe(stage)` at each stage's end)
- Test: `tests/unit/test_story_budgets.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  ```python
  class BudgetSpec(Strict): wall_s: int = Field(ge=1); tokens: int = Field(ge=1)
  LineSpec.budgets: dict[str, BudgetSpec]   # keys: survey, refine, questions, imagine, amigos, plan, package, layer, review, publish
  @dataclass(frozen=True)
  class Figures: tokens_in: int; tokens_out: int; wall_s: float; steps: int; attempts: int
  class StageMeter:
      def __init__(self, state_dir: Path, key: str, budgets: Mapping[str, BudgetSpec], emit: Emit) -> None
      def fold(self, stage: str) -> Figures                       # from the card's run events for that stage
      def observe(self, stage: str) -> Literal["ok", "warning", "exceeded"]
          # writes stages/<stage>.json; budget_warning at 80 %, budget_exceeded at 100 %, each at most once per
          # stage per card; returns the state (the pipeline posts one note at exceeded)
  ```
- `scripts/stage_medians.py` prints the median wall-clock and tokens per stage over the state directory; the
  defaults in `airlie.yaml` are those medians doubled, written with the date and the sample size.
- [ ] **Step 1: Failing tests:** `test_a_stages_figures_fold_from_its_step_events`,
  `test_warning_at_eighty_percent_and_exceeded_at_one_hundred_each_once`, `test_budgets_have_the_ten_stages`.
- [ ] **Step 2: Implement; run the medians script; write the defaults. Step 3: Gate; commit**
  `line: every stage's cost is recorded against its budget, warned, never stopped`

---

### Task 11: Who moved a card, and the stats (D9)

**Files:**
- Create: `src/airlie/story/stats.py`
- Modify: `src/airlie/board/store.py` (`moves` table, `by`), `src/airlie/board/api.py` (the principal as
  `by`), `src/airlie/story/local_board.py` (`by="line"`), `src/airlie/cli.py` (`line stats`), `Taskfile.yml`
  (`line:stats`; `amigos:stats` is this), `src/airlie/serve/app.py` (`GET /v1/airlie/line/stats`)
- Test: `tests/unit/test_board_store.py`, `tests/unit/test_story_stats.py`, `tests/unit/test_serve.py`

**Interfaces:**
- `BoardStore`: `CREATE TABLE IF NOT EXISTS moves (id INTEGER PRIMARY KEY, item_key TEXT NOT NULL,
  from_status TEXT NOT NULL, to_status TEXT NOT NULL, by TEXT NOT NULL, at REAL NOT NULL)`; `update(...,
  by: str = "person")` and `move(..., by: str = "person")` append a row on a status change;
  `moves(key) -> list[Move]` with `Move(key, from_status, to_status, by, at)`. The API passes the request's principal name (today's `PERSON` when there is
  none); `LocalBoard.move` passes `by="line"`.
- ```python
  def line_stats(state_dir: Path, store: BoardStore, *, since: float | None = None) -> dict[str, Any]
      # {"unattended": {"stories": n, "unattended": m, "rate": m/n},  # approved -> review with no person move or comment between
      #  "blocks": {"by_class": {...}, "by_kind": {...}, "fix_cards_filed": n, "resumed_after_fix": n},
      #  "hours": [{"key", "estimate", "actual"}], "amigos": {"objections": {"<role>/<part>": n}, "borne_out": n},
      #  "budgets": {"<stage>": {"warnings": n, "exceeded": n}}}
  ```
  `airlie line stats [--since DATE]` prints it as tables; `GET /v1/airlie/line/stats[?since=]`.
- [ ] **Step 1: Failing tests:** `test_every_status_change_is_recorded_with_who_made_it`,
  `test_the_unattended_rate_counts_only_stories_a_person_never_touched_between_approval_and_review`,
  `test_blocks_group_by_class_and_kind_with_fixes_filed_and_resumed`,
  `test_amigos_objections_count_by_role_and_part_with_the_share_borne_out`, `test_the_stats_route`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `board, line: who moved a card is recorded; task line:stats judges the week`

---

### Task 12: The engine gate and the doctor's two-lane projection (D10)

**Files:**
- Modify: `src/airlie/doctor.py` (`engine_matches_profile`, the projection), `src/airlie/story/line.py`
  (the gate), `src/airlie/engine/registry.py` (the running flags read back)
- Test: `tests/unit/test_doctor.py`, `tests/unit/test_story_line.py`

**Interfaces:**
- `engine_matches_profile(cfg: AirlieConfig, probe: SystemProbe, profile: str) -> CheckResult`: the running
  vllm-mlx process's arguments (`probe.process_args("vllm")`) against the profile's engine spec
  (`--max-kv-size`, `--max-num-seqs`, `--chunked-prefill-tokens`, `--kv-cache-quantization`,
  `--cache-memory-mb`, the preloaded models); `fail` names the differing flags and `task restart --
  --topology <profile>`. `project_memory_gb` counts the build lane's one sequence at the coder's
  `max_context` plus `line.fanout` sequences at `line.consult_context` (8192, a `LineSpec` field) instead of
  `teams.k`. The beat's gate calls `engine_matches_profile` with `story.topology`; a `fail` is an
  `engine_mismatch` event, the log line, and no build this beat (bookkeeping still runs).
- [ ] **Step 1: Failing tests:** `test_an_engine_running_other_flags_than_the_story_profile_is_a_mismatch_naming_the_restart`,
  `test_the_memory_projection_counts_the_two_lanes_sequences`, `test_a_profile_mismatch_is_a_gate_that_starts_no_build`.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `doctor, line: the engine must run the story profile; the projection counts two lanes`

---

### Task 13: The line's rows, the launchd agent, the docs, one unattended story (D11)

**Files:**
- Modify: `src/airlie/story/line.py` (`line/events.jsonl` through `EventSink`), `src/airlie/serve/app.py`
  (`GET /v1/airlie/line`, `GET /v1/airlie/line/events`), `src/airlie/serve/flow.py` (`diagnosis` and
  `budgets` on the story row), `Taskfile.yml` (`line:launchd:install` and `:uninstall` replace the story
  ones), `docs/integrations/story.md` (a *The line* section: the lanes and the gaps, the pause switch, the
  diagnosis and `line:why`, fix cards and `line:deploy`, the stats, the config table), `README.md` (the task
  rows), the P5 design's Status line
- Test: `tests/unit/test_serve.py`, `tests/unit/test_story_flow.py`

**Interfaces:**
- `GET /v1/airlie/line` → `{paused, gates: {engine, profile_match, github}, lanes: {build: {card, title,
  stage, layer, step, tps, elapsed_s, estimate_h} | null, plan: {card, title, stage, elapsed_s} | null},
  queue_hours, blocked: [{key, class, kind, fix}], last_tick}`; `GET /v1/airlie/line/events?limit=` the
  log's tail. The story flow row gains `diagnosis: {class, kind, signature, n} | null` and `budgets:
  {<stage>: Figures}`.
- [ ] **Step 1: Failing tests:** `test_the_line_row_shows_both_lanes_the_gates_and_the_blocked_cards`,
  `test_the_story_flow_carries_its_diagnosis_and_budgets`, `test_the_line_events_tail`.
- [ ] **Step 2: Implement. Step 3: Docs; gate; commit** `serve, docs: the line's rows; task line:launchd:install`
- [ ] **Step 4 (with Noah's go, engine idle and restarted on the story profile by Noah): one small approved
  story through `task serve` ticking, hands off; record every beat's log line, the phases the build wrote,
  the planning lane's waits, any block's diagnosis and the stats line in `docs/bench/2026-10-25-line.md`;
  open the PR with the numbers in its body.**

---

## Self-review notes

- Spec coverage: D1 Task 6; D2 and N5 Task 7; D3 Task 8; D4 Tasks 1–3; D5 Task 4; D6 Task 5; D7 Task 9;
  D8 Task 10; D9 Task 11; D10 Task 12; D11 Task 13; D12 is the split of this document; N1 Tasks 6–7; N2
  Tasks 1–5; N3 nothing (set aside); N4 Tasks 4–5 (todo cards, the person deploys).
- The design's `fix_deployed` is `fix_prs_merged` plus `deployed` here, so the awaiting-deploy case has its
  own test; the design's `release_blocked` is `release_after_fix`.
- `story_lock` callers outside the line (`story_job`, `restack_one`, the tests) move to
  `lane_lock(..., "build")` in Task 6; the old `.lock` file is renamed once so a running tick's lock is not
  lost across the deploy.
- The boundary stage name follows what P2 landed (`layers` today, `agreement` in the amigos design); Task 6
  reads it from one constant in `story/model.py`.
- P4's `initiative_job` and P3's epic steps already take a lock; Task 6 makes it `plan.lock`.
