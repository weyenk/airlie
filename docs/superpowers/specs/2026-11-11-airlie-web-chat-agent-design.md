# Web chat agent, Gemma engines, and dynamic context

Status: agreed with Noah on 2026-11-11 (revised same day to merge Approach C and dynamic
context / engine lifecycle). Parent isolation model:
[`2026-10-16-remote-studio-design.md`](2026-10-16-remote-studio-design.md). Engine shape follows
[`2026-11-08-mlx-vlm-engine-design.md`](2026-11-08-mlx-vlm-engine-design.md) (`max_kv_size` is fixed at
process start; mlx-vlm does not resize KV at runtime).

## Why

The Vue web app already owns navigation, board, jobs, and repository context in a Pinia shell. Embedding a
third-party chat shell (LibreChat or a full assistant-ui app) would fight that layout, duplicate auth and
routing, and make repo-scoped tools hard to gate. OpenCode on laptops must stay a first-class client without
rebuilding parity on day one.

At the same time, chat and OpenCode depend on Gemma behind **`task serve` on :8090**, while the rest of the
studio keeps Qwen and Muse on their own mlx-vlm processes. Two KV budgets are needed for the same weights:
**131072** tokens for everyday turns and **262144** when a thread or role needs a long horizon. Loading both
Gemma processes at once does not fit the doctor's wired budget; the stack must **pin, hold, idle-unload, and
route** between two fixed-`max_kv_size` engines without a second HTTP proxy on :8100.

Approach C keeps airlie's UI and product rules in the shell, and borrows assistant-ui and the Vercel AI SDK
only where they earn their keep: the message thread, streaming, and tool-call rendering. Engine lifecycle,
admission, and the long-context router ship in the **same effort** so chat, OpenCode, and repo-scoped tools see
one consistent context story.

## Goals

- One chat experience in the web app with two clear **modes** (general vs repo-scoped), enforced in the
  server and reflected in the composer and tool surface.
- Repo-scoped sessions run model-chosen commands in **isolated boxes** (Tart first-class), one box per chat
  session, aligned with the remote-studio isolation story.
- A single turn transport for the web (`POST /v1/airlie/chat/turn`) carrying `UIMessage` shapes the AI SDK
  expects, with lifecycle **hooks** for skills, policy, and logging.
- **Engine lifecycle** for managed mlx-vlm processes: `pin`, `ensure`, `hold`, and `idle_unload_s` in config;
  unload means **stop the process** (not an in-process model swap). The default chat Gemma engine stays
  **pinned** at `max_kv_size: 131072`. The doctor's projection stays honest about weights, KV, prefix cache,
  and which engines are resident.
- **Dual Gemma** behind serve (:8090): QAT **8-bit** weights with MTP assistant, two engine entries —
  `gemma` at **131072** on port **8084** and `gemma_long` at **262144** on port **8085** — with **one hot at a
  time** on the chat path. A **hysteresis router** promotes to `gemma_long` when context pressure warrants it
  and demotes when safe; promotion runs **cold prefill** on the long engine after `ensure`. The **same router**
  drives both web chat and OpenCode UX (no separate long-context product surface).
- **Admission and QoS** that are soft, not chat-exclusive: headroom is computed on the fly from wired memory;
  when a peer cannot start, **delay or refuse** that peer — do **not** kill an active chat engine to make room.
  When interactive chat is active, scheduling **prefers chat** over background studio work; every knob is
  configurable.
- OpenCode remains first-class on **:8090** via `POST /v1/chat/completions`; roles advertise
  `limit.context: 128000` against the **128k** Gemma slot. Clients can read **`GET /v1/airlie/chat/context`**
  for `usage`, `effective_limit`, and `backend` (`gemma` vs `gemma_long`).
- Human **confirmation** before destructive or expensive actions: shell, `jobs_start`, and story-driving tools
  in repo-scoped mode.
- **HUD and compaction** in a late phase: token budget UI and summarization, backed by the same context endpoint
  and router signals.

## Non-goals

- Embedding LibreChat or any other chat product in an iframe.
- OpenCode feature parity in v1 of the web agent (OpenCode remains the laptop front end for studio/story).
- **bf16** Gemma weights in v1 (stay on **QAT 8-bit** + MTP as in config today).
- A **:8100** long-context HTTP proxy or any second front door beside serve on **:8090**.
- **Runtime `max_kv_size` resize** (mlx-vlm fixes KV at process start; see mlx-vlm engine design).
- A separate **chat-orchestrator** service or process outside serve and the existing engine registry.
- Hard **chat-exclusive** admission that preempts or kills other engines.

## Anti-goals

- A model choosing shell, write, git, or `run_tests` when the user has not scoped the session to a repository.
- Repo-scoped command execution on the host macOS user without a box (except the documented seatbelt
  fallback, with an explicit unboxed warning).
