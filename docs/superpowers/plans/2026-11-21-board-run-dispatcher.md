# Board Run Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the timer `LineTicker` story beat with an event-driven run dispatcher in `task serve` that starts card workflows on Approved, updates board columns from run lifecycle, lets Airlie chat preempt board work, and serves a four-kind builder catalog — with QA-constrained tests (mocks, no real agents/clocks).

**Architecture:** BoardStore/API notifies a serve-local `RunDispatcher` on Approved. The dispatcher FIFO-queues cards (chat jumps the front and may pause Doing runs), respects `runs.max_concurrent` plus worktree locks, calls `start_workflow`, moves cards Doing → Review/Blocked, and publishes SSE hints on the existing `Feed`. Hub/bridge stays public ingress only.

**Tech Stack:** Python 3.12, FastAPI/`task serve`, `airlie.workflow.start_workflow`, Vue 3 board/chat/builder, Vitest, pytest. Tests follow `skills/qa/` (unit/functional/component as appropriate).

**Spec:** [`docs/superpowers/specs/2026-11-21-board-run-dispatcher-design.md`](../specs/2026-11-21-board-run-dispatcher-design.md)

## Global Constraints

- Base branch: `feature/legacy-story-removal` (has `start_workflow`); cherry-pick/merge the design+plan commits from `main` if needed before coding.
- Gate (after each task that touches code): `uv sync --frozen` → `uv run ruff check .` → `uv run ruff format --check .` → `uv run mypy` → `uv run pytest -q -n auto --maxprocesses 8` → `task web:gate` when `web/` or catalog/fixtures change.
- Tests: `skills/qa/` by layer; **no real agents/models/engines**; **no real clocks/sleeps** in default unit gate — inject clock/event pump; quarantine only with `clock:` / `slow:` if unavoidable.
- System-wide concurrency config is **not** under `board.*` (use `runs.max_concurrent`).
- Bridge/hub is **not** the default Approved→run path.
- Production may ship zero workflow YAML files; kind catalog is independent of that.

## File map

| Path | Responsibility |
| --- | --- |
| `src/airlie/config/schema.py` | `RunsSpec` + `AirlieConfig.runs` |
| `src/airlie/serve/dispatcher.py` | Queue, concurrency, pause/resume, start/finish → board moves |
| `src/airlie/serve/ticker.py` / `app.py` | Remove retired story tick; construct/start dispatcher |
| `src/airlie/board/store.py` / `api.py` | After successful →Approved (and related moves), notify dispatcher + feed |
| `src/airlie/serve/workflows_api.py` | Catalog returns four node kinds + field metadata |
| `web/src/api/workflow_defs.ts` + builder | Consume kind catalog |
| `web/src/screens/board/*` + chat StoryCard | Workflow picker / open builder |
| `web/src/screens/chat/Composer.vue` | Model + persona dropdowns (wire if missing) |
| `tests/unit/test_run_dispatcher.py` | Core dispatcher behaviour (fakes) |
| `tests/unit/test_workflows_catalog_kinds.py` | Catalog shape |
| Builder/board component tests | UI under Vitest + mocked API |

---

### Task 1: `runs.max_concurrent` config

**Files:**
- Modify: `src/airlie/config/schema.py` (add `RunsSpec`, field on `AirlieConfig`)
- Modify: `airlie.yaml` (document default under a short comment; optional explicit key)
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Produces: `RunsSpec(max_concurrent: int = 2)` (ge=1); `AirlieConfig.runs: RunsSpec`

- [ ] **Step 1: Write the failing test**

```python
def test_runs_max_concurrent_defaults_and_rejects_zero(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.runs.max_concurrent >= 1
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, {**MINIMAL, "runs": {"max_concurrent": 0}}))
```

- [ ] **Step 2: Run test — expect FAIL** (missing `runs`)

Run: `uv run pytest -q tests/unit/test_config.py::test_runs_max_concurrent_defaults_and_rejects_zero`

- [ ] **Step 3: Implement `RunsSpec`**

```python
class RunsSpec(Strict):
    max_concurrent: int = Field(default=2, ge=1, description="system-wide in-flight start_workflow runs")
```

Wire `runs: RunsSpec = Field(default_factory=RunsSpec)` on `AirlieConfig`.

- [ ] **Step 4: Run test — expect PASS**; ruff/mypy clean on touched files

- [ ] **Step 5: Commit**

```bash
git add src/airlie/config/schema.py tests/unit/test_config.py airlie.yaml
git commit -m "feat(config): add system-wide runs.max_concurrent"
```

---

### Task 2: Dispatcher — missing workflow → Blocked

**Files:**
- Create: `src/airlie/serve/dispatcher.py`
- Test: `tests/unit/test_run_dispatcher.py`

