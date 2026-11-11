# Chat redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The chat looks and moves like Claude Code desktop: a left nav with pinned repositories (each with its chats and a "new chat" that starts a VM session) and plain chats, a calm thread with collapsible tool blocks, a reasoning disclosure, inline diffs, a todo checklist and permission prompts, and a header with repository, branch, model, context used and engine and VM status. The client runs on `@ai-sdk/vue` 4's `Chat` against `POST /v1/airlie/chat/turn`.

**Architecture:** Mockups come first (three directions, Noah picks; Tasks 1-2). After the pick, the Vue chat screen is rebuilt around one `Chat` instance per open session (`DefaultChatTransport` posting to the turn route), with the thread rendering each message's `parts` through one small component per part type. The nav, header and thread read shared Pinia stores (`engines` exists; `sessions` and `pins` are new). Other screens keep the existing top bar and tab bar. Tasks 3-9 are a skeleton with the decided structure and are refined in Task 3 once the pick is known.

**Tech Stack:** Vue 3.5, Pinia, vue-router, reka-ui 2.11, Tailwind (as `web/` uses it), vitest 5, `ai` (AI SDK 7) and `@ai-sdk/vue` 4 (new), Playwright e2e.

**Spec:** [`docs/superpowers/specs/2026-11-11-airlie-web-chat-agent-design.md`](../specs/2026-11-11-airlie-web-chat-agent-design.md), phase 5: "Thread, transport, and the redesign" (plus "Two kinds of chat", "Status for the UI", "Mobile: a full PWA", "Order of work").

## Global Constraints

