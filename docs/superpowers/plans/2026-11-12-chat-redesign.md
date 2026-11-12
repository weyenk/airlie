# Chat redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The chat looks and moves like Claude Code desktop: under the app's own top bar, a closable chat panel with pinned repositories (each with its chats and a "new chat" that starts a VM session) and plain chats, a calm thread with collapsible tool blocks, a reasoning disclosure, inline diffs, a todo checklist and permission prompts, and a chat row with repository, branch, model, context used and a status chip for the engine and VM. The client runs on `@ai-sdk/vue` 4's `Chat` against `POST /v1/airlie/chat/turn`.

**Architecture:** Mockups come first (three directions, Noah picks; Tasks 1-2). After the pick, the Vue chat screen is rebuilt around one `Chat` instance per open session (`DefaultChatTransport` posting to the turn route), with the thread rendering each message's `parts` through one small component per part type. The panel, chat row and thread read shared Pinia stores (`engines` exists; `sessions`, `pins`, `turns` and `chatPanel` are new). Every screen, the chat included, keeps the existing top bar and phone tab bar (Noah's pick, B2). Task 3 recorded the pick, wrote the backend routes the screen reads and refined Tasks 4-9.

**Tech Stack:** Vue 3.5, Pinia, vue-router, reka-ui 2.11, Tailwind (as `web/` uses it), vitest 5, `ai` (AI SDK 7) and `@ai-sdk/vue` 4 (new), Playwright e2e.

**Spec:** [`docs/superpowers/specs/2026-11-11-airlie-web-chat-agent-design.md`](../specs/2026-11-11-airlie-web-chat-agent-design.md), phase 5: "Thread, transport, and the redesign" (plus "Two kinds of chat", "Status for the UI", "Mobile: a full PWA", "Order of work").

## Global Constraints

- Every task passes the gate from the repository root, in order: `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest -q -n auto --maxprocesses 8`, `task web:gate`.
- Tests follow `skills/qa/*.md`:
  - Web tests (vitest) follow `component.md`: render the real component tree, query by role or text, mock at the network boundary (`web/src/test-utils/fetch-stub.ts`), cover loading, error and empty states, never sleep, names that state what the user sees; one test per state of each component.
  - A backend route driven through `TestClient` goes in `tests/functional`: happy path, 401, 400 or 422, 404 and a dependency failure, asserting the body.
  - E2E (`tests/e2e`, Playwright, `pytestmark = pytest.mark.e2e`, a screenshot per check through `Evidence`) is only for the critical journey (Task 8): open a repository chat from the panel, send a turn, see a tool block, approve a permission prompt.
  - Fakes are rebuilt per test; every new test is seen red before the code is written; unit tests over 500 ms or on the real clock go in `tests/unit/quarantine.toml`.