**Interfaces:**
- Consumes: board with `get`/`move`; `workflow_label(labels)`; fake clock
- Produces: `RunDispatcher.notify_status(key, status)` ; on Approved without workflow → `move(key, "blocked", ...)`

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_approved_without_workflow_moves_to_blocked(tmp_path: Path) -> None:
    board = make_board(tmp_path)  # fixture: BoardStore
    card = board.add(... labels=(), status="todo")
    moves: list[tuple[str, str]] = []
    disp = RunDispatcher(
        board=board,
        cfg=fake_cfg(max_concurrent=2),
        start=AsyncMock(),  # never called
        publish=lambda topic, key: None,
        clock=lambda: 0.0,
    )
    board.move(card.key, "approved")
    await disp.notify_status(card.key, "approved")
    assert board.get(card.key).status == "blocked"
    disp.start.assert_not_awaited()
```

- [ ] **Step 2: Run — expect FAIL** (no `RunDispatcher`)

- [ ] **Step 3: Minimal `RunDispatcher`**

Implement `notify_status`: if status == `approved` and `workflow_label(item.labels)` is None → `board.move(key, "blocked", by="dispatcher")` and record error on card (comment or agreed field — prefer `board.comment(key, "dispatcher", "missing workflow")` if no error field exists).

- [ ] **Step 4: PASS**

- [ ] **Step 5: Commit** `feat(serve): block Approved cards that lack a workflow`

---

### Task 3: Dispatcher — enqueue, start → Doing, success → Review / fail → Blocked

**Files:**
- Modify: `src/airlie/serve/dispatcher.py`
- Test: `tests/unit/test_run_dispatcher.py`

**Interfaces:**
- Consumes: `start_workflow`-shaped async callable returning a result with success/failure (fake)
- Produces: card Doing while in-flight; Review on success; Blocked on hard fail

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_approved_with_workflow_starts_and_moves_doing_then_review() -> None:
    start = AsyncMock(side_effect=fake_success)  # sets event when entered
    # after notify + drain: status review; start awaited once with workflow name

@pytest.mark.asyncio
async def test_hard_fail_moves_to_blocked() -> None:
    start = AsyncMock(side_effect=fake_fail)
    # status blocked; error comment present
```

Use an asyncio Event / injected “pump” — **no** `asyncio.sleep` of wall time.

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement start lifecycle**

On Approved + workflow: enqueue. When a slot is free, `move` → Doing, `await start(...)`, then Review or Blocked. Idempotent: one in-flight/queued entry per card key.

- [ ] **Step 4: PASS**

- [ ] **Step 5: Commit** `feat(serve): dispatcher starts workflows and updates columns`

---

### Task 4: System-wide cap + FIFO + worktree busy

**Files:**
- Modify: `src/airlie/serve/dispatcher.py`
- Test: `tests/unit/test_run_dispatcher.py`

**Interfaces:**
- Consumes: `cfg.runs.max_concurrent`; lock probe (inject `can_lock(workdir) -> bool` or fake `acquire` that returns False)

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_second_card_stays_approved_until_slot_frees() -> None:
    # max_concurrent=1; first start blocks on Event; second stays approved; set Event; second becomes doing

@pytest.mark.asyncio
async def test_fifo_order_when_slot_opens() -> None:
    # A then B approved while one running; on free, A starts before B
```

- [ ] **Step 2–4: TDD implement drain loop** that respects cap and skips/requeues when workdir lock busy (stay Approved)

- [ ] **Step 5: Commit** `feat(serve): dispatcher concurrency cap and FIFO queue`

---

### Task 5: Chat preemption (front of line + pause Doing)

**Files:**
- Modify: `src/airlie/serve/dispatcher.py`
- Test: `tests/unit/test_run_dispatcher.py`
- Modify: `src/airlie/chat/api.py` (or serve chat entry) to call `dispatcher.request_chat_priority(session_id)` / `release_chat_priority`

**Interfaces:**
- Produces: `async request_chat_slot(session_id) -> ChatLease`; pausing a Doing board run (callback `pause_run` / `resume_run` injected fakes in tests)

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_chat_pauses_doing_board_run_then_resumes() -> None:
    paused = AsyncMock()
    resumed = AsyncMock()
    # board run in Doing holding the only slot; request_chat_slot; paused awaited; release; resumed awaited
```

- [ ] **Step 2–4: Implement** prefer pausing board Doing over other chat; never orphan — keep status Doing with comment/marker `paused for chat` if useful

- [ ] **Step 5: Commit** `feat(serve): chat preempts board runs in the dispatcher`

---

### Task 6: Wire board moves + retire story ticker

