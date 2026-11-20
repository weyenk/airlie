
> **NOT APPLICABLE — written against a different codebase. Do not implement.**
>
> Verified against this checkout on 2026-11-19 (`main` @ `2bf88946`; tree clean apart from
> this untracked doc):
>
> - Every path this plan names is absent, and never existed in this repo's history
>   (`git log --all --diff-filter=A -- '*webview*' '*/service/chat*'` returns nothing):
>   there is no `airlie/service/`, `airlie/web/` or `airlie/utils/`. The backend here is
>   `src/airlie/{chat,serve,boxes,engine,flow,…}`, the frontend is Vue in `web/src/`
>   (no `webview.jsx`), `tests/unit/` is flat (no `tests/unit/services/`), there is no
>   `tests/web/`, and there is no `web/vitest.config.ts` to keep a project in — `web/` is a
>   Vite/Vue app whose gate is `task web:gate` (`web:check` then `web:build`).
> - The symbols it names — `resolve_workspace_root`, `_project_project_workspace`,
>   `context_ref`/`contextRef`, `ensure_workspace`, `ChatStarted` — appear nowhere in the
>   workspace except inside this document. So there is no per-turn context write to make
>   idempotent and no workspace-root resolution that could point at a missing clone.
> - Fix 3's premise is already how this server works: `create_thread`
>   (`src/airlie/chat/api.py`) returns 201 straight after `threads.create(...)` and starts
>   the VM in a background task (`may_prewarm` → `prewarm`, api.py:673-674; prewarming is
>   capped per person by `boxes.prewarm_max_per_person`, api.py:605). When it wasn't
>   prewarmed its first turn starts it, and the stream carries `data-engine` status while
>   it waits (`tests/unit/test_chat_vm_status.py`). Chat creation therefore does not block
>   on engine cold start — there is no inline workspace/clone/materialize step to move off
>   the request path.
> - The verification commands it gates on (`pytest tests/unit/services/…`, the `tests/web`
>   suites) do not exist. The real gate is `ruff check`, `ruff format --check`, `mypy`,
>   `uv run pytest -q -n auto --maxprocesses 8` and `task web:gate` (see `AGENTS.md`).
>
> A near-duplicate copy lives in `../airlie-c-fast-first-start/docs/plans/` and that sibling
> directory holds docs only — no code. If this plan does belong to another project, move it
> there; otherwise delete it.

# Fast-first-start regressions — diagnosis-first fix plan

**Goal:** Find and fix, one defect at a time with a pinned failing test each, every behavior the fast-first-start slices broke — then restore real coverage and finish `tests/web/` freeze cleanup.

**Architecture:** Four independent phases with hard gates between them. Phase 1 reproduces the cold-start hang (characterize-only; no unit fix until a one-line repro exists). Phase 2 fixes two confirmed local defects (workspace git root, `contextRef` round trip). Phase 3 makes the composed loop honest end to end: budget per loop, verifier reads the loop's artifact, card records its node and transitions via defined events, lanes persist by draft id so a refresh restores instead of recreating. Phase 4 finishes the `tests/web/` freeze cleanup (the remaining tests/web/ suites — see ledger).

**Tech Stack:** Python 3.12 (`uv`), FastAPI, SQLite (sqlite-vec), pydantic-ai, Playwright (headless Chromium), React + vitest. Every action is a Taskfile task (`task --list`).

## Global constraints

- Gates on every commit: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q <touched test paths>` (focused); the full gate (`uv sync --frozen`, both ruff passes, mypy, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`) before the phase-4 wrap.
- Commits: stage only files the task names; NEVER `git add -A`. Commit messages follow `type(scope): summary` + a body naming the failing test and what broke.
- Freeze ledger: on any new red test in tests/web/, commit `tests/web/vitest.config.ts` plus the casefile's freeze ledger table in the same commit — never delete that file, never revert whole-file (`vitest.config.ts` is included by sibling worktrees — extend only).
- Characterize-first: every defect gets a committed test that fails for the defect before any product fix. If characterization cannot expose it, stop and report instead of fixing blind.
- New timing tests use `FakeClock` (the default test clock); nothing waits on the real clock.
- The failing test must fail for the predicted reason (wrong value / missing file / missing node), never collection or fixture errors.
- A gate failure inside a task's own step (mypy, etc.): fix forward; if the cause is a file outside the plan's scope, stop and report.

---

## Task 0: Baseline ledger (no commits)

**Files:** none.

- [ ] Baseline the two authoritative suites and paste the counts in your first report:

```bash
uv run pytest -q tests/unit/services/test_story_composer.py tests/unit/services/test_workflow_engine.py
```

Expected starting point at plan time: 48 + 16 passed, 0 failed. If it differs, the slice work moved — recompute every later "expected count" from this baseline before proceeding.

---

## Phase 1 — Reproduce the cold-start hang (characterize only)

### Task 1: Cold-pod E2E probe

**Why:** `c0-fast-first-start`'s trace shows 57 provider calls, then silence — neither `story published` nor an error. Unit suites cannot see that; only a cold pod can.

**Files:**
- Test: create `tests/e2e/test_cold_start.py`
- Possibly modify: whatever publishes in that flow, *only* if Step 2 proves the publisher never runs.

