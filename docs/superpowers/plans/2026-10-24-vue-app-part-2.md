# Vue App, Part 2: Components, Every Screen and the Switch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build every screen of the new Vue app at `/next` from the approved mockups, on one shared component set
and the Part 1 live feed, then switch `/` to it and retire today's app.

**Architecture:** `web/` (Vite, Vue 3, TypeScript, Pinia, Vue Router, Tailwind v4) gains shadcn-vue primitives
restyled with the approved tokens, airlie's own shared components, one Pinia store per resource built on
`liveResource` (Part 1), and one screen module per route. Each screen is proven by Vitest component tests and by
the Playwright browser suite. The last task moves the app from `/next` to `/`.

**Tech Stack:** Vue 3, TypeScript, Pinia, Vue Router, Tailwind v4, shadcn-vue (on reka-ui), lucide-vue-next,
Vitest, Vue Test Utils; FastAPI and pytest for the small API additions; Playwright (Python) for the browser suite.

**Spec:** `docs/superpowers/specs/2026-10-24-vue-app-design.md`. **Mockups (binding for layout and copy):**
`docs/superpowers/mockups/2026-10-24-vue-app/` — `workflows.html`, `workflow-story.html`, `workflow-runs.html`,
`board.html`, `chat.html`, `metrics.html`, `admin.html`, `sign-in.html`, and `look.css` (the tokens). Each mockup
has buttons for its other states; open them in a browser while building.

**Part 1** (merged, PR 151) provides: `task web:*`, `/next` serving, `GET /v1/events`, the watcher, `web/src/api.ts`
(`request`, `getKey`, `authHeaders`, `ApiError`), `web/src/feed.ts` (`connectFeed`), `web/src/stores/feed.ts`
(`useFeedStore`: `state`, `generation`, `on`, `start`), `web/src/stores/live.ts` (`liveResource`), and the APIs
`GET /v1/airlie/stories?page&size&stage&repo&blocked&q&sort`, `GET /v1/airlie/running`,
`GET /v1/airlie/runs/{id}/flow[/{node}/{job}]` (with `job` and `plan`).

## Global Constraints

- The mockups are the layout and the copy. Where a mockup and this plan disagree, the mockup wins on look and
  words, the spec wins on behaviour.
- Tokens come from `look.css` only (dark default, light via the theme toggle); no colour literals in components
  except the eight type colours and four series colours named below.
- Type colours: initiative `#a78bfa`, epic `#60a5fa`, story `#34d399`, subtask `#a1a1aa`, spike `#fb923c`,
  research `#38bdf8`, design `#f472b6`, bug `#f87171`. Series colours, in order: `#34d399`, `#38bdf8`, `#fb923c`,
  `#a78bfa`.
- Nothing loads from the internet at runtime; system font stack; icons from lucide-vue-next, bundled.
- Browser keys stay today's, so a person moving between apps keeps their state: `airlie.key`, `airlie.theme`,
  `airlie.repo`, `airlie.chat`, `airlie.chat.draft`.
- Every list and detail a screen shows is a `liveResource` on its feed topic(s): `board`, `users`, `stories`,
  `runs`, `jobs`. A change from anywhere appears without a reload.
- Phone layout below 640 px: the top tabs become the bottom tab bar (Chat, Workflows, Board, Metrics, Admin).
- Tabs: Chat, Workflows, Board, Metrics, Admin. There is no Topology tab and no Start new work page.
- The Create picker offers initiative, epic, story and subtask until the work-types design adds the rest.
- Today's app at `/` and `/app` keeps working until Task 15.
- Only a human merges; no merge control anywhere.
- Every new dependency is pinned to an exact version at least two weeks old; the lockfile is committed.

## Review Focus

- **Long text** (a 200-character title, a long repository, a key like `HDS-1234`) never breaks a row, a card or a
  pipeline box: it truncates with the full text on hover, keys never wrap. Pinned in Task 4 (`ItemCard`, `JobBox`)
  and Task 9.
- **Signed out** on any screen (a 401 from any call): the app goes to `/next/signin`, keeps the page asked for,
  and the feed stops instead of retrying every 15 s. Pinned in Task 3.
- **A story part way or stopped** (no layers yet, a failed stage, a blocked story): the pipeline shows pending and
  failed boxes and opens on the failed job, as today. Pinned in Task 9.
- **Empty states** (no stories, nothing running, no comments, no users, an empty chart): each says so in words,
  never a blank card. Pinned in Tasks 6, 8, 12, 13.
