# Chat turn route, plain chat and superpowers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /v1/airlie/chat/turn` runs a plain chat turn on the server (a streaming tool loop on the `chat` role, holding Gemma through the manager), streams it as the AI SDK 7 UI message stream, saves the thread on the server, and starts every session with the vendored superpowers bootstrap and the `skill` and `todo` tools.

**Architecture:** A new `airlie.chat` package holds the pieces: a protocol encoder (`stream.py`), the kind rules (`kinds.py`), a thread store (`threads.py`), the hooks (`hooks.py`), the superpowers library and prompt builder (`superpowers.py`), the `skill` and `todo` tools (`tools.py`), and the turn runner (`turn.py`) that the route in serve calls. The runner holds the chat role's engine as `chat:{thread_id}` through the manager for the whole turn (every loop step), runs `engine.generate` with an `on_token` callback feeding the stream, runs tools server-side, and persists on `on_finish`. Repository chats, the `task` subagent tool and the VM are plan 4; this plan registers only the plain kind's tools and refuses the other kind with a clear 400.

**Tech Stack:** Python 3.12, FastAPI `StreamingResponse` (SSE), asyncio, sqlite3 (as `auth/store.py`), pydantic config. No new dependencies; no web changes (see "Client switch" below).

**Spec:** [`docs/superpowers/specs/2026-11-11-airlie-web-chat-agent-design.md`](../specs/2026-11-11-airlie-web-chat-agent-design.md), phase 3: "Two kinds of chat", "Superpowers in chat", "Thread, transport, and the redesign" (transport bullets), "Hooks", "Configurable behavior" (`chat.kinds`, `chat.superpowers`), "Testing" (turn route, superpowers harness). Builds on [`2026-11-11-on-demand-engines.md`](2026-11-11-on-demand-engines.md): `Manager.hold(name, owner)`, `Status`, `InProcessHolder`, `ManagedEngine`, `serving`.

**Out of scope (stated, not forgotten):** the `task` subagent tool and everything repository-chat (plan 4: the VM, builtin file and shell tools, `compute`, confirmations in the thread, push/PR); the redesigned thread, left nav and `@ai-sdk/vue` client (plan 5); PWA-b's push (it plugs into the hook point Task 5 leaves); compaction.

**Client switch (decision):** none. Plan 3 adds no web code and no flag. The existing Chat screen stays on `/v1/chat/completions` until plan 5 moves it to `@ai-sdk/vue`'s `Chat` with `DefaultChatTransport`. The end-to-end proof is a functional test that drives the real route with a scripted engine and parses the SSE, plus a live `curl -N` check (Task 8). A temporary flag in `web/` would be a stopgap that plan 5 deletes.

## Global Constraints

