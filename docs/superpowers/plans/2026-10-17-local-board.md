# Local Board Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A board inside airlie (initiative → epic → story → subtask) stored in SQLite, exposed under `/v1/board`, and usable by the story pipeline through the existing `Board` seam.

**Architecture:** A new package `airlie.board` holds the model, a SQLite store and a FastAPI router. `airlie.story.local_board.LocalBoard` adapts the store to the `Board` protocol the pipeline already uses, selected by `story.board`. serve mounts the router; the Taskfile gains an export task.

**Tech Stack:** Python 3.12, pydantic (Strict models as in `config/schema.py`), stdlib `sqlite3`, FastAPI `APIRouter`, pytest with `TestClient`.

**Spec:** `docs/superpowers/specs/2026-10-17-web-app-and-local-board-design.md` (section "The local board").

## Global Constraints

- Levels: `initiative`, `epic`, `story`, `subtask`. A parent must be exactly one level up; an initiative has no parent.
- Statuses: `todo`, `doing`, `review`, `blocked`, `done`.
- Keys: `<prefix>-<n>`, `board.prefix` default `AL`; keys satisfy `story.model.ISSUE_KEY` (`[A-Z][A-Z0-9_]+-\d+`).
- Storage: `observability.state_dir / "board.sqlite"`, WAL mode.
- `story.board: "jira" | "local"`, default `"jira"`.
- Tests: no live engine, no network; `task check` passes. Commits end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

- A parent two levels up (subtask under epic) must be refused with 422, not stored. (Task 2 and Task 4 tests.)
- Deleting or re-parenting is not v1; `PATCH` with `parent` is accepted only when the new parent is a valid level up, else 422. (Task 4.)
- Two serve workers are not supported; the store opens with `check_same_thread=False` and one connection per call, so a test with concurrent requests must still see consistent counters. (Task 2 test `test_keys_are_unique_under_concurrent_creates`.)
- `next_card()` returns only `story`-level items in `todo`, oldest first by `created_at`; epics and initiatives in `todo` are never built. (Task 5.)
- `move` to `blocked` with a reason records the reason as a comment by `airlie`, so the blocked reason survives serve restarts. (Task 5.)

---

### Task 1: Config: `board` section and `story.board`

**Files:**
- Modify: `src/airlie/config/schema.py` (add `BoardSpec`; add `board: BoardSpec` to `AirlieConfig`; add `board: Literal["jira", "local"] = "jira"` to `StorySpec`)
- Modify: `airlie.yaml` (a commented `board:` block under Stories)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `BoardSpec(prefix: str = "AL")` with a validator refusing a prefix that does not match `[A-Z][A-Z0-9_]+`; `cfg.board.prefix`; `cfg.story.board`.

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_config.py`, next to the other schema tests:

```python
def test_board_defaults_and_story_board_default(tmp_path: Path) -> None:
    cfg = load(tmp_path, {})  # the file's existing minimal-config helper
    assert cfg.board.prefix == "AL" and cfg.story.board == "jira"

def test_board_prefix_must_look_like_an_issue_key_prefix(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="board.prefix"):
        load(tmp_path, {"board": {"prefix": "al-1"}})