- **Keyboard**: modals trap focus and close on Escape; the type-ahead takes Tab, Enter, the arrows and Escape;
  Escape in a click-to-edit field discards and keeps the modal. Pinned in Tasks 4 and 6.

---

### Task 1: API additions — repository names and stories by activity window

**Files:**
- Modify: `src/airlie/serve/listing.py`, `src/airlie/serve/app.py`
- Test: `tests/unit/test_serve_listing.py`

**Interfaces:**
- Produces:
  - `repo_names(cfg: AirlieConfig, labels: Iterable[str]) -> list[str]`: sorted, unique `owner/name` from the
    `origin` remote of `story.repo` and of every git checkout directly under `story.repos_root`
    (`git@github.com:o/n.git`, `https://github.com/o/n(.git)` and `ssh://git@github.com/o/n.git` forms), plus every
    `repo:<owner>/<name>` label passed in; a checkout without an origin, or one that is not GitHub, is skipped.
  - `GET /v1/airlie/repos/names` → `{"repos": repo_names(cfg, <every label on the board>)}`.
  - `story_page(..., active_from: float | None = None, active_to: float | None = None)`: keeps rows whose
    `last_activity` is in `[active_from, active_to)`; the route takes `active_from` and `active_to` (epoch seconds).

- [ ] **Step 1: Write the failing tests**

```python
def test_repo_names_read_origins_and_labels(tmp_path: Path) -> None:
    root = tmp_path / "git"
    for name, url in [("airlie", "git@github.com:weyenk/airlie.git"), ("evals", "https://github.com/weyenk/airlie-evals"),
                      ("local", None)]:
        d = root / name; d.mkdir(parents=True); subprocess.run(["git", "init", "-q", str(d)], check=True)
        if url: subprocess.run(["git", "-C", str(d), "remote", "add", "origin", url], check=True)
    cfg = cfg_with(tmp_path, repos_root=root, repo=None)
    assert repo_names(cfg, ["repo:weyenk/openhand", "x:y", "repo:weyenk/airlie"]) == [
        "weyenk/airlie", "weyenk/airlie-evals", "weyenk/openhand"]

def test_stories_by_activity_window() -> None:
    assert keys(story_page(ROWS, active_from=15, active_to=31)) == ["AIR-9", "AIR-17"]   # activity 30 and 20, not 10
```

- [ ] **Step 2: Run them** — `uv run pytest tests/unit/test_serve_listing.py -v` — FAIL
- [ ] **Step 3: Implement** both; `git -C <dir> remote get-url origin` through `airlie.subproc`'s runner with a
  5 s timeout; the route test also checks the names route needs sign-in.
- [ ] **Step 4: Run the tests** — PASS; then the full gate.
- [ ] **Step 5: Commit** — `git commit -m "API: repository names and stories by activity window"`

---

### Task 2: Tokens and the shadcn-vue primitives

**Files:**
- Create: `web/src/styles/tokens.css` (the custom properties of `look.css`, `.dark` and `.light`),
  `web/components.json`, `web/src/lib/cn.ts`, `web/src/components/ui/*` (button, badge, card, input, textarea,
  dialog, dropdown-menu, collapsible, tooltip, progress, select)
- Modify: `web/src/style.css` (import Tailwind and the tokens; map them with `@theme inline` so classes like
  `bg-card`, `text-muted-foreground`, `border-border`, `bg-primary` resolve to the tokens), `web/package.json`
- Test: `web/src/components/ui/ui.test.ts`

**Interfaces:**
- Produces: the shadcn-vue components under `@/components/ui/<name>` with their standard props; `cn(...classes)`;
  the `@` path alias to `web/src` (Vite and tsconfig).

- [ ] **Step 1: Write the failing test**

```ts
test("the primitives render with the approved tokens", () => {
  const b = mount(Button, { slots: { default: "Create" } });
  expect(b.classes().join(" ")).toMatch(/bg-primary/);
  const badge = mount(Badge, { props: { variant: "secondary" }, slots: { default: "story" } });
  expect(badge.text()).toBe("story");
});
```

- [ ] **Step 2: Run `task web:check`** — FAIL (no components)
- [ ] **Step 3: Add the primitives** with the shadcn-vue CLI at a pinned version (`npx shadcn-vue@<version> init`,
  then `add` for each listed component), choosing the neutral base and CSS variables; then replace its colour
  variables with `tokens.css` so the components take the Aura palette. Dependencies it adds (reka-ui,
  class-variance-authority, clsx, tailwind-merge, lucide-vue-next) are pinned by the two-week rule.
