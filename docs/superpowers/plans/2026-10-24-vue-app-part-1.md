# Vue App, Part 1: Mockups, Toolchain, Live Feed and the Workflows API — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything the new Vue app needs before its screens: approved mockups of every screen, the `web/`
toolchain served at `/next`, the live event feed (server and page side), and the API the reshaped Workflows
area reads.

**Architecture:** A Vite + Vue 3 + TypeScript app in `web/` builds into `src/airlie/serve/web_next/`, which
FastAPI serves under `/next`. `GET /v1/events` streams change hints from an in-process `Feed`, fed by the board
router and the jobs manager directly and by a `Watcher` that notices writes from other processes. The page
reads the stream with `fetch` and Pinia stores refetch what changed.

**Tech Stack:** Python 3.12, FastAPI, SQLite, pytest; Node 26, npm, Vite, Vue 3, TypeScript, Pinia, Vue
Router, Tailwind v4, Vitest, Vue Test Utils, ESLint; Playwright (existing e2e suite).

**Spec:** `docs/superpowers/specs/2026-10-24-vue-app-design.md`

**Pull requests**, following the design's order of work: Tasks 2–6 (foundation), Tasks 7–9 with 13–14 (the
live feed), Tasks 10–12 (the Workflows API). Task 1 is approved in conversation, not in a pull request.

**Part 2** (written after Noah approves the Task 1 mockups): the shared components, the app shell, every
screen including the reshaped Workflows area, and the switch. Nothing in this part draws a screen.

## Global Constraints

- Node is pinned only in `web/package.json` `engines` (`"node": "26.x"`) and checked by Taskfile tasks. Nothing
  in `pyproject.toml`, `uv.lock` or uv installs or pins node.
- Every action is a Taskfile task; no build step anyone runs by hand.
- Nothing loads from the internet at runtime: no CDN, no web fonts (system font stack).
- Today's app at `/` and `/app` keeps working unchanged through this whole part.
- No Docker locally: the e2e image builds under Apple `container` (Docker only on GitHub's e2e runner).
- Every `/v1` route keeps the existing sign-in (`_require_principal` in `src/airlie/serve/app.py`).
- Events are hints `{"topic": str, "key": str | null}`; the page refetches through the GET endpoints.
- Feed topics: `board`, `users`, `stories`, `runs`, `jobs`.
- Heartbeat every 15 s; headers `Cache-Control: no-cache` and `X-Accel-Buffering: no`; no replay.
- The test gate in `AGENTS.md` must stay runnable inside airlie's story pipeline (allow list in
  `src/airlie/tools/confine.py`).
- Only a human merges; no merge code path.

## Review Focus

- **A web app that was never built** (fresh clone, `uv sync` only): `/next` answers 503 with the command to
  build it, and `/` still serves today's app. Pinned in Task 4.
- **A burst of writes from another process** inside one watcher interval yields one topic-wide event, never
  zero. Pinned in Task 8 (two commits between polls).
- **A browser that drops and reconnects** gets `: connected` again and every live store refetches, so nothing
  stays stale. Pinned in Task 13.
- **A run that crashed without `run_done`** must leave Running once it has been silent for an hour, not sit
  there forever. Pinned in Task 11.
- **A story's own studio runs** must not show in Running as separate rows next to the story. Pinned in Task 11.

---

### Task 1: Mockups of every screen

A design task: no code, no tests. Its output is Noah's approval, which Part 2 is written from.

**Files:**
- Create: `docs/superpowers/mockups/2026-10-24-vue-app/<screen>.html`, one per screen below
- Reference: `docs/superpowers/mockups/2026-10-24-vue-app/board-look-dark.html`, `board-look-light.html`

- [ ] **Step 1: Draw each screen in the approved look** (shadcn-vue shapes, Aura palette from the look
  reference, pill tabs, system fonts), desktop and phone, dark; one light version per screen family.
  Screens: Chat; Workflows (Running card on top with a progress bar per row, then Stories with paging,
  filters for stage, repository and blocked, search, sort by last activity / key / stage, 25 a page, and the
  Start new work button); one workflow's view for a story; the same view for a studio run with the plan to
  approve and Stop, and for a teams, a fork and a single run; Start new work; Board with the centred item
  modal; Metrics; Admin; Join; Sign in. The top bar carries the "Live" / "Reconnecting" indicator.
- [ ] **Step 2: Show them in the visual companion** (`superpowers:brainstorming` visual-companion.md), one
  screen family per page, and revise until Noah approves each.
- [ ] **Step 3: Commit the approved files**

```bash
git add docs/superpowers/mockups/2026-10-24-vue-app/
git commit -m "Mockups: every screen of the Vue app, approved"
```

---

### Task 2: npm's cache on the agent allow list