```

- [ ] **Step 2: Run them** `uv run pytest tests/unit/test_config.py -k board -v` → FAIL (no `board` attribute).
- [ ] **Step 3: Implement** `BoardSpec` in `schema.py` after `StorySpec`, and the two fields. The prefix validator raises `ValueError("board.prefix must be letters, digits or _ starting with a letter")`; check how other validators surface as `ConfigError` with a path (follow `RoleSpec`'s pattern).
- [ ] **Step 4: Run** the same command → PASS. Then `uv run pytest -q tests/unit/test_config.py` → all pass.
- [ ] **Step 5: Commit** `feat(config): board prefix and story.board backend`.

### Task 2: The model and the SQLite store

**Files:**
- Create: `src/airlie/board/__init__.py` (exports `Item`, `Level`, `Status`, `Comment`, `BoardStore`, `BoardStoreError`)
- Create: `src/airlie/board/model.py`
- Create: `src/airlie/board/store.py`
- Test: `tests/unit/test_board_store.py`

**Interfaces:**
- Produces, in `model.py`:
  - `Level = Literal["initiative", "epic", "story", "subtask"]`; `LEVELS: tuple[Level, ...]` in that order; `Status = Literal["todo", "doing", "review", "blocked", "done"]`.
  - `@dataclass(frozen=True) Comment(author: str, text: str, at: float)`.
  - `@dataclass(frozen=True) Item(key: str, level: Level, title: str, description: str, status: Status, parent: str | None, labels: tuple[str, ...], comments: tuple[Comment, ...], created_at: float, updated_at: float, created_by: str)` with `to_dict() -> dict[str, Any]`.
  - `def parent_level(level: Level) -> Level | None`.
- Produces, in `store.py`:
  - `class BoardStoreError(ValueError)`.
  - `class BoardStore` with `__init__(self, path: Path, *, prefix: str = "AL")`; `create(self, *, level: Level, title: str, description: str = "", parent: str | None = None, labels: Sequence[str] = (), created_by: str = "noah") -> Item`; `get(self, key: str) -> Item` (raises `KeyError`); `list(self, *, level: Level | None = None, status: Status | None = None, parent: str | None = None) -> list[Item]` (ordered by `created_at`); `update(self, key: str, *, title=None, description=None, status: Status | None = None, parent: str | None = None, labels: Sequence[str] | None = None) -> Item`; `comment(self, key: str, author: str, text: str) -> Item`; `export(self) -> dict[str, Any]` (`{"prefix", "items": [item.to_dict()...]}`); `close()`.
  - Schema: tables `items(key PRIMARY KEY, n INTEGER, level, title, description, status, parent, labels_json, created_at, updated_at, created_by)`, `comments(id INTEGER PRIMARY KEY, item_key, author, text, at)`, `meta(k PRIMARY KEY, v)` holding `next_n`. `PRAGMA journal_mode=WAL`. Key allocation and insert happen in one transaction (`BEGIN IMMEDIATE`).

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_board_store.py`):

```python
def test_create_allocates_keys_with_the_prefix_and_sets_defaults(tmp_path): ...  # AL-1, AL-2; status todo; labels (); created_by "noah"
def test_a_parent_must_be_exactly_one_level_up(tmp_path): ...  # epic under initiative ok; subtask under epic -> BoardStoreError; initiative with parent -> BoardStoreError
def test_a_parent_must_exist(tmp_path): ...  # "AL-99" -> BoardStoreError
def test_update_changes_fields_and_bumps_updated_at(tmp_path): ...
def test_comment_appends_in_order_with_author(tmp_path): ...
def test_list_filters_by_level_status_and_parent_in_created_order(tmp_path): ...
def test_keys_are_unique_under_concurrent_creates(tmp_path): ...  # 20 threads × 5 creates on one store -> 100 distinct keys
def test_export_round_trips_every_field(tmp_path): ...
def test_the_store_survives_reopening(tmp_path): ...  # close, reopen same path, keys continue from next_n
```

- [ ] **Step 2: Run** `uv run pytest tests/unit/test_board_store.py -v` → FAIL (import error).
- [ ] **Step 3: Implement** `model.py` then `store.py`. Open one `sqlite3` connection per store with `check_same_thread=False` and a `threading.Lock` around writes; `labels` stored as JSON; `parent` validated with `parent_level`.
- [ ] **Step 4: Run** → PASS. `uv run mypy` clean.
- [ ] **Step 5: Commit** `feat(board): item model and SQLite store`.

### Task 3: `task board:export`

**Files:**
- Modify: `src/airlie/cli.py` (a `board` typer sub-app with `export [--out PATH]`)
- Modify: `Taskfile.yml` (`board:export` task, desc: "Write the local board as JSON next to board.sqlite (task board:export -- --out FILE)")
- Test: `tests/unit/test_cli_board.py`

**Interfaces:**
- Consumes: `BoardStore.export()`.
- Produces: `airlie board export` writing `state_dir/board.json` by default, printing the path.