- [ ] **Step 1: Read the e2e conventions** in `tests/e2e/` (timeouts, how a story is started).
- [ ] **Step 2: Write the probe:** start a cold pod, drive the story to where `c0-fast-first-start` sat, assert the trace reaches `story published` within 120 s. Run: `uv run pytest -q tests/e2e/test_cold_start.py -m e2e` (match the real marker used in that directory). Record, pasted verbatim, where it stops.
- [ ] **Step 3: Branch on that evidence:**
  - Silence with no exception → the `chat_run_finished` handler never publishes; make the publishing node complete at that boundary (one commit).
  - Exception mid-provider-loop → that defect is the work; fix it TDD, re-run.
  - Publishes fine → pod-manager cold path or environment; capture a fresh HANDOFF trace, stop.

Re-run Step 2 after each change until green or the negative-result branch is written.
- [ ] **Step 4: Commit:** `test(e2e): cold-start probe pins the chat reaching story published` (plus the publish fix if Step 3 fired).

**Gate:** probe green, or a written negative result. No unit-level cold-start inference until this exists.

---

## Phase 2 — Local defects with confirmed diagnoses

### Task 2: Runtime chats get a real workspace git root

**Why (confirmed):** `utils/workspace.py::resolve_workspace_root` returns `projects_dir/<project>/.runs/<run>`; that dir is never `git init`-ed (only story pods are), so every runtime chat runs its workspace tools outside any repository — `git rev-parse` fails, grep/glob silently find nothing. `tests/unit/services/test_chat.py` skips the workspace-root assertion for this reason.

**Files:**
- Modify: `src/airlie/utils/workspace.py` (project workspaces resolve to the project's git root)
- Test: extend `tests/unit/utils/test_workspace.py`

- [ ] **Step 1: Write the failing test:**

```python
def test_project_workspace_resolves_to_the_git_root(tmp_path):
    (tmp_path / ".git").mkdir()
    run_dir = tmp_path / ".runs" / "run-1"
    run_dir.mkdir(parents=True)
    assert workspace_root_of(run=run("project-1", "local"), projects_dir=tmp_path) == tmp_path
```

Run `uv run pytest -q tests/unit/utils/test_workspace.py` — expect FAIL (path answer).

- [ ] **Step 2: Implement** the documented promise: for a project run, resolve `projects_dir/<project>` and return it when it is a git root; keep the path answer when it is not.

- [ ] **Step 3: Un-skip** the workspace-root assertion in `tests/unit/services/test_chat.py`; both runtime-chat tests must pass on a git-backed temp project.

- [ ] **Step 4: Focused gate + commit:** `fix(runtime): runtime chats run workspace tools inside the project repository`.

### Task 3: Echo and trim `contextRef` across the chat protocol

**Why (confirmed):** `ChatStart.context_ref` exists (`models/chat.py:764`) and chrome.js sends it on every WS
message, but the handler (`web/chat.py:364`) never builds a `ChatStart` with it, the runtime never stores it, and
nothing echoes it back. The `ChatDraft` round trip (`models/chat.py:712,745`; `web/chat.py:108-127`) is the exact
pattern to copy.

**Files:**
- Modify: `airlie/models/chat.py` (events + payload)
- Modify: `airlie/service/runtime/chat.py`, `airlie/service/chat.py` (carry it)
- Modify: `airlie/web/chat.py` (echo on start; parse + trim on prompt)
- Test: `tests/unit/models/test_chat.py`, `tests/unit/service/` (echo test in the existing runtime chat suite)

- [ ] **Step 1: Failing tests.**
  `uv run pytest -q tests/unit/models/test_chat.py -k context_ref` and an echo test added to the suite that already
  covers `ChatStarted`: construct a runtime with session id `"s1"`, ref
  `{"project":"p1","folder":"docs/plans/2026-11-19-fast-first-start.md","scope":"file"}`, call `start()` with
  `context_ref=...`, assert `event.payload["contextRef"]` equals it. Expect FAIL (no `CONTEXT_REF` event; the
  fixture's `context_ref=` kwarg raises — that failure counts as red for this task).
- [ ] **Step 2: Implement.** `ChatLifecycleEvent.CONTEXT_REF = "context_ref"` after `RESUMED`;
  `ChatRuntimeEvent.context_ref(context_ref: dict[str, Any])` following the `chat_draft` staticmethod;
  `ChatRuntime.__init__` stores `self._context_ref: dict[str, Any] | None = context_ref`; `_open_session` calls
  `self._publish(ChatRuntimeEvent.context_ref(self._context_ref))` right after the `chat_started` publish
  (`service/runtime/chat.py:352-357`). `service/chat.py Start` takes
  `context_ref: dict[str, Any] | None = None`, threads it into `ChatRuntime(...)`.

- [ ] **Step 3: Frontend.** `composerContextRefPayload()` in `webview.jsx` already builds
`{project, folder, scope}`; the WS message key is already `contextRef`. In `web/chat.py` prompt branch:

```python
raw_ref = payload.get("context_ref") or payload.get("contextRef") or {}
ref = {k: raw_ref.get(k) for k in ("project", "folder", "scope") if raw_ref.get(k)}
trimmed = f"{CHAT_CONTEXT_PREFIX} ({ref['folder']})" if ref.get("folder") else ""
full_prompt = f"{trimmed}\n\n{prompt}" if trimmed and prompt else (trimmed or prompt)
```

- [ ] **Step 4: Gate + commit:** `fix(chat): carry the plan context across restarts and trim it out of history`
— body names the user-visible symptom (a rehydrated chat addresses a path that trips the workspace guard).

