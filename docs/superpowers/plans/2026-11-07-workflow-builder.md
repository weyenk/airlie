# Workflow Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A card names its workflow; a canvas builder composes workflows from the node types; drafts live in the board's database and run at once; Publish writes the YAML and opens a PR.

**Architecture:** Back end first: the workflow schema gains `type` and `layout`, the composer takes the mapping it runs from, the card's `workflow:` label picks the workflow and a card snapshots it at start, drafts are a table in `BoardStore` with a `/v1/workflows` router, and publish reuses `Publisher` in its own worktree. Front end second: the card modals gain the mandatory Workflow field, a `BuilderScreen` draws and edits a graph on a hand-rolled SVG canvas and serialises it to YAML, and the pipeline screen gets a generic node view fed by the composer's node events.

**Tech Stack:** Python 3.12 / FastAPI / SQLite (back end); Vue 3.5, vue-router, Pinia, reka-ui, @vueuse/core, Tailwind 4, Vitest (front end); the `yaml` npm package for YAML in the browser; PyYAML on the server.

**Spec:** `docs/superpowers/specs/2026-11-07-workflow-builder-design.md` (with `2026-11-06-generic-nodes-design.md`, "Part 1 as built").

**Branch:** this plan builds on Part 1 (PR #256). Start from main once #256 is merged, or stack the branch on `worktree-generic-nodes-design`.

## Global Constraints

- YAML in the repo is the source of truth. Nothing merged ever comes from the database. A draft runs only while it is a draft.
- Save and Publish run `check_workflows` (`src/airlie/flow/spec.py`); a draft with problems may be saved but cannot be picked on a card or published.
- `type:` and `layout:` are optional workflow keys the composer ignores.
- The Workflow field is the card label `workflow:<name>`. A story card without one runs `story`. Mandatory in the GUI (CreateModal requires it, default `story`).
- A card snapshots its workflow at start (`StoryState.workflow`); every resume runs the snapshot.
- Publish never writes the serve's own checkout; it works in a worktree under the state dir, with the bot identity through `Publisher`.
- Publishing an existing file unchanged yields a diff of only the `layout:` block.
- No new node types, checks or parsers from the GUI. Roles come from `cfg.roles`; prompt, parse, run, when and after ids from the registry catalog.
- Boxes use the pipeline screen's shapes (`JobBox`, `CodeBox`, `MilestoneMark`, the "same time" bracket, dashed gate).
- The six-command gate from AGENTS.md passes after every task, including `task web:gate`. Commit subjects start with `builder:`.
- Trailer on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **A card names a draft that no longer validates** (someone edited it). The line must not crash: it leaves the card where it is with a note. Pinned in Task 6.
2. **The builder opens `story.yaml`, the user changes nothing and publishes.** The PR diff must be only the layout block, comments intact. Pinned in Task 7.
3. **A draft is edited while a card is mid-run.** The running card must keep its snapshot and finish on it. Pinned in Task 3.
4. **"Build new…" then Cancel.** The card's typed fields must still be there, and no draft is created. Pinned in Task 12.
5. **A workflow with no `planned` node.** `plan()` must not hang the plan lane; the build lane runs it whole. Pinned in Task 3.

---

## File Structure

**Create (Python):**
- `src/airlie/story/workflows.py` — `card_workflow`, `resolve_workflows`, `WorkflowError`, snapshot helpers.
- `src/airlie/story/workflow_publish.py` — `publish_workflow`, the layout text patch.
- `src/airlie/serve/workflows_api.py` — `/v1/workflows` router.
- `src/airlie/serve/flow_nodes.py` — generic node view from node events.
- `src/airlie/flow/prompt_files.py` — the `prompt:<id>` brief builder over hub texts and draft texts.

**Modify (Python):**
- `src/airlie/flow/spec.py` (`WorkflowSpec`, `check_workflows`), `src/airlie/flow/composer.py` (`Composer.__init__`, `Registry.__getitem__`), `src/airlie/config/load.py:89-111` (glob includes), `airlie.yaml:5-10`, `config/workflows/story.yaml` (`type: story`).
- `src/airlie/story/state.py:56` (`artifacts`, `workflow`), `src/airlie/story/restart.py:205` (`workflow=None`), `src/airlie/story/flow.py:552` (`compose`), `src/airlie/story/pipeline.py:623-710` (`_drive`), `src/airlie/story/line.py:465` (`_story_line`), `src/airlie/story/fixes.py:339` (`bookkeep`).
- `src/airlie/board/model.py` (`WorkflowDraft`), `src/airlie/board/store.py` (`workflow_drafts` table and methods), `src/airlie/board/api.py` (label and move checks), `src/airlie/serve/app.py:425` (routers), `src/airlie/serve/flow.py:33` (`story_flow` dispatch).

**Create (web):**
- `web/src/api/workflow_defs.ts` — client for `/v1/workflows`.
- `web/src/screens/board/workflow.ts` — `workflowOf`, `withWorkflow`.
- `web/src/stores/card_draft.ts` — the held card form across the builder trip.
- `web/src/screens/BuilderScreen.vue`, `web/src/screens/builder/{graph.ts, layout.ts, yaml.ts, Canvas.vue, Palette.vue, NodeForm.vue, WireForm.vue, Problems.vue}`, with `.test.ts` beside each.
- `tests/e2e/test_workflow_builder.py`.

**Modify (web):** `web/src/router.ts`, `web/src/api/board.ts` (no change to types; labels carry the field), `web/src/screens/board/CreateModal.vue`, `CardModal.vue`, `web/src/screens/WorkflowsScreen.vue` (a Definitions table), `web/package.json` (`yaml`).

---

### Task 1: Workflow `type`, `layout`, and glob includes

**Files:**
- Modify: `src/airlie/flow/spec.py:59-62` (`WorkflowSpec`), `:89` (`check_workflows`); `src/airlie/config/load.py:89-111`; `airlie.yaml:5-10`; `config/workflows/story.yaml` (add `type: story` under `story:`)
- Test: `tests/unit/test_flow_spec.py`, `tests/unit/test_config.py`

**Interfaces:**
- Produces: `class Layout(Strict): x: float; y: float`; `WorkflowSpec.type: str | None = None`; `WorkflowSpec.layout: dict[str, Layout] = {}`; `check_workflows` adds `"workflow <w>: layout names unknown node '<n>'"`. Includes: an entry containing any of `*?[` expands to `sorted(path.parent.glob(inc))`, files only; no match raises `ConfigError(f"{path}: includes: nothing matches {inc}")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_flow_spec.py
def test_type_and_layout_are_optional_and_layout_must_name_nodes():
    w = WorkflowSpec(start="a", type="bug", layout={"a": Layout(x=10, y=20)}, nodes={"a": NodeSpec(type="check", run="r")})
    assert check_workflows({"w": w}) == []
    w2 = WorkflowSpec(start="a", layout={"zz": Layout(x=0, y=0)}, nodes={"a": NodeSpec(type="check", run="r")})
    assert check_workflows({"w": w2}) == ["workflow w: layout names unknown node 'zz'"]

# tests/unit/test_config.py
def test_includes_accept_a_glob_in_sorted_order(tmp_path):
    (tmp_path / "parts").mkdir()
    write(tmp_path, {"workflows": {"b": {"start": "a", "nodes": {"a": {"type": "check", "run": "r"}}}}}, name="parts/b.yaml")
    write(tmp_path, {"workflows": {"a": {"start": "a", "nodes": {"a": {"type": "check", "run": "r"}}}}}, name="parts/a.yaml")
    cfg = load_config(write(tmp_path, {**MINIMAL, "includes": ["parts/*.yaml"]}))
    assert list(cfg.workflows) == ["a", "b"]

def test_a_glob_that_matches_nothing_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="nothing matches parts/\\*.yaml"):
        load_config(write(tmp_path, {**MINIMAL, "includes": ["parts/*.yaml"]}))

def test_the_shipped_story_workflow_declares_its_type():
    assert load_config(Path("airlie.yaml")).workflows["story"].type == "story"
```

(`write(tmp_path, data, name)` writes under `tmp_path`; check it creates parent directories, and add that if not.)

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/unit/test_flow_spec.py tests/unit/test_config.py -q`; expected: ValidationError on `type`/`layout`, `file not found: parts/*.yaml`.
- [ ] **Step 3: Implement** the two fields, the layout check, the glob expansion, `airlie.yaml` includes → `config/workflows/*.yaml` (replacing the two explicit lines), `type: story` in `story.yaml`.
- [ ] **Step 4: Run the tests and the story-flow order test** — `uv run pytest tests/unit/test_flow_spec.py tests/unit/test_config.py tests/unit/test_story_flow.py -q`; expected PASS.
- [ ] **Step 5: Commit** — `builder: workflows declare a type and a layout; includes accept a glob`

---

### Task 2: The composer runs from a given mapping; the state gains `artifacts` and `workflow`

**Files:**
- Modify: `src/airlie/flow/composer.py:82-102, 115, 254`; `src/airlie/story/state.py:56-80`; `src/airlie/story/restart.py:205`
- Test: `tests/unit/test_flow_composer.py`, `tests/unit/test_flow_store.py`, `tests/unit/test_story_restart.py`

**Interfaces:**
- Produces: `Composer.__init__(self, cfg, *, runners, registry, store, sessions, workflows: Mapping[str, WorkflowSpec] | None = None)`; `Composer.spec(name) -> WorkflowSpec` (the override mapping when given, else `cfg.workflows`; `KeyError` names the workflow). `StoryState.artifacts: dict[str, Any] = field(default_factory=dict)`; `StoryState.workflow: dict[str, Any] | None = None` (the snapshot: `{"name": str, "specs": {name: WorkflowSpec.model_dump(mode="json")}}`). `_cleared` sets `workflow = None` for every point.

- [ ] **Step 1: Write the failing tests**

```python
async def test_the_composer_runs_a_workflow_from_a_given_mapping_not_the_config(tmp_path):
    cfg = cfg_for(tmp_path)   # no workflows in config
    specs = {"w": WorkflowSpec(start="a", nodes={"a": NodeSpec(type="check", run="r", outputs={"x": "text"})})}
    c = Composer(cfg, runners={"check": check_that(lambda n, c: {"x": 1})}, registry=Registry(), store=MemoryStore({}), sessions=Sessions(), workflows=specs)
    assert await c.run("w") == "a"

def test_undeclared_outputs_land_in_artifacts(tmp_path, repo_with_marker_tests):
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo_with_marker_tests(tmp_path / "repo"))
    s = StateStore(st, loose={}); s.put("artifacts.repro", {"steps": 3}); s.save()
    assert StoryState.open(tmp_path / "AIR-1", text_card("x"), repo_with_marker_tests(tmp_path / "repo")).artifacts["repro"] == {"steps": 3}

def test_restart_clears_the_workflow_snapshot(...)   # extend the parametrized _cleared test: saved["workflow"] is None after any point
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement**; `_walk` and `_parallel` use `self.spec(name)`.
- [ ] **Step 4: Run** `uv run pytest tests/unit/test_flow_composer.py tests/unit/test_flow_store.py tests/unit/test_story_restart.py tests/unit/test_story_flow.py -q`; PASS.
- [ ] **Step 5: Commit** — `builder: the composer runs from a given mapping; the state carries artifacts and the workflow snapshot`

---

### Task 3: A card names its workflow; the pipeline snapshots and runs it

**Files:**
- Create: `src/airlie/story/workflows.py`
- Modify: `src/airlie/story/flow.py:552` (`compose`), `src/airlie/story/pipeline.py:623-710` (`_drive`)
- Test: `tests/unit/test_story_workflows.py` (new), `tests/unit/test_story_flow.py`

**Interfaces:**
- Produces, in `workflows.py`:
  ```python
  LABEL = "workflow:"
  class WorkflowError(StoryError): ...          # message names the workflow and the problem
  def card_workflow(card: Card) -> str           # the workflow: label's name, else "story"
  def resolve_workflows(cfg: AirlieConfig, drafts: Mapping[str, str], name: str) -> dict[str, WorkflowSpec]
      # cfg.workflows plus each draft's YAML text parsed (yaml.safe_load(text)["workflows"]) into WorkflowSpec; raises WorkflowError when `name` is missing or check_workflows reports problems
  def snapshot(name: str, specs: Mapping[str, WorkflowSpec]) -> dict[str, Any]
  def from_snapshot(data: Mapping[str, Any]) -> tuple[str, dict[str, WorkflowSpec]]
  def has_node(spec: WorkflowSpec, name: str) -> bool
  ```
- `compose(pipe, st, card, *, until, specs: Mapping[str, WorkflowSpec] | None = None)`: when `st.workflow` is `None`, it resolves (drafts from `pipe.board.store` when the board is a `LocalBoard`, else `{}`), stores the snapshot and saves; otherwise it loads `from_snapshot(st.workflow)`. It passes the mapping to `Composer(..., workflows=...)` and runs the snapshot's name.
- `_drive`: `name = card_workflow(card)`; the composer branch is taken when `name != "story" or self.s.composer`. For `until == "planned"`: if the workflow has no node named `planned`, emit `story_planned` with `layers=0`, `st.finish(PLANNED)`, `story_done`, and return (the build lane runs it whole). The GitHub prepare/author block runs only when the workflow has a node named `publish`. `WorkflowError` maps to `_block` with its message.

- [ ] **Step 1: Write the failing tests**

```python
def test_card_workflow_defaults_to_story_and_reads_the_label():
    assert card_workflow(text_card("x")) == "story"
    assert card_workflow(replace(text_card("x"), labels=("repo:o/r", "workflow:bug-fix"))) == "bug-fix"

def test_resolve_adds_a_valid_draft_and_refuses_an_invalid_or_missing_one(tmp_path):
    cfg = load_config(Path("airlie.yaml"))
    good = "workflows:\n  mini:\n    type: bug\n    start: intake\n    loose: [card, repo]\n    nodes:\n      intake: {type: check, run: story.intake}\n"
    assert set(resolve_workflows(cfg, {"mini": good}, "mini")) >= {"mini", "story", "layer"}
    with pytest.raises(WorkflowError, match="node intake: check needs run"):
        resolve_workflows(cfg, {"mini": good.replace("run: story.intake", "")}, "mini")
    with pytest.raises(WorkflowError, match="no workflow named 'nope'"):
        resolve_workflows(cfg, {}, "nope")

async def test_a_card_on_a_draft_runs_it_and_keeps_its_snapshot_when_the_draft_changes(tmp_path):
    # pipeline(..., composer=False) with a LocalBoard whose store holds draft "mini" (one check node + review gate)
    # card labelled workflow:mini; run plan() then edit the draft in the store; run build(); assert st.workflow["name"] == "mini",
    # the events show node_started for the ORIGINAL nodes only, stage ends at review
    ...

async def test_a_workflow_without_a_planned_node_passes_the_plan_lane_at_once(tmp_path):
    # plan() on the mini card: events end story_planned(layers=0), story_done; stage == "layers"; no node_started
    ...

async def test_a_story_card_with_no_label_still_runs_the_driver_when_the_flag_is_off(tmp_path):
    # existing equivalence: no node_* events, same as before
    ...
```

- [ ] **Step 2: Run to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** `uv run pytest tests/unit/test_story_workflows.py tests/unit/test_story_flow.py tests/unit/test_story_pipeline.py -q`; PASS.
- [ ] **Step 5: Commit** — `builder: a card names its workflow; the story snapshots and runs it`

---

### Task 4: Drafts in the board store

**Files:**
- Modify: `src/airlie/board/model.py`, `src/airlie/board/store.py:18-33` (`_SCHEMA`) and methods
- Test: `tests/unit/test_board_store.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class WorkflowDraft: name: str; type: str | None; yaml: str; prompts: Mapping[str, str]; created_at: float; updated_at: float; published_pr: int | None; published_url: str | None`.
  Table `workflow_drafts(name TEXT PRIMARY KEY, type TEXT, yaml TEXT NOT NULL, prompts_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL, published_pr INTEGER, published_url TEXT)`.
  `BoardStore.drafts() -> list[WorkflowDraft]` (by name); `draft(name) -> WorkflowDraft` (KeyError); `save_draft(name, *, type: str | None, yaml: str, prompts: Mapping[str, str] = {}) -> WorkflowDraft` (upsert, keeps `published_*`); `delete_draft(name) -> None`; `mark_published(name, number: int, url: str) -> WorkflowDraft`. Names must match `^[a-z0-9]+(-[a-z0-9]+)*$` or `BoardStoreError`.

- [ ] **Step 1: Write the failing tests** — `test_a_draft_is_saved_listed_updated_and_deleted`, `test_a_draft_keeps_its_pr_through_a_save`, `test_a_draft_name_must_be_kebab_case` (assert on the exact fields above).
- [ ] **Step 2–4:** fail, implement, `uv run pytest tests/unit/test_board_store.py -q` PASS.
- [ ] **Step 5: Commit** — `builder: workflow drafts live in the board store`

---

### Task 5: The `/v1/workflows` router and the registry catalog

**Files:**
- Create: `src/airlie/serve/workflows_api.py`
- Modify: `src/airlie/story/flow.py` (catalog), `src/airlie/serve/app.py:425`
- Test: `tests/unit/test_workflows_api.py` (new; use `TestClient(create_app(cfg, api_key=KEY))` as `tests/unit/test_serve.py` does), `tests/unit/test_story_flow.py` (catalog test)

**Interfaces:**
- `story/flow.py` gains `CATALOG: Mapping[str, tuple[str, ...]]` with keys `prompts`, `parsers`, `checks`, `predicates`, `afters`, `turns`, `meetings` listing every `story.*` id, and the test `test_the_catalog_names_every_registered_id` builds a registry with the `pipeline()` helper and asserts the set of ids registered equals the union of `CATALOG`.
- `workflows_router(cfg: AirlieConfig, store: BoardStore, publish: Callable[[str, str | None], None], publisher: Callable[[WorkflowDraft], Awaitable[tuple[int, str]]]) -> APIRouter`, prefix `/v1/workflows`:
  - `GET /` → `{"workflows": [{"name", "type", "source": "file"|"draft", "problems": [str], "pr": {"number","url"}|None}]}`; files from `cfg.workflows` (bundles with no `type` included, flagged `"bundle": true`), drafts from the store validated against `cfg.workflows`.
  - `GET /catalog` → `{"roles": sorted(cfg.roles), "types": [...NodeType], **CATALOG, "prompt_files": [hub rule ids]}`.
  - `GET /{name}` → `{"name","type","source","yaml","layout","problems","prompts","pr"}`; a file's `yaml` is the file text read from `Path(cfg.source_path).parent / "config/workflows" / f"{name}.yaml"` (check the attribute that holds the loaded config's path; `story_config` uses `cfg.source_path`).
  - `PUT /drafts/{name}` body `{"type": str|None, "yaml": str, "prompts": {id: text}}` → 422 when the YAML does not parse or does not hold exactly `workflows: {<name>: ...}`; 409 when `name` is a file workflow; else saves and returns the `GET /{name}` shape with `problems` (possibly non-empty). Publishes `feed` topic `"workflows"`.
  - `DELETE /drafts/{name}` → 204.
  - `POST /validate` body `{"yaml": str}` → `{"problems": [str]}` (no save).
  - `POST /{name}/publish` → 422 when problems, 404 when not a draft, else awaits `publisher(draft)`, `mark_published`, returns `{"number","url"}`.
- `app.py` wires `app.include_router(workflows_router(cfg, board.store, feed.publish, publisher))` when the board is local; `publisher` is Task 7's function bound to `cfg` (a stub raising `HTTPException(501)` until Task 7 lands).

- [ ] **Step 1: Write the failing tests** — one per route: list shows `story` as a file with `type: story` and `layer` as a bundle; `PUT` a valid draft then `GET` it; `PUT` a draft named `story` → 409; `PUT` unparseable → 422; `POST /validate` returns the exact `check_workflows` problem string; `DELETE`; `GET /catalog` has `roles` and `prompts`.
- [ ] **Step 2–4:** fail, implement, `uv run pytest tests/unit/test_workflows_api.py tests/unit/test_story_flow.py -q` PASS.
- [ ] **Step 5: Commit** — `builder: the workflows API lists, validates and saves drafts; the registry has a catalog`

---

### Task 6: The board refuses bad workflows; the line resolves them

**Files:**
- Modify: `src/airlie/board/api.py` (`board_router(store, publish, workflow_check: Callable[[str], str | None] | None = None)`), `src/airlie/serve/app.py:425`, `src/airlie/story/line.py:465` (`_story_line`)
- Test: `tests/unit/test_board_api.py` (or the existing board API test file), `tests/unit/test_story_line.py`

**Interfaces:**
- `workflow_check(name) -> str | None` returns a problem string or `None`; built in `app.py` from `resolve_workflows(cfg, drafts, name)` catching `WorkflowError`. `PATCH /items/{key}` with a `workflow:` label and `POST /items/{key}/move` to `approved` on a story carrying one return 422 with that problem.
- `_story_line`: before `pipe.<how>(card)`, `resolve_workflows(cfg, drafts, card_workflow(card))`; on `WorkflowError` it posts a note `f"workflow {name}: {problem}"` through the board (once per problem text; reuse the dedupe pattern of `factory._set_aside` notes if one exists, else keep a set on the Line) and returns `("held", None)` without moving the card.

- [ ] **Step 1: Write the failing tests** — `test_a_label_naming_an_unknown_workflow_is_refused_with_422`, `test_moving_a_story_with_an_invalid_draft_to_approved_is_refused`, `test_the_line_holds_a_card_whose_draft_no_longer_validates_with_a_note` (TickRig: save a draft, label the card, break the draft, tick; assert the card's status unchanged and a comment starting `workflow mini:`).
- [ ] **Step 2–4:** fail, implement, `uv run pytest tests/unit/test_board_api.py tests/unit/test_story_line.py -q` PASS.
- [ ] **Step 5: Commit** — `builder: the board refuses a workflow that does not validate; the line holds the card with a note`

---

### Task 7: Publish writes the YAML and opens a PR; bookkeeping retires merged drafts

**Files:**
- Create: `src/airlie/story/workflow_publish.py`
- Modify: `src/airlie/story/fixes.py:339` (`bookkeep`), `src/airlie/serve/app.py` (bind the publisher)
- Test: `tests/unit/test_workflow_publish.py` (new, with `RecordedGh`), `tests/unit/test_story_fixes.py`

**Interfaces:**
- ```python
  def with_layout(text: str, layout: Mapping[str, Layout]) -> str
      # replaces or appends the `layout:` block of the single workflow in `text`, touching no other line
  def same_definition(text: str, draft_yaml: str) -> bool
      # True when both parse to equal `workflows` after dropping `layout`
  async def publish_workflow(cfg: AirlieConfig, publisher: Publisher, draft: WorkflowDraft, *, home: Path, gh: Runner | None = None) -> tuple[int, str]
  ```
  Steps: `publisher.fetch(home, trunk)`; worktree `cfg.observability.state_dir / "workflows" / draft.name / "publish"` via `add_worktree(home, path, branch=f"airlie/workflow/{draft.name}", start_point=f"{remote}/{trunk}")` (remove any stale one first); target `config/workflows/<name>.yaml`: if it exists and `same_definition(existing, draft.yaml)` → `with_layout(existing, layout)`; else write `draft.yaml` as is; write each `draft.prompts[id]` to `src/airlie/prompts/rules/<id>.md`; commit as `publisher.author(home)` with subject `f"workflow: {draft.name} ({draft.type or 'bundle'})"`; `publisher.push_top`, `publisher.create_draft(wt, branch, trunk, title)`, `publisher.pr` → `(number, url)`; `remove_worktree`.
- `bookkeep` gains: for each draft with `published_pr`, when `gh pr view <url> --json state` says `MERGED` and `draft.name in cfg.workflows`, `store.delete_draft(name)` and a `line_event("draft_retired", name=...)`.

- [ ] **Step 1: Write the failing tests** — `test_with_layout_adds_a_block_and_leaves_every_other_line_alone` (story.yaml text: diff of lines is exactly the layout lines), `test_same_definition_ignores_layout_and_formatting`, `test_publish_writes_the_file_and_prompts_in_a_worktree_and_opens_a_pr` (RecordedGh records `gh pr create --draft --base main --head airlie/workflow/mini`; the serve checkout `home` has no new files; the worktree is gone after), `test_bookkeeping_retires_a_merged_draft_once_the_file_is_loaded` (merged_gh; cfg with the workflow present → row deleted; absent → kept).
- [ ] **Step 2–4:** fail, implement, `uv run pytest tests/unit/test_workflow_publish.py tests/unit/test_story_fixes.py tests/unit/test_workflows_api.py -q` PASS.
- [ ] **Step 5: Commit** — `builder: publish writes the workflow and its prompts on a branch and opens a PR; merged drafts retire`

---

### Task 8: Prompt files as a second kind of prompt

**Files:**
- Create: `src/airlie/flow/prompt_files.py`
- Modify: `src/airlie/flow/composer.py:35-48` (`Registry`), `src/airlie/story/flow.py` (`compose` passes draft prompts), `src/airlie/flow/spec.py` (`check_workflows` accepts `prompt: "prompt:<id>"` as satisfying "needs prompt" and checks the id exists in the hub or the given prompt texts)
- Test: `tests/unit/test_prompt_files.py`, `tests/unit/test_flow_spec.py`

**Interfaces:**
- ```python
  PREFIX = "prompt:"
  def prompt_builder(id: str, texts: Mapping[str, PromptText]) -> Callable[[RunContext, str | None], str]
      # renders the hub entry (or texts[id] for a draft) with fields = {input: str(store.get(input)) for input in node.inputs that the file declares} plus "feedback" when declared
  def parse_draft_prompts(prompts: Mapping[str, str]) -> dict[str, PromptText]   # hub.parse(name, "rule", raw)
  ```
  `Registry(prompts: Mapping[str, PromptText] = {})`; `__getitem__` resolves `prompt:<id>` through `prompt_builder` when not registered explicitly. `check_workflows(workflows, *, prompts: Collection[str] = ())` treats `prompt:<id>` as valid when `id` is a hub rule id or in `prompts`, else `"workflow <w>: node <n>: unknown prompt file '<id>'"`.

- [ ] **Step 1: Write the failing tests** — a draft prompt with `fields: [card, feedback]` renders the card text and the feedback through `ask_runner` over a `MemoryStore` (reuse `run_context`), a missing field is a `PromptError`, `check_workflows` flags an unknown prompt file.
- [ ] **Step 2–4:** fail, implement, `uv run pytest tests/unit/test_prompt_files.py tests/unit/test_flow_spec.py tests/unit/test_prompt_hub.py -q` PASS.
- [ ] **Step 5: Commit** — `builder: a workflow may name a prompt file; drafts carry their prompts`

---

### Task 9: The card's Workflow field

**Files:**
- Create: `web/src/api/workflow_defs.ts`, `web/src/screens/board/workflow.ts`, `web/src/stores/card_draft.ts`
- Modify: `web/src/screens/board/CreateModal.vue`, `CardModal.vue`, their tests
- Test: `web/src/screens/board/workflow.test.ts`, `CreateModal.test.ts`, `CardModal.test.ts`, `web/src/stores/card_draft.test.ts`

**Interfaces:**
- `workflow_defs.ts`: `type WorkflowDef = { name; type: string | null; source: "file" | "draft"; problems: string[]; pr: { number; url } | null; bundle?: boolean }`; `listWorkflowDefs(): Promise<WorkflowDef[]>`, `getWorkflowDef(name)`, `saveDraft(name, body: { type; yaml; prompts })`, `deleteDraft(name)`, `validateWorkflow(yaml): Promise<string[]>`, `publishWorkflow(name): Promise<{ number; url }>`, `registryCatalog()`.
- `workflow.ts`: `WORKFLOW_PREFIX = "workflow:"`, `workflowOf(labels): string` (default `"story"`), `withWorkflow(labels, name): string[]`.
- `card_draft.ts` (Pinia): `hold(form: CreateForm): string` (returns a token), `take(token): CreateForm | null`, `setWorkflow(token, name)`. `CreateForm = { level; title; description; parent; repo; workflow }`.
- `CreateModal`: a `Workflow` select (`data-workflow`) after Repository, required, default `story`, options grouped by `type` (files), then "Drafts", then `＋ Build new…`; choosing it holds the form and `router.push({ name: "builder", params: { name: "new" }, query: { card: token } })`. On mount with `?card=<token>` in the route, it restores the form and the chosen workflow. Submit sends `labels` with the repo and workflow labels.
- `CardModal`: a Workflow row (`data-workflow`) with a select; saving patches labels via `withWorkflow`; shows "draft" or `PR #n` from `listWorkflowDefs()`.

- [ ] **Step 1: Write the failing tests** — `workflowOf` default and parse; CreateModal renders the select with `story` selected and submits `workflow:story`; choosing "Build new…" pushes the builder route and the store holds the typed title; CardModal patches `workflow:bug-fix`.
- [ ] **Step 2–4:** fail (`npm test -- workflow`), implement, `cd web && npm run typecheck && npm run lint && npm test` PASS.
- [ ] **Step 5: Commit** — `builder: the card picks its workflow; Build new… holds the card and opens the builder`

---

### Task 10: The builder screen, read-only

**Files:**
- Create: `web/src/screens/BuilderScreen.vue`, `web/src/screens/builder/graph.ts`, `layout.ts`, `yaml.ts`, `Canvas.vue`, `Palette.vue`, with tests
- Modify: `web/src/router.ts` (route `/workflows/build/:name` named `builder`, declared before `/workflows/:key`), `web/package.json` (add `yaml` ^2), `WorkflowsScreen.vue` (a "Definitions" table listing `listWorkflowDefs()` with an Open link)
- Test: `web/src/screens/builder/*.test.ts`, `BuilderScreen.test.ts`

**Interfaces:**
- `graph.ts`: `type GNode = { id; type: NodeType; spec: Record<string, unknown>; x; y }`; `type GEdge = { from; to; kind: "sequence" | "parallel" | "loop" | "branch" | "each"; label?: string; loop?: { max; when?; carry? } }`; `type Graph = { name; type: string | null; start; loose: string[]; nodes: GNode[]; edges: GEdge[]; bundles: Record<string, Graph> }`; `fromWorkflow(name, spec, layout): Graph` (edges: dict-order sequence unless `next`; `branch` → one edge per value labelled with it; `loop.to` → a `loop` edge; `parallel` node → `parallel` edges to each child and from each child to the next sequential node; `each` → the node carries `workflow` and draws as a container).
- `layout.ts`: `autoLayout(graph): Graph` (left-to-right by topological order, 160px columns, parallel children stacked 90px apart).
- `yaml.ts`: `parseWorkflow(text): { name; spec; layout }` and `toYaml(graph): string` (the `yaml` package; node keys in canvas order; `layout:` last).
- `Canvas.vue`: props `{ graph: Graph; selected: string | null; readonly?: boolean }`, emits `select(id)`, `move(id, x, y)`; renders boxes absolutely positioned in the six shapes (reuse `JobBox`/`CodeBox`/`MilestoneMark` styles via a `BuilderBox.vue` that maps `type` → shape: ask/conversation/write/meeting → model, check → code, gate → dashed code, parallel → bracket, each → container), SVG edges (plain, dashed for loop, labelled for branch).
- `BuilderScreen.vue`: loads `getWorkflowDef(name)` (or an empty graph for `new`), shows the canvas, the palette (read-only this task), and a YAML tab with the text.

- [ ] **Step 1: Write the failing tests** — `fromWorkflow` on the shipped `story.yaml` text yields 17 nodes, a `parallel` edge pair for `plan_story`, an `each` container for `build_layers`; `autoLayout` gives distinct, increasing `x` along the sequence; `toYaml(fromWorkflow(...))` reparses to the same `nodes` (order and fields); `BuilderScreen` renders a box per node for a mocked def.
- [ ] **Step 2–4:** fail, implement, web gate PASS.
- [ ] **Step 5: Commit** — `builder: the builder draws a workflow as boxes and wires`

---

### Task 11: Editing: palette, wires, forms, validation, save draft

**Files:**
- Create: `web/src/screens/builder/NodeForm.vue`, `WireForm.vue`, `Problems.vue`
- Modify: `Canvas.vue`, `Palette.vue`, `BuilderScreen.vue`, `graph.ts` (`addNode`, `connect`, `removeEdge`, `toWorkflow(graph): { spec; layout }`)
- Test: beside each

**Interfaces:**
- Palette drag → `addNode(graph, type, x, y)` (id `type-N`, default spec per type: `check` gets `run: ""`, `ask` gets `role`, `prompt`, `outputs: {}`).
- Connect: drag from a box's right port to another's left port opens `WireForm` to pick the move: sequence (sets `next` on the source when it is not the dict-order successor), parallel (adds the target to a `parallel` node created or reused between them), loop (`loop: {to, max, when?, carry?}` on the source), branch (`on` + `branch[value]`), each (the target becomes the `each` node's `workflow`).
- `NodeForm`: fields from the contract with dropdowns fed by `registryCatalog()`: role(s) from `roles`, prompt from `prompts` plus prompt files (and a "New prompt file…" that opens a markdown editor with `fields` and `why`), parse, run, when, after, inputs (checkboxes of upstream outputs and loose names), outputs (name + format), steps, tools, done, on_fail, args as JSON.
- `Validate` → `validateWorkflow(toYaml(graph))` → `Problems.vue` lists them; each problem naming `node <n>` highlights that box (parse the problem string for `node <id>`).
- `Save draft` → `saveDraft(name, { type, yaml: toYaml(graph), prompts })`; for `new`, a name dialog (kebab-case) and a type field first.
- Box drag updates `x, y`.

- [ ] **Step 1: Write the failing tests** — `connect` for each of the five moves produces the expected spec keys; `toYaml` of a graph with a loop reparses with `loop.to`; NodeForm offers the catalog's prompts; a problem string `workflow w: node fix: input 'repro' …` highlights `fix`; Save calls `saveDraft` with the serialised YAML.
- [ ] **Step 2–4:** fail, implement, web gate PASS.
- [ ] **Step 5: Commit** — `builder: boxes, wires, forms, validation and saving a draft`

---

### Task 12: Publish and the round trip to the card

**Files:**
- Modify: `BuilderScreen.vue`, `CreateModal.vue`, `web/src/stores/card_draft.ts`
- Test: `BuilderScreen.test.ts`, `CreateModal.test.ts`

**Interfaces:**
- `Publish` → `publishWorkflow(name)`; the bar then shows `PR #n` as a link; disabled while `problems.length > 0`.
- Return: when the route has `?card=<token>`, `Save draft` and `Publish` both end with `router.push({ name: "board", query: { create: token } })` after `cardDraft.setWorkflow(token, name)`; `BoardScreen` opens `CreateModal` when `?create=` is present (the modal restores the held form, with the new workflow selected). A `Cancel` in the builder returns the same way without saving or setting a workflow.

- [ ] **Step 1: Write the failing tests** — Publish disabled with problems, enabled without, shows the PR link; a builder opened with `?card=` returns to the board with the token and the form restored including the typed title, and the new workflow selected; Cancel restores the form unchanged and no `saveDraft` call was made.
- [ ] **Step 2–4:** fail, implement, web gate PASS.
- [ ] **Step 5: Commit** — `builder: publish opens the PR; the builder returns to the card it came from`

---

### Task 13: The generic run view

**Files:**
- Create: `src/airlie/serve/flow_nodes.py`
- Modify: `src/airlie/serve/flow.py:33` (`story_flow`), `web/src/screens/workflow/Pipeline.vue` only if a `kind: "layer"` lane per each-item needs a label change
- Test: `tests/unit/test_flow_nodes.py` (new), `tests/unit/test_serve_flow.py` (or wherever `story_flow` is tested)

**Interfaces:**
- `def node_flow(st: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]`: walks `st["workflow"]["specs"][name].nodes` in dict order; one `FlowNode` per node with one job (`job(id=node, label=node, role=spec.role or "", status, shape)`), shape by type (ask/conversation/write/meeting → `model`, check → `code`, gate → `code` with tag `gate`, `planned`-like milestones → `milestone`), status from the node events (`node_started` without finish → running; `node_finished` outcome done → passed, failed → failed; `node_skipped` → skipped; none → pending); a `parallel` node's children share a `group`; an `each` node becomes one `kind: "layer"` node per item seen in events, each holding the bundle's nodes as jobs; the hover `card` lists the node's events as `step_row`s.
- `story_flow`: when `st.get("workflow")` is set and its name is not `story`, `nodes = node_flow(st, events)` instead of `plan_nodes + build_nodes`.

- [ ] **Step 1: Write the failing tests** — a snapshot with `intake → reproduce → fix_layers(each) → review` and events for two items yields nodes in order with statuses `passed, running, …`, two layer lanes, and the review gate pending; a story card still uses the story builders.
- [ ] **Step 2–4:** fail, implement, `uv run pytest tests/unit/test_flow_nodes.py tests/unit/test_serve*.py -q` PASS; web gate (no change expected).
- [ ] **Step 5: Commit** — `builder: a card on any workflow has a run view drawn from its nodes`

---

### Task 14: Browser test and the full gate

**Files:**
- Create: `tests/e2e/test_workflow_builder.py`
- Test: run through `task test:e2e`

- [ ] **Step 1: Write the browser test** (pattern of `tests/e2e/test_workflows_list.py`): seed a state dir with the shipped config; open the board, click New, choose `Build new…` in the Workflow select, land on `/workflows/build/new`, name it `mini`, drop a `check` box, set `run: story.intake`, Validate (expect the problems panel empty), Save draft, return to the board, assert the card form shows `mini (draft)` selected and the typed title; open `/workflows/build/story`, assert a box per story node. `evidence.expect` on each check.
- [ ] **Step 2: Run** `task test:e2e -- -k workflow_builder`; PASS with screenshots under `test-results/e2e/evidence/`.
- [ ] **Step 3: Full gate** `uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q -n auto --maxprocesses 8 && task web:gate`; all PASS.
- [ ] **Step 4: Commit** — `builder: browser test for the card field and the builder`
