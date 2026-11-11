# Repository Chats in VMs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A repository chat runs in its own Tart Linux VM: every tool a model calls (files, shell, tests, git, compute, the `task` subagent) acts on a clone inside that VM, the host moves code in and the session branch out and pushes it with the GitHub App after the user confirms, and the VM is a manager resource that counts in the memory budget, is held by each turn, stops when idle with its disk kept, and is deleted with the chat.

**Architecture:** A `Box` seam (`airlie.boxes`) hides the VM tool: `TartBox` shells out to `tart`, `FakeBox` backs the tests. `VMResource` is a manager `Resource` (`kind == "vm"`) over a `Box`; the manager gains `add`/`remove` so VMs register per session. The builtin tools gain a box path behind `ToolContext.box`: `shell.py`'s `_run` and `compute` execute through `box.exec`, and the file tools are re-expressed over `box.exec` in `tools/boxed.py`. A host-side `Mirror` (a per-repository clone fetched with the GitHub App's token) feeds the VM a git bundle and takes the session branch back as a bundle; `Publisher` pushes it. A `Confirmations` gate in the turn handler refuses a listed tool unless the server holds an approval for that exact call.

**Tech Stack:** Python 3.12, asyncio, Tart 2.32.1 (`tart` CLI), git bundles, the existing `Manager`, `Publisher` and `GitHubApp`.

**Spec:** [`docs/superpowers/specs/2026-11-11-airlie-web-chat-agent-design.md`](../specs/2026-11-11-airlie-web-chat-agent-design.md), phase 4: "Two kinds of chat", "Repository chats: a VM per session", "Superpowers in chat" (the `task` tool), "Configurable behavior" (`boxes`, `chat.kinds`), "Testing" (VMs), and the review resolutions 2, 20-23. Isolation and environments: [`2026-10-16-remote-studio-design.md`](../specs/2026-10-16-remote-studio-design.md), "Isolation: Tart, one box per job" and "Environments: a recipe in the repo". Builds on [`2026-11-11-on-demand-engines.md`](2026-11-11-on-demand-engines.md) (the manager) and [`2026-11-12-chat-turn-route-and-superpowers.md`](2026-11-12-chat-turn-route-and-superpowers.md) (plan 3, written in parallel).

## Seams from plan 3

Names below are plan 3's (`2026-11-12-chat-turn-route-and-superpowers.md`), exact. A chat session is plan 3's `Thread`; there is no `ChatSessions` class. Where plan 3 lacks something, this plan adds it (last column). Each is consumed by Task 6 or 7 only.

| Plan 3 provides | Where | This plan adds |
|---|---|---|
| Route `POST /v1/airlie/chat/turn`, body `{id, kind: "plain" \| "repository", messages, skill?}`; `chat/api.py::chat_router(...)`; thread routes `GET/DELETE /v1/airlie/chat/threads/{id}`; `app.state.chat_threads`, `app.state.chat_hooks`; today `kind: "repository"` is a 400 naming plan 4 (`KindUnavailable`) | `chat/api.py`, Task 7 | Body fields `repo: str \| None`, `base_ref: str \| None`, `approvals: list[{id, approved}] = []`; the 400 becomes the real path (Task 6) |
| `Thread(id, owner, kind: ChatKind, title, messages, todos, created, updated)`, `ThreadStore.open(thread_id, owner, kind)`, `.get`, `.delete(thread_id, owner) -> bool`, `ThreadNotFound` | `chat/threads.py`, Task 4 | `Thread.repo`, `Thread.base_ref` (nullable columns; `open(..., repo=None, base_ref=None)`, fixed once set) and `ThreadStore.on_delete(cb: Callable[[str], Awaitable[None]])`, run by `delete` before it returns |
| `ChatKind = Literal["plain", "repository"]`; `registry_for(kind, cfg, *, library, todos, ctx, web) -> ToolRegistry` (raises `KindUnavailable` for `"repository"`); `ask_first(kind, cfg) -> frozenset[str]`; `PLAIN_TOOLS`, `REPOSITORY_TOOLS`, `KNOWN_CHAT_TOOLS` | `chat/kinds.py`, `chat/tools.py`, Task 2 | The `"repository"` branch of `registry_for` (it calls `build_repo_tools`) replacing `KindUnavailable`; `push_branch`, `open_pr` added to `REPOSITORY_TOOLS` and `KNOWN_CHAT_TOOLS`, and to the plain-kind forbidden list |
| Config `chat.kinds.<kind>: ChatKindSpec(tools: list[str], ask_first: list[str] = [])`, `chat.max_steps`, `chat.superpowers` | `config/schema.py`, Task 1 | Defaults only: repository `tools` gains `push_branch`, `open_pr`; repository `ask_first = [push_branch, open_pr, build_image]` (no new field; plan 3 names it `ask_first`, not `confirm`) |
| `Hooks` with `session_start`, `before_turn`, `after_tool`, `on_finish` (`on(name, fn)`, `async run(name, *args)`); `TurnContext`, `ToolEvent`, `TurnSummary`, `TurnRefused` | `chat/hooks.py`, Task 5 | A fifth hook `before_tool` (`async (ctx: TurnContext, call_id, name, arguments) -> ToolGate`; `ToolGate = Literal["allow", "pending", "deny"]`) run by `run_turn` before `registry.call`; plan 3 has no pre-tool point |
| `StreamWriter` (`tool_call`, `tool_result`, `data`, ...) | `chat/stream.py`, Task 3 | `StreamWriter.tool_approval_request(approval_id, call_id) -> list[str]` (`{"type": "tool-approval-request", "approvalId", "toolCallId"}`); plan 3 has no permission part |
| `run_turn(req, *, engine, engine_name, holder, registry, todos, store, hooks, role, max_steps, clock, status)` yielding SSE strings; the route builds the registry and calls `session_start`/`before_turn` first | `chat/turn.py`, Task 6-7 | In `api.py`, repository turns run inside `RepoSessions.turn(...)` and pass its `ToolContext` to `registry_for`; `run_turn` gains `before_tool` handling |
| `build_system_prompt(cfg, kind, library, chat_skills, *, subagent: bool = False, skill=None)`; `skill` and `todo` tools; `SuperpowersLibrary` | `chat/hooks.py`, `chat/tools.py` | `task` (Task 7); a subagent builds its prompt from the role's own prompt, and `subagent=True` is for a subagent on the `chat` role |

## Global Constraints