**Files:**
- Modify: `src/airlie/tools/confine.py` (`_uv_dirs`, `writable_roots`, module docstring's Writable list)
- Test: `tests/unit/test_confine.py`

**Interfaces:**
- Produces: `_npm_dirs() -> list[Path]` and its entries in `writable_roots(...)`.

- [ ] **Step 1: Write the failing test**

```python
def test_npm_cache_is_writable_so_the_gate_can_install(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.delenv("npm_config_cache", raising=False)
    monkeypatch.delenv("NPM_CONFIG_CACHE", raising=False)
    roots = writable_roots(repo)
    assert Path(os.path.realpath(Path.home() / ".npm")) in roots
    monkeypatch.setenv("npm_config_cache", str(repo / "npmcache"))
    assert Path(os.path.realpath(repo / "npmcache")) in writable_roots(repo)
```

- [ ] **Step 2: Run it to see it fail**

Run: `uv run pytest tests/unit/test_confine.py::test_npm_cache_is_writable_so_the_gate_can_install -v`
Expected: FAIL (`~/.npm` not in roots)

- [ ] **Step 3: Implement `_npm_dirs() -> list[Path]`**: `npm_config_cache`, else `NPM_CONFIG_CACHE`, else
  `~/.npm`; add `*_npm_dirs()` to `writable_roots` after `*_uv_dirs()`; add "npm's cache" to the docstring's
  Writable list.
- [ ] **Step 4: Run `uv run pytest tests/unit/test_confine.py -v`** — Expected: all PASS
- [ ] **Step 5: Commit** — `git commit -am "confine: npm's cache is writable, so a story's gate can install"`

---

### Task 3: The `web/` app and its Taskfile tasks

**Files:**
- Create: `web/package.json`, `web/package-lock.json`, `web/vite.config.ts`, `web/tsconfig.json`,
  `web/eslint.config.js`, `web/index.html`, `web/src/main.ts`, `web/src/App.vue`, `web/src/style.css`,
  `web/scripts/check-node.mjs`, `web/src/App.test.ts`
- Modify: `Taskfile.yml` (new `web:*` tasks; `setup` and `serve` call `web:build`), `.gitignore`
  (`src/airlie/serve/web_next/`, `web/node_modules/`, `.task/`), `README.md` (node 26 in Install)

**Interfaces:**
- Produces: `npm run typecheck | lint | test | build` in `web/`; tasks `web:install`, `web:check`,
  `web:build`, `web:dev`, `web:gate`; build output `src/airlie/serve/web_next/index.html` +
  `src/airlie/serve/web_next/assets/*`, built with Vite `base: "/next/"`.

- [ ] **Step 1: Write the failing test** `web/src/App.test.ts`

```ts
import { mount } from "@vue/test-utils";
import App from "./App.vue";
test("the shell mounts and names the app", () => {
  expect(mount(App).text()).toContain("airlie");
});
```

- [ ] **Step 2: Scaffold `web/`** with `package.json` (`"private": true`, `"type": "module"`,
  `"engines": {"node": "26.x"}`, scripts `typecheck: vue-tsc --noEmit`, `lint: eslint .`, `test: vitest run`,
  `build: vite build`, `dev: vite`), dependencies `vue`, `vue-router`, `pinia`, dev dependencies `vite`,
  `@vitejs/plugin-vue`, `typescript`, `vue-tsc`, `vitest`, `@vue/test-utils`, `jsdom`, `tailwindcss`,
  `@tailwindcss/vite`, `eslint`, `eslint-plugin-vue`, `typescript-eslint`; each at an exact version at least
  two weeks old; `npm install` writes the lockfile. `vite.config.ts`: `base: "/next/"`,
  `build.outDir: "../src/airlie/serve/web_next"`, `build.emptyOutDir: true`, test environment `jsdom`,
  dev server proxy `/v1` → `http://127.0.0.1:8090`. `App.vue` renders "airlie" and nothing else yet.
- [ ] **Step 3: Write `web/scripts/check-node.mjs`**: reads `engines.node` from `web/package.json`, takes its
  first number as the major, compares `process.versions.node`'s major, and on a mismatch prints
  `node <found> is on the path; airlie's web app needs node <major>` and exits 1.
- [ ] **Step 4: Add the tasks** to `Taskfile.yml`, each with `dir: web` where it runs npm:
  - `web:install` — `npm ci`; precondition `node web/scripts/check-node.mjs` (message: "install node 26").
  - `web:check` — deps `web:install`; `npm run typecheck`, `npm run lint`, `npm test`.
  - `web:build` — deps `web:install`; `npm run build`; `sources: [web/**/*]` excluding `web/node_modules/**`,
    `generates: [src/airlie/serve/web_next/index.html]`.
  - `web:gate` — `web:check` then `web:build`; `sources: [web/**/*]` excluding `web/node_modules/**`, so
    `task` skips it when nothing under `web/` changed since it last passed in this checkout.
  - `web:dev` — `npm run dev`.
  - `setup` gains `task: web:build` after `uv sync`; `serve` gains `deps: [web:build]`.
- [ ] **Step 5: Run `task web:gate`** — Expected: typecheck, lint, 1 test and the build pass;
  `src/airlie/serve/web_next/index.html` exists.
- [ ] **Step 6: Run `task web:gate` again** — Expected: `task: Task "web:gate" is up to date`.
- [ ] **Step 7: Commit**

```bash
git add web/ Taskfile.yml .gitignore README.md
git commit -m "web: the Vue app's toolchain, behind task web:*"
```

---

### Task 4: Serve the built app at `/next`

**Files:**
- Modify: `src/airlie/serve/app.py` (`create_app` gains `next_dir`; routes for `/next`)
- Test: `tests/unit/test_serve_next.py`

**Interfaces:**
- Produces: `NEXT_DIR: Path = Path(__file__).parent / "web_next"`;
  `create_app(..., next_dir: Path | None = None)` (None means `NEXT_DIR`).

- [ ] **Step 1: Write the failing tests** (build a fake `next_dir` in `tmp_path` with `index.html` containing
  `<div id="app">` and `assets/app-1a2b.js`; use `tests.unit.serve_helpers.TestClient` and `cfg` /
  `FakeUpstream` from `tests.unit.test_serve` as `test_serve_web.py` does)

```python
def test_next_serves_the_page_for_any_app_path(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=True)
    for path in ("/next/", "/next/board", "/next/workflows/AIR-17"):
        r = c.get(path)
        assert r.status_code == 200 and '<div id="app">' in r.text
        assert r.headers["cache-control"] == "no-cache"
    assert c.get("/next", follow_redirects=False).headers["location"] == "/next/"

def test_next_serves_built_files_with_a_long_cache(tmp_path: Path) -> None:
    r = client_with(tmp_path, built=True).get("/next/assets/app-1a2b.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable"

def test_next_never_serves_outside_its_folder(tmp_path: Path) -> None:
    r = client_with(tmp_path, built=True).get("/next/..%2f..%2fpyproject.toml")
    assert "pyproject" not in r.text

def test_an_unbuilt_app_says_how_to_build_it_and_today_app_still_works(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=False)
    r = c.get("/next/board")
    assert r.status_code == 503
    assert r.json() == {"error": "the new web app is not built: run task web:build"}
    assert c.get("/").status_code == 200
```

- [ ] **Step 2: Run `uv run pytest tests/unit/test_serve_next.py -v`** — Expected: FAIL (404s)
- [ ] **Step 3: Implement** a `GET /next` redirect to `/next/` and `GET /next/{path:path}`: a path naming a
  regular file inside the resolved `next_dir` is served (`assets/` with the long cache header); anything
  else gets `index.html` with `no-cache`; no `index.html` → the 503 above. Both routes stay outside `/v1`, so
  the page itself needs no sign-in, like `/` today.
- [ ] **Step 4: Run `uv run pytest tests/unit/test_serve_next.py tests/unit/test_serve_web.py -v`** — PASS
- [ ] **Step 5: Commit** — `git commit -am "serve: the Vue app at /next"` (with the new test file added)

---

### Task 5: `task doctor` checks node

**Files:**
- Modify: `src/airlie/doctor.py` (`run_checks`)
- Test: `tests/unit/test_doctor.py`

**Interfaces:**
- Consumes: `SystemProbe.tool_version("node")` (returns e.g. `"v26.10.0"`); `web/package.json`.
- Produces: `node_check(version: str | None, pin: str) -> CheckResult` named `tool:node`;
  `web_node_pin(root: Path) -> str` (the `engines.node` string).

- [ ] **Step 1: Write the failing tests**

```python
def test_node_matching_the_pin_is_ok() -> None:
    assert node_check("v26.10.0", "26.x") == CheckResult("tool:node", "ok", "v26.10.0")

def test_node_of_another_major_fails_and_names_both() -> None:
    r = node_check("v24.16.0", "26.x")
    assert r.status == "fail" and "v24.16.0" in r.detail and "26" in r.detail

def test_no_node_fails_and_says_to_install_it() -> None:
    r = node_check(None, "26.x")
    assert r.status == "fail" and "install node 26" in r.detail
```

- [ ] **Step 2: Run them** — Expected: FAIL (`node_check` not defined)
- [ ] **Step 3: Implement** `node_check` and `web_node_pin`; `run_checks` appends
  `node_check(probe.tool_version("node"), web_node_pin(<repo root>))`, the repo root being
  `Path(__file__).resolve().parents[2]`. Extend `FakeProbe` in the test file with `node` → `"v26.10.0"` so
  `test_all_green_when_everything_present` stays green.
- [ ] **Step 4: Run `uv run pytest tests/unit/test_doctor.py -v`** — PASS
- [ ] **Step 5: Commit** — `git commit -am "doctor: node must match web/package.json's pin"`

---

### Task 6: The gate, CI and the e2e image build the web app

**Files:**
- Modify: `AGENTS.md` (Test gate), `.github/actions/setup/action.yml`, `.github/workflows/pipeline.yml`
  (static job), `ops/e2e/Containerfile`

**Interfaces:**
- Consumes: `task web:gate` (Task 3).

- [ ] **Step 1: Add `task web:gate` as the last line** of the fenced block under `## Test gate` in `AGENTS.md`.
- [ ] **Step 2: CI setup action**: before "Install locked dependencies", `actions/setup-node@v4` with
  `node-version-file: web/package.json`, `cache: npm`, `cache-dependency-path: web/package-lock.json`.
- [ ] **Step 3: Static job**: after `task static`, `- run: task web:gate`.
- [ ] **Step 4: e2e image**: a first stage `FROM node:26-slim AS web`, `WORKDIR /src`, `COPY web web`,
  `RUN cd web && npm ci && npm run build`; in the final stage, after `COPY . .`,
  `COPY --from=web /src/src/airlie/serve/web_next src/airlie/serve/web_next`.
- [ ] **Step 5: Run the full gate** from `AGENTS.md`, in order — Expected: every command passes.
- [ ] **Step 6: Run `task test:e2e`** (never at the same time as the host gate) — Expected: the existing
  suite passes and the build log shows the `web` stage.
- [ ] **Step 7: Commit** — `git commit -am "gate, CI and the e2e image build the web app"`

---

### Task 7: The feed and `GET /v1/events`

**Files:**
- Create: `src/airlie/serve/feed.py`
- Modify: `src/airlie/serve/app.py` (one `Feed` per app; the route)
- Test: `tests/unit/test_serve_feed.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Change: id: int; topic: str; key: str | None`
  - `class Feed`: `publish(topic: str, key: str | None = None) -> None` (safe from any thread: board routes
    run in FastAPI's thread pool); `subscribe() -> Subscription` (async context manager whose `__aiter__`
    yields `Change`); `subscribers: int`; `close() -> None` (ends every subscription).
  - `HEARTBEAT_S: float = 15.0`
  - `async def event_stream(feed: Feed, *, heartbeat_s: float = HEARTBEAT_S) -> AsyncIterator[bytes]`
  - `create_app` exposes the feed as `app.state.feed`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_subscriber_gets_what_is_published_in_order() -> None:
    feed = Feed()
    async with feed.subscribe() as sub:
        feed.publish("board", "AIR-17"); feed.publish("runs")
        it = aiter(sub)
        assert (await anext(it)).key == "AIR-17"
        second = await anext(it)
        assert (second.topic, second.key) == ("runs", None) and second.id > 1

async def test_the_stream_opens_with_connected_then_sends_changes_and_pings() -> None:
    feed = Feed()
    stream = event_stream(feed, heartbeat_s=0.05)
    assert await anext(stream) == b": connected\n\n"
    feed.publish("board", "AIR-1")
    assert await anext(stream) == b'id: 1\nevent: change\ndata: {"topic": "board", "key": "AIR-1"}\n\n'
    assert await anext(stream) == b": ping\n\n"

def test_events_needs_sign_in_and_streams_with_proxy_safe_headers(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    remote = TestClient(app, client=("10.0.0.5", 1))
    assert remote.get("/v1/events").status_code == 401
    app.state.feed.close()  # the stream ends after ": connected"
    r = TestClient(app).get("/v1/events", headers={"Authorization": "Bearer k"})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    assert r.text.startswith(": connected")
```

- [ ] **Step 2: Run `uv run pytest tests/unit/test_serve_feed.py -v`** — Expected: FAIL (module missing)
- [ ] **Step 3: Implement `feed.py`**: one `asyncio.Queue` per subscriber, filled through
  `loop.call_soon_threadsafe`; `event_stream` subscribes before it yields `: connected`, so nothing published
  after that line is missed; a closed feed yields `: connected` and then ends. Route `GET /v1/events` returns
  `StreamingResponse(event_stream(feed), media_type="text/event-stream", headers=...)`.
- [ ] **Step 4: Run the tests** — PASS
- [ ] **Step 5: Commit** — `git commit -m "serve: GET /v1/events, the live feed"` (files added)

---

### Task 8: The watcher for writes from other processes

**Files:**
- Create: `src/airlie/serve/watch.py`
- Modify: `src/airlie/serve/app.py` (the `/v1/events` route starts the watcher)
- Test: `tests/unit/test_serve_watch.py`

**Interfaces:**
- Consumes: `Feed` (Task 7).
- Produces: `class Watcher(state_dir: Path, feed: Feed, *, recent_runs: int = 100)` with
  `poll() -> list[tuple[str, str | None]]` (publishes and returns what changed) and
  `ensure_running(interval_s: float = 1.0) -> None` (when no loop is running: one `poll()` at once, which
  records the starting point, then one background loop that polls while `feed.subscribers > 0` and exits
  when it reaches 0). The route calls it before it sends `: connected`, so any write after a page is
  connected is seen.

What it compares between polls: `PRAGMA data_version` of `board.sqlite` and `auth.sqlite`, each on a
connection the watcher keeps open (any other connection's commit changes it, this server's own included);
the size of `stories/<key>/events.jsonl` and the mtime of `stories/<key>/state.json`; the size of
`runs/<id>/events.jsonl` for the newest `recent_runs` run directories. The first poll only records.

- [ ] **Step 1: Write the failing tests** (`BoardStore(state/"board.sqlite").create(...)` stands in for the
  story tick; a fresh `sqlite3` connection on `auth.sqlite` stands in for `task auth:invite`)

```python
def test_the_first_poll_publishes_nothing(state: Path) -> None:
    assert Watcher(state, Feed()).poll() == []

def test_two_board_commits_from_another_connection_give_one_board_event(state: Path) -> None:
    w = Watcher(state, Feed()); w.poll()
    other = BoardStore(state / "board.sqlite")
    other.create(level="initiative", title="one"); other.create(level="initiative", title="two")
    assert w.poll() == [("board", None)]
    assert w.poll() == []

def test_auth_writes_publish_users(state: Path) -> None: ...      # insert via sqlite3 → [("users", None)]

def test_a_growing_run_log_publishes_its_run(state: Path) -> None: ...   # append a line → [("runs", "r1")]

def test_a_story_state_change_publishes_its_story(state: Path) -> None: ...  # rewrite state.json → [("stories", "AIR-7")]
```

- [ ] **Step 2: Run `uv run pytest tests/unit/test_serve_watch.py -v`** — Expected: FAIL
- [ ] **Step 3: Implement `watch.py`**; the `/v1/events` route calls `watcher.ensure_running()` before it
  streams. A database that does not exist yet is skipped until it does.
- [ ] **Step 4: Run the tests** — PASS
- [ ] **Step 5: Commit** — `git commit -m "serve: notice other processes' writes for the live feed"`

---

### Task 9: In-process changes publish at once

**Files:**
- Modify: `src/airlie/board/api.py` (`board_router`), `src/airlie/serve/jobs.py` (`Jobs`),
  `src/airlie/serve/app.py` (wiring)
- Test: `tests/unit/test_serve_feed.py`

**Interfaces:**
- Consumes: `Feed.publish` (Task 7).
- Produces: `board_router(store: BoardStore, publish: Callable[[str, str | None], None] | None = None)`;
  `Jobs(publish: Callable[[str, str | None], None] | None = None)`.

Auth writes are left to the watcher (Task 8), which sees this server's own commits too.

- [ ] **Step 1: Write the failing tests**

```python
def test_each_board_write_publishes_its_key(tmp_path: Path) -> None:
    seen: list[tuple[str, str | None]] = []
    app = FastAPI(); app.include_router(board_router(BoardStore(tmp_path / "b.sqlite"), lambda t, k: seen.append((t, k))))
    c = TestClient(app)
    key = c.post("/v1/board/items", json={"level": "initiative", "title": "x"}).json()["key"]
    c.patch(f"/v1/board/items/{key}", json={"title": "y"})
    c.post(f"/v1/board/items/{key}/move", json={"status": "doing"})
    c.post(f"/v1/board/items/{key}/comments", json={"text": "hi"})
    assert seen == [("board", key)] * 4

async def test_a_job_publishes_when_it_starts_and_when_it_ends(tmp_path: Path) -> None:
    seen: list[tuple[str, str | None]] = []
    jobs = Jobs(publish=lambda t, k: seen.append((t, k)))
    async def work() -> dict[str, Any]: return {}
    job = jobs.start("run", "r1", tmp_path / "e.jsonl", work())
    await job.task; await asyncio.sleep(0)
    assert seen == [("jobs", "r1"), ("jobs", "r1")]
```

- [ ] **Step 2: Run them** — Expected: FAIL (unexpected keyword `publish`)
- [ ] **Step 3: Implement**: each board write route (create, update, move, comment, edit comment) publishes
  after the store call succeeds; `Jobs.start` publishes and adds a done callback that publishes; `create_app`
  passes `app.state.feed.publish` to both.
- [ ] **Step 4: Run `uv run pytest tests/unit -q -k "board or job or feed"`** — PASS
- [ ] **Step 5: Commit** — `git commit -am "board and jobs publish their changes to the feed"`

---

### Task 10: Stories list: paging, filters, search, sort, last activity

**Files:**
- Modify: `src/airlie/serve/listing.py` (`stories`; new `story_page`), `src/airlie/serve/app.py`
  (`GET /v1/airlie/stories` parameters)
- Test: `tests/unit/test_serve_listing.py`

**Interfaces:**
- Produces:
  - each row of `stories(state_dir)` gains `"last_activity": float` — the newest mtime of the story's
    `events.jsonl`, its `state.json`, and the `events.jsonl` of the last run in its `runs`.
  - `story_page(rows: list[dict[str, Any]], *, page: int = 1, size: int | None = None, stage: str | None = None,
    repo: str | None = None, blocked: bool | None = None, q: str | None = None,
    sort: Literal["activity", "key", "stage"] = "activity") -> dict[str, Any]` returning
    `{"stories": [...], "total": int, "page": int, "size": int | None}`.
  - `GET /v1/airlie/stories?page=&size=&stage=&repo=&blocked=&q=&sort=`; `size` 1–100; no `size` returns
    every story, so today's app keeps working; `stage` outside `STAGES` is a 422.

Sort rules: `activity` newest first; `key` natural (`AIR-9` before `AIR-17`); `stage` by `STAGES` order,
then newest first. `q` matches key or title, case-insensitively. `total` counts after filtering.

- [ ] **Step 1: Write the failing tests** with rows for AIR-9 (refine, repo a, activity 30), AIR-17 (build,
  repo b, blocked "x", activity 20), AIR-2 (review, repo a, activity 10):

```python
def test_story_page_sorts_filters_and_pages() -> None:
    assert keys(story_page(ROWS)) == ["AIR-9", "AIR-17", "AIR-2"]
    assert keys(story_page(ROWS, sort="key")) == ["AIR-2", "AIR-9", "AIR-17"]
    assert keys(story_page(ROWS, repo="a")) == ["AIR-9", "AIR-2"]
    assert keys(story_page(ROWS, blocked=True)) == ["AIR-17"]
    assert keys(story_page(ROWS, q="air-1")) == ["AIR-17"]
    p = story_page(ROWS, page=2, size=2)
    assert keys(p) == ["AIR-2"] and p["total"] == 3

def test_last_activity_is_the_newest_of_the_story_and_its_last_run(tmp_path: Path) -> None:
    ...  # os.utime the story's state.json to 100, its events.jsonl to 200, its last run's log to 300 → 300.0

def test_the_stories_route_takes_the_options_and_rejects_an_unknown_stage(tmp_path: Path) -> None:
    ...  # ?size=1&sort=key → one story and "total": 3; ?stage=nope → 422; no params → every story
```

- [ ] **Step 2: Run them** — FAIL
- [ ] **Step 3: Implement** `story_page`, the `last_activity` field and the route parameters.
- [ ] **Step 4: Run `uv run pytest tests/unit/test_serve_listing.py -v`** — PASS
- [ ] **Step 5: Commit** — `git commit -am "stories API: paging, filters, search, sort and last activity"`

---

### Task 11: The running list with progress

**Files:**
- Create: `src/airlie/serve/running.py`
- Modify: `src/airlie/serve/app.py` (`GET /v1/airlie/running`)
- Test: `tests/unit/test_serve_running.py`

**Interfaces:**
- Consumes: `stories(state_dir)` with `last_activity` (Task 10); `build_tree` from `airlie.serve.tree`;
  `read_events` from `airlie.serve.jobs`; story state files (`stage`, `layers`, `built`, `blocked`, `runs`).
- Produces:
  - `ACTIVE_S: float = 3600.0`
  - `story_progress(stage: str, layers: int, built: int) -> float`
  - `run_progress(tree: dict[str, Any]) -> float | None`
  - `running(state_dir: Path, *, live: frozenset[str] = frozenset(), now: float, recent_runs: int = 100)
    -> list[dict[str, Any]]`, each row `{"kind": "story" | "run", "key", "title", "topology", "progress",
    "last_activity"}`, newest activity first. `live` holds this server's running job ids
    (`story-<KEY>` for stories, the run id for plans and runs).
  - `GET /v1/airlie/running` → `{"running": running(...)}` with `live` from `jobs.list()`.

Progress rules. A story: `pre = {"new": 0, "intake": 1, "refine": 2}.get(stage, 3)`,
`total = 3 + max(layers, 1) + 1`, `done = pre + built + (1 if stage in ("publish", "review") else 0)`,
progress `round(done / total, 3)`. A run: the share of the tree's nodes in state `done` or `failed`; `None`
(an indeterminate bar) when the tree has one node or none.

Who is running. A story whose stage is not `review`, that is not blocked, and whose last activity is within
`ACTIVE_S` of `now` or whose job id is in `live`. A run among the newest `recent_runs` with a `run_start` and
no `run_done`, not named in any story's `runs`, whose log changed within `ACTIVE_S` or whose id is in `live`.

- [ ] **Step 1: Write the failing tests**

```python
def test_story_progress() -> None:
    assert story_progress("refine", 0, 0) == 0.4
    assert story_progress("layers", 4, 1) == 0.5
    assert story_progress("publish", 2, 2) == 1.0

def test_run_progress_counts_finished_nodes_and_is_none_for_a_single_agent() -> None:
    ...  # nodes in states done, failed, active, waiting → 0.5; one node → None

def test_running_shows_a_tick_story_and_a_standalone_run_but_not_the_story_own_runs(tmp_path: Path) -> None:
    ...  # story AIR-7 at "layers" naming run r1; runs r1 and r2 both started, neither done
    ...  # → [("story", "AIR-7"), ("run", "r2")] as (kind, key); r2's "topology" is "studio"

def test_a_run_silent_for_an_hour_without_run_done_leaves_running(tmp_path: Path) -> None:
    ...  # log mtime = now - 3601 → not listed; now - 3599 → listed

def test_a_live_job_stays_running_however_quiet(tmp_path: Path) -> None:
    ...  # log mtime = now - 7200 and live={"r2"} → listed; a story quiet as long with live={"story-AIR-7"} → listed
```

- [ ] **Step 2: Run them** — FAIL
- [ ] **Step 3: Implement `running.py`** and the route.
- [ ] **Step 4: Run `uv run pytest tests/unit/test_serve_running.py -v`** — PASS
- [ ] **Step 5: Commit** — `git commit -m "running API: every story and run in progress, with progress"`

---

### Task 12: A run's flow for every topology

**Files:**
- Create: `src/airlie/serve/run_flow.py`
- Modify: `src/airlie/serve/flow.py` (pull the trace reading out of `job_detail` into a shared helper),
  `src/airlie/serve/app.py` (two routes)
- Test: `tests/unit/test_serve_run_flow.py`

**Interfaces:**
- Consumes: `build_tree`, `RunNotFound` (`airlie.serve.tree`); `_job` and the new helper from `flow.py`;
  `run_progress` (Task 11).
- Produces:
  - `trace_of(state_dir: Path, run: str, team: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]`
    in `flow.py` (the opening prompt and the steps); `job_detail` uses it.
  - `run_flow(state_dir: Path, run_id: str, *, models: Mapping[str, str] | None = None) -> dict[str, Any]`:
    `{"key": run_id, "kind": "run", "topology", "mode", "title": <goal>, "summary": None, "stage": "running" |
    "done", "blocked": None, "acceptance": [], "ok", "progress": run_progress(...), "nodes": [...]}`; each node
    `{"id", "label", "kind": "stage", "status", "jobs": [...]}`, each job made with `_job(...)` from a tree
    node (`studio_run=run_id`, the node's `tokens`, `fix_rounds`, and `won`).
  - `run_job_detail(state_dir: Path, run_id: str, node_id: str, job_id: str) -> dict[str, Any]` with the
    same keys as `job_detail`; trace folder `runs/<run_id>/trace/<job_id>`.
  - `GET /v1/airlie/runs/{run_id}/flow` → `run_flow(...)` plus `"job"` (this server's job state or `None`) and
    `"plan"` (the plan dict when this server's job for the run is a finished `plan` job, else `None`);
    `GET /v1/airlie/runs/{run_id}/flow/{node_id}/{job_id}` → `run_job_detail(...)`; 404 for an unknown run,
    node or job.

Stages by topology, from tree node kinds: studio → `plan` "Plan" [`chief`], `specialists` "Specialists"
[every `role` node], `review` "Review" [`reviewer`] if present, `synthesis` "Synthesis" [`synthesis`] if
present (a plan-mode run has only `plan`); teams → `teams` "Teams" [every `team` node], `judge` "Judge"
[`aggregate`] if present; single → `agent` "Agent" [its one `team` node]; fork → `agent` "Agent" [the first
`branch` node], `forks` "Forks" [the other `branch` nodes] if any. Job status from node state: waiting →
pending, active / idle / stuck → running, done → passed, failed → failed. A stage is failed if any job
failed, else running if any runs, else passed if it has jobs and all passed, else pending.

- [ ] **Step 1: Write the failing tests**, building event lists with the `ev` / `step` helpers from
  `tests/unit/test_serve_tree.py` (import them) and writing them to `runs/<id>/events.jsonl`:

```python
def test_a_studio_run_part_way_has_plan_specialists_and_who_is_working(tmp_path: Path) -> None:
    f = run_flow(write_run(tmp_path, "r1", STUDIO_START), "r1")
    assert [n["id"] for n in f["nodes"]] == ["plan", "specialists", "review"]
    spec = node(f, "specialists")
    assert [(j["id"], j["status"]) for j in spec["jobs"]] == [("fix", "running"), ("note", "pending")]
    assert f["topology"] == "studio" and f["stage"] == "running"

def test_a_teams_run_has_a_lane_per_team_and_the_judge(tmp_path: Path) -> None:
    ...  # team_start t1, t2; team_done both; aggregate winner t2 → nodes ["teams", "judge"], t2 "won"
def test_a_single_run_has_one_agent_and_no_progress_number(tmp_path: Path) -> None:
    ...  # run_start k=1, team_start t1 → nodes ["agent"], progress None
def test_a_fork_run_has_the_agent_then_its_forks_and_marks_the_winner(tmp_path: Path) -> None:
    ...  # run_start topology="fork", branch_start b0, fork_point, branch_start b1 (parent b0), fork_resolved winner b1
    ...  # → nodes ["agent", "forks"], forks' job b1 "won"
def test_the_flow_route_adds_the_plan_of_a_finished_plan_job_and_404s_an_unknown_run(tmp_path: Path) -> None:
    ...  # a finished plan job whose result has "type": "plan" → body["plan"] is that plan; /runs/nope/flow → 404
def test_story_job_detail_is_unchanged_after_the_trace_helper(tmp_path: Path) -> None:
    ...  # the existing story fixture in test_serve_listing.py: job_detail(...) equals its value before the refactor
```

- [ ] **Step 2: Run them** — FAIL
- [ ] **Step 3: Implement** `trace_of`, `run_flow.py` and the routes.
- [ ] **Step 4: Run `uv run pytest tests/unit/test_serve_run_flow.py tests/unit/test_story_flow.py tests/unit/test_serve_listing.py -v`** — PASS
- [ ] **Step 5: Commit** — `git commit -m "runs API: a workflow flow for single, teams, fork and studio runs"`

---

### Task 13: The page side of the feed

**Files:**
- Create: `web/src/api.ts`, `web/src/feed.ts`, `web/src/stores/feed.ts`, `web/src/stores/live.ts`,
  `web/src/feed.test.ts`, `web/src/stores/live.test.ts`
- Modify: `web/src/main.ts` (install Pinia; start the feed)

**Interfaces:**
- Produces:
  - `api.ts`: `KEY = "airlie.key"` (the same localStorage key today's app uses, so a signed-in person stays
    signed in); `getKey(): string`; `class ApiError extends Error { status: number }`;
    `request<T>(path: string, init?: { method?: string; body?: unknown; signal?: AbortSignal }): Promise<T>`
    (`credentials: "same-origin"`, `Authorization: Bearer <key>` when a key is stored).
  - `feed.ts`: `type Change = { topic: string; key: string | null }`;
    `type FeedState = "connecting" | "live" | "reconnecting"`;
    `parseSse(buffer: string): { blocks: Array<{ event?: string; data?: string; comment?: string }>; rest: string }`;
    `connectFeed(opts: { onChange(c: Change): void; onState(s: FeedState): void; fetchImpl?: typeof fetch;
    backoffMs?: number[] }): () => void` — reads `GET /v1/events` with `fetch` (the key header as in
    `request`), goes `live` on the `: connected` comment, and on any end or error goes `reconnecting` and
    retries after `backoffMs` `[1000, 2000, 4000, 8000, 15000]`, staying at the last; the returned function
    stops it.
  - `stores/feed.ts`: `useFeedStore` with `state: FeedState`, `generation: number` (one more on every
    `live`), `on(topic: string, fn: (key: string | null) => void): () => void`, `start(): void`.
  - `stores/live.ts`: `liveResource<T>(topic: string, load: () => Promise<T>, key?: string):
    { data: Ref<T | undefined>; error: Ref<Error | undefined>; refresh(): Promise<void> }` — loads at once,
    refetches on its topic when the event's key is `null` or equals `key` (or `key` is unset), and refetches
    whenever `generation` changes.

- [ ] **Step 1: Write the failing tests**

```ts
test("parseSse splits blocks across chunks and keeps the unfinished rest", () => {
  const a = parseSse(': connected\n\nid: 1\nevent: change\ndata: {"topic":"bo');
  expect(a.blocks).toEqual([{ comment: "connected" }]);
  const b = parseSse(a.rest + 'ard","key":"AIR-1"}\n\n');
  expect(b.blocks[0]).toMatchObject({ event: "change", data: '{"topic":"board","key":"AIR-1"}' });
});

test("a dropped stream reconnects and goes live again", async () => {
  // fake fetch: the first body sends ": connected" then ends, the second stays open; backoffMs [0]
  // → onState calls are ["connecting", "live", "reconnecting", "live"]
});

test("a live resource refetches on its topic and key, not on others", async () => {
  // liveResource("board", load, "AIR-1"): board/AIR-2 → no call; board/AIR-1 → one; board/null → one; runs/null → none
});

test("a reconnect refetches every live resource", async () => {
  // two resources on different topics; generation goes 1 → 2 → each load called once more
});
```

- [ ] **Step 2: Run `task web:check`** — Expected: the new tests FAIL
- [ ] **Step 3: Implement** the four modules; `main.ts` installs Pinia and calls `useFeedStore().start()`.
- [ ] **Step 4: Run `task web:gate`** — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: the live feed client and live resources"` (files added)

---

### Task 14: The feed in a real browser

**Files:**
- Create: `tests/e2e/test_live_feed.py`

**Interfaces:**
- Consumes: the `own_server` fixture and `KEY` from `tests/e2e/conftest.py`; the `evidence` fixture;
  `GET /v1/events`, `POST /v1/board/items`; `BoardStore`.

- [ ] **Step 1: Write the tests.** The page reads the stream itself and keeps the first change it sees:

```python
FIRST_CHANGE_JS = """key => {
  window.firstChange = null; window.connected = false;
  fetch("/v1/events", { headers: { Authorization: "Bearer " + key } }).then(async (r) => {
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) return;
      buf += dec.decode(value, { stream: true });
      if (buf.includes(": connected")) window.connected = true;
      const m = buf.match(/event: change\ndata: (.*)\n\n/); if (m) { window.firstChange = m[1]; return; }
    }
  });
}"""

def test_a_board_write_through_the_api_reaches_an_open_page(page: Page, own_server: Server, evidence: Evidence) -> None:
    page.goto(own_server.url + "/next/")
    page.evaluate(FIRST_CHANGE_JS, KEY)
    page.wait_for_function("() => window.connected", timeout=3000)  # no replay: write only once connected
    httpx.post(own_server.url + "/v1/board/items", json={"level": "initiative", "title": "live"},
               headers={"Authorization": f"Bearer {KEY}"})
    page.wait_for_function("() => window.firstChange", timeout=3000)
    change = json.loads(page.evaluate("() => window.firstChange"))
    assert change["topic"] == "board" and change["key"].startswith("AIR-")
    evidence.shot("a board write through the API reached the open page")

def test_a_write_from_another_process_reaches_an_open_page_within_two_seconds(page: Page, own_server: Server, evidence: Evidence) -> None:
    ...  # same page set-up; BoardStore(own_server.state / "board.sqlite").create(level="initiative", title="tick")
    ...  # → window.firstChange is {"topic": "board", "key": null} within 3000 ms; evidence.shot(...)
```

- [ ] **Step 2: Run `task test:e2e -- -k live_feed`** — Expected: both PASS, with evidence screenshots
- [ ] **Step 3: Run the full gate from `AGENTS.md`**, then `task test:e2e` on its own — Expected: all green
- [ ] **Step 4: Commit** — `git commit -m "e2e: the live feed reaches a browser from the API and from another process"`
