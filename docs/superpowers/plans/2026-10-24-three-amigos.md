# The Three Amigos (P2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After refine and the architect's split, a story holds one meeting, an AutoGen group chat of product,
the architect, the tester and airlie's checker, that ends in an agreement on the requirements, the changes
and the proof (a test plan per criterion at the right level of the repository's test pyramid), or at a turn
cap with each owner's part standing. The agreement feeds P1's package call, and a tester critic reads each
package's tests against the agreed proof.

**Architecture:** `airlie.story.amigos` holds the contracts (proof, verdict, agreement) and the meeting: three
`AssistantAgent`s built with the AutoGen wiring the team orchestrator already has (`teams/autogen_team.py`,
`teams/autogen_client.py`), a model-less `Checker` agent, a custom `AgreementReached` termination and a
`RoundRobinGroupChat` whose state is saved after every turn. `airlie.story.amigos_checks` holds the
mechanical checks on the split, the proof and their references; `airlie.story.testing_conventions` reads a
repository's test tree, testing notes and guideline library. The pipeline gains an `_agree` stage between
layers and build, stores the agreement in the state, and P1's package path gains the critic turn, the
`proof_change` branch and the `proof_mismatch` check. `Layer` gains structured `files` and `interfaces`.

**Tech Stack:** Python 3.12, uv, pydantic v2, autogen-agentchat / autogen-ext 0.7.5
(`RoundRobinGroupChat`, `AssistantAgent`, `BaseChatAgent`, `TerminationCondition`,
`MaxMessageTermination`, `ReplayChatCompletionClient` for tests), pytest (asyncio auto), git worktrees,
the prompt hub.

**Spec:** `docs/superpowers/specs/2026-10-24-three-amigos-design.md` (N1–N5, D1–D14), on
`docs/superpowers/specs/2026-10-24-planning-line-design.md` and the P1 plan
`docs/superpowers/plans/2026-10-24-layer-package.md`, which must be built first.

## Global Constraints

- Every change passes, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`.
- uv only; Taskfile tasks; `Strict` config models; a removed key gets a `REMOVED` message.
- Model-facing prose is a hub file; output contracts (`PROOF_CONTRACT`, `VERDICT_CONTRACT`, the updated
  `LAYERS_CONTRACT`) stay in Python beside their parsers. After any hub change, regenerate
  `tests/unit/data/prompt_texts.json` (`AIRLIE_WRITE_PROMPT_TEXTS=1 …`) and review the diff.
- Everything an agent says is data: parsed and validated; a proof's `file` and `level` are checked against
  the test tree before they form a path; the meeting writes nothing to the repository (detached worktrees at
  the base, read-only tools).
- The stage name stays `layers` in `STAGES` (the stage in which the layers are agreed); the agreement is a
  new state field. *(Plan's call: the design's rename would cost every state on disk a migration for a label.)*
- Tests double the model with `autogen_ext.models.replay.ReplayChatCompletionClient` through a
  `client_factory`; no test reaches the engine. Test first; commit per task; dense docstrings with the
  incident and date.
- Do not restart the engine; a story may be running on this Mac.

## Review Focus

1. **A turn that carries a revised part but no verdict, or a verdict inside prose,** must become a checker
   finding on the next turn, never a crash or a silent "agree". Pinned in Task 5
   (`test_a_turn_without_a_verdict_is_a_finding_not_an_agreement`).
2. **Two agents objecting to each other's parts without end** must stop at `max_turns` with each owner's part
   standing and every open objection recorded. Pinned in Task 5
   (`test_a_meeting_that_never_agrees_ends_at_max_turns_with_owner_decides_on_record`).
3. **Product drops a criterion the proof covers:** the agreement must never leave the meeting with a dangling
   reference. Pinned in Task 4 (`test_a_dropped_criterion_leaves_stale_references_in_the_split_and_the_proof`)
   and Task 6 (`test_the_agreement_stored_has_no_reference_to_a_dropped_criterion`).
4. **A resume mid-meeting** must replay the saved history and give the next turn to the right speaker.
   Pinned in Task 6 (`test_a_story_killed_mid_meeting_resumes_the_meeting_from_its_saved_history`).
5. **The tester's verdict on Muse arrives wrapped in prose and channel text;** the verdict must be the last
   ```json fence, and a reply with none is a finding. Pinned in Task 3
   (`test_a_verdict_is_read_from_the_last_json_fence_whatever_surrounds_it`).

---

### Task 1: A layer names its files and its boundary interfaces (D14)

**Files:**
- Modify: `src/airlie/story/model.py` (`Layer`, `parse_layers`), `src/airlie/story/briefs.py`
  (`LAYERS_CONTRACT`, `layer_paths`), `src/airlie/prompts/rules/brief-layers-rules.md` (one added
  sentence), `src/airlie/story/pipeline.py` (`map_focus` in `_build`, from P1)
- Test: `tests/unit/test_story_model.py`, `tests/unit/test_story_briefs.py`

**Interfaces:**
- Produces: `ScopedPath(path: str, new: bool)`; `Layer.files: tuple[ScopedPath, ...] = ()`,
  `Layer.interfaces: tuple[str, ...] = ()`; `Layer.paths() -> tuple[str, ...]` (the files' paths, else
  `named_paths(self.scope)`); `parse_layers` reads `files` (a list of `{"path", "new"}` objects or of path
  strings, `new` defaulting to false) and `interfaces` (a list of strings), refusing a path that is absolute
  or has `..`; `from_dict` defaults both to empty.
- `LAYERS_CONTRACT` gains, inside each layer object: `"files": [{"path": "src/pkg/mod.py", "new": false}],
  "interfaces": ["def name(arg: T) -> R  (what later layers import from this one)"]`, and the sentence
  "List every file the layer creates or changes under files, and under interfaces the signatures a later
  layer will import from it."
- `brief-layers-rules` gains: "Name each layer's files and the interfaces at its boundary."

- [ ] **Step 1: Write the failing tests** (`test_story_model.py`: `test_a_layer_parses_its_files_and_interfaces`,
  `test_a_layer_without_files_or_interfaces_still_parses_and_paths_fall_back_to_its_scope`,
  `test_a_layer_file_path_that_escapes_the_repository_is_refused`; `test_story_briefs.py`:
  `test_the_layers_contract_asks_for_files_and_interfaces`). Run; expected FAIL.
- [ ] **Step 2: Implement; regenerate the golden snapshot; run the gate**
- [ ] **Step 3: Commit** `story: a layer names its files and its boundary interfaces`

---

### Task 2: Reading a repository's testing conventions (D6)

**Files:**
- Create: `src/airlie/story/testing_conventions.py`
- Modify: `src/airlie/teams/worktree.py` (`tree_paths`)
- Test: `tests/unit/test_story_testing_conventions.py`

**Interfaces:**
- Produces:
  ```python
  def tree_paths(repo: Path, commit: str, prefix: str = "") -> list[str]        # worktree.py: git ls-tree -r --name-only
  def test_levels(repo: Path, commit: str, root: str = "tests") -> dict[str, int]
      # level -> count of test modules; a level is a directory directly under `root` with a test module in it
      # or below; `fixtures`, `data`, `__pycache__` are never levels; a root with test modules directly in it
      # and no such directory is one level, `root`; a repository without `root` gives {}
  def testing_notes(repo: Path, commit: str, limit: int = 4_000) -> str
      # docs/testing.md whole, then the "## Testing" section of AGENTS.md and of CONTRIBUTING.md; clipped
      # to `limit` with "[clipped]"; "" when none
  @dataclass(frozen=True)
  class Conventions: levels: Mapping[str, int]; notes: str; guidelines: tuple[Skill, ...]; routed: Skill | None
  def conventions(repo: Path, commit: str, library: Path | None, routed: Skill | None) -> Conventions
  def conventions_text(c: Conventions, gate: Sequence[str]) -> str
      # the tester's view: the gate's commands, the levels with counts, the guideline summaries, the routed
      # guideline's full text, the notes; headings as a brief's
  ```
- [ ] **Step 1: Failing tests:** `test_levels_are_the_test_directories_with_modules_and_never_fixtures_or_data`,
  `test_a_flat_tests_directory_is_one_level`, `test_a_repository_without_tests_has_no_levels`,
  `test_notes_come_from_docs_testing_then_the_testing_sections_clipped`,
  `test_conventions_text_lists_gate_levels_guidelines_and_the_routed_one_in_full`. Expected FAIL.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: a repository's testing conventions, read for the tester`

---

### Task 3: The proof, verdict and agreement contracts (D2, D3, D1)

**Files:**
- Create: `src/airlie/story/amigos.py` (contracts and parsers; the meeting comes in Task 5)
- Test: `tests/unit/test_story_amigos_contracts.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class ProofTest: criterion: str | None; layer: str; file: str; name: str; level: str; guideline: str | None; asserts: str; needs: str
      @property id -> str   # "file::name"
  PROOF_CONTRACT: str   # the design's D2 JSON, verbatim, plus: "criterion is null for a test that proves a behaviour of a layer with no criterion"
  def parse_proof(obj: dict | None, story: Story, layers: tuple[Layer, ...], levels: Mapping[str, int], guidelines: Collection[str]) -> tuple[ProofTest, ...]
      # StoryError (a reason for the next turn) when: no JSON; type != "proof"; a criterion id the story lacks;
      # a layer slug not in the split; a level not in `levels`; `file` not under `tests/<level>/` (or under
      # `tests/` when the one level is `tests`); a guideline not in `guidelines` (omitted is fine); two tests
      # with one id; a criterion of the story with no test; a layer with no test
  Part = Literal["requirements", "changes", "proof"]
  @dataclass(frozen=True)
  class Objection: part: Part; about: str; claim: str; suggest: str
  @dataclass(frozen=True)
  class Verdict: role: str; requirements: str; changes: str; proof: str; objections: tuple[Objection, ...]
      def agrees_to_all(self) -> bool
  VERDICT_CONTRACT: str   # the design's D3 JSON, verbatim, plus: "end every turn with it"
  def parse_verdict(text: str, role: str) -> Verdict
      # the last ```json fence (extract_json_block); StoryError when none, type != "verdict", a value not
      # agree|object, an objection on the role's own part, or "object" on a part with no objection
  @dataclass(frozen=True)
  class Agreement: story: Story; layers: tuple[Layer, ...]; proof: tuple[ProofTest, ...]; turns: int
                   unresolved: tuple[Objection, ...]; decided_by_owner: tuple[Part, ...]
      def to_dict / from_dict; def proof_for(self, layer_slug: str) -> tuple[ProofTest, ...]
  OWNER: dict[Part, str] = {"requirements": "product", "changes": "architect", "proof": "tester"}
  ```
- [ ] **Step 1: Failing tests:** `test_a_proof_plan_parses_and_each_test_has_an_id`,
  parametrised `test_a_flawed_proof_is_refused_with_a_reason` (the nine flaws above),
  `test_a_verdict_is_read_from_the_last_json_fence_whatever_surrounds_it`,
  `test_a_verdict_that_objects_to_the_speakers_own_part_or_objects_with_nothing_is_refused`,
  `test_an_agreement_round_trips_through_its_dict_and_knows_each_layers_proof`. Expected FAIL.
- [ ] **Step 2: Implement. Step 3: Gate; commit** `story: the proof, verdict and agreement contracts`

---

### Task 4: The checker's mechanical checks (D4, D13)

**Files:**
- Create: `src/airlie/story/amigos_checks.py`, `src/airlie/prompts/steering/amigos-findings.md`
- Test: `tests/unit/test_story_amigos_checks.py`

**Interfaces:**
- Produces:
  ```python
  def check_split(layers, story, repo: Path, base: str, max_scope_files: int) -> list[Finding]
      # file_missing (a file not marked new that is not at `base`), over_size (more than max_scope_files files),
      # plus parse_layers' own refusals surfaced as Finding("split_invalid", ...)
  def check_proof(proof, story, layers) -> list[Finding]
      # criterion_unproven, layer_unproven, proof_layer_mismatch (a test's layer does not own its criterion)
  def check_references(story, layers, proof) -> list[Finding]
      # stale_reference: a layer's acceptance id or a proof test's criterion that the story no longer has
  def check_agreement(story, layers, proof, repo, base, max_scope_files) -> list[Finding]   # all three
  def findings_message(findings: Sequence[Finding]) -> str   # hub `amigos-findings` + one line per finding
  ```
- `layer_checks.Kind` gains `split_invalid`, `over_size`, `criterion_unproven`, `layer_unproven`,
  `proof_layer_mismatch`, `stale_reference`, `verdict_missing`, `part_invalid`.
- `amigos-findings` (steering, roles `[product, architect, tester]`, `when`: the checker found a problem in
  a part after a turn, governs `agreement.findings: fix-next-turn`): *"airlie checked the parts as they stand
  and found these problems. The owner of each part fixes it in their next turn; nobody else changes it."*
- [ ] **Step 1: Failing tests:** `test_a_split_naming_a_file_the_base_lacks_without_new_is_a_finding`,
  `test_a_layer_over_the_size_rule_is_a_finding`, `test_a_criterion_or_a_layer_without_a_test_is_a_finding`,
  `test_a_test_whose_layer_does_not_own_its_criterion_is_a_finding`,
  `test_a_dropped_criterion_leaves_stale_references_in_the_split_and_the_proof`,
  `test_findings_message_names_each_finding_under_the_hub_text`. Expected FAIL.
- [ ] **Step 2: Implement; add the CASES entry; regenerate the snapshot. Step 3: Gate; commit**
  `story: the checker's mechanical checks on a story's agreement`

---

### Task 5: The meeting (D7, D3, D8)

**Files:**
- Modify: `src/airlie/story/amigos.py` (the meeting), `src/airlie/teams/autogen_team.py`
  (`build_model_client` passes thinking through)
- Create: `src/airlie/prompts/rules/amigos-opening.md`, `amigos-turn.md`, `amigos-product.md`,
  `amigos-architect.md`, `amigos-tester.md`; rewrite `prompts/roles/tester.md` (the critic)
- Test: `tests/unit/test_story_amigos_meeting.py`, `tests/unit/test_autogen_team.py`

**Interfaces:**
- `build_model_client(...)` adds to `extra_body`: `"enable_thinking": role.thinking`,
  `"chat_template_kwargs": {"enable_thinking": role.thinking, **role.chat_template_kwargs}`, and
  `"thinking_token_budget": role.thinking_budget` when thinking (what `HTTPEngine._body` sends).
- Produces:
  ```python
  ClientFactory = Callable[[str, RoleSpec], ChatCompletionClient]    # role name -> a model client
  @dataclass
  class Turn: role: str; text: str; verdict: Verdict | None; parts: dict[Part, Any]   # the contracts the turn carried
  class Checker(BaseChatAgent):
      # name "airlie"; produced_message_types = (TextMessage,); on_messages: re-parse the latest parts from the
      # history it was given since its last turn, run check_agreement, answer findings_message(...) or
      # "no findings"; a turn without a verdict → Finding("verdict_missing", role, ...); a part that does not
      # parse → Finding("part_invalid", role, reason)
  class AgreementReached(TerminationCondition):
      # keeps the latest Verdict per role (parsed from each TextMessage whose source is a role); terminated
      # when all three roles' latest verdicts agree_to_all and the checker's latest message is "no findings"
  class Meeting:
      def __init__(self, cfg: AirlieConfig, *, clients: ClientFactory, tools: Callable[[str], ToolRegistry] | None,
                   max_turns: int, tool_iterations: int, on_turn: Callable[[Turn, Mapping[str, Any]], None]) -> None
          # `tools(role)` gives the architect and the tester their read-only registries; `on_turn` receives every
          # turn and team.save_state(), for the pipeline to persist and emit
      async def run(self, opening: str, story: Story, layers: tuple[Layer, ...], levels, guidelines,
                    state: Mapping[str, Any] | None = None) -> Agreement
          # builds product, architect, tester (AssistantAgent: name, model_client, tools, system_message =
          # prompt_text(role.prompt) + "\n\n" + amigos-turn + amigos-<role>, max_tool_iterations=tool_iterations)
          # and the Checker; RoundRobinGroupChat([product, architect, tester, checker],
          # termination_condition=AgreementReached() | MaxMessageTermination(max_turns)); load_state(state) when
          # given; run_stream(task=opening); the Agreement is the latest version of each part, turns, the
          # objections open at the end, and the parts whose owner decided (each part not agreed by all)
  def opening_message(story, layers, conventions_text: str, lessons: Sequence[Lesson]) -> str
      # amigos-opening, story_text, the layers as their JSON, the conventions, the lessons, PROOF_CONTRACT and
      # VERDICT_CONTRACT, LAYERS_CONTRACT and STORY_CONTRACT (so a revised part is posted in its own shape)
  def latest_parts(turns: Sequence[Turn], story, layers, proof) -> tuple[Story, tuple[Layer, ...], tuple[ProofTest, ...]]
      # the last valid version of each part, by its owner
  ```
- Hub texts: `amigos-opening` (roles `[product, architect, tester]`): the meeting's purpose in Noah's words
  (agree the requirements, the changes, the proof, within this story), who owns what, that the checker
  speaks after any changed part, that it ends when all three agree or at the turn cap when each owner's part
  stands. `amigos-turn`: *"Speak to the others by name. To change your own part, post it whole in its JSON
  shape. Object to another's part with a claim and a suggestion; never rewrite it. End every turn with your
  verdict."* plus `VERDICT_CONTRACT`. `amigos-product`: owns requirements; may reword under an id, add the
  next id, mark dropped with why; never renumbers; watches for contradictions, untestable criteria, scope
  creep. `amigos-architect`: owns changes; files, interfaces, order; a layer small enough to finish.
  `amigos-tester`: owns proof; one or more tests per criterion at the level the repository's conventions give,
  under its guideline, with exact asserts and the state needed; a check that something does NOT happen inside
  a test that also makes it happen. `prompts/roles/tester.md`: the critic's prompt (summary line for the
  router kept; `governs` updated).
- [ ] **Step 1: Failing tests** with `ReplayChatCompletionClient` scripts per role (the factory returns the
  role's replay client) and a repository from `tests/unit/test_story_layer_checks.py`'s `repo` helper:
  `test_a_meeting_that_agrees_in_one_round_ends_with_the_agreement_and_no_unresolved`,
  `test_an_objection_is_answered_by_a_revision_the_checker_reads_and_the_objector_agrees`,
  `test_a_turn_without_a_verdict_is_a_finding_not_an_agreement`,
  `test_a_meeting_that_never_agrees_ends_at_max_turns_with_owner_decides_on_record`,
  `test_the_checkers_findings_are_posted_after_a_changed_part_and_nothing_after_an_unchanged_one`,
  `test_on_turn_receives_every_turn_and_the_teams_state`,
  `test_a_meeting_resumes_from_a_saved_state_with_the_next_speaker_in_order`; in `test_autogen_team.py`:
  `test_model_client_passes_the_roles_thinking_settings_via_extra_body`. Expected FAIL.
- [ ] **Step 2: Implement; add CASES for the five hub ids; regenerate the snapshot; run the contradiction
  check (the tester's new `governs` against the rules it receives)**
- [ ] **Step 3: Gate; commit** `story: the three amigos meet as one AutoGen group chat with airlie's checker`

---

### Task 6: The pipeline agrees a story before it builds (D1, D11, D13, D8)

**Files:**
- Modify: `src/airlie/story/pipeline.py` (`_agree`, `_layers` returns the first split, `_build` reads the
  agreement), `src/airlie/story/state.py` (`agreement`, `meeting`), `src/airlie/story/factory.py` (the
  meeting's clients and tools from the config), `src/airlie/config/schema.py` (`AmigosSpec`),
  `airlie.yaml` (`story.amigos`, `roles.tester`)
- Test: `tests/unit/test_story_pipeline.py`, `tests/unit/test_config.py`

**Interfaces:**
- `StorySpec.amigos: AmigosSpec` with `enabled: bool = True`, `max_turns: int = Field(12, ge=4)`,
  `max_scope_files: int = Field(5, ge=1)`, `tool_iterations: int = Field(3, ge=0)`.
- `StoryState.agreement: dict | None = None`, `StoryState.meeting: dict | None = None` (the team state after
  the latest turn; cleared when the agreement is stored).
- `StoryPipeline.__init__(..., meeting: MeetingFactory | None = None)` where
  `MeetingFactory = Callable[[AirlieConfig, Callable[[str], ToolRegistry], Callable[[Turn, Mapping], None]], Meeting]`;
  `factory.story_job` builds the real one (clients from `build_model_client` with each role's engine and
  served name, as `_default_team` does for `autogen`); tests pass a factory that returns a `Meeting` over
  replay clients, or a `FakeMeeting` with the same `run` signature.
- `_agree(st, card, story, layers) -> Agreement`: skipped when `st.agreement` is set (resume) or
  `amigos.enabled` is false (the agreement is then the story, the split and an empty proof, and P1's package
  brief carries no proof); conventions from Task 2 at the base, the routed guideline via the router with
  `skill_criteria(load_skills(roles.tester.skills))` as `_skill` does; `opening_message`; `Meeting.run` with
  `state=st.meeting`; `on_turn` saves `st.meeting`, emits `amigos_turn(role, turn, verdict)`,
  `amigos_objection` per objection, `amigos_findings(kinds)` for a checker turn, and `amigos_resolved` when a
  part objected to earlier is agreed by the objector; at the end `amigos_agreed(turns)` or
  `amigos_owner_decided(parts)` with `amigos_unresolved` per open objection and a lesson
  (`owner_decided`, detail the parts); `st.agreement`, `st.story = agreement.story.to_dict()`,
  `st.layers = [...]`, `st.meeting = None`, `st.save()`; the card's comments: the agreement rendered
  (criteria, layers with files, the proof as a table), the reworded/added/dropped criteria when any, the
  unresolved objections when any; then `st.finish("layers")`.
- `_build` reads layers from `Agreement.from_dict(st.agreement)` and passes `agreement.proof_for(layer.slug)`
  to the package brief (Task 7).
- [ ] **Step 1: Failing tests:** `test_a_story_is_agreed_between_its_layers_and_its_first_build_and_the_card_hears_it`,
  `test_the_agreement_stored_has_no_reference_to_a_dropped_criterion`,
  `test_a_story_killed_mid_meeting_resumes_the_meeting_from_its_saved_history`,
  `test_a_meeting_that_ends_by_owner_decides_records_a_lesson_and_the_objections`,
  `test_with_amigos_off_the_split_is_the_agreement`; `test_config.py`:
  `test_story_amigos_has_defaults_and_floors`. Expected FAIL.
- [ ] **Step 2: Implement; `airlie.yaml`: `story: { amigos: { enabled: true, max_turns: 12 } }` and
  `roles.tester: { prompt: prompts/roles/tester.md, model: reviewer, tools: [read_file, list_dir, grep],
  skills: skills/qa, max_context: 131072, max_tokens: 8192, thinking: false, chat_template_kwargs:
  { reasoning_strength: low }, sampling as the reviewer's }`**
- [ ] **Step 3: Gate; commit** `story: a story is agreed by product, the architect and the tester before it is built`

---

### Task 7: The package follows the agreed proof (D9, D12, D14; changes to P1)

**Files:**
- Modify: `src/airlie/story/briefs.py` (`package_brief` gains `proof: Sequence[ProofTest]`, `files`,
  `interfaces` sections), `src/airlie/story/package.py` (`parse_package` accepts a `proof_change` reply),
  `src/airlie/story/package_checks.py` (`proof_mismatch`), `src/airlie/story/pipeline.py`
  (`_ready_package`: the critic turn and the `proof_change` branch; `amigos_borne_out`)
- Create: `src/airlie/prompts/rules/package-critic.md`
- Test: `tests/unit/test_story_package.py`, `tests/unit/test_story_package_checks.py`,
  `tests/unit/test_story_layer_checks_flow.py`, `tests/unit/test_story_briefs.py`

**Interfaces:**
- `package_brief(..., proof: Sequence[ProofTest] = (), layer_files, layer_interfaces)`: a `## The agreed
  tests` section (id, level, guideline, asserts, needs per test) and `## Files and interfaces` from the layer;
  the contract adds: `"covers" names exactly the agreed tests; when the proof itself is wrong, reply instead
  with {"type": "proof_change", "changes": [{"test": "<id>", "why": "..."}]} and no package`.
- `parse_package` returns `Package | ProofChange` (`ProofChange(changes: tuple[tuple[str, str], ...])`).
- `PackageChecker.check(...)` takes `proof_ids: Collection[str]`: `Finding("proof_mismatch", id, why)` for an
  agreed test the package does not cover and for a covered test the proof does not name.
- `_ready_package`: after a `Package` parses, one critic turn: a read-only ask of the tester role at the
  base with `package-critic` (the test files as fenced blocks, the layer's proof, the conventions text; answer
  a verdict on `proof` alone); an objection is one `package-fix` revision of the architect; then the checks.
  A `ProofChange` reply: one tester ask with `PROOF_CONTRACT` for that layer's tests only and the architect's
  reasons as data, parsed with `parse_proof` against the agreement, stored into `st.agreement`, event
  `amigos_proof_revised`, then the architect is asked again (the attempt is not charged). A check finding
  that names a test id with an earlier `amigos_objection` about it (same `about`) emits `amigos_borne_out`.
- `package-critic` (roles `[tester]`): *"Read these tests against the agreed proof and the repository's
  conventions: each agreed test is present, at its level, asserting what the plan says, with the state it
  named; nothing is weakened and nothing is added that the plan does not name. Answer with your verdict on
  proof alone."*
- [ ] **Step 1: Failing tests:** `test_the_package_brief_carries_the_agreed_tests_and_the_layers_files_and_interfaces`,
  `test_a_package_that_misses_an_agreed_test_or_adds_one_is_a_proof_mismatch`,
  `test_the_critics_objection_gives_the_architect_one_revision_before_the_checks`,
  `test_a_proof_change_reply_revises_the_layers_proof_through_the_tester_and_asks_the_architect_again`,
  `test_a_check_finding_on_an_objected_test_is_borne_out`. Expected FAIL.
- [ ] **Step 2: Implement; CASES for `package-critic`; snapshot. Step 3: Gate; commit**
  `story: a layer's package follows the agreed proof, read once by the tester critic`

---

### Task 8: The flow API, the docs and the design's status

**Files:**
- Modify: `src/airlie/serve/flow.py` (a `meeting` job on the story's stage row from `amigos_*` events: role
  `amigos`, status running/passed/failed, turns and objections counts; `STAGE_ROLES["agreement"]` is not
  used since the stage stays `layers`: the job is derived from events), `tests/unit/test_story_flow.py`,
  `README.md` (the `task story` row), `docs/integrations/story.md` (stage table: a `meeting` row; a section
  *The three amigos* with the contracts, the checker's findings, owner-decides, the events; the config
  table), `docs/superpowers/specs/2026-10-24-three-amigos-design.md` and
  `…planning-line-design.md` (Status lines)
- [ ] **Step 1: Failing flow test** `test_a_story_shows_its_meeting_with_turns_and_objections`. Implement.
- [ ] **Step 2: Docs. Step 3: Gate; commit** `serve, docs: the meeting in the flow and the story docs`

- [ ] **Step 4 (with Noah's go, engine idle): one story through refine, the meeting and a layer; record
  the meeting's turns, tokens and wall-clock and the first layer's package outcome in
  `docs/bench/2026-10-24-three-amigos.md`; open the PR with the numbers in its body.**

---

## Self-review notes

- Spec coverage: D1 (card and state) Task 6; D2, D3 Task 3; D4 Task 4 and the Checker in Task 5; D5 Task 6's
  `airlie.yaml`; D6 Task 2; D7, D8 Task 5; D9, D12 Task 7; D10 events Tasks 6 and 7 (stats are P5's); D11
  Task 6 (lessons in the opening, a lesson on owner-decides); D13 Tasks 4 and 6; D14 Task 1.
- The design's rename of the stage to `agreement` is not done (Global Constraints); the state field is.
- Thinking passthrough changes the AutoGen team orchestrator's behaviour too (its agents now think as their
  roles say); `test_autogen_team.py` pins the body sent, and the `autogen` topology is not the default.