- [ ] **Step 4: Run `task web:gate`** — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: tokens and the shadcn-vue primitives"`

---

### Task 3: The app shell, router, sign-in guard and theme

**Files:**
- Create: `web/src/router.ts`, `web/src/stores/session.ts`, `web/src/components/AppShell.vue`,
  `web/src/components/TopBar.vue`, `web/src/components/TabBar.vue`, `web/src/components/LiveBadge.vue`,
  `web/src/components/ThemeToggle.vue`, `web/src/screens/NotFound.vue`, tests beside each
- Modify: `web/src/App.vue`, `web/src/main.ts`, `web/src/stores/feed.ts` (add `stop(): void`), `web/src/api.ts`
  (a 401 calls the session store's `signedOut()`)

**Interfaces:**
- Produces:
  - Routes (history mode, base `/next/`): `/` Chat, `/workflows`, `/workflows/:key/:node?/:job?` (story),
    `/workflows/runs/:id/:node?/:job?` (run), `/board/:key?`, `/metrics`, `/admin`, `/signin` (public, bare),
    `/join/:token` (public, bare), `/start` → redirect `/`, `/jobs/:id` → redirect `/workflows/runs/:id`,
    `*` NotFound. The run route is registered before the story route, so `/workflows/runs/<id>` is never read as a
    story called "runs". Route meta `{ public?: boolean, bare?: boolean, tab: "chat" | "workflows" | "board" | "metrics" | "admin" }`.
  - `useSessionStore`: `me: { name: string; role: "admin" | "member"; via: string } | null`, `load(): Promise<void>`
    (`GET /v1/auth/me`), `signedOut(): void` (clears `me`, stops the feed, goes to `/signin?next=<path>`),
    `signOut(): Promise<void>` (`POST /v1/auth/logout`, then `signedOut()`).
  - Guard: a non-public route loads `me` once; none → `/signin?next=…`. The feed starts only after `me` exists,
    and stops on public routes.
  - `LiveBadge` shows "Live" (ok badge) or "Reconnecting" (warn badge) from `useFeedStore().state`.
  - `ThemeToggle` flips `dark`/`light` on `<html>` and stores it under `airlie.theme`.

- [ ] **Step 1: Write the failing tests**

```ts
test("a signed-out visit goes to sign in and keeps the page asked for", async () => {
  // fake fetch: /v1/auth/me → 401
  await router.push("/board/AIR-17"); await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe("/signin?next=/board/AIR-17");
});
test("the old start and job addresses redirect", async () => { /* /start → /, /jobs/r1 → /workflows/runs/r1 */ });
test("a 401 from any call stops the feed", async () => { /* feed.stop called once, state no longer reconnecting */ });
test("the live badge follows the feed", () => { /* state live → "Live"; reconnecting → "Reconnecting" */ });
test("the theme toggle persists under airlie.theme", () => { /* click → html.light, localStorage "light" */ });
```

- [ ] **Step 2: Run `task web:check`** — FAIL
- [ ] **Step 3: Implement**; the top bar and tab bar match the mockups (pill tabs; avatar menu with Sign out).
- [ ] **Step 4: Run `task web:gate`** — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: the shell, routes, sign-in guard and theme"`

---

### Task 4: Shared airlie components

**Files:**
- Create in `web/src/components/`: `StatusChip.vue`, `TypeTag.vue`, `ProgressBar.vue`, `EmptyState.vue`,
  `ErrorBanner.vue`, `ConfirmDialog.vue`, `SafeMarkdown.vue`, `Typeahead.vue`, `ItemKey.vue`, tests beside each

**Interfaces:**
- Produces:
  - `StatusChip { kind: "board" | "stage" | "job" | "run"; value: string }` — label and colour from one table:
    board statuses todo/approved/doing/review/blocked/done; story stages as words (refining, planning layers,
    building, publishing, in review, blocked); job statuses passed/running/failed/pending/waiting; run kinds.
  - `TypeTag { type: string }` — the type's colour square and name.
  - `ProgressBar { value: number | null }` — `null` slides (indeterminate).
  - `EmptyState { text: string }`, `ErrorBanner { error: Error | string }`.
  - `ConfirmDialog` — `confirm(message): Promise<boolean>` via a composable `useConfirm()`.
  - `SafeMarkdown { text: string; linkKeys?: boolean }` — paragraphs, headings, lists, fenced and inline code, bold,
    italic, http(s) links; builds nodes, never `v-html`; with `linkKeys`, story keys (`[A-Z]+-\d+`) become router
    links to `/workflows/<key>`.
  - `Typeahead { modelValue: string; source: string[] | ((q: string) => Promise<Suggestion[]>); placeholder?: string }`,
    emits `update:modelValue`; `Suggestion = { value: string; label?: string; meta?: string }`. Shows up to three
    matches below the field (prefix matches first, then substring, case-insensitive), the typed part bold, the rest
    of the top match greyed inside the field; Tab or Enter accepts, ArrowUp/ArrowDown move, Escape closes.
  - `ItemKey { value: string }` — monospace, never wraps.

- [ ] **Step 1: Write the failing tests** (one file per component); the decisive ones:

```ts
test("typeahead completes the top match on Tab", async () => {
  const w = mount(Typeahead, { props: { modelValue: "", source: ["weyenk/airlie", "weyenk/airlie-evals", "weyenk/openhand"] } });
  await w.find("input").setValue("weyenk/air");
  expect(w.findAll("[data-suggestion]").map((s) => s.text())).toEqual(["weyenk/airlie", "weyenk/airlie-evals"]);
  expect(w.find("[data-ghost]").text()).toBe("lie");
  await w.find("input").trigger("keydown", { key: "Tab" });
  expect(w.emitted("update:modelValue")!.at(-1)).toEqual(["weyenk/airlie"]);
});
test("markdown never turns text into HTML", () => {
  const w = mount(SafeMarkdown, { props: { text: "<img src=x onerror=alert(1)> **bold**" } });
  expect(w.find("img").exists()).toBe(false); expect(w.find("strong").text()).toBe("bold");
});
test("a long key never wraps and a long title truncates with its full text on hover", () => { /* ItemKey white-space; title attr */ });
test("status chips use one table for every kind", () => { /* review → "in review" warn; failed → danger */ });
```

- [ ] **Step 2: Run `task web:check`** — FAIL
- [ ] **Step 3: Implement** the components on the Task 2 primitives.
- [ ] **Step 4: Run `task web:gate`** — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: shared components"`

---

### Task 5: Stores and typed API calls

**Files:**
- Create: `web/src/api/board.ts`, `web/src/api/workflows.ts`, `web/src/api/chat.ts`, `web/src/api/auth.ts`,
  `web/src/api/metrics.ts`, `web/src/stores/board.ts`, `web/src/stores/workflows.ts`, `web/src/stores/people.ts`,
  tests beside each

**Interfaces:**
- Produces typed functions over the existing routes (one per route the screens call; shapes as the server returns
  them): board items (list, get, create, patch, move, comment, edit comment), stories page, running, story flow and
  job detail, run flow and job detail, repo names, chat skills, attachment limits, uploads (POST with progress, GET
  url), chat stream (port of `api.js` `stream()`), jobs (`POST /v1/airlie/stories`, `POST /v1/airlie/runs`,
  `DELETE /v1/airlie/jobs/{id}`), auth (me, users, invites, user invites, delete user, passkey register and login,
  logout), metrics summary and series. `ApiError`'s message reads both 422 shapes the API returns: a string
  `detail` (the stories route's unknown stage) and FastAPI's validation list.
- `useBoardStore`: `items: Item[]`, `changed: Set<string>` (keys changed by someone else in the last 4 s), built
  on `liveResource("board", …)`. A refetch compares with the previous list by key and `updated_at`; a key whose
  item changed and that this tab did not just write joins `changed` for 4 s. This is how the "updated just now" ring
  survives the watcher's keyless echo of the tab's own writes (spec, Part 1 final review).
- `useWorkflowsStore`: `running` (`liveResource` on `stories`, `runs`, `jobs`), `stories` page with its query.
- `usePeopleStore`: `users` on `users`.

- [ ] **Step 1: Write the failing tests**

```ts
test("only someone else's change rings a card, and the ring fades after 4 s", async () => {
  // own move of AIR-17 then a board/null refetch with the new updated_at → not in changed
  // a refetch where AIR-8's updated_at moved without a local write → changed has AIR-8; after 4000 ms it does not
});
test("the chat stream yields deltas and reasoning and reports a cut stream", async () => { /* port of api.js stream tests */ });
```

- [ ] **Step 2–4:** fail, implement, `task web:gate` — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: typed API calls and the resource stores"`

---

### Task 6: The Board

**Files:**
- Create: `web/src/screens/BoardScreen.vue`, `web/src/screens/board/BoardColumn.vue`,
  `web/src/screens/board/ItemCard.vue`, `web/src/screens/board/StatusPills.vue`, tests beside each;
  `tests/e2e/next/test_board.py`
- Modify: `tests/e2e/conftest.py` (a `next_page` fixture: signed in at `/next/`)

**Interfaces:**
- Consumes: `useBoardStore`, `StatusChip`, `TypeTag`, `ItemKey`, `EmptyState`.
- Produces: the board as in `board.html`: six columns To do, Approved, Doing, Review, Blocked, Done with counts; the
  column area is `calc(100vh - <header>)` minus a 24 px buffer and each column scrolls on its own; cards show key,
  type, title and repo label; the ring and "updated just now" for keys in `changed`; the Type dropdown filter (the
  four levels); Create opens Task 7's modal; drag and drop moves a card (`POST …/move` with `status` and `before`,
  the card under the pointer, or the bottom); below 640 px the status pills with counts replace the columns, one
  column at a time.

- [ ] **Step 1: Write the failing tests** — Vitest: a column renders its items in order; a drop calls `move` with
  the right `before`; an empty column says "Nothing here". Browser suite (`next_page`):

```python
def test_each_status_has_a_column_and_columns_fit_the_window(next_page: Page, server: Server, evidence: Evidence) -> None:
    next_page.goto(server.url + "/next/board")
    evidence.expect(next_page.get_by_role("region", name=re.compile("To do|Approved|Doing|Review|Blocked|Done")), "six columns").to_have_count(6)
    box = next_page.locator("[data-columns]").bounding_box()
    assert box["y"] + box["height"] <= next_page.viewport_size["height"] - 16
    evidence.shot("the columns end above the window's bottom edge")

def test_a_change_from_another_process_rings_its_card(next_page: Page, own_server: Server, evidence: Evidence) -> None: ...
def test_the_phone_board_shows_one_column_and_the_status_pills(next_page: Page, server: Server, evidence: Evidence) -> None: ...
```

- [ ] **Step 2–4:** fail, implement, `task web:gate`, then `task test:e2e -- -k next` (on its own) — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: the Board"`

---

### Task 7: The card modal and Create

**Files:**
- Create: `web/src/screens/board/CardModal.vue`, `web/src/screens/board/CreateModal.vue`,
  `web/src/screens/board/ClickToEdit.vue`, `web/src/screens/board/Comments.vue`, tests beside each;
  `tests/e2e/next/test_board_modal.py`

**Interfaces:**
- Consumes: `useBoardStore`, `Typeahead` (repo names), `SafeMarkdown`, `useConfirm`, the story flow (for the
  Workflow section).
- Produces, as in `board.html`:
  - **Card modal** at `/board/:key`: key, close; title (click to edit); status, type, parent link, repository (click
    to edit with `Typeahead`, saved as a `repo:<owner>/<name>` label, refused unless `owner/name`); Move to (the
    statuses except the current one); Description (click to edit, Markdown shown); Acceptance criteria with Given,
    When and Then coloured; Workflow with each layer's status and Open pipeline; Subtasks with Add child (opens
    Create with the parent set); Comments with Edit on your own (`PATCH …/comments/{id}`; the server's author check
    decides); the created and updated line. Click-to-edit: Enter or a click outside saves, Escape discards and keeps
    the modal. The modal traps focus, closes on Escape or the backdrop, and fills the screen below 1024 px. Below
    640 px, Acceptance criteria and Workflow start folded.
  - **Create**: the type tiles (four levels), title, optional parent, repository `Typeahead`, description;
    "Create <type>" (`POST /v1/board/items`); a child starts with its parent's repository.