- Two competing sources of truth for thread state (Pinia and a nested chat app both owning routes or session).
- Admitting studio work by **stopping** the pinned chat Gemma or the engine an interactive user is waiting on.
- Advertising a 262k context window while `gemma_long` is cold and the effective backend is still `gemma`.

## Decisions

### Shape: Approach C hybrid (shell + thread library)

- **Pinia + Vue own:** layout, left nav, board and StoryCard entry points, repo picker, mode indicators,
  confirmation modals, attachment and skill pickers, HUD chrome, and everything that is not the scrolling
  transcript.
- **assistant-ui + AI SDK own:** the `Thread` (messages, streaming deltas, tool-call parts, markdown/code
  blocks). No separate chat SPA, no iframe.
- **No LibreChat.** All chat UI lives in the existing `web/` app.

### Engine lifecycle (dynamic context)

Managed engines gain explicit lifecycle verbs in config and in the engine registry:

| Verb | Meaning |
|------|---------|
| `pin` | Process should stay up across idle (chat Gemma default). |
| `ensure` | Start if down; wait until health and model loaded. |
| `hold` | Keep resident while a session or job references this engine. |
| `idle_unload_s` | After this many seconds with no holders, stop the process if not pinned. |

**Unload** always means **SIGTERM/SIGKILL via `engine/process.py`**, not mlx-vlm's in-process model swap (that
path is irrelevant when each process holds one model and one fixed `--max-kv-size`).

The doctor sums preloaded weights, per-engine `max_kv_size` KV, prefix-cache `cache_memory_gb`, and which
engines are actually running, against the wired limit — including the **one-hot** rule that only one of
`gemma` / `gemma_long` should be hot for chat at a time unless a migration window explicitly holds both.

Default pinning: the **`gemma`** engine (`max_kv_size: 131072`, port **8084**) is pinned for interactive chat.
**`gemma_long`** (`max_kv_size: 262144`, port **8085**) starts on demand and unloads on `idle_unload_s` when no
holder references it.

### Dual Gemma behind serve (:8090)

Both engines share the same **QAT 8-bit** snapshot and **MTP assistant** draft head (per `config/models.yaml`
and the existing `gemma` engine block in `airlie.yaml`). They differ only in **`max_kv_size`**:

| Engine | Port | `max_kv_size` | Role |
|--------|------|---------------|------|
| `gemma` | 8084 | 131072 | Default chat, design, ux, OpenCode roles on Gemma |
| `gemma_long` | 8085 | 262144 | Long threads after router promotion |

**One-hot on the chat path:** serve's chief/chat roles target **at most one** of these processes for
generation at a time. Switching backends stops using the demoted engine and routes new prefill/decode to the
promoted one.

**No :8100 proxy.** Long context is selected inside serve (router + engine registry), not by forwarding to
another HTTP port.

**Hysteresis router** (configurable thresholds, shared by web and OpenCode):

- **Promote** to `gemma_long` when estimated context (prompt + completion + attachments) crosses an **upper**
  bound below 131072 effective, or when the turn handler cannot fit the next message in `gemma`'s KV.
- **Demote** to `gemma` only when usage falls below a **lower** bound with margin (hysteresis avoids flapping).
- **Cold prefill on promote:** `ensure` `gemma_long`, run prefill for the retained transcript (and configured
  summary slice), then attach the session holder so `idle_unload_s` does not drop the engine mid-turn.

Studio router (`jev` / `chief`) for **specialist role picking** stays as today; this router is **only** for
Gemma **context backend** selection on the chat/completions path.

### Admission and QoS

Admission is **not** hard-exclusive for chat:

- On each start request (engine, team slot, or heavy peer), compute **free headroom** from the doctor's wired
  projection and currently running processes.
- If headroom is insufficient, **queue, delay, or refuse** the new peer with a structured reason. Do **not**
  terminate `gemma`, `gemma_long`, or another engine solely to satisfy a background job.
- When **interactive chat** is active (web turn in flight or OpenCode stream registered on serve), the
  scheduler **prefers** chat: background studio lanes back off within existing slot limits rather than
  competing for the last gigabyte beside Gemma.
- Knobs live under committed config (and `airlie.local.yaml` overrides): hysteresis bytes/tokens, `idle_unload_s`,
  pin defaults, chat-preferring weights, max wait before refuse.

### OpenCode, limits, and HUD

- **OpenCode** keeps `POST /v1/chat/completions` on **`http://127.0.0.1:8090/v1`** (unchanged transport).
- Role metadata exposes **`limit.context: 128000`** aligned with the 128k Gemma slot (not 262144 unless the
  client has promoted and `GET /v1/airlie/chat/context` reports `backend: gemma_long` with a higher
  `effective_limit`).
- **`GET /v1/airlie/chat/context`** (session id or OpenCode correlation id): returns current **`usage`**
  (prompt/completion/total as serve tracks them), **`effective_limit`** (131072 or 262144 per active backend),
  and **`backend`** (`gemma` | `gemma_long`). Web HUD and OpenCode-compatible clients poll or subscribe alongside
  the stream.