**Files:**
- Modify: `src/airlie/board/api.py` (after successful move/change to approved — and dispatcher-driven moves still publish feed)
- Modify: `src/airlie/serve/app.py` (construct `RunDispatcher`, pass into board routes / chat; stop calling `factory.tick`)
- Modify: `src/airlie/serve/ticker.py` — remove retired beat **or** make default beat a no-op that does not import `story.jobs.tick`
- Test: `tests/unit/test_serve_ticker.py` / dispatcher wiring test with fake board API

- [ ] **Step 1: Failing test** — serve/ticker no longer raises retired story tick; Approved via API notifies dispatcher (mock)

- [ ] **Step 2–4: Wire**; `feed.publish("board", key)` on auto moves (dispatcher already has `publish`)

- [ ] **Step 5: Commit** `feat(serve): wire dispatcher; retire story line tick`

---

### Task 7: Kind catalog API + web types

**Files:**
- Modify: `src/airlie/serve/workflows_api.py` (`/catalog`)
- Modify: `src/airlie/workflow/retired.py` usage — stop relying on empty `STORY_CATALOG` for kinds
- Modify: `web/src/api/workflow_defs.ts`, builder consumers as needed
- Test: `tests/unit/test_workflows_catalog_kinds.py`; update Vitest catalog fixtures

**Interfaces:**
- Produces catalog JSON shaped like:

```python
{
  "roles": [...],
  "node_kinds": [
    {"kind": "WORK", "fields": ["prompt|prompt_id|run", "inputs", "outputs", ...]},
    {"kind": "ROUND_ROBIN", "fields": ["roles", "prompt", "agreement_when", ...]},
    {"kind": "SUPERVISOR_WORKERS", "fields": [...]},
    {"kind": "K_INDEPENDENT_TEAMS", "fields": [...]},
  ],
  # keep backward-compatible empty checks/meetings during transition OR remove with coordinated web update in same commit
}
```

Prefer one commit that updates server + web together so `task web:gate` stays green.

- [ ] **Step 1–4: TDD** assert four kinds present; builder still paints palette from kinds

- [ ] **Step 5: Commit** `feat(workflows): catalog exposes four node kinds`

---

### Task 8: Card workflow picker + builder link

**Files:**
- Modify: `web/src/screens/board/CardModal.vue` (and/or column card chrome)
- Modify: `web/src/screens/chat/thread/StoryCard.vue` (replace blank slot with real options from `listWorkflowDefs` + “Open builder”)
- Modify: board label write path to set/clear `workflow:<name>`
- Test: Vitest component tests with mocked `/v1/workflows` (skills/qa component: role queries, user-visible assertions)

- [ ] **Step 1: Failing component test** — select workflow updates label; “Open builder” navigates/routes to builder

- [ ] **Step 2–4: Implement**

- [ ] **Step 5: Commit** `feat(web): card workflow picker and builder entry`

---

### Task 9: Chat model + persona dropdowns

**Files:**
- Modify: `web/src/screens/chat/Composer.vue` (and session settings API if needed)
- Test: Composer Vitest tests with mocked session/model/role lists

- [ ] **Step 1: Failing test** — user can choose model and persona; selection shown and sent on next turn

- [ ] **Step 2–4: Implement** (reuse existing model chip/session APIs where present; add persona if missing)

- [ ] **Step 5: Commit** `feat(web): chat model and persona dropdowns`

---

### Task 10: Docs + AGENTS alignment

**Files:**
- Modify: `AGENTS.md` (dispatcher / no timer ticker; `runs.max_concurrent`; catalog = kinds)
- Modify: `README.md` only if user-facing run/board behaviour is documented there
- No new speculative hub default path

- [ ] **Step 1: Edit docs** to match shipped behaviour
- [ ] **Step 2: Full gate**
- [ ] **Step 3: Commit** `docs: board dispatcher and kind catalog as built`

---

## Spec coverage check

| Spec requirement | Task |
| --- | --- |
| Approved + workflow → queue/start | 3–4 |
| Approved − workflow → Blocked | 2 |
| Doing / Review / Blocked transitions | 3 |
| System-wide `runs.max_concurrent` | 1, 4 |
| Worktree busy waits in Approved | 4 |
| FIFO + chat preempt/pause | 4, 5 |
| SSE via existing feed | 3, 6 |
| Local `start_workflow` default; hub not default | 3, 6 |
| Retire timer story tick | 6 |
| Catalog = four kinds | 7 |
| Card picker + builder | 8 |
| Chat model + persona | 9 |
| QA: mocks, no agents/clocks | all test steps |

## Placeholder / consistency notes

- Fake `start` callable signature in tests should match how Task 6 wires real `start_workflow` (name, workdir, card dict).
- Board error surfacing: prefer `board.comment` unless an existing card error field is found — pick one in Task 2 and keep it through later tasks.
- Do not reintroduce `config/workflows/story.yaml` as production; fixtures stay under `tests/fixtures/workflows/` on the legacy-story-removal branch.