- Dependencies through uv (Python) and npm in `web/` (pinned exact versions, as `web/package.json` does). No Docker.
- The plan keeps the chat in `web/` on Vue (spec). assistant-ui is not adopted.
- Layout (Noah's rule): plain layout; rich detail in hover cards on desktop and in a bottom sheet on a phone.
- The board, workflows, metrics and admin screens stay reachable on desktop and phone through the top bar tabs and the phone tab bar, which stay on the chat screen too (B2).
- Transport copy is the spec's: header `x-vercel-ai-ui-message-stream: v1`; chunk types `start`, `text-*`, `reasoning-*`, `tool-input-*`, `tool-output-available`, `data-*` (engine and VM status, todos), `finish`; session id, kind, repository and branch travel in the request body.
- Context readout is tokens used from the `finish` usage against the engine's limit (262144 for Gemma).
- Status copy comes from `EngineStatusLine` ("Starting Gemma…", "Downloading …", "Waiting for memory: …", "… failed: …"; VM states add "building image", "cloning", "running setup"); no new wording is invented.
- `POST /v1/chat/completions`, `/v1/airlie/chat/attachments` and `/v1/airlie/chat/skills` stay.

## Review Focus

1. **A turn that waits for an engine or VM** (downloading, starting, waiting for memory). Expected: the header and an inline line say so with progress; the composer stays usable for a draft and Stop works. Test in Task 6 (`test_a_waiting_turn_shows_the_engine_line_inline`).
2. **A stream cut mid-turn** (network drop, phone sleep, serve restart). Expected: the partial reply stays, a "cut off" line with Retry appears, the open tool block shows "interrupted", and no permission prompt stays live. Test in Task 4 (`test_a_cut_stream_keeps_the_partial_reply_and_offers_retry`).
3. **A permission prompt left unanswered, then a reload or a second device.** Expected: the prompt is still there (it is part of the persisted thread) and answering it from either place resumes the turn once. Test in Task 6 (`test_a_pending_prompt_survives_a_reload`).
4. **A very long tool output or diff.** Expected: collapsed by default with a line count, the open block scrolls inside itself, the page does not grow sideways on a phone. Test in Task 6 (`test_a_long_output_is_collapsed_with_its_line_count`).
5. **A chat of a repository that was unpinned, deleted, or whose VM is gone.** Expected: its chats stay listed under "Other repositories" in the panel, open with the reason the turn route gives (409 or 422), and never vanish. Test in Task 5 (`test_chats_of_an_unpinned_repository_stay_listed`).

---

## File Structure

Create (names fixed by Task 3, for the B2 pick):

| File | Responsibility |
|---|---|
| `web/src/api/sessions.ts` | `listSessions`, `createSession`, `getSession`, `deleteSession`, `stopSession`, `listPins`, `pinRepository`, `unpinRepository`, `listRepositories`, `toUIMessage` over the chat routes |
| `web/src/stores/sessions.ts` | `useSessions()`: sessions grouped by pinned repository, other repositories and plain; the current id; create/remove |
| `web/src/stores/pins.ts` | `usePins()`: pinned repositories, pin/unpin |
| `web/src/stores/chatPanel.ts` | `useChatPanel()`: the panel open or closed |
| `web/src/stores/turns.ts` | `useTurns()`: one `Chat` per session id, built with `DefaultChatTransport`; send, stop, retry, answer a prompt, usage, the waiting line |
| `web/src/screens/chat/panel/ChatPanel.vue`, `PanelRepository.vue`, `PanelChatRow.vue`, `PinPicker.vue`, `PanelSlideOver.vue` | the chat panel, a repository group, a chat row, the pin picker, the phone slide-over |
| `web/src/screens/chat/thread/MessageView.vue` | renders one message's `parts` |
| `web/src/screens/chat/thread/ToolBlock.vue`, `ToolDetail.vue`, `ReasoningBlock.vue`, `DiffBlock.vue`, `TodoList.vue`, `PermissionPrompt.vue`, `WaitingLine.vue`, `CutLine.vue` | one part or line each |
| `web/src/screens/chat/ChatHeader.vue`, `SessionStatus.vue`, `ContextMeter.vue` | the chat row, the status chip with its popover or sheet, the context readout |
| `docs/superpowers/mockups/2026-11-12-chat-redesign/{a,b,b2,c}.html` + `look.css` | Tasks 1-2 |

Modify: `web/src/screens/ChatScreen.vue` (becomes the shell under `AppShell`: panel + chat row + thread + composer), `Thread.vue`, `Bubble.vue`, `Composer.vue`, `StatusStrip.vue` (removed in Task 8), `web/src/components/EngineStatusLine.vue` (a `label` prop), `web/src/stores/chat.ts` (legacy thread read and slash/attachment helpers stay; sending moves to `turns`), `web/src/router.ts` (`/chat/:session?`), `web/package.json`. `TopBar.vue`, `TabBar.vue`, `tabs.ts` and `AppShell.vue` stay as they are.

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

**Pick: B2 (B with the current top nav), 2026-11-12.** Mockup: [`b2.html`](../mockups/2026-11-12-chat-redesign/b2.html), README row "B2. Top nav and panel". Noah: "that looks perfect". What the pick changes in this plan:

- The app's top bar (`TopBar.vue`), the phone tab bar (`TabBar.vue`, at 640 px and below), `tabs.ts` and `AppShell.vue` stay as they are on every screen, the chat included. There is no left nav, no icon rail, and no Board / Workflows / Metrics / Admin links in the chat screen: the top tabs carry them.
- Under the top bar, the chat screen is a closable **chat panel** (268 px: the pinned repositories, each with its chats and New, then "Chats" with New) beside the thread. The panel closes from its X or from the panel button at the left of the chat row; on a phone it is a **slide-over** (not a drawer component of its own) opened from that button, between the top bar and the tab bar.
- A slim **chat row** over the thread (46 px; 48 on a phone): the panel button, title, repository, branch, model, context, and a **status chip** that opens a popover (VM, engine, context, memory, branch). On a phone the popover is a bottom sheet, as the tool detail is.
- Thread parts as in all three mockups (Task 6).

Task 5 is therefore the chat panel (was: the left nav and its drawer), Task 7 gains the status popover, and Task 8 keeps the top bar and the tab bar reaching every screen.

**Step 1 (done 2026-11-12): what the backend had, and what was missing.**

| Need | Before this task | Now |
|---|---|---|
| List sessions | `GET /v1/airlie/chat/threads` gave `id, title, kind, updated` | adds `repo, base_ref, branch, created, running` |
| Create a session | none: a thread was made by its first turn, with a client id | `POST /v1/airlie/chat/threads` (server id; a refused first turn no longer drops it) |
| Delete a session | `DELETE /v1/airlie/chat/threads/{id}` (VM deleted with it) | unchanged, plus the feed hint |
| Pins per person | none | `GET /v1/airlie/chat/pins`, `PUT` / `DELETE /v1/airlie/chat/pins/{owner}/{name}`, in the user store (`auth.sqlite`, table `chat_pins`) |
| Repositories that can be pinned | `GET /v1/airlie/repos/names` (origins of `story.repo` and the checkouts under `story.repos_root`, and board `repo:` labels) | reused as is |
| Message history | `GET /v1/airlie/chat/threads/{id}` (stored parts, usage, interrupted, todos) | adds `branch`, `running`; a permission request is now kept in the parts |
| Answer a permission prompt | the next turn's `approvals` field, only with a new user message; the request was not kept in the thread | the one way stays `approvals` in the turn body; a turn may now carry `approvals` and no new user message (it resumes the last assistant message), and the request and its answer are kept in the thread |
| Usage in `finish` | `finish.messageMetadata.usage` = `{prompt_tokens, completion_tokens, cached_tokens, max_context}`; the stored assistant message keeps it | unchanged |
| Engine and VM status | `GET /v1/airlie/engines` (engines and VMs; feed topic `engines`); transient `data-engine` chunks while a turn waits | unchanged |
| Live updates of the chat list | none | feed topic `chat` |

**Step 2 (done 2026-11-12):** the routes above, in `src/airlie/chat/api.py`, `src/airlie/chat/pins.py`, `src/airlie/chat/threads.py`, `src/airlie/chat/turn.py`, `src/airlie/auth/store.py`; tests in `tests/functional/test_chat_sessions.py`, `tests/functional/test_repo_chat_turn.py` (the prompt kept and answered alone), `tests/functional/test_chat_turn_route.py` (`running`). Commit `chat: session routes for the redesign: …`.

**The contract `web/src/api/sessions.ts` codes against** (every route answers 401 `{"error": "not signed in"}` without a principal; another person's chat is 404 `{"error": "no such thread"}`, the same as one that is not there):

| Route | Body | Answer |
|---|---|---|
| `GET /v1/airlie/chat/threads?kind=plain\|repository` | | 200 `ThreadRow[]`, newest `updated` first |
| `POST /v1/airlie/chat/threads` | `{kind, repo?, base_ref?}` (`repo` is owner/name and required for `repository`, forbidden for `plain`) | 201 `ThreadRow`; 422 bad body; 503 repository chats not configured |
| `GET /v1/airlie/chat/threads/{id}` | | 200 `ThreadRow & {todos: TodoItem[], messages: StoredMessage[]}`; 404 |
| `DELETE /v1/airlie/chat/threads/{id}` | | 204; 404; 409 a turn is running, or its VM could not be deleted (`error` says which) |
| `POST /v1/airlie/chat/threads/{id}/stop` | | 204 (also when nothing runs); 404 |
| `POST /v1/airlie/chat/turn` | `{id, kind, messages, skill?, repo?, base_ref?, approvals?: {id, approved}[]}`: only the newest message is read; `repo`/`base_ref` only on a thread's first turn | 200 SSE (below); 404 no such thread; 409 a turn is running, or (an answer alone) no request of this chat waits for that answer, or the VM is unavailable (Tart or softnet missing, with the install hint); 422 bad body or unreadable repository; 400 too many attachments; 503 chat misconfigured |
| `GET /v1/airlie/chat/pins` | | 200 `{pins: string[]}` in the order pinned |
| `PUT /v1/airlie/chat/pins/{owner}/{name}` | | 204 (pinning again, in any case, keeps the first); 422 not owner/name |
| `DELETE /v1/airlie/chat/pins/{owner}/{name}` | | 204; 404 `{"error": "<repo> is not pinned"}`. Unpinning keeps the repository's chats |
| `GET /v1/airlie/repos/names` | | 200 `{repos: string[]}`: what the pin picker offers |
| `GET /v1/airlie/engines` | | 200 `EngineStatus[]` (`web/src/api/engines.ts`); a chat's VM is the row named `vm:<thread id>`, `kind: "vm"`, its `detail` "building image", "cloning <image> (its first use downloads it)", "starting VM" while `starting` |

```ts
type ThreadRow = { id: string; kind: "plain" | "repository"; title: string; repo: string | null;
  base_ref: string | null; branch: string | null /* "airlie/chat-<12 hex>" for a repository chat */;
  running: boolean /* a turn is in flight */; created: number; updated: number /* epoch seconds */ };
type Usage = { prompt_tokens: number; completion_tokens: number; cached_tokens: number; max_context: number };
type StoredMessage = { id: string; role: "user" | "assistant"; parts: StoredPart[]; created: number;
  usage: Usage | null; interrupted: boolean };
type TodoItem = { id: string; text: string; status: "pending" | "in_progress" | "done" };
type ApprovalData = { id: string; tool: string; arguments: Record<string, unknown>;
  answer?: "approved" | "denied" /* only in the stored part, once answered */ };
```

Stored parts (`StoredPart`): user `text` and `airlie_upload` (`{upload, name?}`); assistant `text`, `reasoning`, `step-start`, `tool-<name>` (`toolCallId`, `input` or `rawInput`, `state` one of `output-available | output-error | approval-requested | approval-responded`, `output` or `errorText`, and `approval: {id, approved?}` on a call that asked), and `data-approval` (`{type, id, data: ApprovalData}`, right after its tool part; also alone for a request that is no tool call, the image build). Not stored: `data-engine` (transient) and `data-todos` (the thread's `todos` hold the latest list).

The stream (header `x-vercel-ai-ui-message-stream: v1`): `start {messageId}`, `start-step`, `text-start|delta|end`, `reasoning-start|delta|end`, `tool-input-start`, `tool-input-available`, `tool-input-error`, `tool-output-available`, `tool-output-error`, `tool-approval-request {approvalId, toolCallId}`, `data-approval {id, data: ApprovalData}`, `data-engine {data: {engine, state, detail, progress}, transient: true}` (the engine's or the VM's row, `engine` = its name; only while the turn waits for it), `data-todos {data: TodoItem[]}`, `finish-step`, `error {errorText}`, `finish {messageMetadata: {usage: Usage}}`, `[DONE]`.

Answering a prompt: the client sends a turn with `approvals: [{id, approved}]` and, as its newest message, the assistant's (or none). The server marks the stored request answered (`approval-responded` / `data.answer`), tells the model the answer, and the turn goes on the same assistant message (its `start` names that message's id, so `Chat` continues it). A second answer to the same request, from another device or a second tap, is 409 and runs nothing. An answer sent with a new user message works as before.

Live: the feed topic `chat`, key = the thread id when a thread is made, a turn ends (whatever its end), its prompt is answered, or it is deleted; key `null` when the person's pins change. A hint carries no data: the panel refetches `GET /v1/airlie/chat/threads` and the pins, the open chat refetches its thread when it is not streaming.

Context readout: `used = usage.prompt_tokens` of the newest `finish` (or of the last stored assistant message after a reload): what the model last read; `limit = usage.max_context` (262144 for Gemma's chat role).

**Scope against the mockup, for Noah (not built; the screen shows what the API has):**

1. The popover's memory row ("41 of 64 GB in use") has no route: the manager's used and budget GB are not served. Built as: the row shows the manager's waiting reason when a resource waits for memory, else is left out. A `GET /v1/airlie/memory` (or the figures on `/v1/airlie/engines`) is a follow-up.
2. VM uptime ("up 14 min"), the engine's model id and size, and "3 commits ahead of main, not pushed" are not served either: the VM row shows its state, the engine row its name and state, the branch row the session branch and its base.
3. The session branch is `airlie/chat-<12 hex>`, not the mockup's `chat/ab12`.
4. New does not start the VM: the first turn does (the stream shows the VM's status while it starts), so an abandoned New costs nothing.
5. A prompt answered after serve restarted is 409 (the server keeps waiting requests in memory): the prompt shows the server's message and the person asks again.
6. Retry after a cut resends the user message; the server keeps it as a second user message in the thread, as it does today.

### Task 4: `api/sessions.ts`, the transport and the turns store on `@ai-sdk/vue`

**Files:**
- Create: `web/src/api/sessions.ts`, `web/src/api/sessions.test.ts`, `web/src/stores/turns.ts`, `web/src/stores/turns.test.ts`, `web/src/test-utils/ui-stream.ts` (a fake UI message stream body for the fetch stub)
- Modify: `web/package.json` and `package-lock.json` (`ai` 7.x and `@ai-sdk/vue` 4.x, exact versions)

**Interfaces:**
- Consumes: Task 3's contract.
- Produces:

```ts
// web/src/api/sessions.ts
export type ChatKind = "plain" | "repository";
export type { ThreadRow, Usage, StoredMessage, StoredPart, TodoItem, ApprovalData }; // as in Task 3
export type ThreadDetail = ThreadRow & { todos: TodoItem[]; messages: StoredMessage[] };
export type ChatMeta = { usage?: Usage; interrupted?: boolean; created?: number };
export type ChatData = { approval: ApprovalData; engine: EngineStatus /* the data-engine payload as a row */; todos: TodoItem[] };
export type AirlieUIMessage = UIMessage<ChatMeta, ChatData>;
export function listSessions(kind?: ChatKind): Promise<ThreadRow[]>;
export function createSession(kind: ChatKind, repo?: string, baseRef?: string): Promise<ThreadRow>;
export function getSession(id: string): Promise<ThreadDetail>;
export function deleteSession(id: string): Promise<void>;
export function stopSession(id: string): Promise<void>;
export function listPins(): Promise<string[]>;
export function pinRepository(repo: string): Promise<void>;   // PUT /v1/airlie/chat/pins/{owner}/{name}
export function unpinRepository(repo: string): Promise<void>;
export function listRepositories(): Promise<string[]>;        // GET /v1/airlie/repos/names
/** A stored message as the client's UI message: `airlie_upload` parts stay as they are (SentFile renders
 * them), usage and interrupted go to `metadata`. */
export function toUIMessage(m: StoredMessage): AirlieUIMessage;

// web/src/stores/turns.ts
export const useTurns: () => {
  /** The session's Chat, made once per id, holding its history from getSession. */
  chatFor(session: ThreadRow): Chat<AirlieUIMessage>;
  /** Refetch the history into the Chat (only while it is not streaming). */
  reload(id: string): Promise<void>;
  send(id: string, opts: SendOptions): Promise<void>;   // SendOptions as stores/chat.ts has it
  stop(id: string): Promise<void>;                      // POST .../stop, then the Chat stops reading
  retry(id: string): Promise<void>;                     // Chat.regenerate
  answer(id: string, approvalId: string, approved: boolean): Promise<void>;
  status(id: string): "ready" | "submitted" | "streaming" | "error";
  /** The turn's last cut: the stream ended without `finish` and not by Stop. */
  cut(id: string): boolean;
  /** The latest transient data-engine row of the turn in flight; null once it streams text or ends. */
  waiting(id: string): EngineStatus | null;
  usage(id: string): { used: number; limit: number } | null;
  /** The answer to an answer that the server refused (409), shown on the prompt. */
  refused(id: string, approvalId: string): string | null;
};
```

`DefaultChatTransport({ api: "/v1/airlie/chat/turn", prepareSendMessagesRequest })` sends `{id, kind, repo, base_ref, skill, approvals, messages: [newest]}` (repo and base_ref from the session row; `approvals` only from `answer`), with the key header from `authHeaders()`; a 401 calls `signalUnauthorized()`. `answer` sends through the same `Chat` with no new user message, so the reply streams onto the asked message. `onData` keeps the newest `data-engine` row per session for `waiting`. On a cut the store calls `reload`: while the row says `running` it waits for the `chat` hint with that id and reloads again; Retry is offered only when the saved thread ends in an interrupted or missing reply.

- [ ] **Step 1: Write the failing tests** (`sessions.test.ts`: each function's method, URL and body through `stubJson`; `toUIMessage` keeps the parts and moves usage to metadata. `turns.test.ts`, a fake UI message stream body through the fetch stub):
  - `test_a_text_stream_becomes_one_assistant_message_with_its_text`
  - `test_finish_usage_sets_the_context_used_and_its_limit`
  - `test_the_request_names_the_session_kind_repository_and_branch_base_and_sends_only_the_newest_message`
  - `test_a_401_from_the_turn_reaches_the_sign_in_redirect`
  - `test_a_cut_stream_keeps_the_partial_reply_and_offers_retry` (Review Focus 2: the partial text stays, `cut` is true, the open tool part reads as interrupted, no prompt stays live)
  - `test_a_cut_while_the_server_still_runs_waits_for_the_chat_hint_then_shows_the_saved_reply`
  - `test_answer_sends_the_approval_with_no_new_message_and_the_reply_continues_the_asked_message`
  - `test_an_answer_the_server_refuses_shows_its_reason_on_the_prompt` (409)
  - `test_a_data_engine_chunk_is_the_waiting_line_until_text_arrives`
  - `test_stop_posts_the_stop_route_and_ends_the_stream`
  - `test_a_reloaded_chat_shows_its_stored_history_and_its_last_usage`
- [ ] **Step 2: Run them to see them fail** (`cd web && npx vitest run src/api/sessions.test.ts src/stores/turns.test.ts`).
- [ ] **Step 3: Implement.** The slash menu, skills and attachments (`attachments.ts`) keep their routes; `send` takes `chat.ts`'s `SendOptions`.
- [ ] **Step 4: Run the tests and the gate** → PASS; `task web:gate` → exit 0.
- [ ] **Step 5: Commit** (`web: chat turns on the AI SDK UI message stream`).

### Task 5: The chat panel, pins, and the phone slide-over

**Files:**
- Create: `web/src/stores/sessions.ts`, `web/src/stores/pins.ts`, `web/src/stores/chatPanel.ts`, `web/src/screens/chat/panel/ChatPanel.vue`, `PanelRepository.vue`, `PanelChatRow.vue`, `PinPicker.vue`, `PanelSlideOver.vue`, a test beside each
- Modify: `web/src/router.ts` (`/chat/:session?` beside `/`, both `meta.tab: "chat"`), `web/src/router.test.ts`. `TopBar.vue`, `TabBar.vue`, `tabs.ts` and `AppShell.vue` do not change.

**Interfaces:**
- Consumes: Task 3's contract, Task 4's `api/sessions.ts`; `liveResource` (`stores/live.ts`) on topic `chat`; `Typeahead.vue` for the picker; reka-ui `Dialog` (the `ui/dialog` wrappers) for the slide-over.
- Produces:

```ts
// web/src/stores/sessions.ts (live on topic "chat")
export const useSessions: () => {
  rows: ComputedRef<ThreadRow[]>; loaded: ComputedRef<boolean>; error: Ref<Error | undefined>; refresh(): Promise<void>;
  /** Pinned repositories in pin order, each with its chats (none yet is fine); chats of repositories not
   * pinned under "Other repositories"; plain chats. Newest first inside each. */
  grouped(pins: string[]): { pinned: RepoGroup[]; other: RepoGroup[]; plain: ThreadRow[] };
  currentId: Ref<string | null>;
  create(kind: ChatKind, repo?: string): Promise<ThreadRow>;  // then the router opens /chat/<id>
  remove(id: string): Promise<void>;
};
export type RepoGroup = { repo: string; sessions: ThreadRow[] };
// web/src/stores/pins.ts (live on topic "chat")
export const usePins: () => { pinned: ComputedRef<string[]>; error: Ref<Error | undefined>;
  pin(repo: string): Promise<void>; unpin(repo: string): Promise<void> };
// web/src/stores/chatPanel.ts
export const useChatPanel: () => { open: Ref<boolean> /* desktop: remembered per browser in localStorage
  "airlie.chat.panel", default open; phone: closed until asked */ ; toggle(): void };
```

Components (props / emits):
- `ChatPanel` `{ currentId: string | null }`, emits `pick(id)`, `close()`. Header: "Chats" and the close X (desktop) ; then one `PanelRepository` per pinned repository, a "Pin a repository" control (`PinPicker`), "Other repositories" when any chat's repository is not pinned, and the "Chats" group with its New.
- `PanelRepository` `{ group: RepoGroup; currentId: string | null; pinned: boolean }`, emits `new(repo)`, `pick(id)`, `unpin(repo)`.
- `PanelChatRow` `{ session: ThreadRow; current: boolean }`: the title ("New chat" while empty), a second line "repo, branch" for a repository chat, a quiet dot while `running`; `aria-current="page"` when current.
- `PinPicker` emits `pin(repo)`: a `Typeahead` over `listRepositories()` that also takes any owner/name typed.
- `PanelSlideOver` `{ open: boolean }` (v-model): `ChatPanel` inside a left sheet between the top bar and the tab bar; focus is trapped, Escape, the scrim and a pick close it.

- [ ] **Step 1: Write the failing tests, one per state:**
  - `ChatPanel`: `test_each_pinned_repository_lists_its_chats_newest_first`; `test_new_under_a_repository_makes_a_repository_chat_and_opens_it` (POST body `{kind: "repository", repo}`, then the route is `/chat/<id>`); `test_new_under_chats_makes_a_plain_chat_and_opens_it`; `test_pinning_and_unpinning_move_a_repository_between_its_group_and_other_repositories`; `test_chats_of_an_unpinned_repository_stay_listed` (Review Focus 5: under "Other repositories", never gone); `test_with_no_pins_and_no_chats_it_says_pin_a_repository_to_start_a_chat_in_a_vm`; `test_a_failed_load_shows_the_error_with_retry`; `test_while_loading_it_shows_no_stale_rows`; `test_the_current_chat_is_marked_current`; `test_a_chat_hint_refetches_the_list`; `test_close_hides_the_panel_and_the_chat_rows_button_brings_it_back`.
  - `PanelChatRow`: `test_a_new_chat_reads_new_chat`; `test_a_repository_chat_shows_its_repository_and_branch`; `test_a_running_chat_shows_its_running_dot`.
  - `PinPicker`: `test_it_offers_the_repositories_airlie_knows_and_pins_the_one_picked`; `test_a_typed_name_that_is_not_owner_name_cannot_be_pinned`; `test_a_failed_lookup_still_takes_a_typed_name`.
  - `PanelSlideOver` (window 375 px): `test_on_a_phone_the_panel_is_closed_until_the_chat_rows_button_opens_it`; `test_focus_stays_inside_the_open_slide_over`; `test_escape_the_scrim_and_a_pick_close_it`.
  - `router.test.ts`: `test_chat_session_routes_light_the_chat_tab`; `test_slash_opens_the_last_chat_of_this_browser_or_a_fresh_one`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement** the stores and components. The last chat per browser is a convenience in `localStorage` (`airlie.chat.last`), wrapped in try/catch.
- [ ] **Step 4: Run the tests and the gate** → PASS.
- [ ] **Step 5: Commit** (`web: chat panel with pinned repositories and a phone slide-over`).

### Task 6: The thread parts

**Files:**
- Create: `web/src/screens/chat/thread/MessageView.vue`, `ToolBlock.vue`, `ToolDetail.vue`, `ReasoningBlock.vue`, `DiffBlock.vue`, `TodoList.vue`, `PermissionPrompt.vue`, `WaitingLine.vue`, `CutLine.vue`, a test beside each; `web/src/components/ui/hover-card/` (the reka-ui `HoverCard` wrappers, as `ui/tooltip` is)
- Modify: `web/src/screens/chat/Thread.vue` (lists `MessageView`s for a session; the story and job cards stay), `Bubble.vue` (the user's message only)

**Interfaces:**
- Consumes: `AirlieUIMessage` parts: `text`, `reasoning`, `step-start` (renders nothing), `tool-<name>` with `state` `input-streaming | input-available | approval-requested | approval-responded | output-available | output-error | output-denied`, `data-approval`, `data-todos`, `airlie_upload` and `file`; `useTurns().answer / waiting / cut / refused`; `EngineStatusLine`, `SafeMarkdown`, `SentFile`.
- Produces:
  - `MessageView` `{ message: AirlieUIMessage; sessionId: string; last: boolean }`: one component per part; of several `data-todos` parts only the newest renders; an unknown part type renders nothing and logs nothing.
  - `ToolBlock` `{ part: ToolUIPart; interrupted: boolean }`: a collapsed row (tool name, one-line summary: the path for file tools, the command for `shell`, the first line of the output when done); open: input and output, an output over 12 lines collapsed with "Show all N lines", scrolling inside the block. Desktop: hovering the row shows `ToolDetail` in a hover card; phone: a tap opens it as a bottom sheet.
  - `ToolDetail` `{ part: ToolUIPart }`: the call's input as key and value, its output, its state.
  - `ReasoningBlock` `{ text: string; streaming: boolean }`: a closed disclosure labelled "Reasoning".
  - `DiffBlock` `{ path: string; old?: string; new: string }`: `edit_file` (`old` to `new`) and `write_file` (all `new`), lines marked by a `+`/`-` gutter and text, never by colour alone, rendered as text.
  - `TodoList` `{ items: TodoItem[] }`: each of `pending`, `in_progress`, `done`, and "2 of 4 done".
  - `PermissionPrompt` `{ sessionId: string; approval: ApprovalData }`: the question for the tool (`push_branch`: "Push session branch to origin?", with the branch and its commits from `arguments`; `open_pr` and `build_image` from their arguments), Approve and Deny buttons; both disabled once one is pressed or once `answer` is set ("Approved" / "Denied"); the server's 409 reason in place of the buttons.
  - `WaitingLine` `{ status: EngineStatus }`: `EngineStatusLine` inline in the thread under the turn that waits.
  - `CutLine` `{ sessionId: string }`: "Cut off" with Retry.

- [ ] **Step 1: Write the failing tests, one per state:** a text part renders safe markdown; `test_reasoning_is_a_closed_disclosure_that_opens_on_click`; `ToolBlock`: `test_a_tool_row_is_collapsed_and_opens_to_its_input_and_output`, `test_a_call_with_input_and_no_output_reads_running`, `test_an_output_error_shows_the_error`, `test_an_open_call_after_a_cut_reads_interrupted`, `test_a_long_output_is_collapsed_with_its_line_count` (Review Focus 4: also no sideways scroll at 375 px), `test_hovering_a_row_on_desktop_shows_its_detail_card`, `test_tapping_a_row_on_a_phone_opens_its_detail_sheet`; `DiffBlock`: `test_an_edit_shows_removed_and_added_lines_with_a_gutter`, `test_a_diff_with_markup_renders_as_text`; `TodoList`: `test_each_status_renders_and_the_count_reads_two_of_four_done`, `test_a_newer_todos_part_replaces_the_older_in_place`; `PermissionPrompt`: `test_approve_and_deny_are_buttons_that_send_the_answer`, `test_both_buttons_are_disabled_after_one_is_pressed`, `test_a_pending_prompt_survives_a_reload` (Review Focus 3: from `getSession` parts, live; answered parts read "Approved" or "Denied"), `test_a_refused_answer_shows_the_servers_reason`; `test_a_waiting_turn_shows_the_engine_line_inline` (Review Focus 1); `test_a_cut_reply_shows_cut_off_with_retry`; `test_an_unknown_part_renders_nothing`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement** one component per part type.
- [ ] **Step 4: Run the tests and the gate** → PASS.
- [ ] **Step 5: Commit** (`web: thread parts: tool blocks, reasoning, diffs, todos, permission prompts`).

### Task 7: The chat row, the status chip and its popover, and the context readout

**Files:**
- Create: `web/src/screens/chat/ChatHeader.vue`, `SessionStatus.vue` (the chip and its popover or sheet), `ContextMeter.vue`, a test beside each
- Modify: `web/src/screens/chat/StatusStrip.vue` (removed once its content is in the chat row: the engine line and the memory wait move to `SessionStatus` and `WaitingLine`), `web/src/components/EngineStatusLine.vue` (an optional `label` prop: the name it says, so a VM reads "Starting a VM for airlie…" with its detail rather than its resource name)

**Interfaces:**
- Consumes: `useEngines().forRole("chat")` and `useEngines().byName("vm:" + session.id)`; `useTurns().usage(id)`; the `ThreadRow`; `useChatPanel().toggle`.
- Produces:
  - `ChatHeader` `{ session: ThreadRow | null }`: the panel button (`aria-label="Chats"`, `aria-expanded`), title ("New chat" while empty), and for a repository chat its repository and branch; the model (the chat engine's name); `ContextMeter`; `SessionStatus`. On a phone: the panel button, the title and the chip; repository, branch and model move into the sheet.
  - `ContextMeter` `{ used: number | null; limit: number | null }`: "118k / 262k", `role="progressbar"` with `aria-valuenow`/`aria-valuemax`; warning at 90 % of the limit; nothing before the first `finish`.
  - `SessionStatus` `{ session: ThreadRow }`: a chip (a dot and a word: nothing but the dot when everything is ready; the `EngineStatusLine` text while the engine or the VM starts, downloads or waits; the failure while one failed); a click opens a popover (desktop) or a bottom sheet (phone) with rows VM (repository chat only: its state and `detail`), engine (name and state, the download progress), context (used of limit), memory (the waiting reason while one waits), branch (the session branch and its base).

- [ ] **Step 1: Write the failing tests:** `ChatHeader`: `test_a_repository_chat_shows_its_repository_branch_model_context_and_status`, `test_a_plain_chat_shows_no_repository_or_branch`, `test_the_panel_button_toggles_the_panel_and_says_so`, `test_on_a_phone_the_row_keeps_the_title_and_the_chip`; `ContextMeter`: `test_it_reads_used_of_limit_as_a_progressbar`, `test_it_warns_at_ninety_percent`, `test_before_any_finish_it_shows_nothing`; `SessionStatus`: `test_ready_shows_only_the_dot`, `test_a_starting_engine_and_a_downloading_one_show_the_engine_line`, `test_a_starting_vm_says_starting_a_vm_for_the_repository_and_its_step`, `test_a_failed_engine_shows_its_reason`, `test_the_chip_opens_the_popover_with_vm_engine_context_memory_and_branch`, `test_a_plain_chats_popover_has_no_vm_or_branch_row`, `test_on_a_phone_the_chip_opens_a_bottom_sheet`.
- [ ] **Step 2: Run them to see them fail.** **Step 3: Implement.** **Step 4: Tests and gate** → PASS.
- [ ] **Step 5: Commit** (`web: chat row with repository, branch, model, context and the status popover`).

### Task 8: Assemble the screen, keep every other screen reachable, one e2e

**Files:**
- Modify: `web/src/screens/ChatScreen.vue` (under `AppShell`: `ChatPanel` or `PanelSlideOver`, then `ChatHeader`, the thread and `Composer`; the composer, slash menu and attachments keep their behaviour), `web/src/screens/ChatScreen.test.ts`, `web/src/router.test.ts`, `web/src/stores/chat.ts` (its send path goes; the slash and attachment helpers stay; the old `airlie.chat` sessionStorage thread is shown once, read-only, as "Legacy chat" in the panel's Chats group)
- Create: `tests/e2e/test_chat_redesign.py`; delete or rewrite the checks in `tests/e2e/test_chat.py` the redesign makes stale (a screenshot per check stays)

**Interfaces:**
- Consumes: Tasks 4-7.
- Produces: the finished screen at `/` and `/chat/:session`.

- [ ] **Step 1: Write the failing tests:** component: `test_slash_opens_the_last_session`, `test_a_new_profile_opens_a_fresh_plain_chat_with_the_greeting`, `test_the_top_bar_tabs_and_the_phone_tab_bar_reach_board_workflows_metrics_and_admin_from_a_chat` (the chat tab is current on `/chat/<id>`), `test_an_old_airlie_chat_thread_appears_as_legacy_chat_read_only`, `test_opening_another_chat_keeps_the_first_ones_stream_running`. E2E (the one critical journey; fake turn route behind the server's test seam): open a pinned repository's chat from the panel, send a message, see the tool block and the permission prompt, approve it, see the reply; the same at 375 px through the slide-over.
- [ ] **Step 2: Run them to see them fail** (`task test:e2e -- -k chat_redesign`, or the e2e task `task --list` names).
- [ ] **Step 3: Implement** the assembly and remove the dead code (the old send path in `chat.ts`, replaced bubbles, `StatusStrip.vue`) in the same commit.
- [ ] **Step 4: Run the tests and the full gate** → PASS.
- [ ] **Step 5: Commit** (`web: the redesigned chat screen`).

### Task 9: Docs and the live checks

**Files:**
- Modify: `README.md` (the chat section: the panel, pins, the status chip), `docs/superpowers/mockups/2026-11-12-chat-redesign/README.md` (mark B2 as picked)
- Create: `docs/bench/2026-11-1x-chat-redesign-check.md` (screenshots of the real screen at 1280 and 375 px, as `docs/bench/2026-11-02-pipeline-screen-check.md` did)

- [ ] **Step 1: Live checks** (outside the gate; Noah's go first): a real repository chat on this Mac (New under a pinned repository, the VM start in the chip and inline, a tool block, a diff, a permission prompt answered from the phone while the laptop shows it, a todo list, the context readout moving), a plain chat, the panel on Noah's iPhone as a slide-over, and the engine waiting line with the reviewer cold.
- [ ] **Step 2: Write the bench doc and README text; commit** (`docs: chat redesign check`).

---

## Self-review

- **Spec coverage:** the chat list with pinned repositories, their chats, new chat, plain list, pin/unpin, and the phone slide-over (Task 5; the spec's "left nav" became B2's panel under the top bar, Noah's pick); transport on `@ai-sdk/vue` with `DefaultChatTransport` (Task 4); tool blocks, reasoning, diffs, todos, permission prompts (Task 6); header, status and context readout (Task 7); board/workflows/metrics/admin reachable through the unchanged top bar and tab bar (Task 8); mockups, Noah's pick (Tasks 1-2); the backend routes the screen reads (Task 3); assistant-ui not adopted (Global Constraints); old routes stay (Global Constraints).
- **Placeholders:** none in Tasks 4-9; the component bodies are left to the build, their props, store signatures and per-state tests are fixed here.
- **Types:** `ThreadRow`, `AirlieUIMessage`, `useTurns`, `useSessions`, `usePins`, `useChatPanel`, `ChatHeader` and `SessionStatus` are used consistently across Tasks 4-8; `TodoItem.status` is the server's `pending | in_progress | done`.
