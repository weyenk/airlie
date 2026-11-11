# airlie web chat agent (Approach C hybrid)

Status: agreed with Noah on 2026-11-11. Parent isolation model:
[`2026-10-16-remote-studio-design.md`](2026-10-16-remote-studio-design.md).

## Why

The Vue web app already owns navigation, board, jobs, and repository context in a Pinia shell. Embedding a
third-party chat shell (LibreChat or a full assistant-ui app) would fight that layout, duplicate auth and
routing, and make repo-scoped tools hard to gate. OpenCode on laptops must stay a first-class client without
rebuilding parity on day one.

Approach C keeps airlie's UI and product rules in the shell, and borrows assistant-ui and the Vercel AI SDK
only where they earn their keep: the message thread, streaming, and tool-call rendering.

## Goals

- One chat experience in the web app with two clear **modes** (general vs repo-scoped), enforced in the
  server and reflected in the composer and tool surface.
- Repo-scoped sessions run model-chosen commands in **isolated boxes** (Tart first-class), one box per chat
  session, aligned with the remote-studio isolation story.
- A single turn transport for the web (`POST /v1/airlie/chat/turn`) carrying `UIMessage` shapes the AI SDK
  expects, with lifecycle **hooks** for skills, policy, and logging.
- OpenCode continues to use `POST /v1/chat/completions` on serve; the Studio advertises itself on **:8090** as
  today.
- Human **confirmation** before destructive or expensive actions: shell, `jobs_start`, and story-driving tools
  in repo-scoped mode.

## Non-goals

- Embedding LibreChat or any other chat product in an iframe.
- OpenCode feature parity in v1 of the web agent (OpenCode remains the laptop front end).
- A dual-engine Gemma stack or a second inference path for chat (out of scope for this design).
- Compaction, a rich HUD, or long-horizon memory UX (phase 4).

## Anti-goals

- A model choosing shell, write, git, or `run_tests` when the user has not scoped the session to a repository.
- Repo-scoped command execution on the host macOS user without a box (except the documented seatbelt
  fallback, with an explicit unboxed warning).
- Two competing sources of truth for thread state (Pinia and a nested chat app both owning routes or session).

## Decisions

### Shape: Approach C hybrid (shell + thread library)

- **Pinia + Vue own:** layout, left nav, board and StoryCard entry points, repo picker, mode indicators,
  confirmation modals, attachment and skill pickers, and everything that is not the scrolling transcript.
- **assistant-ui + AI SDK own:** the `Thread` (messages, streaming deltas, tool-call parts, markdown/code
  blocks). No separate chat SPA, no iframe.
- **No LibreChat.** All chat UI lives in the existing `web/` app.

### Modes

| Mode | How the user gets there | Tools | Isolation |
|------|-------------------------|-------|-----------|
| **General** | Chat without a selected repository (home / global chat) | Skills and prose; optional web search/fetch when configured. No `shell`, `write`, `git`, or `run_tests`. | N/A (no repo commands) |
| **Repo-scoped** | Left-nav **+** on a repo, or opening chat from a board card / StoryCard with repo context | Full `tools.builtin` for that checkout, plus story and job tools where policy allows | Tart box per session (see below) |

Mode is not honor-system: the turn handler rejects disallowed tools for general chat. Repo context is carried
on the session (repository identity and ref), not inferred from prompt text alone.

### Confirmations (repo-scoped)

Before execution, the client shows a confirm step (and the server enforces the same policy) for:

- **shell** (any command the model proposes),
- **jobs_start** (starting studio work from chat),
- story tools that mutate pipeline state (move card, comment, start story workflow), as defined in the tool
  catalog.

General mode never offers these confirms because those tools are not registered.

### Isolation: Tart first-class for repo-scoped chat

Repo-scoped chat sessions follow the same trust boundary as remote studio jobs, adapted to **one box per chat
session** instead of one box per job:

- **Host (trusted):** `task serve`, models, credentials, orchestration, GitHub App publish, and the chat turn
  API.
- **Box (untrusted):** the session's checkout and every command the model runs through `subproc` / `agent_env`
  for that session.

Implementation sits on the existing seam where airlie already starts model-chosen processes; the box backend
is Tart as in [`2026-10-16-remote-studio-design.md`](2026-10-16-remote-studio-design.md).

- **Day one:** Linux boxes by default; macOS boxes later (queue when the host hits the two-VM cap).
- **Fallback:** if Tart is unavailable, seatbelt-style sandboxing may run commands with an **unboxed**
  warning in the thread. Repo-scoped chat must not silently run on the host without that warning.

### Hooks and skills bootstrap

The chat agent runtime exposes hooks the shell and server can rely on:

| Hook | Purpose |
|------|---------|
| `sessionStart` | Bind mode, repo, box id; load skills; emit system context |
| `beforeTurn` | Policy, attachment limits, mode guardrails |
| `afterTool` | Audit log, UI side-effects (job cards, story updates) |
| `onFinish` | Persist transcript slice, release box resources when the session ends |

On `sessionStart`, bootstrap from `skills/chat/SKILL.md` (and repo-local skills when repo-scoped) so every
thread starts from the same conversational contract.

### Transport

- **Web agent:** `POST /v1/airlie/chat/turn` accepts and returns `UIMessage`-compatible payloads (AI SDK),
  including streaming tool parts. Session id and mode travel in the request envelope the builder defines.
- **General chat + OpenCode:** `POST /v1/chat/completions` remains unchanged for the chief role, streaming SSE,
  and MCP clients. No requirement that OpenCode speak `UIMessage` in v1.

OpenCode's documented entry point stays **port 8090** on the Studio (serve URL + API key), alongside the web
app.

### Repo and shell integration (later phases)

Phase 3 wires repo-scoped chat into navigation the shell already has: left-nav repo **+**, board and
StoryCard deep links, shared Pinia stores for the active checkout, and library paths for attachments. Details
of each screen stay in the web-app specs; this design only requires that repo-scoped chat reuses the same repo
identity serve already uses for jobs and stories.

## Order of work (phases)

1. **Modes, hooks, and turn API** — general vs repo-scoped enforcement, `sessionStart` / `beforeTurn` /
   `afterTool` / `onFinish`, `POST /v1/airlie/chat/turn`, assistant-ui `Thread` wired to the AI SDK transport.
2. **Tart, tools, and confirms** — box per repo-scoped session, builtin tools in the box, confirmation flow for
   shell, `jobs_start`, and story tools.
3. **Shell, libs, and repo nav** — Pinia session store, repo **+**, board / StoryCard entry, attachment and
   skill surfaces aligned with existing `web/` API routes.
4. **HUD and compaction** — token budget UI, summarization, and long-thread ergonomics (explicitly deferred).

## Later, not in this effort

- Full OpenCode parity in the browser (plan approval UX, teleport, every MCP tool).
- macOS Tart boxes as the default for Apple-app repos (queued behind Linux default).
- Replacing `/v1/chat/completions` for OpenCode with the turn API.

## Left to the builder

Exact `UIMessage` envelope fields (session id, box id, stream markers); assistant-ui component boundaries
inside `web/`; seatbelt fallback mechanics; which story tools require confirm in v1; queue behavior when Tart
is at capacity.