- [ ] **Step 1: Write the failing tests** — port the behaviours `tests/e2e/test_board_modal.py`,
  `test_board_edit.py` and `test_board.py` check for today's modal (open centred over the dimmed board, Escape and
  backdrop close, focus stays inside, Escape in an editor discards, repository edit and its owner/name rule, a new
  child takes the parent's repository) as `tests/e2e/next/test_board_modal.py`; plus:

```python
def test_the_repository_field_completes_a_known_repository_with_tab(next_page: Page, own_server: Server, evidence: Evidence) -> None:
    # open a card, click the repository, type "weyenk/air", Tab → the field reads "weyenk/airlie"; Enter saves the label
    ...
def test_on_the_phone_criteria_and_workflow_start_folded(next_page: Page, server: Server, evidence: Evidence) -> None: ...
```

- [ ] **Step 2–4:** fail, implement, gates, e2e — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: the card modal and Create"`

---

### Task 8: The Workflows list

**Files:**
- Create: `web/src/screens/WorkflowsScreen.vue`, `web/src/screens/workflows/RunningCard.vue`,
  `web/src/screens/workflows/StoriesTable.vue`, tests beside each; `tests/e2e/next/test_workflows_list.py`

**Interfaces:**
- Consumes: `useWorkflowsStore`, `StatusChip`, `ProgressBar`, `ItemKey`, `EmptyState`.
- Produces, as in `workflows.html`: **Running** on top (key or kind tag, title, progress bar; a click opens
  `/workflows/<key>` or `/workflows/runs/<id>`; "Nothing is running" when empty); **Stories** with search, Stage,
  Repository (values from the stories' own `repo` fields, sent back verbatim), Blocked (All / Blocked / Not blocked),
  Sort (last activity, key, stage), 25 a page, the query mirrored in the URL; blocked rows show their reason; on the
  phone the filters sit behind a Filters button. No Start button. `?blocked=true` from Chat's "needs you" pill and
  `?active_from=…&active_to=…` from Metrics apply on load.

- [ ] **Step 1: Write the failing tests** — Vitest: the query string round-trips the filters; page 2 asks for
  `page=2&size=25`. Browser suite: a running story's row opens its workflow view; a seeded blocked story shows its
  reason and appears under Blocked; the list updates when a story's state changes on disk.
- [ ] **Step 2–4:** fail, implement, gates, e2e — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: the Workflows list"`

---

### Task 9: The workflow view for a story

**Files:**
- Create: `web/src/screens/WorkflowScreen.vue`, `web/src/screens/workflow/Pipeline.vue`,
  `web/src/screens/workflow/JobBox.vue`, `web/src/screens/workflow/LayerLane.vue`,
  `web/src/screens/workflow/JobDetail.vue`, `web/src/screens/workflow/StepList.vue`, tests beside each;
  `tests/e2e/next/test_workflow_story.py`

**Interfaces:**
- Consumes: the story flow and job detail calls, `liveResource` on `stories` and `runs` keyed by the story,
  `StatusChip`, `ProgressBar`, `ItemKey`.
- Produces, as in `workflow-story.html` (the page uses the full window width): header (back to Workflows, key,
  stage chip, Board card link, title, blocked reason, Stop when this server runs the story's job); Pipeline (stages
  left to right, a lane of Tests, Test checks and Build per layer with its acceptance ids, sideways scroll with a
  fade, the progress figure); the selected job (in the URL as `/:node/:job`; with none, the failed job, else the
  running one, else the last that ran) with its numbers, why it retried, input, output and steps (each step opens
  its thinking, text and observation). Below 640 px the pipeline runs top to bottom with each layer's three jobs side
  by side. `JobDetail` and `StepList` take any job detail, so Task 10 reuses them.

- [ ] **Step 1: Write the failing tests** — port `tests/e2e/test_workflows.py`'s pipeline and job checks to
  `/next/workflows/AIR-…` (stages and a lane per layer, opens on the failed job, says why it retried, the tester's
  prompt, its speed, a step's reasoning opens, a job without a recorded prompt says so); Vitest: a story with no
  layers yet draws only its stages; a running job box pulses; a 120-character layer title truncates in its lane.
- [ ] **Step 2–4:** fail, implement, gates, e2e — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: the workflow view for a story"`

---

### Task 10: The workflow view for a run, with plan approval and Stop

**Files:**
- Create: `web/src/screens/RunWorkflowScreen.vue`, `web/src/screens/workflow/StageBox.vue`,
  `web/src/screens/workflow/PlanCard.vue`, tests beside each; `tests/e2e/next/test_workflow_run.py`

**Interfaces:**
- Consumes: the run flow and job detail calls (`job`, `plan`), `JobDetail`, `StepList`, `DELETE /v1/airlie/jobs/{id}`,
  `POST /v1/airlie/runs`.
- Produces, as in `workflow-runs.html`: header (kind tag, state chip, run id and repository, goal, Stop while
  `job === "running"`, behind a confirm); stages as boxes holding their jobs (parallel stages stack them, the winner
  starred); the plan card when `plan` is set: assumptions, the subtask table, Approve plan (`POST /v1/airlie/runs`
  with the plan, then go to the new run's view) and Change it (opens the plan as editable JSON in a dialog, then
  Approve sends the edit). Single, teams, fork and studio runs all use this screen. Once the run is done the
  progress figure is hidden (a finished fork run's tree can leave it below 1).

- [ ] **Step 1: Write the failing tests** — Vitest: a teams flow draws the Teams stage with three jobs and the
  starred winner; Approve posts the plan; Stop asks first. Browser suite: seeded studio, teams, fork and single runs
  each draw their stages; a finished plan job shows the plan card.
- [ ] **Step 2–4:** fail, implement, gates, e2e — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: the workflow view for runs, with plan approval and Stop"`

---

### Task 11: Chat — thread, streaming and status strip

**Files:**
- Create: `web/src/screens/ChatScreen.vue`, `web/src/screens/chat/Thread.vue`, `web/src/screens/chat/Bubble.vue`,
  `web/src/screens/chat/StatusStrip.vue`, `web/src/stores/chat.ts`, tests beside each;
  `tests/e2e/next/test_chat.py`

**Interfaces:**
- Consumes: the chat stream, `SafeMarkdown { linkKeys: true }`, `useWorkflowsStore().running`, the stories page
  (blocked count), `GET /v1/models` (Studio online).
- Produces, as in `chat.html`: the thread kept in `sessionStorage` under `airlie.chat` in today's format (so a thread
  carries over between apps), the draft under `airlie.chat.draft`; the greeting with quick replies (What can you do?,
  What's running right now?, /story); a streamed reply with "Thinking" until the first token; a cut stream says so
  and keeps what arrived; the status strip pinned at the top ("N needs you" → `/workflows?blocked=true`, "N running"
  → `/workflows`, Studio online or offline).

- [ ] **Step 1: Write the failing tests** — port `tests/e2e/test_chat.py`'s thread and reply checks (a JSON plan
  reply stays text, /clear starts a fresh session without asking the model); Vitest: a thread written by today's app
  loads; the strip counts update when the running list changes.
- [ ] **Step 2–4:** fail, implement, gates, e2e — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: Chat thread, streaming and status strip"`

---

### Task 12: Chat — composer, attachments, the / menu, story and job cards

**Files:**
- Create: `web/src/screens/chat/Composer.vue`, `web/src/screens/chat/AttachTray.vue`,
  `web/src/screens/chat/SlashMenu.vue`, `web/src/screens/chat/StoryCard.vue`, `web/src/screens/chat/JobCard.vue`,
  tests beside each; `tests/e2e/next/test_chat_composer.py`

**Interfaces:**
- Consumes: chat skills, attachment limits, uploads, `POST /v1/airlie/stories`, `Typeahead` (repo names),
  `useWorkflowsStore().running`.
- Produces: the composer (`+` attaches pictures, video and files, up to the limits; chips with thumbnail, name,
  size and remove; a file over the limit says so and keeps Send off); your picture shown in your bubble and again
  after a reload, the thread holding ids and names, never bytes; the `/` menu filtering as you type; `/story` turns
  the reply into an editable story card with the repository type-ahead (default `airlie.repo`) and Start story; a
  started story becomes a job card with its stage and progress, live, and "Open workflow". Empty state of the menu:
  "No command matches".

- [ ] **Step 1: Write the failing tests** — port `tests/e2e/test_chat.py`'s /story checks and every check in
  `tests/e2e/test_chat_attachments.py` to `/next/`; Vitest: Start story without a repository says to pick one and
  posts nothing.
- [ ] **Step 2–4:** fail, implement, gates, e2e — PASS
- [ ] **Step 5: Commit** — `git commit -m "web: Chat composer, attachments and cards"`

---

### Task 13: Metrics

**Files:**
- Create: `web/src/screens/MetricsScreen.vue`, `web/src/screens/metrics/StatTiles.vue`,
  `web/src/screens/metrics/SeriesChart.vue`, tests beside each; `tests/e2e/next/test_metrics.py`

**Interfaces:**
- Consumes: metrics summary and series; the series colours.
- Produces, as in `metrics.html`: range (4 hours, 24 hours, 7 days, 30 days, All time), Slice by (role, model,
  topology, task, agent); four tiles (generation and prefill tok/s with the change against the period before —
  a second summary call for the previous window; stories with their breakdown; fix rounds per layer); the chart (one
  line per slice, a hand-drawn SVG, a tooltip for the point under the pointer, a click opening
  `/workflows?active_from=…&active_to=…` for that bucket); missing values read "unknown", an empty series says
  "No runs in this range".

- [ ] **Step 1–5:** tests (Vitest: the delta against the previous window; a click builds the activity window from
  the bucket; browser suite: the tiles and the chart render against seeded metrics), implement, gates, e2e, commit
  `"web: Metrics"`.

---

### Task 14: Admin, Sign in and Join

**Files:**
- Create: `web/src/screens/AdminScreen.vue`, `web/src/screens/SignInScreen.vue`, `web/src/screens/JoinScreen.vue`,
  tests beside each; `tests/e2e/next/test_people.py`

**Interfaces:**
- Consumes: the auth calls, `usePeopleStore`, `useConfirm`, `useSessionStore`.
- Produces, as in `admin.html` and `sign-in.html`:
  - **People** (admins only; "Only admins can see who's here." otherwise): each person's role, passkey count, since
    when, Tailscale login, "invite waiting" for an unused invite; New link; Revoke behind a confirm; Invite someone
    (name, Member or Admin, optional Tailscale login) with the link in a box and Copy link; the bootstrap
    hand-over note when the first person is invited.
  - **Sign in** (bare): Use this device (passkey), the invite-link pointer, Sign in with the admin key (folded);
    today's error wording for each failure; behind Tailscale serve it hands straight on to `next`.
  - **Join** (bare): who the invite is for and who sent it, Create passkey, the expired-link page, and today's
    error wording.

- [ ] **Step 1–5:** tests (port today's sign-in checks in `tests/e2e/test_board.py` — the admin key lands on the page
  asked for, a wrong key stays; browser suite: inviting shows a copyable link; Revoke asks first; an expired token
  shows the expired page), implement, gates, e2e, commit `"web: Admin, Sign in and Join"`.

---

### Task 15: The switch

**Files:**
- Modify: `src/airlie/serve/app.py` (serve the new app at `/`), `web/vite.config.ts` (`base: "/"`),
  `src/airlie/cli.py` and `src/airlie/auth/api.py` (invite URLs `/join/<token>`), `web/src/main.ts` (an old
  `#/x` address is replaced by `/x` before the router starts), `README.md`, `docs/integrations/sign-in.md`
- Delete: `src/airlie/serve/web/`, `tests/unit/test_serve_web.py`, and the old-app browser tests
  (`tests/e2e/test_board.py`, `test_board_edit.py`, `test_board_modal.py`, `test_board_width.py`, `test_chat.py`,
  `test_chat_attachments.py`, `test_topology.py`, `test_workflows.py`)
- Move: `tests/e2e/next/*` → `tests/e2e/`, pointed at `/`
- Test: `tests/unit/test_serve_next.py` (becomes the root serving test), the moved browser suite

**Interfaces:**
- Produces: `/` and every app path return the new app's page; files under `/assets/` with the long cache; `/v1`
  and `/health` unchanged; `/next` and `/next/<path>` redirect (308) to `/<path>` with the query kept; `/app/*` is
  gone (404); an old `#/board/AIR-17` link lands on `/board/AIR-17`; invite links read `/join/<token>`.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_app_path_serves_the_new_app_and_next_redirects(tmp_path: Path) -> None:
    c = client_with(tmp_path, built=True)
    assert '<div id="app">' in c.get("/board/AIR-17").text
    r = c.get("/next/workflows?blocked=true", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/workflows?blocked=true"
    assert c.get("/app/app.js").status_code == 404 and c.get("/health").status_code == 200

def test_an_invite_link_is_a_clean_path(tmp_path: Path) -> None: ...   # POST /v1/auth/invites → "url": "/join/<token>"
```

  plus a browser check that `/#/board/AIR-…` lands on `/board/AIR-…`.
- [ ] **Step 2–4:** fail, implement, move and delete, the full gate, then `task test:e2e` on its own — PASS
- [ ] **Step 5: Commit** — `git commit -m "Switch / to the Vue app; retire the old web app"`

---

**Pull requests:** Tasks 1–5 (foundation for the screens), 6–7 (Board), 8–10 (Workflows), 11–12 (Chat), 13–14
(Metrics and people), 15 (the switch, alone, after everything else has merged).