- Every task passes the gate from the repository root, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`.
- Unit tests over 500 ms or on the real clock go in `tests/unit/quarantine.toml`. VM tests use `FakeBox` and `airlie.serve.bridge.clock.FakeClock`; no test starts Tart, a VM, a real engine or the network. The real Tart boot is a live check (Task 8), not a gate test.
- Tests follow `skills/qa/*.md`:
  - Unit (`tests/unit`): one behaviour per test, named for the scenario and outcome, Arrange-Act-Assert, no loops or conditionals in the body; fakes over mocks, doubles only at seams (clock, network, process, disk); no real clock, network or VM.
  - A test that drives a FastAPI app through `TestClient` goes in `tests/functional`: real routes and middleware, doubles only beyond the module boundary, one request per test, and for each new route the happy path, 401, 422 or 400, 404 and a dependency failure, asserting the body and not just the status.
  - Fakes are rebuilt per test (fixtures), never shared; every new test is seen red before the code is written.
  - New modules that hold logic are enrolled in `[tool.airlie.mutation].enrolled` once `task mutate:changed` is clean for them; changes to an already enrolled module need tests that catch every new mutant.
  - Web tests (vitest) follow `component.md`. This plan adds none: its status copy rides the engine status line plan 1 built (VMs arrive as `kind: "vm"` rows).
  - E2E: none (the live check covers the journey).
- Dependencies through uv only. No Docker. Tart is a host tool, never a Python dependency; it is called as a subprocess.
- Repository chats exist only with their VM. Without Tart a repository chat says so and says how to install it (`brew install cirruslabs/cli/tart`); it never runs a command on the host, and never falls back to seatbelt (`tools/confine.py` stays the story line's).
- No push or merge credential ever enters a VM: the box environment is built from scratch (`box_env`), not from the host's (`agent_env`), and the VM has no git remote. Setup secrets exist only while setup runs.
- The manager never stops a resource that has a holder; a turn holds its VM and its engines for as long as it runs.
- Linux guests only. Defaults: backend `tart`, image `ghcr.io/cirruslabs/ubuntu:latest`, 8 GB memory, 4 CPUs, 40 GB disk, idle stop 900 s, session branch `airlie/chat-<first 8 of the session id>`.
- Status copy for VMs (same shapes as engines, from plan 1): "Starting {display}…", "Waiting for memory: {holder list}", "{display} failed: {reason}", where `{display}` is "{repo} VM".
- Tart commands used, all checked against `tart --help` of 2.32.1 on this Mac: `clone <source> <new>` (also pulls an OCI image), `set <name> --cpu --memory (MB) --disk-size (GB, grow only)`, `run --no-graphics [--net-softnet-block CIDRS] [--net-softnet-allow CIDRS] [--net-host] <name>` (runs in the foreground until the VM stops), `ip <name> --wait <s>`, `exec [-i] <name> <command…>` (needs the Tart Guest Agent in the guest), `stop <name> [--timeout]`, `delete <name>`, `get <name> --format json`, `list`. Not run here: a boot, `exec` against the default image, or the softnet allow list; the live check does that.

## Review Focus

1. **Tart is not installed (or the default image is not pulled).** Expected: opening a repository chat returns a clear message with the install command, nothing runs on the host, and a plain chat is unaffected. Test in Task 2 (`test_a_missing_tart_says_how_to_install_it`) and Task 6 (`test_opening_a_repository_chat_without_tart_is_refused_with_the_install_hint`).
2. **A model tries to push or reach a credential from inside the VM** (`git push`, `gh`, `env | grep TOKEN`, a remote it adds). Expected: the VM's environment holds none, `git push` has nowhere to go, and the `git` tool still refuses network subcommands. Test in Task 1 (`test_box_env_carries_no_credential`) and Task 4 (`test_the_git_tool_in_a_box_still_refuses_push`).
3. **A tool call that needs confirmation arrives without one, or with one for different arguments.** Expected: refused server-side with the request id; the model cannot approve for itself, and an approval is single use. Test in Task 6 (`test_an_unapproved_push_is_refused`, `test_an_approval_for_other_arguments_is_refused`, `test_an_approval_is_spent_by_its_call`).
4. **The Mac is full when a chat opens, or a VM is idle while an engine needs the room.** Expected: the VM waits with a reason, or the least recently used idle VM or engine is stopped (its disk kept); a held VM is never evicted. Test in Task 3 (`test_an_idle_vm_is_evicted_for_an_engine_but_a_held_one_is_not`).
5. **serve restarts with sessions open, or a chat is deleted while its VM runs.** Expected: running VMs are adopted, stopped ones stay registered with disks, and deleting the chat stops the VM and deletes its disk and mirror branch. Test in Task 6 (`test_serve_restart_adopts_running_vms`, `test_deleting_a_chat_deletes_its_vm`).

---

### Task 1: `boxes:` config, the `Box` seam, `FakeBox`, and `box_env`

**Files:**
- Create: `src/airlie/boxes/__init__.py`, `src/airlie/boxes/protocol.py`, `tests/support/box.py`, `tests/unit/test_boxes_config.py`, `tests/unit/test_box_env.py`
- Modify: `src/airlie/config/schema.py` (`BoxesSpec`, `AirlieConfig.boxes`), `airlie.yaml` (a `boxes:` block with the defaults), `src/airlie/subproc.py` (`box_env`), `tests/unit/test_config.py`

**Interfaces:**
- Produces:
  - Config (`Strict`):
    - `BoxesSpec`: `backend: Literal["tart"] = "tart"`, `image: str = "ghcr.io/cirruslabs/ubuntu:latest"`, `memory_gb: float = 8.0` (gt 0), `cpus: int = 4`, `disk_gb: int = 40`, `idle_stop_s: float = 900.0`, `setup_timeout_s: float = 1800.0`, `boot_timeout_s: float = 120.0`, `network: BoxNetworkSpec`.
    - `BoxNetworkSpec`: `after_setup: Literal["off", "allow_list"] = "allow_list"`, `allow_hosts: list[str] = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]`. `off` means host-only networking; `allow_list` means everything blocked except the CIDRs the hosts resolve to.
    - `AirlieConfig.boxes: BoxesSpec`.
  - `airlie.boxes.protocol`:
    - `ExecResult(NamedTuple)`: `rc: int | None` (None: could not run or timed out), `output: str` (stdout and stderr merged).
    - `BoxUnavailable(EngineError)`: the backend cannot be used; its message says why and how to fix it.
    - `Box(Protocol)`:
      - `name: str`
      - `async exists() -> bool`: the disk exists.
      - `async running() -> bool`
      - `async create(image: str, *, cpus: int, memory_gb: float, disk_gb: int) -> None`: clone `image` to `name` and size it.
      - `async boot(*, network: Network, timeout_s: float) -> None`: start it with `network` and wait until `exec` answers.
      - `async exec(argv: Sequence[str], *, env: Mapping[str, str] = {}, cwd: str | None = None, stdin: bytes | None = None, timeout_s: float = 120.0) -> ExecResult`
      - `async exec_bytes(argv: Sequence[str], *, stdin: bytes | None = None, timeout_s: float = 120.0) -> tuple[int | None, bytes]`: for bundles; stdout only.
      - `async stop() -> None`: the disk stays.
      - `async delete() -> None`
    - `Network(NamedTuple)`: `mode: Literal["open", "off", "allow_list"]`, `allow: tuple[str, ...] = ()` (CIDRs).
  - `airlie.subproc.box_env(extra: Mapping[str, str] = {}) -> dict[str, str]`: the environment of a command in a VM, built from scratch: `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME=/home/admin`, `LANG=C.UTF-8`, `NO_PROMPTS`, `NO_SSH`, `GIT_CONFIG_*` resetting credential helpers, then `extra` (the recipe's variables). It never reads `os.environ`.
  - `tests/support/box.py::FakeBox(name: str)`: an in-memory box. It keeps a `files: dict[str, bytes]` tree under `/work`, records every `exec` (`calls: list[Call]` with argv, env, stdin, cwd), answers `exec` from a scripted `responses: dict[tuple[str, ...], ExecResult]` or runs a tiny interpreter for `cat`, `mkdir -p`, `test`, `find`, `git` calls the tests use (a test registers extra ones), and has `state: Literal["absent", "stopped", "running"]`, `boots: list[Network]`, `deleted: bool`, `fail_boot: str | None`.

- [ ] **Step 1: Write the failing tests.**
  - `tests/unit/test_boxes_config.py`: `test_box_defaults` (each default above), `test_boxes_reject_unknown_keys_and_a_bad_backend`, `test_the_committed_boxes_block_loads` (`load_config(ROOT / "airlie.yaml", NO_LOCAL).boxes.image == "ghcr.io/cirruslabs/ubuntu:latest"`).
  - `tests/unit/test_box_env.py`:

```python
def test_box_env_carries_no_credential(monkeypatch) -> None:
    for name in (*CREDENTIALS, "HOME", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "host-secret")
    env = box_env()
    assert not any(v == "host-secret" for v in env.values())
    assert env["HOME"] == "/home/admin" and env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_COUNT"]

def test_box_env_adds_the_recipes_variables_last() -> None:
    assert box_env({"CI": "1"})["CI"] == "1"
```

- [ ] **Step 2: Run them to see them fail.** Run: `uv run pytest tests/unit/test_boxes_config.py tests/unit/test_box_env.py -v` → FAIL (no `boxes`, no `box_env`).
- [ ] **Step 3: Implement** the schema (each field a `Field(description=…)`, as the neighbours), the `boxes:` block in `airlie.yaml` (the root file owns it, like `manager`), `box_env` in `subproc.py` reusing `NO_PROMPTS`, `NO_SSH` and `with_git_config`, and `protocol.py`. `FakeBox` in `tests/support/box.py` (a `Box` conformance check at the bottom: `_: Box = FakeBox("x")`, so mypy fails on drift).
- [ ] **Step 4: Run the tests** (the two new files and `tests/unit/test_config.py`) → PASS. `config/schema.py` is enrolled in mutation: pin every new default with a test, and run `task mutate:changed`.
- [ ] **Step 5: Commit**

```bash
git add src/airlie/boxes src/airlie/config/schema.py src/airlie/subproc.py airlie.yaml tests/support/box.py tests/unit pyproject.toml
git commit -m "boxes: config, the Box seam, a fake box, and a credential-free box environment"
```

### Task 2: `TartBox`, the recipe, and the cached image

**Files:**
- Create: `src/airlie/boxes/tart.py`, `src/airlie/boxes/recipe.py`, `src/airlie/boxes/images.py`, `tests/unit/test_tart_box.py`, `tests/unit/test_recipe.py`, `tests/unit/test_images.py`
- Modify: `src/airlie/doctor.py` (a row: "Tart installed, default image pulled", warn-level, with the install and pull commands), `tests/unit/test_doctor.py`, `pyproject.toml` (enrol the three modules)

**Interfaces:**
- Consumes: `Box`, `BoxUnavailable`, `Network`, `ExecResult`, `BoxesSpec` (Task 1); `box_env`.
- Produces:
  - `Runner = Callable[[Sequence[str], bytes | None, float], Awaitable[tuple[int | None, bytes]]]`: runs a host process (argv, stdin, timeout) and returns (rc, stdout+stderr). `RealRunner` uses `asyncio.create_subprocess_exec` with `start_new_session=True` and `kill_group` on timeout or cancel (the pattern of `tools/shell.py::_run`). The only code that starts `tart`.
  - `TartBox(name: str, *, run: Runner = RealRunner, tart: str = "tart", spawn: Spawner = real_spawn, resolve: Callable[[str], Awaitable[list[str]]] = dns_resolve)`: implements `Box`:
    - `exists`: `tart get <name> --format json` rc 0. `running`: the same JSON's `Running` flag.
    - `create`: `tart clone <image> <name>` then `tart set <name> --cpu N --memory MB --disk-size G`.
    - `boot`: spawns `tart run --no-graphics <network flags> <name>` as a detached process (`Spawner` returns a handle kept on the box; its log goes to `state_dir/boxes/<name>.log`), then polls `tart exec <name> true` every second until it answers or `timeout_s`. Network flags: `open` none; `off` `--net-host`; `allow_list` `--net-softnet-block=0.0.0.0/0 --net-softnet-allow=<cidrs>`.
    - `exec`: `tart exec [-i] <name> env -i <KEY=VAL…> sh -c 'cd <cwd> && exec "$@"' sh <argv…>`; the environment goes through `env -i`, so nothing of the host's reaches the guest (the Guest Agent's own environment is the guest's).
    - `stop`: `tart stop <name>`, then waits for the spawned process to end. `delete`: stop if running, `tart delete <name>`.
    - A missing `tart` binary (`FileNotFoundError` from the runner) raises `BoxUnavailable("Tart is not installed: brew install cirruslabs/cli/tart (airlie repository chats run in a Tart VM and never on the host)")`.
  - `Recipe` (frozen, `Strict`): `base: str | None`, `setup: tuple[str, ...]`, `env: dict[str, str]`, `test: tuple[str, ...]`, `workdir: str = "/home/admin/work"`. `load_recipe(text: str | None, defaults: BoxesSpec) -> Recipe`: `None` (no `.airlie/environment.json`) is the default image, no setup, no variables, no tests; malformed JSON or an unknown key raises `RecipeError` naming the key. `Recipe.key(image: str) -> str`: sha256 of the canonical JSON of (image, setup, env keys without values), first 12 hex chars. `test` is this plan's one addition to remote studio's "base image, setup script, variables": the commands `run_tests` runs in the VM.
  - `Images(box_for: Callable[[str], Box], spec: BoxesSpec, *, secrets: SecretStore, confirm: Callable[[str], bool])`:
    - `image_name(recipe) -> str` = `airlie-env-<recipe.key>`.
    - `async ensure(recipe: Recipe, repo: str) -> str`: the name of a stopped, cached image. With no `setup` it is the base image itself (no build). Otherwise, if `airlie-env-<key>` does not exist: clone the base, boot with `Network("open")`, run each setup line through `exec` with `box_env(recipe.env)` plus this repo's setup secrets, stop it. Secrets reach the guest only on stdin (`sh -s` reading an `export K=V` header then the script), never in argv, and are not part of the key. A failed setup deletes the half-built image and raises `RecipeError` with the failing line's output tail.
    - Building with secrets for a recipe key not seen before asks first: `confirm("build_image:<repo>:<key>")` must be true or `ConfirmationRequired` is raised (a changed recipe on a branch is how a setup script would read the secrets).
  - `SecretStore(dir: Path)`: `for_repo(repo: str) -> dict[str, str]` reads `<dir>/<owner>__<name>.env` (`KEY=VALUE` lines, mode 0600 enforced; none: empty). `dir` = `state_dir/boxes/secrets`. Per person is out of scope until chat has per-person ownership (noted in the doctor row, not built).
  - `dns_resolve(host) -> list[str]` (IPv4 CIDRs, `/32`) for the allow list.

- [ ] **Step 1: Write the failing tests** (a `FakeRunner` records argv and scripts replies; a fake spawner).
  - `tests/unit/test_tart_box.py`:
    - `test_create_clones_the_image_then_sizes_it`: argv `["tart","clone","ghcr.io/cirruslabs/ubuntu:latest","airlie-chat-ab12"]` then `["tart","set","airlie-chat-ab12","--cpu","4","--memory","8192","--disk-size","40"]`.
    - `test_boot_with_an_allow_list_blocks_everything_else`: the run argv contains `--net-softnet-block=0.0.0.0/0` and `--net-softnet-allow=151.101.0.223/32,...` from the fake resolver.
    - `test_boot_with_network_off_is_host_only` (`--net-host`); `test_boot_open_has_no_network_flag`.
    - `test_boot_waits_until_exec_answers_and_times_out_with_the_log_tail`.
    - `test_exec_passes_the_environment_through_env_i_and_never_the_hosts` (set a host variable; assert argv contains only the given pairs after `env -i`).
    - `test_exec_with_stdin_attaches_it` (`-i`); `test_exec_timeout_returns_none_rc`.
    - `test_stop_keeps_the_disk_and_delete_removes_it` (`tart stop`, then `tart delete` only for delete).
    - `test_a_missing_tart_says_how_to_install_it`.
  - `tests/unit/test_recipe.py`: `test_no_recipe_is_the_default_image`, `test_a_recipe_names_base_setup_env_and_tests`, `test_an_unknown_key_is_refused_naming_it`, `test_the_key_changes_with_setup_and_not_with_env_values`.
  - `tests/unit/test_images.py` (with `FakeBox`): `test_no_setup_uses_the_base_image_without_building`, `test_setup_builds_once_and_the_second_ensure_reuses_it`, `test_setup_secrets_reach_the_guest_on_stdin_only` (no argv, no `exec` env, no recorded call other than the stdin one contains the value), `test_a_failed_setup_deletes_the_partial_image`, `test_a_new_recipe_with_secrets_needs_confirmation`.
  - `tests/unit/test_doctor.py`: `test_the_tart_row_warns_with_the_install_command_when_tart_is_missing` (through the doctor's probe, not a real `tart`).
- [ ] **Step 2: Run them to see them fail.** Run: `uv run pytest tests/unit/test_tart_box.py tests/unit/test_recipe.py tests/unit/test_images.py -v` → FAIL.
- [ ] **Step 3: Implement** `tart.py`, `recipe.py`, `images.py` and the doctor row as specified. Parsing uses pydantic `Strict` models like the rest of the config.
- [ ] **Step 4: Run the tests, then enrol** the three modules after `task mutate:changed` is clean for them.
- [ ] **Step 5: Commit**

```bash
git add src/airlie/boxes src/airlie/doctor.py tests/unit pyproject.toml
git commit -m "boxes: the Tart backend, the repository recipe, and a cached image built with setup-only secrets"
```

### Task 3: A VM is a manager resource

**Files:**
- Create: `src/airlie/manager/vms.py`, `tests/unit/test_vm_resource.py`
- Modify: `src/airlie/manager/core.py` (`Manager.add`, `Manager.remove`), `tests/unit/test_manager.py`

**Interfaces:**
- Consumes: `Resource`, `Manager`, `Report` (existing); `Box`, `Network`, `Images`, `Recipe`, `BoxesSpec`.
- Produces:
  - `Manager.add(resource: Resource) -> None`: registers a resource after construction (a name already registered raises `ValueError`). `Manager.remove(name: str) -> None`: raises `EngineError` while the slot has holders or a lock held; otherwise forgets it, and a `stop_failed` slot is refused (still counts for memory). Both `_notify()` waiters.
  - `VMResource`, implementing `Resource` with `kind: Literal["engine","vm"] = "vm"`, `lifecycle = "on_demand"`:
    - `VMResource(session_id: str, repo: str, box: Box, spec: BoxesSpec, *, image: Callable[[], Awaitable[str]], network: Callable[[], Awaitable[Network]])`; `name = f"vm:{session_id}"`, `idle_unload_s = spec.idle_stop_s`, `footprint_gb = spec.memory_gb`, `display = f"{repo} VM"`.
    - `running()` = `box.running()`.
    - `start(report)`: if the disk is absent, `report("starting", "preparing the environment", None)`, `image()` then `box.create(...)`; then `report("starting", "", None)`, `box.boot(network=await network(), timeout_s=spec.boot_timeout_s)`. Any `BoxUnavailable` or boot failure raises `EngineError` (the manager marks it failed with the message).
    - `stop()` = `box.stop()` (the disk stays). `delete()` = `box.delete()`.
  - `Manager.status()` already reports `kind`; VMs list beside engines in `GET /v1/airlie/engines` with no route change (plan 1's route reads `manager.status()`); the engines table in Admin shows the `vm:` rows.

- [ ] **Step 1: Write the failing tests.**
  - `tests/unit/test_manager.py` additions: `test_a_resource_added_after_construction_is_held_and_reaped`, `test_removing_a_held_resource_is_refused`, `test_removing_a_stopped_resource_forgets_it`, `test_adding_a_name_twice_is_refused`.
  - `tests/unit/test_vm_resource.py` (`FakeBox`, `FakeClock`, a real `Manager`):
    - `test_the_first_hold_creates_and_boots_the_vm_and_a_second_hold_reuses_it` (`FakeBox.boots` has one entry).
    - `test_footprint_is_the_configured_memory_and_counts_in_the_budget`: budget 20 GB, engine 14 GB held, VM 8 GB → the VM's hold waits with "waiting for memory: …".
    - `test_an_idle_vm_is_evicted_for_an_engine_but_a_held_one_is_not`.
    - `test_an_idle_vm_stops_after_idle_stop_s_and_its_disk_is_kept` (`state == "stopped"`, `deleted is False`), and `test_the_next_hold_boots_it_again_without_cloning`.
    - `test_a_boot_failure_is_a_failed_status_with_the_reason` (`fail_boot="no guest agent"`).
    - `test_a_vm_that_exited_is_failed_and_the_next_hold_restarts_it`.
    - `test_delete_removes_the_disk_after_removal_from_the_manager`.
- [ ] **Step 2: Run them to see them fail.** Run: `uv run pytest tests/unit/test_manager.py tests/unit/test_vm_resource.py -v` → FAIL.
- [ ] **Step 3: Implement** `Manager.add`/`remove` (dict mutation; `core.py` is enrolled, so every new line needs a mutant-killing test: run `task mutate:changed`) and `VMResource`.
- [ ] **Step 4: Run the tests, then enrol** `src/airlie/manager/vms.py`.
- [ ] **Step 5: Commit**

```bash
git add src/airlie/manager tests/unit pyproject.toml
git commit -m "manager: VMs register per session and are held, evicted, idle-stopped and deleted like engines"
```

### Task 4: The builtin tools run in the box

**Files:**
- Create: `src/airlie/tools/boxed.py`, `tests/unit/test_tools_boxed.py`
- Modify: `src/airlie/tools/protocol.py` (`ToolContext.box`), `src/airlie/tools/shell.py` (`_run` and `run_steps` through the box), `src/airlie/tools/compute.py` (`Compute.run`), `src/airlie/tools/registry.py` (`ToolRegistry.boxed`), `tests/unit/test_shell.py`

**Interfaces:**
- Consumes: `Box`, `box_env`, `Recipe` (for `env`, `test`, `workdir`).
- Produces:
  - `ToolContext.box: Box | None = field(default=None, hash=False, compare=False)` and `ToolContext.box_env: Mapping[str, str]` (hash/compare off): the recipe's variables. `ToolContext.worktree` is `Path(recipe.workdir)` in box mode and is only a path inside the VM (the tools never touch it on the host).
  - `shell._run`: when `ctx.box` is set it calls `ctx.box.exec(["/bin/sh", "-c", cmd] or argv, env=box_env(ctx.box_env), cwd=str(ctx.worktree), stdin=None, timeout_s=ctx.shell_timeout_s)` instead of spawning and sandboxing on the host, and returns `(rc, text)` as before. `_run` never reads `os.environ`, `agent_env`, `confine` or `started` on this path. `run_steps` keeps its `cd` carry-over with a `pwd` echoed on stdout's last line instead of the host temp file. The `git` tool's deny list and `-c`-option refusal are unchanged and apply in the box too; its identity pair `user.name=airlie`, `user.email=airlie@local` still applies.
  - `tools/boxed.py`: `BoxReadFile`, `BoxWriteFile`, `BoxEditFile`, `BoxListDir`, `BoxGrep` with the same `name`, `description`, `parameters` as the host tools (import and reuse the host classes' attributes) and `run(args, ctx)` over `ctx.box`: read via `cat`, write via `exec(["sh","-c",'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path], stdin=content)`, list via `find`, grep via `grep -rnE` with the same skip dirs, caps and secret-file refusal. `edit_file` reuses `fs._not_found` and `fs._loose_span` for matching. Paths are normalised lexically against `ctx.worktree` and a path that leaves it is refused (`PermissionError` text as the host tool); inside the VM the boundary is the VM, so there is no symlink resolution and none of the story line's locks (`read_only`, `locked_tests`, `write_scope`): a chat has none of them.
  - `Compute.run`: with `ctx.box`, runs `python3 -I -c <code>` in `/tmp/airlie-compute` in the box (same limits: `TIMEOUT_S`, `OUTPUT_CHARS`, `CALLS`, repeated-program message); no `confine` (the VM is the confinement; network is off by then).
  - `ToolRegistry.boxed(names: Iterable[str], ctx: ToolContext) -> ToolRegistry`: like `builtin`, but maps `read_file…grep` to the `Box*` classes and requires `ctx.box`; shell, run_tests, git and compute are the same classes (they branch on `ctx.box`). `run_tests` in a box runs `Recipe.test` steps through `run_steps`; no steps: `error: no test command: add "test" to .airlie/environment.json`.

- [ ] **Step 1: Write the failing tests** (`FakeBox` with its mini interpreter; context built with `ToolContext(worktree=Path("/home/admin/work"), box=box, box_env={"CI": "1"})`):
  - `test_shell_in_a_box_runs_through_the_box_with_the_box_environment` (the recorded call's env is `box_env({"CI":"1"})` and cwd the workdir; no host process: `monkeypatch` `asyncio.create_subprocess_shell`/`_exec` to raise).
  - `test_the_git_tool_in_a_box_still_refuses_push` and `test_the_git_tool_in_a_box_rejects_dash_c`.
  - `test_run_tests_in_a_box_runs_the_recipe_steps_and_reports_each`, and `test_run_tests_with_no_recipe_tests_says_how_to_add_them`.
  - `test_read_file_in_a_box_numbers_lines_and_honours_offset_and_limit`, `test_write_file_in_a_box_creates_parents`, `test_edit_file_in_a_box_replaces_a_unique_match_and_explains_a_miss`, `test_list_dir_and_grep_skip_build_dirs_and_secret_files`, `test_a_path_that_leaves_the_workdir_is_refused`.
  - `test_compute_in_a_box_runs_python_there` and `test_compute_in_a_box_keeps_its_call_limit`.
  - `test_boxed_registry_needs_a_box` (`KeyError`/`ValueError` naming it).
- [ ] **Step 2: Run them to see them fail.** Run: `uv run pytest tests/unit/test_tools_boxed.py tests/unit/test_shell.py -v` → FAIL.
- [ ] **Step 3: Implement.** Keep the host path byte-for-byte unchanged when `ctx.box is None`; `shell.py` mutants run on the mac job already (`mac_functions`), so the box branch goes in its own small function (`_run_in_box`) that the Linux mutation shards test.
- [ ] **Step 4: Run the tests** (the two files plus `tests/unit/test_tools*.py`, `tests/unit/test_compute.py`) → PASS; enrol `tools/boxed.py` after `task mutate:changed`.
- [ ] **Step 5: Commit**

```bash
git add src/airlie/tools tests/unit pyproject.toml
git commit -m "tools: a box path behind the shell seam; file tools, shell, run_tests, git and compute run in the VM"
```

### Task 5: Code in, branch out, push through the host

**Files:**
- Create: `src/airlie/boxes/mirror.py`, `src/airlie/boxes/transit.py`, `tests/unit/test_transit.py`, `tests/functional/test_mirror_git.py`
- Modify: `src/airlie/story/publish.py` only if a public method is missing (see Interfaces), `pyproject.toml`

**Interfaces:**
- Consumes: `Box`, `Recipe`, `Publisher` (`prepare`, `push_top(wt, branch)`, `create_draft(wt, branch, base, title)`, `pr`), `GitHubApp`, `find_checkout`, `without_hooks`.
- Produces:
  - `Mirror(root: Path, repo: str, publisher: Publisher, *, run: Runner, local_checkout: Path | None = None)`: a host clone at `root/<owner>__<name>` that is the only place the host's credentials touch git for chat. `async refresh() -> None`: clone on first use, then `git fetch --prune origin` under `Publisher._step`'s App-token environment (the existing `Publisher.fetch(repo, trunk)` is used; if it only fetches a trunk, add `Publisher.fetch_all(repo)` there with the same `_step` discipline). Without a GitHub App configured it clones/fetches from `local_checkout` (`find_checkout(story.repos_root, repo)`) and `can_push` is False. `async recipe_text(ref: str) -> str | None`: `git show <ref>:.airlie/environment.json`, None if absent. `async resolve(ref: str) -> str` (commit sha).
  - `seed(box: Box, mirror: Mirror, recipe: Recipe, *, ref: str, branch: str, session_id: str) -> None`: on the host `git bundle create <tmp> <ref>` (airlie's git: `without_hooks` environment, no credentials needed: it is local), streams the bundle into the VM with `box.exec(["sh","-c","cat > /tmp/in.bundle"], stdin=bytes)`, then in the VM `git clone /tmp/in.bundle <workdir>`, `git -C <workdir> remote remove origin`, `git checkout -b <branch>`, and sets `user.name=airlie`, `user.email=airlie@local`. Idempotent: a workdir that already holds a clone is left alone (a resumed session).
  - `harvest(box: Box, mirror: Mirror, *, base_sha: str, branch: str) -> HarvestResult`: in the VM `git bundle create - <base_sha>..<branch>` read with `box.exec_bytes`; on the host write it to a temp file, `git bundle verify`, then `git -c transfer.fsckObjects=true fetch <bundle> refs/heads/<branch>:refs/heads/<branch>` into the mirror with hooks, fsmonitor and `replace` refs off (`without_hooks`). `HarvestResult(commits: int, sha: str, changes_workflows: bool)`. A VM with nothing new returns `commits == 0`.
  - `push_branch(mirror, publisher, *, box, base_sha, branch) -> HarvestResult`: `harvest`, refuse `commits == 0` ("nothing to push"), refuse a mirror without `can_push` with the message "set story.github_app to push from chat", then `publisher.push_top(mirror.path, branch)`.
  - `open_pr(mirror, publisher, *, branch, base: str, title: str) -> tuple[int, str]`: `create_draft` then `pr`; always a draft. Both are host tools that never run in the VM.

- [ ] **Step 1: Write the failing tests.**
  - `tests/unit/test_transit.py` (`FakeBox`, a `FakeRunner` for the host's git, a fake `Publisher` that records calls):
    - `test_seed_streams_a_bundle_in_clones_it_and_removes_the_remote`: the VM's recorded calls, in order: stdin write of the bundle bytes, `git clone`, `git remote remove origin`, `git checkout -b airlie/chat-ab12cd34`.
    - `test_seed_leaves_an_existing_clone_alone`.
    - `test_harvest_fetches_the_bundle_with_fsck_and_no_hooks`: the host git argv contains `transfer.fsckObjects=true`, `core.hooksPath=/dev/null`, and the fetch target is the mirror.
    - `test_harvest_rejects_a_bundle_that_does_not_verify` (nothing is fetched).
    - `test_nothing_new_in_the_vm_is_not_pushed`.
    - `test_push_goes_through_the_publisher_from_the_mirror_and_never_the_box` (the publisher saw `push_top(mirror.path, branch)`; `FakeBox` saw no `git push` and no credential in any call's env).
    - `test_push_without_a_github_app_says_how_to_enable_it`.
    - `test_open_pr_is_a_draft`.
  - `tests/functional/test_mirror_git.py` (real git on a temp repo, no network: `tests/support/git.py` helpers): `test_a_real_bundle_round_trips_a_commit_from_a_cloned_workdir_to_the_mirror` (clone a bundle into a second temp dir standing for the VM, commit, bundle back, fetch into the mirror, assert the commit is on the branch there and on no other ref).
- [ ] **Step 2: Run them to see them fail.** Run: `uv run pytest tests/unit/test_transit.py tests/functional/test_mirror_git.py -v` → FAIL.
- [ ] **Step 3: Implement.** The checkout travels as a git bundle over `exec` stdin and stdout: no shared folder (a read-write virtiofs share would be a host write channel for untrusted code) and no host proxy. The VM never has a remote, so `git fetch`/`push` inside it have nowhere to go even if the `git` tool's deny list were bypassed through `shell`.
- [ ] **Step 4: Run the tests** → PASS; enrol `mirror.py` and `transit.py`.
- [ ] **Step 5: Commit**

```bash
git add src/airlie/boxes src/airlie/story/publish.py tests pyproject.toml
git commit -m "boxes: the host fetches the code in as a bundle and takes the session branch out; push and PR stay on the host"
```

### Task 6: Repository chat sessions, the turn handler, and server-side confirmations

**Files:**
- Create: `src/airlie/chat/repo_session.py`, `src/airlie/chat/confirm.py`, `src/airlie/chat/repo_tools.py`, `tests/unit/test_confirmations.py`, `tests/unit/test_repo_session.py`, `tests/functional/test_repo_chat_turn.py`
- Modify: `src/airlie/serve/app.py` (build the `RepoSessions` service, register it on `app.state`, hook thread delete, `adopt` after restart), `src/airlie/chat/api.py` (body fields `repo`, `base_ref`, `approvals`; the repository path replaces the 400), `src/airlie/chat/threads.py` (`repo`, `base_ref`, `on_delete`), `src/airlie/chat/kinds.py`, `src/airlie/chat/tools.py`, `src/airlie/chat/hooks.py` (`before_tool`), `src/airlie/chat/stream.py` (`tool_approval_request`), `src/airlie/chat/turn.py` (run `before_tool`); plan 3's tests that pin the 400 (`test_a_repository_chat_is_400_naming_plan_4`, `test_repository_kind_is_unavailable_until_plan_4`) are replaced here, `src/airlie/config/schema.py` (the repository kind's defaults below; `ChatKindSpec` and `ask_first` are plan 3's), `airlie.yaml`, `tests/support/serve_chat.py`, `tests/unit/test_config.py`

**Interfaces:**
- Consumes: from plan 3 (see the table above) `chat_router`, `Thread`, `ThreadStore`, `ChatKind`, `registry_for`, `ask_first`, `Hooks`, `TurnContext`, `StreamWriter`, `run_turn`; `Manager` (`add`, `hold`, `adopt`, `remove`), `VMResource`, `Images`, `Mirror`, `seed`, `push_branch`, `open_pr`, `holder_for`.
- Produces:
  - Config: plan 3's `ChatSpec.kinds: ChatKindsSpec` with `ChatKindSpec(tools, ask_first)`; no new fields. This plan changes the repository defaults to `tools = [read_file, write_file, edit_file, list_dir, grep, shell, run_tests, git, compute, skill, todo, task, web_search, web_fetch, push_branch, open_pr]` and `ask_first = [push_branch, open_pr, build_image]`, adds `push_branch` and `open_pr` to plan 3's plain-kind forbidden-name validator and to `KNOWN_CHAT_TOOLS` (`build_image` is allowed in `ask_first` only: it is a gate on `Images`, not a tool), and updates plan 3's `test_chat_kind_defaults`.
  - `Confirmations`: `request(session_id, call_id, tool, args) -> str` records a pending item and returns its id (and the stream emits `StreamWriter.tool_approval_request(approval_id, call_id)` followed by `data("approval", {tool, arguments})` for the tool name and the arguments); `approve(session_id, approvals: Sequence[Approval]) -> None` (`Approval(id, approved)`); `check(session_id, tool, args) -> Literal["allowed","pending","denied"]`: an approval matches only the same `tool` and the sha256 of the canonical JSON of `args`, belongs to this session, and is single use (`check` consumes it). Nothing a model emits can create an approval: only the turn body's `approvals` field (added here to `chat/api.py`), authenticated as the person, reaches `approve`; the route calls it before the turn starts. Wired as a `before_tool` hook (added here to plan 3's `Hooks`) for tools in `ask_first(kind, cfg)`.
  - `RepoSessions(cfg, manager, publisher_factory, images, mirrors, box_for, clock)`, a session being plan 3's `Thread` (id, `repo`, `base_ref`):
    - `async open(thread: Thread) -> RepoChat`: raises `BoxUnavailable` (surfaced as a 409 `{"error": "...install hint..."}` by `chat_router`) when Tart is missing; otherwise refreshes the mirror, resolves `base_ref` to `base_sha`, reads the recipe (`Mirror.recipe_text`), registers `VMResource` with `Manager.add` (idempotent per session), and returns a handle holding `session_id`, `branch`, `base_sha`, `recipe`.
    - `@asynccontextmanager turn(chat: RepoChat, owner: str) -> AsyncIterator[ToolContext]`: `manager.hold(f"vm:{id}", owner)`, then on the first turn `seed(...)` (only if the VM has no clone), then yields a `ToolContext(worktree=Path(recipe.workdir), box=box, box_env=recipe.env, test_steps=recipe.test, shell_timeout_s=…)`. The VM hold is released at the end of the turn; plan 3's per-turn engine hold (`chat:{thread_id}` on the `chat` role's engine) is separate, so a closed tab leaves each timer to run on its own (review resolution 23).
    - `async delete(session_id: str) -> None`: wired to `ThreadStore.on_delete` (added here): wait for no holders, `manager.remove`, `box.delete()`, delete the mirror's session branch.
    - `async adopt() -> None`: at serve startup, re-register a `VMResource` for each stored repository chat whose disk exists (`box.exists()`), then `manager.adopt()` marks running ones ready.
  - `repo_tools.py::build_repo_tools(chat, ctx, names) -> ToolRegistry`, called by `registry_for("repository", ...)`: `ToolRegistry.boxed(names ∩ boxed set, ctx)` plus the host tools `PushBranch` and `OpenPr` (`name = "push_branch"`, parameters `{}`; `name = "open_pr"`, parameters `{"title": str, "body": str}`), which call Task 5's `push_branch`/`open_pr` with the session's `branch`/`base_sha` and are never in a registry of a plain chat.

- [ ] **Step 1: Write the failing tests.**
  - `tests/unit/test_confirmations.py`: `test_an_unapproved_push_is_refused` (`check` → `"pending"` and a request id exists), `test_an_approval_for_other_arguments_is_refused`, `test_an_approval_is_spent_by_its_call`, `test_an_approval_from_another_session_is_refused`, `test_a_denied_approval_denies`, `test_a_tool_not_on_the_confirm_list_is_allowed`.
  - `tests/unit/test_repo_session.py` (real `Manager`, `FakeBox`, a fake `Mirror`, `FakeClock`):
    - `test_opening_a_repository_chat_without_tart_is_refused_with_the_install_hint` (a box whose `boot` raises `BoxUnavailable`; no host process).
    - `test_the_first_turn_seeds_the_clone_and_the_second_does_not`.
    - `test_a_turn_holds_the_vm_and_releases_it_at_the_end` (holder `chat:<id>` during, none after).
    - `test_the_tool_context_carries_the_box_and_the_recipes_variables_and_tests`.
    - `test_a_plain_kind_registry_has_no_file_shell_git_or_push_tool` and `test_config_rejects_a_plain_kind_that_lists_a_file_tool`.
    - `test_serve_restart_adopts_running_vms` and `test_deleting_a_chat_deletes_its_vm` (VM stopped, disk deleted, removed from the manager, branch dropped).
    - `test_a_vm_waits_for_memory_with_the_holders_named`.
  - `tests/functional/test_repo_chat_turn.py` (through `TestClient`, `FakeBox`, a scripted engine from `tests/support/serve_chat.py` that emits one `push_branch` call): `test_a_push_call_streams_a_permission_request_and_does_not_push`, `test_a_turn_resent_with_the_approval_pushes_once`, `test_a_turn_without_a_key_is_401`, `test_an_unknown_session_is_404`, `test_a_malformed_approval_is_422`, `test_tart_missing_is_a_409_with_the_install_hint`, `test_a_failed_vm_boot_is_reported_in_the_stream_as_an_error_part` (assert bodies).
- [ ] **Step 2: Run them to see them fail.** Run: `uv run pytest tests/unit/test_confirmations.py tests/unit/test_repo_session.py tests/functional/test_repo_chat_turn.py -v` → FAIL.
- [ ] **Step 3: Implement.** `chat_router` (plan 3) calls `RepoSessions.open` when a thread of kind `repository` is first opened (`ThreadStore.open(..., repo=, base_ref=)`; a missing `repo` is a 422) and wraps each turn in `RepoSessions.turn`, passing its `ToolContext` to `registry_for`. `run_turn` runs the new `before_tool` hook before `registry.call`: `Confirmations.check` returning `"pending"` ends the call with a `tool_result(..., ok=False)` "waiting for your approval" and emits `tool_approval_request`; `"denied"` ends it with a refusal; `"allowed"` runs it. `app.py` builds `RepoSessions` only when `cfg.boxes.backend` is set and passes `box_for = lambda name: TartBox(name, …)`; tests inject `FakeBox`. Wire `app.state.chat_threads.on_delete(repo_sessions.delete)`.
- [ ] **Step 4: Run the tests and the serve suites.** Run: `uv run pytest tests/unit tests/functional -q -k "confirm or repo_ or chat or manager"` → PASS. Enrol `chat/confirm.py`, `chat/repo_session.py`.
- [ ] **Step 5: Commit**

```bash
git add src/airlie/chat src/airlie/serve/app.py src/airlie/config/schema.py airlie.yaml tests pyproject.toml
git commit -m "chat: a repository chat opens a VM per session; confirmations are enforced server-side"
```

### Task 7: The `task` subagent tool

**Files:**
- Create: `src/airlie/chat/task_tool.py`, `tests/unit/test_task_tool.py`
- Modify: `src/airlie/chat/repo_tools.py` (register it), `src/airlie/config/schema.py` (`ChatTaskSpec`), `airlie.yaml`, `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `generate_with_tools(engine, req, tools, max_steps, *, on_steer=None)`, `ToolRegistry.boxed`, `ManagedEngine`, `holder_for`, `cli._http_engine` (the engine factory), `GenRequest`, `Message`, the roles' prompts (`cfg.roles[name]`), plan 3's `build_system_prompt(..., subagent=True)` (skips the bootstrap and the skills listing; used when the subagent's role is `chat`).
- Produces:
  - Config: `ChatTaskSpec(Strict)` at `chat.task`: `default_role: str = "coder"`, `roles: list[str] = ["coder", "reviewer", "tester", "architect"]` (the roles a model may choose; the validator checks each exists in `roles:`), `max_steps: int = 30`, `max_depth: int = 1`.
  - `Task` tool: `name = "task"`, `parameters = {"role": string (optional, enum from config), "prompt": string}`, `required = ["prompt"]`, `async run(args, ctx) -> ToolResult`: builds a fresh `GenRequest` (the role's own prompt from `cfg.roles[role]`, or `build_system_prompt(cfg, "repository", library, chat_skills, subagent=True)` for the `chat` role; no thread history, no superpowers bootstrap) with the prompt as the one user message; a registry from `ToolRegistry.boxed(<the repository kind's tools minus task, push_branch, open_pr, skill, todo>, ctx)` over the same `ctx` (so the same VM, the same `box_env`); an engine from the role's engine through `ManagedEngine` (so the subagent holds its engine for each call through the manager: a subagent on the reviewer starts Muse on demand and may wait for memory with the reason shown); runs `generate_with_tools(..., max_steps=cfg.chat.task.max_steps)`; returns the final text capped at `ctx.output_cap_chars`. A role not in the list is an error result naming the allowed ones; no role means `default_role` (a skill that does not name one gets the coder, "Left to the builder" decision). A subagent cannot call `task` (depth 1), cannot push or open a PR, and its confirmations are never auto-approved: it simply does not have those tools.
  - The tool takes `engine_for: Callable[[str], Engine]` and `model_context` by constructor injection so tests pass a scripted engine.

- [ ] **Step 1: Write the failing tests** (scripted `FakeEngine` from `tests/support/tool_loop.py`; `FakeBox`; real `Manager` with a fake engine resource):
  - `test_task_runs_a_fresh_context_and_returns_its_final_text` (the engine's first request has exactly the role prompt and the one user message; none of the session's history).
  - `test_task_tools_run_in_the_same_vm` (a scripted `write_file` call lands in the session's `FakeBox`).
  - `test_task_holds_its_roles_engine_through_the_manager` (holder `role:reviewer` during the call, none after; a stopped reviewer is started).
  - `test_task_has_no_task_push_or_pr_tool` (the schemas offered to the subagent).
  - `test_task_without_a_role_uses_the_default_role` and `test_task_with_an_unknown_role_lists_the_allowed_ones`.
  - `test_task_stops_at_max_steps_and_still_returns_an_answer`.
  - `test_task_skips_the_superpowers_bootstrap`.
  - `tests/unit/test_config.py`: `test_chat_task_defaults` and `test_chat_task_rejects_a_role_that_does_not_exist`.
- [ ] **Step 2: Run them to see them fail.** Run: `uv run pytest tests/unit/test_task_tool.py tests/unit/test_config.py -v` → FAIL.
- [ ] **Step 3: Implement** `Task` and register it in `build_repo_tools` for repository chats only (a plain chat's validator already rejects it).
- [ ] **Step 4: Run the tests** → PASS; enrol `chat/task_tool.py`.
- [ ] **Step 5: Commit**

```bash
git add src/airlie/chat src/airlie/config/schema.py airlie.yaml tests pyproject.toml
git commit -m "chat: the task tool runs a fresh-context subagent on a chosen role in the session's VM"
```

### Task 8: Docs and the live checks

**Files:**
- Modify: `README.md` (a "Repository chats" section after "Engines on demand": Tart install, the recipe format with an example, the default image, `boxes:` keys, the confirm list, secrets file location and mode, "no host fallback"), `docs/superpowers/specs/2026-10-16-remote-studio-design.md` (one line under "Environments": chat uses the `.airlie/environment.json` recipe as built here, with `test` added)
- Create: `docs/integrations/environment.example.json`, `tests/unit/test_recipe_example.py` (the example parses with `load_recipe`)

- [ ] **Step 1: Write the failing test** `test_the_documented_recipe_example_loads` and run it (FAIL: no file).
- [ ] **Step 2: Write the example and the README section**, then run the test (PASS) and `uv run ruff check . && uv run ruff format --check .`.
- [ ] **Step 3: Live check (Noah's go; the Studio; not in the gate).** `tart --version` and `tart list` show the default image pulled (`tart pull ghcr.io/cirruslabs/ubuntu:latest`). Then, in the web chat's repository selector (a board card's "open in chat" until the redesign), open a repository chat on a small repository and check, writing each result under "Live checks" in `docs/bench/2026-11-12-repository-chat-vm.md`:
  1. The VM boots and `exec` answers (the guest agent is in the default image; if not, the recipe must install it, and the default image changes).
  2. A turn clones, edits, and `run_tests` passes in the VM; `tart list` shows `airlie-chat-<id>`.
  3. With `network.after_setup: allow_list`, `pip download` of a package works and `curl https://example.com` fails (the softnet block plus the resolved CIDRs; if a CDN's addresses rotate, record it).
  4. Ask for a push: the permission prompt appears, approve it, and the branch exists on GitHub under the App's identity; refuse once and confirm nothing was pushed.
  5. `env` inside the VM shows no token; `git remote -v` there is empty.
  6. Idle past `idle_stop_s`: `tart list` shows it stopped; the next message resumes with the clone intact. Delete the chat: the VM is gone from `tart list`.
  7. A `task` call on the reviewer starts Muse through the manager (the engines table shows it) and the review text comes back.
  8. Memory: with gemma and the VM up, the doctor's resident-total row still reads under the budget.
- [ ] **Step 4: Commit**

```bash
git add README.md docs tests/unit/test_recipe_example.py
git commit -m "docs: repository chats, the recipe, and the live check list"
```

---

## Self-Review

- **Spec coverage.** VM per session as a `kind == "vm"` resource with footprint = configured memory, on_demand, idle stop keeping the disk, delete with the chat: Tasks 3, 6. Box backend behind the shell seam (`subproc`/`agent_env`, `tools/shell.py`) with builtin tools and compute in the VM: Tasks 1, 4. Recipe, default image, cached image, fresh copy per session (`tart clone` of the cached image), setup secrets only during setup, network off after setup with an allow list: Tasks 1, 2. Host fetches code in and branch out, pushes with the GitHub App after confirmation, no credential in the VM: Tasks 1 (`box_env`), 5, 6. Confirmations enforced server-side and listed in config: Task 6. `task` subagent holding its engine through the manager in the same VM: Task 7. No host fallback and the install hint: Tasks 2, 6. `boxes:` config: Task 1. Linux first, macOS later: Global Constraints. Phase-4 testing line (fake box: start, hold, idle stop with disk kept, delete; tools routed to the box; push through the host; no credential in the box; memory in the budget): Tasks 1-6. Live check from the spec: Task 8.
- **Placeholders.** None left open.
- **Type consistency.** `Box`, `Network`, `ExecResult`, `Recipe`, `Images`, `Mirror`, `HarvestResult`, `VMResource`, `RepoSessions`, `Confirmations` are defined once and used with those names; `box_env` is the one name for the VM environment (never `agent_env`).
- **Proportion.** Eight tasks for one spec phase; most of the weight is test lists, which are the spec's behaviours.

## Decisions the spec leaves open (for Noah)

1. **Checkout in and out is a git bundle over `tart exec` stdin and stdout** (no shared folder, no proxy): a read-write virtiofs share would be a host write channel for untrusted code; the host fetches the returned bundle with `fsckObjects` and hooks off.
2. **Network allow list is by hostname, resolved to `/32` CIDRs at each VM start** (softnet allows CIDRs only): a CDN whose addresses rotate can fail an install mid-session; the live check records it. `boxes.network.after_setup: off` is the strict alternative.
3. **The recipe gains a `test` list** (the commands `run_tests` runs in the VM) beyond remote studio's base, setup and variables; without it `run_tests` says how to add it.
4. **Building an image from a new recipe while setup secrets exist asks first** (`build_image` in the confirm list), because a changed recipe on a branch is how a setup script would read the secrets. Secrets are one file per repository, not yet per person.
5. **`task` defaults to the `coder` role** and offers `coder`, `reviewer`, `tester`, `architect` (config); depth is 1 and a subagent has no push, PR or `task` tool.