- Phase 6 adds **compaction** (summarize-and-continue) using the same endpoint so the UI does not guess.

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
- **General chat + OpenCode:** `POST /v1/chat/completions` remains unchanged for the chief role and Gemma roles,
  streaming SSE, and MCP clients. No requirement that OpenCode speak `UIMessage` in v1.

OpenCode's documented entry point stays **port 8090** on the Studio (serve URL + API key), alongside the web
app. Engine ports for Gemma remain **8084** and **8085**; other studio engines keep **8080**, **8081**,
**8082**, **8083** per base config.

### Repo and shell integration (later phases)

Phase 4 wires repo-scoped chat into navigation the shell already has: left-nav repo **+**, board and
StoryCard deep links, shared Pinia stores for the active checkout, and library paths for attachments. Details
of each screen stay in the web-app specs; this design only requires that repo-scoped chat reuses the same repo
identity serve already uses for jobs and stories.

## Configurable behavior

All new behavior is **config-driven**, merged in the usual order (`includes` → root → `airlie.local.yaml` →
topology profile). Expected keys (exact schema is for the builder):

- `engines.gemma` / `engines.gemma_long` — ports, `max_kv_size`, pin, `idle_unload_s`, MTP and cache fields.
- `chat.context_router` — promote/demote thresholds, hysteresis, cold-prefill policy.
- `chat.admission` — headroom margins, chat-preferring scheduler weights, max queue wait.
- `chat.modes` — tool allowlists for general vs repo-scoped (or reuse `tools` policy hooks).

No hard-coded memory floors in Python without a config override.

## Testing

Every change must pass the Airlie gate from the repository root, in order:

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q -n auto --maxprocesses 8
task web:gate
```

Tests that exceed **500 ms** setup+call+teardown or depend on the **real clock** belong in
[`tests/unit/quarantine.toml`](../../../tests/unit/quarantine.toml) with a `slow:` or `clock:` reason; CI sets
`AIRLIE_TEST_BUDGET_MS=500` and fails on new slow tests in the default run. Run quarantined tests with
`task test:quarantine`; they must still pass.

**Unit tests** for this effort (no real mlx-vlm, Tart, or GPU in the default gate):

- **Context router** — promote/demote hysteresis with injected token counts and fake engine handles.
- **Admission** — headroom calculations from stubbed doctor projections; refuse/delay paths; assert chat engines
  are never chosen as victims.
- **QoS** — chat-preferring scheduling with a fake clock and controllable slot releases.
- **Mode gating** — general vs repo-scoped tool registration and turn rejection.
- **Hooks** — `sessionStart` / `beforeTurn` / `afterTool` / `onFinish` invoked in order with test doubles.

Use **injected memory and fake clocks**; do not start real engine processes in unit tests. Integration and
live tests may hit real engines under `tests/live/` or quarantine, not in the default parallel unit job.

Config for tests should use **isolated fixtures** (temporary config trees), not the developer's
`airlie.local.yaml`. Public seams (`Engine` protocol, turn handler, router interface) must accept fakes without
network I/O.

## Order of work (phases)

| Phase | Scope |
|-------|--------|
| **0 — Lifecycle MVP** | `pin` / `ensure` / `hold` / `idle_unload_s`, honest doctor projection, `gemma` pinned at 131072, process stop = unload. |
| **1 — Modes, hooks, turn API** | General vs repo-scoped enforcement, hooks, `POST /v1/airlie/chat/turn`, assistant-ui `Thread` on AI SDK transport. |
| **2 — Dynamic router** | `gemma_long` at 262144 on 8085, one-hot chat path, hysteresis, cold prefill on promote, `GET /v1/airlie/chat/context`. |
| **3 — Tart, tools, confirms** | Box per repo-scoped session, builtin tools in the box, confirmation flow for shell, `jobs_start`, story tools. |
| **4 — Shell, libs, repo nav** | Pinia session store, repo **+**, board / StoryCard entry, attachments and skills aligned with `web/` routes. |
| **5 — Composer JIT** | Skills and policy injected per turn without duplicating thread ownership (composer integration as config allows). |
| **6 — HUD and compaction** | Token budget UI, summarization, long-thread ergonomics using `usage` / `effective_limit` / `backend`. |

## Later, not in this effort

- Full OpenCode parity in the browser (plan approval UX, teleport, every MCP tool).
- macOS Tart boxes as the default for Apple-app repos (queued behind Linux default).
- Replacing `/v1/chat/completions` for OpenCode with the turn API.
- bf16 Gemma snapshots or a third Gemma process beyond the 128k/262144 pair.

## Left to the builder

Exact `UIMessage` envelope fields (session id, box id, stream markers); assistant-ui component boundaries
inside `web/`; seatbelt fallback mechanics; which story tools require confirm in v1; queue behavior when Tart
is at capacity; numeric defaults for `chat.context_router` and `chat.admission`; whether promotion copies the
full transcript or a summary slice on cold prefill.