- [ ] **Step 1: Test** with typer's `CliRunner` (as `tests/unit/test_cli_story.py` does): create two items in a store under a temp state_dir, run `export`, assert the JSON file lists both keys.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS. **Step 5: Commit** `feat(cli): board export`.

### Task 4: The HTTP API

**Files:**
- Create: `src/airlie/board/api.py`
- Modify: `src/airlie/serve/app.py` (in `create_app`: build `BoardStore(cfg.observability.state_dir / "board.sqlite", prefix=cfg.board.prefix)`, `app.include_router(board_router(store))`, close it in the existing `shutdown` handler)
- Test: `tests/unit/test_board_api.py`

**Interfaces:**
- Produces: `def board_router(store: BoardStore) -> APIRouter` with prefix `/v1/board`:
  - `GET /items?level=&status=&parent=` → `{"items": [...]}`
  - `POST /items` body `{"level","title","description"?,"parent"?,"labels"?}` → 201 + item; a level or parent error → 422 `{"detail": "<BoardStoreError message>"}`
  - `GET /items/{key}` → item or 404
  - `PATCH /items/{key}` body any of `title, description, status, parent, labels` → item; errors 422
  - `POST /items/{key}/comments` body `{"text"}` → item (author `"noah"` in v1; a later per-person auth sets it)
  - `GET /export` → the export dict
- The key middleware already guards `/v1`, so these need the Bearer key.

- [ ] **Step 1: Tests** with `TestClient(create_app(cfg))` using `test_serve.py`'s `cfg` fixture pattern: create an initiative, an epic under it, a story under the epic; list by parent; patch status to `doing`; a subtask under the initiative → 422; unknown key → 404; with `api_key="k"` a request without the header → 401.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement** with pydantic request models (`extra="forbid"`). **Step 4: Run** → PASS; `uv run pytest -q tests/unit/test_serve.py` still passes. **Step 5: Commit** `feat(serve): /v1/board API`.

### Task 5: `LocalBoard` behind the `Board` seam

**Files:**
- Create: `src/airlie/story/local_board.py`
- Modify: `src/airlie/story/factory.py:143-149` (`make_board`: `if cfg.story.board == "local": return LocalBoard(BoardStore(...))`)
- Modify: `docs/integrations/story.md` (a short "Local board" subsection: `story.board: local`, keys, `task board:export`)
- Test: `tests/unit/test_local_board.py`

**Interfaces:**
- Consumes: `BoardStore`, `Card`, `Column`, `note()` from `story/board.py`.
- Produces: `class LocalBoard(Board)` with `__init__(self, store: BoardStore)`; `card(ref)` → `Card(key=item.key, id=item.key, ref=item.key, title, description, url=None, comments=tuple(c.text for c in item.comments), labels=item.labels)` for a `story`; a non-story ref → `BoardError("AL-3 is an epic; only stories are built")`; `next_card()` → oldest `story` in `todo` or None; `move(card, column)` maps intake→todo, building→doing, review→review, blocked→blocked; `comment(card, text)` → `store.comment(key, "airlie", note(text))`.

- [ ] **Step 1: Tests**: `next_card` skips epics and initiatives and picks the oldest story; `card` of an epic raises `BoardError`; `move` maps every column; `comment` is written with the airlie prefix; `make_board` returns a `LocalBoard` when `story.board: local` (config via the YAML helper), and still a `NullBoard` when `jira` is unconfigured.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS; then the full gate: `uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`. **Step 5: Commit** `feat(story): local board behind the Board seam`.

### Task 6: PR

- [ ] Push and open the PR against `main` (`gh pr create --repo weyenk/airlie --base main`); body: why (spec link), what, the gate's result; ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- [ ] Merge only when the gate passes locally on the branch merged with current `origin/main` AND CI `static` and `unit` are green: `gh pr merge <N> --repo weyenk/airlie --merge --admin`. A known-flaky cancellation-timing test gets one rerun (`gh run rerun <run> --failed`).