- Every task passes the gate from the repository root, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`.
- Unit tests over 500 ms or on the real clock go in `tests/unit/quarantine.toml`. Tests use `airlie.serve.bridge.clock.FakeClock` and a scripted engine, never a real engine, network or VM.
- Tests follow `skills/qa/*.md` (the layer guidelines):
  - Unit (`tests/unit`): one behaviour per test, named for the scenario and outcome, Arrange-Act-Assert, no loops or conditionals in the body; fakes over mocks, doubles only at seams (clock, network, process, disk).
  - A test that drives a FastAPI app through `TestClient` goes in `tests/functional`: real routes and middleware, doubles only beyond the module boundary, one request per test, and for each new route the happy path, 401, 422 or 400, 404 and a dependency failure, asserting the body and not just the status.
  - Fakes are rebuilt per test (fixtures), never shared; every new test is seen red before the code is written.
  - New modules that hold logic are enrolled in `[tool.airlie.mutation].enrolled` once `task mutate:changed` is clean for them; changes to an already enrolled module (`config/schema.py`) need tests that catch every new mutant.
  - E2E is only for critical journeys: this plan adds none (no web change).
- Dependencies through uv only. No Docker.
- Wire format: SSE, response header `x-vercel-ai-ui-message-stream: v1`, each event `data: <json>\n\n`, chunk `type`s `start`, `start-step`, `text-start`/`text-delta`/`text-end`, `reasoning-start`/`reasoning-delta`/`reasoning-end`, `tool-input-start`, `tool-input-available`, `tool-output-available`, `tool-output-error`, `data-<name>` (with `"transient": true` for status), `finish-step`, `finish`, `error`, last event `data: [DONE]`.
- The server enforces the kind: the runner builds the registry from the kind's tool list and nothing the client sends can add a tool. A plain chat never gets `read_file`, `write_file`, `edit_file`, `list_dir`, `grep`, `shell`, `run_tests`, `git` or `compute`.
- The superpowers bootstrap (`using-superpowers` plus `airlie-tools.md`) is in every session's system prompt and never in a subagent's. The vendored skills are pinned and unedited (Task 1 checks their hashes).
- The turn holds the chat role's engine through the manager: `chat:{thread_id}` is the owner string. Status the user waits on arrives as `data-engine` chunks.
- `POST /v1/chat/completions`, `/v1/airlie/chat/attachments` and `/v1/airlie/chat/skills` keep working unchanged.
- Config keys (exact): `chat.superpowers: {enabled: bool = true, path: Path = vendor/superpowers}`; `chat.kinds.plain: {tools: list[str] = ["web_search", "web_fetch", "skill", "todo"], ask_first: list[str] = []}`; `chat.kinds.repository: {tools: [...], ask_first: [...]}` is parsed and validated here but unused until plan 4; `chat.max_steps: int = 12` (tool calls per turn before the forced answer).

## Review Focus

1. **The client closes the tab mid-turn** (phone locks, network drops). Expected: generation is cancelled, the engine hold is released (the idle timer starts), the partial reply is saved with `interrupted: true`, and a later GET shows it. Test in Task 7 (`test_a_disconnect_cancels_generation_releases_the_hold_and_saves_the_partial`).
2. **The model calls a tool the kind does not have** (`shell` in a plain chat), or a skill file path that climbs out of the skill's directory. Expected: a `tool-output-error` the model can read, no side effect, the turn continues. Tests in Task 2 (`test_skill_refuses_paths_outside_the_skill_directory`) and Task 6 (`test_a_tool_outside_the_kind_is_an_error_result_not_a_run`).
3. **Two turns on one thread at once** (two devices). Expected: the second gets 409 and the thread is not interleaved. Test in Task 7 (`test_a_second_turn_on_a_busy_thread_is_409`).
4. **The model never stops calling tools, or the engine fails mid-turn** (waiting for memory times out, the engine dies). Expected: after `chat.max_steps` one last call offers no tools; an engine failure is an `error` chunk, then `finish` and `[DONE]`, the hold released, the user message still saved. Tests in Task 6 (`test_steps_are_bounded_then_one_last_call_without_tools`) and Task 7 (`test_an_engine_failure_is_an_error_chunk_and_the_user_message_is_kept`).
5. **A thread another person owns, or a turn with no session.** Expected: 401 with no principal, 404 (not 403) for another person's thread id, so ids do not leak. Tests in Task 7.

---

### Task 1: Vendored superpowers, the airlie mapping, the library, the config

**Files:**
- Create: `vendor/superpowers/` (copy of the pinned release's `skills/` tree and `LICENSE`), `vendor/superpowers/PIN.md` (upstream URL, version and commit, date vendored), `vendor/superpowers/MANIFEST.sha256` (one line per vendored file), `vendor/superpowers/airlie-tools.md`, `src/airlie/chat/__init__.py`, `src/airlie/chat/superpowers.py`, `tests/unit/test_chat_superpowers.py`
- Modify: `src/airlie/config/schema.py` (`ChatSpec`), `airlie.yaml` (nothing needed if defaults hold; add a commented example only), `Taskfile.yml` (`superpowers:verify`), `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  - Config (`config/schema.py`):
    - `SuperpowersSpec(Strict)`: `enabled: bool = True`, `path: Path = Path("vendor/superpowers")` (resolved against the root file's directory, like other data paths).
    - `ChatKindSpec(Strict)`: `tools: list[str]`, `ask_first: list[str] = []`.
    - `ChatKindsSpec(Strict)`: `plain: ChatKindSpec = ChatKindSpec(tools=["web_search", "web_fetch", "skill", "todo"])`, `repository: ChatKindSpec` (default: the spec's repository list: `read_file, write_file, edit_file, list_dir, grep, shell, run_tests, git, compute, skill, todo, task, web_search, web_fetch`).
    - `ChatSpec` gains `superpowers: SuperpowersSpec`, `kinds: ChatKindsSpec`, `max_steps: int = Field(default=12, ge=1)`. A validator refuses a `plain` list containing any of `read_file, write_file, edit_file, list_dir, grep, shell, run_tests, git, compute, task` (the kind rule is config-proof too) and any name outside the known chat tool names.
  - `SkillDoc` (frozen dataclass): `name: str`, `description: str`, `text: str`, `dir: Path`.
  - `SuperpowersLibrary(root: Path)`:
    - `skills() -> list[SkillDoc]` (every `skills/*/SKILL.md`, `description:` from the frontmatter, sorted by name; a skill without a description is an error naming the file);
    - `get(name: str) -> SkillDoc` (raises `KeyError` naming the known skills);
    - `read_sibling(name: str, rel: str) -> str` (a file inside that skill's directory; raises `ValueError` for an absolute path, `..` escape or symlink out of the directory, `FileNotFoundError` for a missing file);
    - `bootstrap() -> str` (the `using-superpowers` `SKILL.md` body followed by `airlie-tools.md`, framed with a heading each);
    - `listing() -> str` (one line per skill, `- name: description`, for the system prompt).
  - `verify_manifest(root: Path) -> list[str]` (the problems: a file whose hash differs, a file missing, a file not in the manifest).

- [ ] **Step 1: Vendor the tree.** Pin superpowers 7.0.0 (the version in `~/.claude/plugins/cache/claude-plugins-official/superpowers/7.0.0`; confirm the tag and commit upstream and record both in `PIN.md`). Copy `skills/` and `LICENSE` byte for byte; generate `MANIFEST.sha256` with `shasum -a 256`. Add `Taskfile.yml` task `superpowers:verify` running `uv run python -m airlie.chat.superpowers vendor/superpowers` (exit 1 and print problems). Do not edit a vendored file; the update path is a deliberate re-copy and a new pin.
- [ ] **Step 2: Write `vendor/superpowers/airlie-tools.md`**, beside the skills, in the shape of the other harnesses' `skills/using-superpowers/references/*-tools.md` (read Hermes's first): a table from the skills' action words to airlie's tools. Rows: "invoke a skill" → `skill(name)`; "read a skill's sibling file" → `skill(name, file)`; "create a todo / task list" → `todo`; "dispatch a subagent" → `task` (repository chats only; in a plain chat do the work inline); file, shell and test actions → "not available in a plain chat: say what you would do, or write the content in your reply"; "ask the human" → ask in the reply and stop. Keep it under 700 tokens. State at the top that it is airlie's, not upstream's.
- [ ] **Step 3: Write the failing tests** in `tests/unit/test_chat_superpowers.py`, on a tmp tree built by a fixture (two skills, `using-superpowers`, an `airlie-tools.md`, a sibling file):

```python
def test_skills_are_listed_with_their_descriptions() -> None: ...
def test_a_skill_without_a_description_is_an_error_naming_the_file() -> None: ...
def test_get_an_unknown_skill_names_the_known_ones() -> None: ...
def test_bootstrap_is_using_superpowers_then_the_airlie_mapping() -> None: ...
def test_read_sibling_returns_a_file_in_the_skill_directory() -> None: ...
def test_read_sibling_refuses_dotdot_absolute_and_symlink_escapes() -> None: ...   # three cases as three tests
def test_verify_manifest_reports_a_changed_a_missing_and_an_extra_file() -> None: ...
def test_the_committed_vendor_tree_matches_its_manifest() -> None: ...   # real vendor/superpowers, fast hashing
def test_the_committed_bootstrap_is_under_2000_tokens() -> None: ...     # len(text)/4 <= 2000: the spec's 1-2k budget
```

  In `tests/unit/test_config.py`: `test_chat_kind_defaults` (both lists exactly), `test_plain_chat_refuses_a_file_or_shell_tool_in_config` (each forbidden name, as parametrized cases), `test_chat_max_steps_must_be_positive`, `test_superpowers_defaults`.
- [ ] **Step 4: Run to see them fail**: `uv run pytest tests/unit/test_chat_superpowers.py tests/unit/test_config.py -q` → FAIL.
- [ ] **Step 5: Implement** the config models and `SuperpowersLibrary` (use `airlie.skills.library.split_frontmatter` for the frontmatter; resolve sibling paths with `Path.resolve()` and `is_relative_to`). `python -m airlie.chat.superpowers <root>` is a 10-line `__main__` calling `verify_manifest`.
- [ ] **Step 6: Run the tests and `task superpowers:verify`** → PASS. Run `task mutate:changed`, kill survivors in `superpowers.py` and the new `schema.py` lines, then enrol `src/airlie/chat/superpowers.py`.
- [ ] **Step 7: Commit**

```bash
git add vendor/superpowers src/airlie/chat src/airlie/config/schema.py Taskfile.yml pyproject.toml tests/unit
git commit -m "chat: vendored superpowers (pinned, unedited, hash-checked), the airlie tools mapping, chat.kinds and chat.superpowers config"
```

### Task 2: The `skill` and `todo` tools and the kind registry

**Files:**
- Create: `src/airlie/chat/tools.py`, `src/airlie/chat/kinds.py`, `tests/unit/test_chat_tools.py`, `tests/unit/test_chat_kinds.py`
- Modify: `pyproject.toml` (enrol `kinds.py`, `tools.py`)

**Interfaces:**
- Consumes: Task 1's `SuperpowersLibrary`, `ChatKindsSpec`; `airlie.tools.protocol.Tool`, `ToolContext`, `ToolResult`; `airlie.tools.registry.ToolRegistry`; `WebSearch`, `WebFetch` from `airlie.tools.web` (built the way roles with `web_search` and `web_fetch` get them today: the same provider construction from `cfg`, found by reading how `ToolRegistry.builtin`'s `BUILTIN` registers them).
- Produces:
  - `TodoItem` (frozen dataclass): `id: str`, `text: str`, `status: Literal["pending", "in_progress", "done"]`.
  - `TodoList`: `items() -> list[TodoItem]`; `replace(items: list[TodoItem]) -> None`; `load(items: list[dict]) -> TodoList` (classmethod, for the thread store).
  - `SkillTool(library, skills_extra: Mapping[str, Skill] = {})`: `name = "skill"`, `parameters = {name: string, file: string (optional)}`, `required: ["name"]`. `run` returns the skill's `SKILL.md` text, or with `file` the sibling's text; unknown names and bad paths come back as `ToolResult(False, "error: ...")` naming the known skills (never an exception).
  - `TodoTool(todos: TodoList)`: `name = "todo"`, `parameters = {items: array of {id, text, status}}` (the whole list each call, as Claude Code's TodoWrite does), `required: ["items"]`; `run` validates (status values, at most one `in_progress`, non-empty text, at most 50 items), replaces the list and returns the rendered checklist.
  - `PLAIN_TOOLS: frozenset[str]`, `REPOSITORY_TOOLS: frozenset[str]`, `KNOWN_CHAT_TOOLS`.
  - `ChatKind = Literal["plain", "repository"]`.
  - `registry_for(kind: ChatKind, cfg: AirlieConfig, *, library: SuperpowersLibrary | None, todos: TodoList, ctx: ToolContext, web: Mapping[str, Tool]) -> ToolRegistry`: raises `KindUnavailable` (a new `Exception` carrying `kind`) for `"repository"` in this plan; for `"plain"` builds only `cfg.chat.kinds.plain.tools`, leaving out `web_search` and `web_fetch` when `web` lacks them ("when configured") and `skill` when superpowers is disabled.
  - `ask_first(kind: ChatKind, cfg: AirlieConfig) -> frozenset[str]` (parsed here, enforced by plan 4; plain returns the configured set).

- [ ] **Step 1: Write the failing tests.** `test_chat_tools.py`:

```python
async def test_skill_returns_the_skill_md() -> None: ...
async def test_skill_returns_a_sibling_file_when_asked() -> None: ...
async def test_skill_unknown_name_is_an_error_result_naming_the_known_skills() -> None: ...
async def test_skill_refuses_paths_outside_the_skill_directory() -> None: ...
async def test_todo_replaces_the_list_and_renders_a_checklist() -> None: ...
async def test_todo_refuses_two_in_progress_items() -> None: ...
async def test_todo_refuses_empty_text_a_bad_status_and_more_than_fifty_items() -> None: ...   # three tests
def test_a_todo_list_round_trips_through_load() -> None: ...
```

  `test_chat_kinds.py`:

```python
def test_plain_registry_has_exactly_the_configured_tools() -> None: ...             # names() == web_search, web_fetch, skill, todo
def test_plain_registry_has_no_file_shell_or_task_tool() -> None: ...               # intersection with the forbidden set is empty
def test_plain_registry_leaves_out_web_tools_when_no_search_is_configured() -> None: ...
def test_plain_registry_leaves_out_skill_when_superpowers_is_disabled() -> None: ...
def test_repository_kind_is_unavailable_until_plan_4() -> None: ...                 # raises KindUnavailable("repository")
def test_ask_first_is_read_from_the_kind_config() -> None: ...
```

- [ ] **Step 2: Run to see them fail**: `uv run pytest tests/unit/test_chat_tools.py tests/unit/test_chat_kinds.py -q` → FAIL.
- [ ] **Step 3: Implement** the two modules as specified. `skill` also serves the slash-command skills' names when the model asks for one (`skills_extra`), so `/story` stays reachable by name; the description listing is separate (Task 5).
- [ ] **Step 4: Run the tests; `task mutate:changed`; kill survivors; enrol `kinds.py` and `tools.py`.**
- [ ] **Step 5: Commit**

```bash
git add src/airlie/chat/tools.py src/airlie/chat/kinds.py pyproject.toml tests/unit
git commit -m "chat: the skill and todo tools, and the per-kind tool registry (plain only; repository refused until plan 4)"
```

### Task 3: The UI message stream encoder

**Files:**
- Create: `src/airlie/chat/stream.py`, `tests/unit/test_chat_stream.py`
- Modify: `pyproject.toml` (enrol)

**Interfaces:**
- Produces:
  - `STREAM_HEADERS: dict[str, str]` = `{"content-type": "text/event-stream", "cache-control": "no-cache", "x-vercel-ai-ui-message-stream": "v1", "x-accel-buffering": "no"}`.
  - `sse(chunk: Mapping[str, Any]) -> str` (`"data: " + compact json + "\n\n"`); `DONE = "data: [DONE]\n\n"`.
  - `StreamWriter(message_id: str)`: a stateful encoder that yields the strings for each call and keeps the part structure correct:
    - `start() -> list[str]` (`{"type":"start","messageId":...}` then `start-step`);
    - `text(delta: str) -> list[str]` (opens `text-start` with a part id on the first delta, `text-delta` after; a switch from reasoning closes the reasoning part first);
    - `reasoning(delta: str) -> list[str]` (same, `reasoning-*`);
    - `tool_call(call_id: str, name: str, arguments: str) -> list[str]` (closes an open text/reasoning part, then `tool-input-start`, `tool-input-available` with the parsed JSON input, falling back to `{"_raw": arguments}` when it does not parse);
    - `tool_result(call_id: str, output: str, ok: bool) -> list[str]` (`tool-output-available`, or `tool-output-error` with `errorText` when not ok);
    - `data(name: str, payload: Any, *, transient: bool = False) -> list[str]` (`data-<name>`);
    - `next_step() -> list[str]` (`finish-step` then `start-step`);
    - `error(text: str) -> list[str]`;
    - `finish(usage: Mapping[str, Any]) -> list[str]` (closes any open part, `finish-step`, then `{"type":"finish","messageMetadata":{"usage": ...}}`, then `DONE`).
- Behaviour pinned: exactly one open part at a time; every open part is closed before `finish` and before a tool call; ids are unique and stable within a part; JSON is compact and never contains a raw newline.

- [ ] **Step 1: Write the failing tests** (parse each yielded string back with `json.loads(s[6:])`):

```python
def test_start_emits_start_then_start_step_with_the_message_id() -> None: ...
def test_first_text_delta_opens_a_part_and_later_ones_reuse_its_id() -> None: ...
def test_switching_from_reasoning_to_text_closes_the_reasoning_part() -> None: ...
def test_a_tool_call_closes_an_open_text_part_then_announces_input() -> None: ...
def test_tool_input_available_carries_the_parsed_arguments() -> None: ...
def test_unparseable_arguments_are_sent_raw_not_dropped() -> None: ...
def test_a_failed_tool_result_is_tool_output_error_with_its_text() -> None: ...
def test_data_parts_are_named_and_may_be_transient() -> None: ...
def test_finish_closes_the_open_part_carries_usage_and_ends_with_done() -> None: ...
def test_error_chunk_carries_the_text() -> None: ...
def test_a_delta_with_a_newline_stays_on_one_sse_line() -> None: ...
def test_each_event_ends_with_a_blank_line() -> None: ...
```

- [ ] **Step 2: Run to see them fail** → FAIL. **Step 3: Implement** `StreamWriter` (no I/O, no clock: pure). **Step 4: Run; `task mutate:changed`; kill survivors; enrol `src/airlie/chat/stream.py`.**
- [ ] **Step 5: Commit**

```bash
git add src/airlie/chat/stream.py pyproject.toml tests/unit/test_chat_stream.py
git commit -m "chat: the AI SDK UI message stream encoder"
```

### Task 4: The thread store

**Files:**
- Create: `src/airlie/chat/threads.py`, `tests/unit/test_chat_threads.py`
- Modify: `pyproject.toml` (enrol)

**Interfaces:**
- Produces (SQLite file `<state_dir>/chat/threads.db`, same pragmas as `auth/store.py`; `now: Callable[[], float] = time.time` injected):
  - `StoredMessage` (frozen dataclass): `id: str`, `role: Literal["user", "assistant"]`, `parts: list[dict[str, Any]]` (UI message parts, as streamed: text, reasoning, tool parts with input and output, data parts), `created: float`, `usage: dict[str, Any] | None = None`, `interrupted: bool = False`.
  - `Thread` (dataclass): `id: str`, `owner: str`, `kind: ChatKind`, `title: str`, `messages: list[StoredMessage]`, `todos: list[dict[str, Any]]`, `created: float`, `updated: float`.
  - `ThreadStore(path: Path, *, now=time.time)`:
    - `open(thread_id: str, owner: str, kind: ChatKind) -> Thread` (creates it when new; raises `ThreadNotFound` when the id exists under another owner or another kind);
    - `get(thread_id: str, owner: str) -> Thread` (raises `ThreadNotFound`; an admin does not bypass: the route decides);
    - `append(thread_id: str, message: StoredMessage) -> None`;
    - `set_todos(thread_id: str, todos: list[dict]) -> None`;
    - `list(owner: str, kind: ChatKind | None = None) -> list[ThreadSummary]` (`id`, `title`, `kind`, `updated`, newest first);
    - `delete(thread_id: str, owner: str) -> bool`;
    - `title` defaults to the first user text, 60 characters.
  - `ThreadNotFound(Exception)`.
- Why a new file and not `AuthStore`: threads grow large and are written every turn; a separate database keeps auth's file small and lets the two be backed up and pruned separately.

- [ ] **Step 1: Write the failing tests:**

```python
def test_a_new_thread_is_created_for_its_owner() -> None: ...
def test_open_on_an_existing_thread_returns_it_with_its_messages() -> None: ...
def test_another_owners_thread_is_not_found_not_forbidden() -> None: ...
def test_opening_a_thread_with_a_different_kind_is_not_found() -> None: ...
def test_append_keeps_order_and_updates_the_updated_time() -> None: ...
def test_parts_and_usage_round_trip_exactly() -> None: ...                   # a tool part with input and output
def test_an_interrupted_message_is_flagged() -> None: ...
def test_todos_persist_across_a_reopened_store() -> None: ...
def test_list_is_newest_first_and_filtered_by_owner_and_kind() -> None: ...
def test_title_is_the_first_user_text_cut_to_60_characters() -> None: ...
def test_delete_removes_the_thread_and_its_messages() -> None: ...
def test_delete_of_another_owners_thread_is_false_and_leaves_it() -> None: ...
```

- [ ] **Step 2: Run to see them fail** → FAIL. **Step 3: Implement** (`tmp_path` real sqlite file; no mocks). **Step 4: Run; `task mutate:changed`; kill survivors; enrol `threads.py`.**
- [ ] **Step 5: Commit**

```bash
git add src/airlie/chat/threads.py pyproject.toml tests/unit/test_chat_threads.py
git commit -m "chat: server-side threads (owner-scoped sqlite store)"
```

### Task 5: The hooks and the session prompt

**Files:**
- Create: `src/airlie/chat/hooks.py`, `tests/unit/test_chat_hooks.py`
- Modify: `pyproject.toml` (enrol)

**Interfaces:**
- Consumes: Tasks 1, 2.
- Produces:
  - `TurnContext` (dataclass, mutable fields where hooks change them): `thread_id: str`, `owner: str`, `kind: ChatKind`, `session: Principal`, `system_prompt: str = ""`, `attachments: int = 0`, `skill: str | None = None`.
  - `ToolEvent` (frozen): `call_id: str`, `name: str`, `arguments: str`, `output: str`, `ok: bool`.
  - `TurnSummary` (frozen): `thread_id: str`, `owner: str`, `kind: ChatKind`, `title: str`, `text: str` (the final assistant text), `usage: Mapping[str, Any]`, `interrupted: bool`, `failed: bool`.
  - `Hooks`: four ordered lists, `session_start`, `before_turn`, `after_tool`, `on_finish`, with `on(name: Literal[...], fn) -> None` to register and `async run(name, *args) -> None`, which awaits them in order; a hook that raises is logged and does not stop the turn, except `before_turn` guardrails, which may raise `TurnRefused(status: int, message: str)`, and that stops the turn (HTTP status carried to the route). `on_finish` hooks run even when the turn failed or was interrupted (`failed`/`interrupted` flags), each isolated from the others.
  - Default hooks, built by `default_hooks(cfg, library, *, chat_skills: Mapping[str, Skill], uploads_limit: int) -> Hooks`:
    - `session_start`: `build_system_prompt` (below) sets `ctx.system_prompt`;
    - `before_turn`: refuses a message with more attachments than `cfg.chat.attachments.max_per_message` (400) and an unknown slash skill (404);
    - `after_tool`: appends an audit record (`thread_id`, `call_id`, `name`, `ok`, byte sizes, no arguments or output text) with the standard logger `airlie.chat.audit`;
    - `on_finish`: no default hook persists here (persistence is the runner's own step so a failing hook cannot lose a turn); **the PWA-b hook point**: a registered `on_finish` hook receives a `TurnSummary`. PWA-b's chat_reply notification (`docs/superpowers/plans/2026-11-11-pwa.md`, Task 5) adds `async def notify_reply(summary: TurnSummary) -> None` that calls `Notifier.notify(summary.owner, "chat_reply", title=summary.title, url=f"/chat/{summary.thread_id}", body=summary.text[:140])` and skips `failed` and `interrupted` turns; it registers with `hooks.on("on_finish", ...)` from `create_app` when `app.state.notifier` exists. This plan registers nothing for it and builds no notification.
  - `build_system_prompt(cfg, kind, library: SuperpowersLibrary | None, chat_skills, *, subagent: bool = False, skill: str | None = None) -> str`: the `chat` role's prompt, then (unless `subagent` or superpowers is disabled) `library.bootstrap()` first-class after it, then `## Skills` with `library.listing()`, then `## Slash commands` (the existing `chat_prompt` text, so the two stay identical), then the run slash skill's body when `skill` is set; a plain chat appends the one sentence "This is a plain chat on the host: there is no repository, file or shell; skills that need files: do the work inline." The prompt is rebuilt every turn (never stored), and byte-identical for the same inputs so the engine's prefix cache holds. `subagent=True` is the seam plan 4's `task` tool uses: no bootstrap, no skills listing.

- [ ] **Step 1: Write the failing tests:**

```python
async def test_hooks_run_in_registration_order() -> None: ...
async def test_a_failing_after_tool_hook_is_logged_and_does_not_stop_the_turn() -> None: ...
async def test_before_turn_can_refuse_with_a_status() -> None: ...
async def test_on_finish_runs_for_a_failed_turn_and_an_interrupted_one() -> None: ...
async def test_one_failing_on_finish_hook_does_not_stop_the_next() -> None: ...
async def test_an_on_finish_hook_receives_the_summary_a_notifier_needs() -> None: ...   # owner, thread_id, title, text, flags
def test_the_system_prompt_starts_with_the_role_prompt_then_the_bootstrap() -> None: ...
def test_the_bootstrap_is_in_the_session_prompt_exactly_once() -> None: ...
def test_a_subagent_prompt_has_no_bootstrap_and_no_skills_listing() -> None: ...
def test_superpowers_disabled_leaves_the_bootstrap_and_listing_out() -> None: ...
def test_the_prompt_lists_every_skills_description() -> None: ...
def test_the_prompt_is_byte_identical_for_identical_inputs() -> None: ...
def test_a_plain_chat_prompt_says_there_is_no_repository() -> None: ...
async def test_before_turn_refuses_too_many_attachments_and_an_unknown_slash_skill() -> None: ...   # two tests
async def test_the_audit_record_has_names_and_sizes_but_not_arguments_or_output() -> None: ...
```

- [ ] **Step 2: Run to see them fail** → FAIL. **Step 3: Implement.** The slash-command text must come from the same function the old route uses: move `chat_prompt` out of the `create_app` closure in `serve/app.py` into `airlie/chat/hooks.py` (`slash_section(chat_skills, skill) -> str`) and have the old route call it, with `tests/functional/test_serve_chat.py` unchanged and green.
- [ ] **Step 4: Run these tests and `tests/functional/test_serve_chat.py`; `task mutate:changed`; enrol `hooks.py`.**
- [ ] **Step 5: Commit**

```bash
git add src/airlie/chat/hooks.py src/airlie/serve/app.py pyproject.toml tests/unit/test_chat_hooks.py
git commit -m "chat: the four hooks, the superpowers session prompt, and the on_finish hook point for notifications"
```

### Task 6: The streaming turn runner

**Files:**
- Create: `src/airlie/chat/turn.py`, `tests/unit/test_chat_turn.py`
- Modify: `pyproject.toml` (enrol `turn.py` only if `task mutate:changed` can be made clean; otherwise leave it reported, never failed, and say so in the commit)

**Interfaces:**
- Consumes: `Engine`, `GenRequest`, `GenResult`, `Message`, `OnToken`, `TokenEvent`; `ToolRegistry`; `Holder` (`hold(engine, owner)`); `Manager.status()`; `Clock`; Tasks 2-5.
- Produces:
  - `TurnRequest` (dataclass): `thread: Thread`, `user_message: StoredMessage`, `ctx: TurnContext`.
  - `run_turn(req: TurnRequest, *, engine: Engine, engine_name: str, holder: Holder, registry: ToolRegistry, todos: TodoList, store: ThreadStore, hooks: Hooks, role: RoleSpec, max_steps: int, clock: Clock, status: Callable[[], list[Status]]) -> AsyncIterator[str]`: yields SSE strings (Task 3). Steps:
    1. yield `start`; run `session_start` then `before_turn` (a `TurnRefused` becomes an `error` chunk and a closed stream; the route turns refusals that happen before the first byte into HTTP errors, so the route calls these two hooks itself first: `run_turn` is given an already-prepared `TurnContext`; see Task 7).
    2. Hold `engine_name` as `chat:{thread.id}` for the whole turn, as an `AsyncExitStack` entered in a task, so while it waits the stream yields `data-engine` chunks (transient) from `status()` for that engine every second (clock.sleep) whenever its state or text changed: `{"engine": name, "state": ..., "detail": ..., "progress": ...}`. The hold is released in a `finally`.
    3. Loop up to `max_steps`: build `GenRequest(role="chat", messages=<system prompt, stored history, the user message>, sampling=role.sampling, max_tokens=role.max_tokens, thinking=role.thinking, tools=registry.schemas())`; run `engine.generate(req, on_token)` as a task; `on_token` puts `TokenEvent`s on an `asyncio.Queue` that the generator drains into `text`/`reasoning` chunks as they come; when the result has a tool call (first only, as the loop in `tools/loop.py` does), emit `tool_call`, run it with `registry.call` (a name the registry lacks is an error result, as `ToolRegistry.call` already returns), emit `tool_result`, run `after_tool`, and, if the tool was `todo`, emit `data-todos` and `store.set_todos`; then `next_step`. No tool call: done.
    4. After `max_steps` tool calls: one last `generate` with `tools=None` and `Message(role="user", content=text("no-more-tools"))` (reuse the hub text `no-more-tools`).
    5. A generation that ends `cut` (finish_reason length) with a half tool call is reported as a tool error to the model once, not silently dropped (same rule as `ToolRegistry.call`'s cut-off message).
    6. `finally` (also on `asyncio.CancelledError` from a client disconnect and on `EngineError`): `engine.cancel(request_id)` for a generation in flight, save the assistant `StoredMessage` (parts accumulated from what was streamed; `interrupted=True` on cancel; usage from the last `GenResult`) and the user message, run `on_finish` with a `TurnSummary`, release the hold. A `EngineError` becomes `error(text)` then `finish`.
    7. `finish` usage: `{"prompt_tokens", "completion_tokens", "cached_tokens", "max_context": role.max_context}` summed over steps' completion tokens, with `prompt_tokens` from the last step (the context in use).
  - Stored history is rebuilt into `Message`s: user text parts become user messages; assistant tool parts become an assistant message with `tool_calls` plus `tool` messages with `tool_call_id` (via `history_arguments`); reasoning parts are not sent back.

- [ ] **Step 1: Write the failing tests** with a `ScriptedEngine` (a list of `GenResult`s and the token events to deliver through `on_token`; records each `GenRequest`), a `FakeHolder` (records hold/release order and owner), a real `ThreadStore` on `tmp_path`, and `FakeClock`. Collect the stream, parse the chunks:

```python
async def test_a_plain_answer_streams_text_deltas_then_finish_and_done() -> None: ...
async def test_reasoning_tokens_stream_as_reasoning_parts() -> None: ...
async def test_a_tool_call_streams_input_then_output_and_the_result_goes_back_to_the_model() -> None: ...  # second GenRequest has assistant tool_calls + tool message
async def test_the_engine_is_held_as_chat_thread_id_for_the_whole_turn() -> None: ...   # owner string; released after finish, not between steps
async def test_a_waiting_engine_streams_status_data_parts_until_ready() -> None: ...    # status() script: waiting -> starting -> ready
async def test_a_tool_outside_the_kind_is_an_error_result_not_a_run() -> None: ...      # model calls shell; tool-output-error; turn continues
async def test_steps_are_bounded_then_one_last_call_without_tools() -> None: ...        # max_steps=2: third request has tools=None and the no-more-tools text
async def test_only_the_first_tool_call_of_a_reply_runs() -> None: ...
async def test_a_todo_call_emits_a_todos_data_part_and_saves_the_list() -> None: ...
async def test_after_tool_runs_after_each_tool_with_its_result() -> None: ...
async def test_finish_carries_usage_and_the_roles_max_context() -> None: ...
async def test_the_engine_error_becomes_an_error_chunk_then_finish_and_the_hold_is_released() -> None: ...
async def test_cancelling_the_stream_cancels_generation_releases_the_hold_and_saves_the_partial() -> None: ...
async def test_on_finish_gets_the_final_text_and_an_interrupted_flag_on_cancel() -> None: ...
async def test_the_user_message_is_saved_even_when_the_turn_fails() -> None: ...
async def test_history_is_replayed_with_tool_calls_and_without_reasoning() -> None: ...
async def test_a_cut_off_tool_call_is_reported_to_the_model_not_dropped() -> None: ...
```

- [ ] **Step 2: Run to see them fail**: `uv run pytest tests/unit/test_chat_turn.py -q` → FAIL.
- [ ] **Step 3: Implement** `run_turn` and the history rebuild. Do not call `generate_with_tools` (it returns only at the end and cannot stream or emit per-tool events); the loop here is the same shape, and the shared pieces (`history_arguments`, `text("no-more-tools")`) are imported, not copied.
- [ ] **Step 4: Run the tests; `task mutate:changed` and decide enrolment as in Files.**
- [ ] **Step 5: Commit**

```bash
git add src/airlie/chat/turn.py pyproject.toml tests/unit/test_chat_turn.py
git commit -m "chat: the streaming turn runner (tool loop, engine hold, hooks, persistence)"
```

### Task 7: The route in serve

**Files:**
- Modify: `src/airlie/serve/app.py` (build the store, library, hooks and `web` tools in `create_app`; routes below), `tests/support/serve_chat.py` (config helper: `chat.superpowers.path` at a tmp fixture tree)
- Create: `src/airlie/chat/api.py` (`chat_router(...) -> APIRouter`, so `app.py` only wires it), `tests/functional/test_chat_turn_route.py`

**Interfaces:**
- Consumes: everything above; `owner_of(request)` (the principal the auth middleware sets), `manager`, `InProcessHolder(manager)`, `_http_engine(cfg, engine_name, transport=transport, manager=manager)`.
- Produces routes (all behind the existing auth middleware):
  - `POST /v1/airlie/chat/turn`, body `{id: str, kind: "plain" | "repository", messages: list[UIMessage], skill?: str}` (the AI SDK's request shape plus ours: `messages` is the client's list; the server takes only the last `user` message's parts as the new input and uses its own stored thread as history, so two devices agree). Response `StreamingResponse(media_type="text/event-stream", headers=STREAM_HEADERS)`.
    - 401 no principal (middleware); 422 for a malformed body (pydantic) or no user message; 400 `{"error": ...}` for `kind: "repository"` ("repository chats arrive with plan 4"; `KindUnavailable`) and for refusals from `before_turn` (status carried); 404 for a thread id owned by another person or of another kind (`ThreadNotFound`); 409 when that thread already has a turn running (an in-memory set guarded by a lock, cleared in the stream's `finally`); 503 `{"error": ...}` with the engine named when the `chat` role is not configured. Anything that fails after the first byte is an `error` chunk, not an HTTP status.
    - It runs `session_start` and `before_turn` before returning the response, so refusals are real statuses.
  - `GET /v1/airlie/chat/threads?kind=` → `[{id, title, kind, updated}]` for the signed-in person; `GET /v1/airlie/chat/threads/{id}` → the thread (messages as stored, todos); `DELETE /v1/airlie/chat/threads/{id}` → 204. 404 for another person's id or none.
  - `app.state.chat_hooks: Hooks` and `app.state.chat_threads: ThreadStore` (so PWA-b and tests reach them).
- Not changed: `/v1/chat/completions`.

- [ ] **Step 1: Write the failing tests** in `tests/functional/test_chat_turn_route.py` with `create_app(chat_cfg(tmp_path), transport=httpx.MockTransport(up))` where the upstream (`tests.support.serve.FakeUpstream`, extended to stream SSE `chat/completions` chunks the way mlx-vlm does) answers; a small helper parses the response into chunk dicts:

```python
def test_a_turn_streams_the_ui_message_stream_with_its_header() -> None: ...        # header, start ... text ... finish, [DONE]
def test_the_turn_is_saved_and_get_returns_it() -> None: ...
def test_the_system_prompt_carries_the_bootstrap_and_the_skills_listing() -> None: ...   # read from the upstream request body
def test_a_plain_chat_offers_the_model_only_the_plain_tools() -> None: ...             # upstream request `tools` names; no shell, no read_file
def test_a_tool_call_runs_on_the_server_and_its_output_goes_back_to_the_model() -> None: ...  # skill call; second upstream request has the tool message
def test_a_client_supplied_tool_list_is_ignored() -> None: ...                          # body carries "tools": [shell]; upstream never sees it
def test_the_second_turn_uses_the_stored_history_not_the_clients() -> None: ...
def test_a_turn_without_a_session_is_401() -> None: ...
def test_a_malformed_body_is_422() -> None: ...
def test_a_body_with_no_user_message_is_422() -> None: ...
def test_a_repository_chat_is_400_naming_plan_4() -> None: ...
def test_another_persons_thread_id_is_404() -> None: ...
def test_a_second_turn_on_a_busy_thread_is_409() -> None: ...
def test_an_unknown_slash_skill_is_404_before_any_byte_streams() -> None: ...
def test_too_many_attachments_is_400() -> None: ...
def test_an_engine_failure_is_an_error_chunk_and_the_user_message_is_kept() -> None: ...    # upstream 500 mid-stream
def test_a_disconnect_cancels_generation_releases_the_hold_and_saves_the_partial() -> None: ...   # manager holders empty afterwards (app.state.manager.status())
def test_the_manager_shows_chat_thread_id_as_a_holder_during_the_turn() -> None: ...
def test_an_on_finish_hook_registered_on_the_app_receives_the_summary() -> None: ...    # the PWA-b seam, via app.state.chat_hooks
def test_thread_list_get_delete_and_their_404s() -> None: ...                            # one test each for list, get, delete; 401 each
def test_chat_completions_still_works_unchanged() -> None: ...                           # the existing route's happy path next to the new one
```

- [ ] **Step 2: Run to see them fail**: `uv run pytest tests/functional/test_chat_turn_route.py -q` → FAIL.
- [ ] **Step 3: Implement** `chat/api.py` and wire it in `create_app`. Attachments (`airlie_upload` parts) go through the same conversion the old route's `attach` does; extract that function from the `create_app` closure into a module function both routes call, with `tests/functional/test_serve_chat.py` and `test_serve_uploads.py` still green. The `chat` role's engine is `cfg.engine_for_role("chat")`; an engine the manager does not manage is run unheld (as `InProcessHolder` already does).
- [ ] **Step 4: Run the new tests and the old chat tests** → PASS: `uv run pytest tests/functional -q`.
- [ ] **Step 5: Commit**

```bash
git add src/airlie/chat/api.py src/airlie/serve/app.py tests/support tests/functional/test_chat_turn_route.py
git commit -m "serve: POST /v1/airlie/chat/turn (UI message stream), thread routes, plain chat on the server loop"
```

### Task 8: Docs, the gate, and the live checks

**Files:**
- Modify: `README.md` (a "Chat" paragraph: the turn route, the two kinds, superpowers and `task superpowers:verify`), `AGENTS.md` (Configuration: `chat.kinds`, `chat.superpowers`, `vendor/superpowers` is unedited), `config/` (nothing)
- Create: `docs/bench/2026-11-12-chat-turn-live.md` (the live results)

- [ ] **Step 1: Docs.** README and AGENTS.md entries as listed. Add the route, its body and the chunk types to the README's API section, and say the existing Chat screen still uses `/v1/chat/completions` until the redesign.
- [ ] **Step 2: The full gate**, in order, from the repository root (`uv sync --frozen`, ruff check, ruff format --check, mypy, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`) and `task superpowers:verify` and `task test:quarantine` → all PASS.
- [ ] **Step 3: Live checks** (need the Studio with Gemma; Noah's go before restarting serve). Record each in `docs/bench/2026-11-12-chat-turn-live.md`:
  1. `curl -N -H 'Authorization: Bearer <key>' -d '{"id":"t1","kind":"plain","messages":[{"role":"user","parts":[{"type":"text","text":"say hi"}]}]}' http://127.0.0.1:8090/v1/airlie/chat/turn` → readable SSE ending `[DONE]`.
  2. With Gemma stopped (`airlie engine stop gemma` or before first use), the same call streams `data-engine` chunks ("Starting gemma…") and then text.
  3. "Let's brainstorm a small idea" → the model calls `skill` for `brainstorming` (the bootstrap works); tool output visible in the stream; `GET /v1/airlie/chat/threads/t1` returns it.
  4. Ctrl-C the curl mid-reply → `GET /v1/airlie/engines` shows no `chat:t1` holder; the thread has the partial with `interrupted`.
  5. Context readout: the `finish` chunk's `prompt_tokens` and `max_context: 262144`.
- [ ] **Step 4: Commit**

```bash
git add README.md AGENTS.md docs/bench/2026-11-12-chat-turn-live.md
git commit -m "docs: the chat turn route, the two kinds, superpowers; live check results"
```

---

## Self-review

- **Spec coverage:** turn route and SSE protocol (Tasks 3, 7); server-side streaming tool loop (6); two kinds enforced server-side, plain only (2, 7); `skill`, `todo` (2); the four hooks (5); threads persisted on the server (4, 7); vendored pinned unedited skills with LICENSE and `airlie-tools.md` (1); bootstrap in every session prompt, not subagents (5); config keys `chat.kinds`, `chat.superpowers` (1); engine held through the manager as `chat:{id}`, status as data parts (6, 7); context readout in `finish` (6); PWA hook point (5, 7); existing routes unchanged (7). `task` tool: stated out of scope, with the `subagent=True` prompt seam in Task 5 as plan 4's entry. Slash commands stay (5). Visual companion: later.
- **Type consistency:** `ChatKind`, `TodoList`, `ThreadStore`, `Hooks`, `TurnContext`, `TurnSummary`, `StreamWriter` are defined once (Tasks 2-5) and consumed by name in 6-7; the owner string is `chat:{thread_id}` throughout.
- **Proportion:** eight tasks for four spec sections; bodies are signatures and test names.