- Every task passes the gate from the repository root, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`.
- Tests follow `skills/qa/*.md`:
  - Web tests (vitest) follow `component.md`: render the real component tree, query by role or text, mock at the network boundary (`web/src/test-utils/fetch-stub.ts`), cover loading, error and empty states, never sleep, names that state what the user sees; one test per state of each component.
  - A backend route driven through `TestClient` goes in `tests/functional`: happy path, 401, 400 or 422, 404 and a dependency failure, asserting the body.
  - E2E (`tests/e2e`, Playwright, `pytestmark = pytest.mark.e2e`, a screenshot per check through `Evidence`) is only for the critical journey (Task 8): open a repository chat from the nav, send a turn, see a tool block, approve a permission prompt.
  - Fakes are rebuilt per test; every new test is seen red before the code is written; unit tests over 500 ms or on the real clock go in `tests/unit/quarantine.toml`.
- Dependencies through uv (Python) and npm in `web/` (pinned exact versions, as `web/package.json` does). No Docker.
- The plan keeps the chat in `web/` on Vue (spec). assistant-ui is not adopted.
- Layout (Noah's rule): plain layout; rich detail in hover cards on desktop and in a bottom sheet on a phone.
- The board, workflows, metrics and admin screens stay reachable on desktop and phone (top bar tabs, phone tab bar, and links from the nav).
- Transport copy is the spec's: header `x-vercel-ai-ui-message-stream: v1`; chunk types `start`, `text-*`, `reasoning-*`, `tool-input-*`, `tool-output-available`, `data-*` (engine and VM status, todos), `finish`; session id, kind, repository and branch travel in the request body.
- Context readout is tokens used from the `finish` usage against the engine's limit (262144 for Gemma).
- Status copy comes from `EngineStatusLine` ("Starting Gemma…", "Downloading …", "Waiting for memory: …", "… failed: …"; VM states add "building image", "cloning", "running setup"); no new wording is invented.
- `POST /v1/chat/completions`, `/v1/airlie/chat/attachments` and `/v1/airlie/chat/skills` stay.

## Review Focus

1. **A turn that waits for an engine or VM** (downloading, starting, waiting for memory). Expected: the header and an inline line say so with progress; the composer stays usable for a draft and Stop works. Test in Task 6 (`test_a_waiting_turn_shows_the_engine_line_inline`).
2. **A stream cut mid-turn** (network drop, phone sleep, serve restart). Expected: the partial reply stays, a "cut off" line with Retry appears, the open tool block shows "interrupted", and no permission prompt stays live. Test in Task 4 (`test_a_cut_stream_keeps_the_partial_reply_and_offers_retry`).
3. **A permission prompt left unanswered, then a reload or a second device.** Expected: the prompt is still there (it is part of the persisted thread) and answering it from either place resumes the turn once. Test in Task 6 (`test_a_pending_prompt_survives_a_reload`).
4. **A very long tool output or diff.** Expected: collapsed by default with a line count, the open block scrolls inside itself, the page does not grow sideways on a phone. Test in Task 6 (`test_a_long_output_is_collapsed_with_its_line_count`).
5. **A chat of a repository that was unpinned, deleted, or whose VM is gone.** Expected: its chats stay listed under "Other repositories", open read-only with the reason, and never vanish. Test in Task 5 (`test_chats_of_an_unpinned_repository_stay_listed`).

---

## File Structure

Create (names fixed; contents refined in Task 3):

| File | Responsibility |
|---|---|
| `web/src/api/sessions.ts` | `listSessions`, `createSession`, `deleteSession`, `listPins`, `pinRepository`, `unpinRepository`, `listRepositories` over the chat routes |
| `web/src/stores/sessions.ts` | `useSessions()`: sessions grouped by repository and plain, the current id, create/delete |
| `web/src/stores/pins.ts` | `usePins()`: pinned repositories, pin/unpin |
| `web/src/stores/turns.ts` | `useTurns()`: one `Chat` per session id, built with `DefaultChatTransport`; send, stop, retry, answer a prompt |
| `web/src/screens/chat/nav/ChatNav.vue`, `NavGroup.vue`, `NavDrawer.vue` | the left nav, a repository group, the phone drawer |
| `web/src/screens/chat/thread/MessageView.vue` | renders one message's `parts` |
| `web/src/screens/chat/thread/ToolBlock.vue`, `ReasoningBlock.vue`, `DiffBlock.vue`, `TodoList.vue`, `PermissionPrompt.vue` | one part type each |
| `web/src/screens/chat/ChatHeader.vue` | repository, branch, model, context used, `EngineStatusLine` |
| `docs/superpowers/mockups/2026-11-12-chat-redesign/{a,b,c}.html` + `look.css` | Task 1 |

Modify: `web/src/screens/ChatScreen.vue` (becomes the shell: nav + header + thread + composer), `Thread.vue`, `Bubble.vue`, `Composer.vue`, `StatusStrip.vue`, `web/src/stores/chat.ts` (legacy thread read and slash/attachment helpers stay; sending moves to `turns`), `web/src/router.ts` (`/chat/:session?`), `web/src/components/AppShell.vue`, `web/package.json`.

---

### Task 1: Three mockups for Noah to pick from

**Files:**
- Create: `docs/superpowers/mockups/2026-11-12-chat-redesign/a.html`, `b.html`, `c.html`, `look.css`, `README.md`

**Interfaces:**
- Consumes: the app's tokens and look (`docs/superpowers/mockups/2026-10-24-vue-app/look.css`, `chat.html`; the token block of `docs/superpowers/specs/2026-11-02-pipeline-matches-diagram-mockups.html`), the process in `prompts/roles/ux.md` ("Mockup directions": directions differ in how a person moves through the screen or how it is laid out, never colour or wording) and `prompts/roles/design.md` ("Mockup pages": one self-contained HTML page, inline CSS, no script, no link, no URL).
- Produces: three pages Noah compares side by side, each with the same fixtures so only the layout differs.

- [ ] **Step 1: Fix the directions** (each is a layout and a way to move, written at the top of its page):
  - **A, "Sidebar, one column"** (closest to Claude Code desktop): a fixed left nav (pinned repositories with their chats, a "New chat" under each, then "Chats"), a calm single-column thread, the header as one slim row over the thread. Tool blocks are collapsed rows; detail on hover (desktop) and a bottom sheet (phone).
  - **B, "Rail and panel"**: a narrow icon rail (repositories as avatars, chats, board, workflows, metrics, admin) and a slide-out list panel beside the thread; the header carries a status popover. Favours screen width and phone parity.
  - **C, "Header switcher"**: no persistent nav; the header's repository and chat switcher opens a sheet/popover listing pins and chats; the thread uses the full width. Smallest chrome; the board and others stay in the top bar.
- [ ] **Step 2: Fixtures, identical in all three.** A pinned repository `airlie` with two chats and a plain chat list of three; the open chat shows: a user message; a reasoning disclosure (collapsed); a `read_file` tool block (collapsed); a `shell` block (open, with 12 lines of output); an `edit_file` block with an inline diff; the todo checklist (2 of 4 done); a permission prompt ("Push session branch to origin?" Approve / Deny); an assistant reply; the composer. The header shows `airlie`, branch `chat/ab12`, model `Gemma`, `118k / 262k`, a status chip ("Starting a VM for airlie…" in one variant).
- [ ] **Step 3: Each page has two sections**, "Desktop" (1280 px) and "On a phone" (375 px; nav as a drawer or sheet, hover card replaced by the bottom sheet), plus a "States" strip: waiting for memory, downloading with progress, cut stream, failed engine. Light and dark both work (tokens from `look.css`).
- [ ] **Step 4: Check the pages**: they obey the mockup-page rules (no `<script>`, no `<link>`, no `@import`, no URL, no event-handler attributes). Run `grep -nE "<script|<link|@import|https?://|on[a-z]+=" docs/superpowers/mockups/2026-11-12-chat-redesign/*.html` → no output. Screenshot each at both widths with Playwright into the README (as `docs/bench/2026-11-02-pipeline-screen-check.md` did for its screens).
- [ ] **Step 5: README** lists the three directions in a table (layout, how you move, what it costs on a phone, which of the spec's parts it shows where) and what Noah is choosing between.
- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/mockups/2026-11-12-chat-redesign
git commit -m "docs: three chat redesign mockups for Noah to pick from"
```

Mockups can start while the engine and VM plans are built; the build waits for the pick and for the turn route (plan 3).

### Task 2: Noah picks (stop point)

**Files:**
- Modify: this plan (record the pick and the changes Noah asks for)

- [ ] **Step 1: Present the three** (link the pages and the README) and **stop**. No build task starts without Noah's explicit go in chat (his review-before-build rule).
- [ ] **Step 2: Record the pick** at the top of Task 3 ("Pick: X, with these changes: …") and re-present the refined plan if the pick changes a task's scope materially (for example B or C removes `ChatNav`/`NavDrawer` in favour of a rail or a switcher).

### Task 3: Refine the build tasks to the pick, and settle the backend they read

**Files:**
- Modify: this plan (Tasks 4-9); create `src/airlie/serve/chat_sessions.py` and `tests/functional/test_chat_sessions.py` only if plans 3-4 did not ship the routes below

**Interfaces:**
- Consumes (to confirm by reading the code as it stands, then name exactly): from plan 3, the turn route and the persisted-thread routes; from plan 4, the repository-chat session shape (repository, branch, VM state); from plan 1, `GET /v1/airlie/engines` and the VM status (the `Status` shape) and the feed's `engines` hint.
- Produces: the contract `web/src/api/sessions.ts` codes against, written into this plan.

- [ ] **Step 1: Read** the turn route, the session persistence and the status routes; list for each of these what exists and what is missing: list sessions (id, kind, repository, branch, title, updated), create session, delete session, per-person pinned repositories (list, pin, unpin), list the repositories that can be pinned, message history for a session (UI messages), answer a permission prompt, usage in `finish`.
- [ ] **Step 2: Write the missing routes** (functional tests first, per Global Constraints) under `/v1/airlie/chat/` and a feed hint `chat` per change. Pins are stored per person in the user store (decision: server side, so a phone and a laptop see the same nav; Noah to confirm).
- [ ] **Step 3: Refine Tasks 4-9** to the pick: exact component names, props, store signatures, and the tests per component state. Re-present to Noah if scope changed.
- [ ] **Step 4: Commit** the plan update (and routes).

### Task 4: Transport and the turns store on `@ai-sdk/vue`

**Files:**
- Create: `web/src/stores/turns.ts`, `web/src/api/sessions.ts`, tests beside each
- Modify: `web/package.json` (`ai`, `@ai-sdk/vue`, pinned exact), `web/src/stores/chat.ts` (the sessionStorage thread and today's `airlie.chat` format are read once into a "Legacy chat" plain session, then left)

**Interfaces:**
- Consumes: `POST /v1/airlie/chat/turn` (body: session id, kind, repository, branch, messages); `@ai-sdk/vue` `Chat`, `DefaultChatTransport`.
- Produces: `useTurns()` with `chatFor(sessionId: string): Chat<UIMessage>`, `send(sessionId, text, options)`, `stop(sessionId)`, `retry(sessionId)`, `answer(sessionId, promptId, approved: boolean)`, `usage(sessionId): { used: number; limit: number } | null`.

- [ ] **Step 1: Write the failing tests** (a fake SSE body through the fetch stub): a text stream renders as one assistant message with streamed text; a `finish` with usage sets `usage`; a 401 reaches the app's existing sign-in redirect; `test_a_cut_stream_keeps_the_partial_reply_and_offers_retry`; the request body carries session id, kind, repository and branch.
- [ ] **Step 2: Run them to see them fail** (`cd web && npx vitest run src/stores/turns.test.ts`).
- [ ] **Step 3: Implement** with one `Chat` per session id kept in a map; the transport's `prepareSendMessagesRequest` adds the session fields. The existing slash menu, skills and attachments (`attachments.ts`) keep their routes; `send` takes the same `SendOptions` shape as `chat.ts`.
- [ ] **Step 4: Run the tests and the gate** → PASS; `task web:gate` → exit 0.
- [ ] **Step 5: Commit** (`web: chat turns on the AI SDK UI message stream`).

### Task 5: The left nav (or the picked equivalent), pins, and the phone drawer

**Files:**
- Create: `web/src/stores/sessions.ts`, `web/src/stores/pins.ts`, `web/src/screens/chat/nav/ChatNav.vue`, `NavGroup.vue`, `NavDrawer.vue`, tests beside each
- Modify: `web/src/router.ts` (`/chat/:session?`, `meta.tab: "chat"`, `/` redirects to the last session), `web/src/components/AppShell.vue` and `tabs.ts` (nav links to Board, Workflows, Metrics, Admin on the chat route), `web/src/router.test.ts`

**Interfaces:**
- Consumes: Task 3's session and pin contract; `useSessions` and `usePins` are live through the feed.
- Produces: `useSessions()` with `grouped: { pinned: { repository, sessions }[]; other: Session[]; plain: Session[] }`, `currentId`, `create(kind, repository?)`, `remove(id)`; `usePins()` with `pinned`, `pin(repository)`, `unpin(repository)`.

- [ ] **Step 1: Write the failing tests:** each pinned repository lists its chats and "New chat" creates a repository session and opens it; "New chat" under Chats creates a plain one; pin and unpin move a repository between groups; `test_chats_of_an_unpinned_repository_stay_listed`; empty state ("Pin a repository to start a chat in a VM"); error with retry; on a phone width the nav is closed until the menu button opens it as a drawer, a focus trap holds, Escape and a chat pick close it; the nav has links to Board, Workflows, Metrics, Admin.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement** the stores and components (drawer on reka-ui `Dialog`; a row's detail on a hover card from reka-ui on desktop, and a bottom sheet on a phone, per the pick).
- [ ] **Step 4: Run the tests and the gate** → PASS.
- [ ] **Step 5: Commit** (`web: chat nav with pinned repositories and a phone drawer`).

### Task 6: The thread parts

**Files:**
- Create: `web/src/screens/chat/thread/MessageView.vue`, `ToolBlock.vue`, `ReasoningBlock.vue`, `DiffBlock.vue`, `TodoList.vue`, `PermissionPrompt.vue`, tests beside each
- Modify: `web/src/screens/chat/Thread.vue` (lists `MessageView`s; the story and job cards stay), `Bubble.vue`

**Interfaces:**
- Consumes: `Chat` messages' `parts` (`text`, `reasoning`, `tool-<name>` with `state` `input-streaming | input-available | output-available | output-error`, `data-todos`, `data-status`, `data-permission`); `useTurns().answer`.
- Produces: `MessageView` takes `{ message: UIMessage; sessionId: string }`; `ToolBlock` takes `{ part }` (collapsed row: tool name and a one-line summary; open: input and output, long output collapsed with its line count); `DiffBlock` takes `{ path: string; diff: string }` for `edit_file` and `write_file`; `TodoList` takes `{ items: { text: string; status: "pending" | "doing" | "done" }[] }`; `PermissionPrompt` takes `{ id: string; question: string; detail?: string }` and emits through `answer`.

- [ ] **Step 1: Write the failing tests, one per state:** a text part renders safe markdown (`SafeMarkdown`); a reasoning part is a closed disclosure that opens on click and is labelled; a tool block is collapsed, opens to show input and output, shows "running" while input is available and no output, shows the error on `output-error`, "interrupted" after a cut; `test_a_long_output_is_collapsed_with_its_line_count`; an edit part renders a diff with added and removed lines distinguished by more than colour (a `+`/`-` gutter and text); the todo list renders each status and updates in place as a new `data-todos` part replaces the old; a permission prompt shows Approve and Deny buttons by role, disables both after one is pressed, and `test_a_pending_prompt_survives_a_reload`; `test_a_waiting_turn_shows_the_engine_line_inline` (a `data-status` part renders through `EngineStatusLine`).
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement** one component per part type; unknown part types render nothing and log nothing (forward compatible); diffs are plain text rendering (no HTML injection).
- [ ] **Step 4: Run the tests and the gate** → PASS.
- [ ] **Step 5: Commit** (`web: thread parts: tool blocks, reasoning, diffs, todos, permission prompts`).

### Task 7: The header, and the context readout

**Files:**
- Create: `web/src/screens/chat/ChatHeader.vue`, `ChatHeader.test.ts`
- Modify: `web/src/screens/chat/StatusStrip.vue` (its content moves into the header; the strip stays only for what the header cannot show on a phone), `web/src/screens/ChatScreen.vue`

**Interfaces:**
- Consumes: `useEngines().forRole("chat")`, the VM status from the session, `useTurns().usage(sessionId)`, the session's repository, branch and model.
- Produces: `ChatHeader` takes `{ session: Session }`; the context readout is "118k / 262k" with `role="progressbar"` and `aria-valuenow`.

- [ ] **Step 1: Write the failing tests:** a repository session shows repository, branch, model, context and status; a plain chat shows no repository or branch; the context bar reaches a warning state at 90% of the limit; a status of `ready` shows nothing but the dot, `starting` and `downloading` show the `EngineStatusLine` text; the failed engine shows its reason; on a phone the header collapses to the title and a status dot, and a tap opens the bottom sheet with the full detail.
- [ ] **Step 2: Run them to see them fail.** **Step 3: Implement.** **Step 4: Tests and gate** → PASS.
- [ ] **Step 5: Commit** (`web: chat header with repository, branch, model, context and status`).

### Task 8: Assemble the screen, keep every other screen reachable, one e2e

**Files:**
- Modify: `web/src/screens/ChatScreen.vue` (nav + header + thread + composer; the composer, slash menu and attachments keep their behaviour), `web/src/components/TopBar.vue` and `TabBar.vue` (unchanged on other screens), `web/src/screens/ChatScreen.test.ts`, `web/src/router.test.ts`
- Create: `tests/e2e/test_chat_redesign.py`; delete or rewrite the checks in `tests/e2e/test_chat.py` that the redesign makes stale (a screenshot per check stays)

**Interfaces:**
- Consumes: Tasks 4-7.
- Produces: the finished screen at `/` and `/chat/:session`.

- [ ] **Step 1: Write the failing tests:** component: `/` opens the last session, a new profile opens a fresh plain chat with the greeting; the top bar and phone tab bar still reach Board, Workflows, Metrics and Admin from the chat route; an old `airlie.chat` thread appears as "Legacy chat". E2E (the one critical journey, fake turn route behind the server's test seam): open a pinned repository chat from the nav, send a message, see the tool block and the permission prompt, approve it, see the reply; the same at 375 px with the drawer.
- [ ] **Step 2: Run them to see them fail** (`task test:e2e -- -k chat_redesign` or the repo's e2e task from `task --list`).
- [ ] **Step 3: Implement** the assembly and remove the dead code (old send path in `chat.ts`, replaced bubbles) in the same commit.
- [ ] **Step 4: Run the tests and the full gate** → PASS.
- [ ] **Step 5: Commit** (`web: the redesigned chat screen`).

### Task 9: Docs and the live checks

**Files:**
- Modify: `README.md` (the chat section), `docs/superpowers/mockups/2026-11-12-chat-redesign/README.md` (mark the picked option)
- Create: `docs/bench/2026-11-1x-chat-redesign-check.md` (screenshots of the real screen at 1280 and 375 px, as `docs/bench/2026-11-02-pipeline-screen-check.md` did)

- [ ] **Step 1: Live checks** (outside the gate; Noah's go first): a real repository chat on this Mac (VM start shown in the header, a tool block, a diff, a permission prompt, a todo list, the context readout moving), a plain chat, the nav on Noah's iPhone as a drawer, and the engine waiting line with the reviewer cold.
- [ ] **Step 2: Write the bench doc and README text; commit** (`docs: chat redesign check`).

---

## Self-review

- **Spec coverage:** left nav with pinned repositories, their chats, new chat, plain list, pin/unpin, phone drawer (Task 5); transport on `@ai-sdk/vue` with `DefaultChatTransport` (Task 4); tool blocks, reasoning, diffs, todos, permission prompts (Task 6); header and context readout (Task 7); board/workflows/metrics/admin reachable (Task 8); mockups three options, Noah picks (Tasks 1-2); assistant-ui not adopted (Global Constraints); old routes stay (Global Constraints).
- **Placeholders:** Tasks 4-9 are deliberately a skeleton until Task 3 refines them to the pick; each already names files, interfaces and the tests per state.
- **Types:** `useTurns`, `useSessions`, `usePins`, `ChatHeader` signatures are used consistently across Tasks 4-8.
