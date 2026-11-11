# Web chat agent, on-demand models, and long context

Status: agreed with Noah on 2026-11-11; revised the same day after a fact check against the code and the
upstream docs (mlx-vlm 0.7.12, MLX 0.32.4, huggingface_hub 2.2.0, AI SDK 7, assistant-ui, Tart), the design
review at the end of this file, and Noah's answers. The revision replaces the two-Gemma router with one Gemma
engine at 262144, makes every engine start and stop on demand, adds download progress, splits chat into plain
chats and repository chats that run in a VM, and adds superpowers. Parent isolation model:
[`2026-10-16-remote-studio-design.md`](2026-10-16-remote-studio-design.md). Engine shape:
[`2026-11-08-mlx-vlm-engine-design.md`](2026-11-08-mlx-vlm-engine-design.md).

## Why

Noah wants airlie chat to be a first-class part of the app. Plain chat should work like most web chats. A chat
started from a repository should work like Cursor's and Claude's cloud agents: it spins up a VM with the
repository in it, and he develops there outside the story workflow, with the superpowers skills he likes the
results of, in a chat that looks and feels like Claude Code desktop, with repositories pinned in a left nav. Two
things get in the way today:

- **Models are either always up or not there.** `task up` starts every autostart engine and nothing stops them;
  a role whose engine is down gets an error (serve's chat route does not catch the connection failure, so it most
  likely surfaces as a 500). Noah's words: models should spin up and down on demand, and the user should not have
  to care whether one is running. When a model is not downloaded that is too slow to hide, so the user sees that
  it is downloading and how far along it is.
- **Sometimes 128k context is enough and sometimes 256k is needed.** The first draft solved this with two Gemma
  processes and a router that swapped between them. The fact check showed that is unnecessary (below): one Gemma
  engine at 262144 costs memory only for the context a thread actually uses.

Chat today is prose only (`roles.chat.tools: []`), its skills are slash commands pasted into the prompt, and the
one tool-using skill (`lhr-publish`) runs a non-streamed loop on another role. Development needs tools, a
repository, an isolated place to run commands, and the superpowers flow.

## Goals

1. **Every model on demand.** A model manager inside `task serve` starts an engine when a request needs it,
   waits until it is ready, keeps it while work holds it, and stops it after it has been idle for its
   `idle_unload_s`. Engines may be pinned to stay up. Callers (serve's chat route, the story line, the studio
   roles) never see a down engine; they wait for it, and the UI says it is starting.
2. **Downloads with progress.** A model not on disk is downloaded by airlie at its pinned revision, with byte
   progress the web app shows (size, done, rate, time left), then started. Nothing silently blocks for minutes.
3. **One Gemma engine for 128k and 256k.** `gemma` serves chat at `max_kv_size: 262144`; a 128k thread costs
   what a 128k thread costs.
4. **Plain chat works like most web chats.** No repository, no VM: conversation, attachments, web search and
   fetch, and skills.
5. **Repository chats run in a VM.** Starting a chat from a pinned repository spins up a Tart Linux VM for that
   session with the repository cloned in it, like a cloud agent. Every model-chosen command runs in the VM. The
   chat streams tool calls and results and asks before actions that leave the VM.
6. **Superpowers in chat.** The superpowers skills run in airlie chat as they do in Claude Code: bootstrapped at
   session start, loaded on demand by a skill tool, with todos and subagents.
7. **Looks and feels like Claude Code desktop.** Repositories pinned in a left nav with their chats under them,
   plain chats beside them, and a thread in that look and vibe: mockups first, Noah picks, then build (airlie's
   web work is mockups-first).
8. **The doctor stays honest** about what can be resident at once (models and VMs), and the manager enforces the
   same budget at run time.

## Non-goals

- OpenCode work. `POST /v1/chat/completions` keeps working as it does; nothing new is built for OpenCode, and
  this effort may make it unnecessary.
- Embedding LibreChat or any other chat product.
- bf16 Gemma (Noah 2026-11-11: stay on QAT 8-bit).
- Host worktrees for chat: a repository chat's checkout lives in its VM.
- macOS VMs in this effort (Linux first; macOS VMs for Apple-app repositories later).
- Sharing weights between processes (MLX 0.32.4 cannot; with one engine per model nothing needs to).
- Long-horizon memory and cross-session recall.

## Anti-goals

- A user (or a role) getting an error because a model is not running.
- A download, a cold start, or a VM boot that looks like a hang: no progress, no status.
- The manager stopping an engine or a VM that a request or a held session is using, to make room for something
  else.
- A plain chat needing a repository or a VM.
- Model-chosen commands from chat running on the host at all.
- A push or merge credential inside a VM.
- Two sources of truth for a thread (the Vue store and a chat library both owning session or routes).
- Editing the superpowers skill bodies (the porting guide asks ports not to).

## Decisions

### One Gemma engine at 262144

Verified in the installed mlx-vlm 0.7.12: Gemma 4 builds its own cache (`models/gemma4/language.py`
`make_cache`): a growing `KVCache` for its 5 full-attention layers, allocated 256 tokens at a time, and a
1024-token `RotatingKVCache` for its 25 sliding layers. For Gemma, `--max-kv-size` reserves nothing; it is the
server's context budget check (`server/generation.py` `_check_configured_context_budget`), and it can even be
patched live (`server/runtime_config.py` lists it as a live knob). At 8-bit KV a full 262144-token thread holds
about 3 GB; a 128k thread about 1.5 GB.

So: one `gemma` engine, `max_kv_size: 262144`, the `chat` role's `max_context: 262144`. No `gemma_long`, no
promote/demote router, no cold prefill, no one-hot rule. Long threads decode slower than short ones because
attention reads more; that is the only cost.

**Weights: QAT 8-bit** (`mlx-community/gemma-4-26B-A4B-it-qat-8bit`, 28.0 GB) with its QAT assistant in bf16
(`gemma-4-26B-A4B-it-qat-assistant-bf16`, 0.87 GB) as the MTP draft head, as on the doctor branch. Noah
2026-11-11: stay on 8-bit. bf16 (52.5 GB) was weighed: about 24 GB more for a quality difference QAT is trained to
remove, and about half the decode speed (decoding reads twice the bytes per active parameter).

**Risk to validate first:** mlx-vlm's README documents Gemma 4 MTP (`--draft-kind mtp --draft-block-size 4`)
only for the bf16 body with the bf16 assistant. The QAT pairing is unlisted, and the QAT assistant's config says
`max_position_embeddings: 131072` (the body and the non-QAT assistant say 262144). The first task checks draft
acceptance at short and long context; if MTP misbehaves past 128k, MTP is turned off for that request range or
the engine runs without MTP, not bf16.

### The model manager

A new component in serve owns every `mlx_vlm` engine's process, and every chat VM (below). It builds on
`engine/process.py`, which already starts an engine detached, waits for `/health` and `/v1/models`
`loaded: true` (mlx-vlm preloads in its lifespan, so a 200 means ready), and stops it with SIGTERM then SIGKILL.

| Concept | Meaning |
|---|---|
| `lifecycle: pinned` | Started when serve starts, never stopped for idleness. |
| `lifecycle: on_demand` | Started by the first request that needs it; stopped after `idle_unload_s` with no holders. |
| `ensure(engine)` | Start if down (downloading first if the snapshot is missing), wait until ready. Concurrent callers share one start. |
| `hold(engine, owner)` / release | A request, a chat session with an open turn, or a story layer keeps the engine resident. |
| `idle_unload_s` | Seconds without holders before an on-demand engine stops. |

- **Where it hooks in.** Every path to an engine goes through it: serve's `upstream()` (the chat route and
  `/v1/chat/completions`), and the orchestrator's `HTTPEngine` / `RoutedEngine` for the story line and studio
  roles. A caller asks for a role's engine, the manager ensures and holds it, the caller releases when done.
- **Budget.** Before starting an engine or a VM the manager adds its projected footprint (for an engine, the
  doctor's arithmetic: weights, MTP head, one live sequence at its longest role context, prefix cache; for a VM,
  its configured memory) to what is running and compares with the limits. If it does not fit, it stops idle
  on-demand engines and idle VMs, least recently used first. If it still does not fit, the request waits with a
  visible status ("waiting for memory: reviewer is running a review") and starts when a holder releases. It never
  stops anything with a holder.
- **Unload means stopping the process.** mlx-vlm has an in-process swap (a request naming another model unloads
  the current one; `POST /unload`), but stopping the process is the only way to return all Metal memory, and
  airlie keeps one model per process. Serve pins the `model` name it sends so a request never triggers the swap.
- **Defaults** (Noah 2026-11-11: the reviewer does not need to stay warm; the rest are Claude's call):

  | Engine | Lifecycle | Why |
  |---|---|---|
  | `main` (generalist) | pinned | the chief and every studio role |
  | `decision` (decider-2b) | pinned | small, and the router asks it on every route |
  | `gemma` | pinned | interactive chat should answer at once |
  | `small` | on_demand | summarizer, judge, hypothesizer |
  | `reviewer` (Muse) | on_demand | reviews are occasional |

  `idle_unload_s` defaults to 600. All of it is config.
- **Prefix cache across restarts.** An on-demand engine loses its in-memory prefix cache when it stops.
  mlx-vlm's APC has a warm-disk tier that persists prefixes and survives a restart; the builder decides whether
  to turn it on for on-demand engines.
- **Owner.** `task serve` owns the manager. `task up` keeps working by starting the pinned engines; `airlie
  engine up NAME` starts any engine by hand and the manager adopts a running engine it finds on the configured
  port. A one-off CLI run (`task run`) without serve ensures engines itself and leaves them for serve to stop.

### Downloads with progress

mlx-vlm reports no download or load progress (its `snapshot_download` output goes to its log), so airlie
downloads before it starts the server:

- `huggingface_hub.snapshot_download(repo, revision=<pinned sha>, tqdm_class=…)` drives two aggregate byte bars
  ("Downloading bytes", "Reconstructing") in 2.2.0; a `dry_run=True` call first gives the total size and what is
  already cached. hf_xet is the transfer path (`hf_transfer` is deprecated).
- `task models:pull` moves from shelling out to `hf download` onto the same function, so the CLI and serve share
  one downloader.
- Serve keeps one download per snapshot; progress is published as an event the web app shows wherever the model
  is waited on (the chat thread, a story card). Body and MTP head download together.
- A failed download is a visible error with a retry, not a hang. Disk space is checked against the dry-run size
  first.

### Status for the UI

Serve exposes each engine's and VM's state (`stopped`, `downloading` with progress, `starting`, `ready`,
`waiting for memory` with the reason, `stopping`, `failed` with the reason; for a VM also `building image`,
`cloning`, `running setup`) as a GET and as server events. The chat shows it inline when a turn waits ("Starting
Gemma…", "Downloading Muse Glimmer: 7.2 of 19.4 GB, 3 min left", "Starting a VM for airlie…"). The Admin or
Metrics tab can show the full table. The user never has to start anything.

### Two kinds of chat

| | Plain chat | Repository chat |
|---|---|---|
| Started from | "New chat" in the left nav | a pinned repository in the left nav (or "open in chat" on a board card) |
| Runs | on the host, like most web chats | in a Tart Linux VM for that session |
| Tools | web search and fetch (when configured), `skill`, `todo`, attachments | the builtin tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `grep`, `shell`, `run_tests`, `git`), `compute`, `skill`, `todo`, `task`, and host-side push / PR |
| Repository, VM | none | the repository cloned in the VM on a session branch |

The server enforces the kind: the turn handler registers and accepts only that kind's tools, and a plain chat
never gets a file or shell tool. The superpowers bootstrap goes into both kinds: brainstorming works without a
repository, and skills that need files fall back to their inline wording in a plain chat (Claude's call).

### Repository chats: a VM per session

Repository chats follow the remote-studio trust boundary, with one VM per chat session instead of one per job:

- **Host (trusted):** serve, the models, the model manager, credentials, and publishing with the GitHub App.
- **VM (untrusted):** the session's clone and every command a model chooses: shell, tests, git, edits. The tools
  run in the VM through the seam where airlie already starts model-chosen processes (`subproc` / `agent_env`,
  `tools/shell.py`); a VM backend sits behind it, as remote studio planned.
- **No host worktree.** The clone lives on the VM's disk, so chat needs no git worktrees on the host (Noah
  wondered whether the VM removes that need: it does for chat; the story line keeps its own).
- **Getting the code in and out.** The host fetches the repository with its credentials and hands the VM a
  checkout at the chosen branch; the VM never holds a key that can push or merge. A push or PR is a host-side
  tool: the host fetches the session branch from the VM and pushes it with the GitHub App, after the user
  confirms.
- **The environment.** The repository's recipe (`.airlie/environment.json`, from remote studio: base image,
  setup script, variables) builds a cached image; each session starts from a fresh copy. Setup secrets are present
  only while setup runs. Network is off after setup by default, with an allow list for package registries. A
  repository without a recipe gets a default Linux image.
- **Lifecycle.** The model manager owns VMs the way it owns engines: started when the chat is opened, held while a
  turn runs, stopped after an idle period with its disk kept so the session resumes where it was, and deleted
  with the chat. Its RAM counts in the budget.
- **Linux first.** Linux guests have no framework cap (the two-VM limit is macOS guests only, from the macOS
  EULA); macOS VMs for Apple-app repositories come later and queue at two. Tart is free under its Fair Source
  licence on a personal workstation.
- **No host fallback.** Without Tart, repository chat says so and how to install it; it does not run commands on
  the host (Claude's call). Today's seatbelt confinement (`tools/confine.py`) stays for the story line.
- **Confirmations.** The client asks before pushes and PRs, starting studio work, and story tools that change the
  pipeline; the server enforces the same list. Commands and edits inside the VM do not ask by default, since the
  VM is the boundary (Claude's call); which tools ask is config.

### Superpowers in chat

The superpowers plugin (MIT, Jesse Vincent) is built to be ported: skills name actions ("invoke a skill",
"dispatch a subagent", "create a todo"), and each harness ships a small mapping from those actions to its tools
(`skills/using-superpowers/references/*-tools.md`; Hermes's is the closest shape to airlie). airlie becomes one
more harness:

- **Vendored, pinned, unedited.** A pinned copy of the superpowers skills lives in the repo with its LICENSE,
  updated deliberately. airlie adds an `airlie-tools.md` mapping beside them, not edits.
- **Bootstrap.** Every chat session's system prompt starts with `using-superpowers` and the airlie mapping
  (about 1–2k tokens), injected automatically (the porting guide's one hard requirement). Subagents skip it.
- **Tools to add:**
  - `skill(name)`: returns a skill's `SKILL.md`, and its sibling files on request; the session prompt lists each
    skill's `description:`.
  - `todo`: a per-session task list the thread renders as a checklist.
  - `task` (subagent, repository chats): runs a fresh-context tool loop on a chosen role with a prompt, in the
    same VM, and returns its final text; used by subagent-driven-development and parallel dispatch. It holds that
    role's engine through the manager, so a subagent on the reviewer starts Muse on demand.
- **airlie's own chat skills** (`skills/chat/*.md`, the slash commands with `summary:` frontmatter) stay as slash
  commands. Picking one from the slash menu still injects it.
- **Context.** A brainstorm → plan → subagent-driven run loads about 15–20k tokens of skill text; subagents keep
  the controller's context small, and 262k leaves room for diffs and test output.
- **Visual companion** (the brainstorming skill's local browser server) is optional and later.

### Thread, transport, and the redesign

- **Vue owns the screen.** The chat stays in `web/` (Vue 3.5, Pinia, reka-ui). The current thread
  (`web/src/screens/chat/Thread.vue`, `Bubble.vue`, `Composer.vue`, `SlashMenu.vue`, the story and job cards) is
  redesigned, not replaced by another app.
- **Transport: the AI SDK 7 UI message stream.** A new `POST /v1/airlie/chat/turn` emits the AI SDK UI message
  stream protocol (SSE, header `x-vercel-ai-ui-message-stream: v1`, `data:` JSON chunks: `start`, `text-*`,
  `reasoning-*`, `tool-input-*`, `tool-output-available`, `data-*` for engine and VM status and todos, `finish`,
  `[DONE]`). The client uses `@ai-sdk/vue` 4's `Chat` with `DefaultChatTransport`. Session id, kind, repository and
  branch travel in the request body. The tool loop runs on the server and streams.
- **assistant-ui is not adopted in v1.** The first draft chose assistant-ui for the thread. It is a React
  library; `@assistant-ui/vue` exists but is 0.0.7 (2026-10-28) with several features React-only, and its AI SDK
  runtime pulls in `@ai-sdk/react`. The redesign builds the thread in Vue on `@ai-sdk/vue`'s message parts;
  `@assistant-ui/vue` can be revisited once it matures (Claude's call; Noah to confirm).
- **Look and feel.** Toward Claude Code desktop:
  - **Left nav:** pinned repositories, each with its chats under it and a "new chat" that starts a VM session;
    plain chats in their own list; pin and unpin a repository. Today's app has top tabs and a phone tab bar and no
    left nav, so this is new; on a phone the nav is a drawer.
  - **Thread:** a calm single column, collapsible tool-call blocks with their output, a reasoning disclosure,
    inline diffs for edits, the todo checklist, permission prompts in the thread.
  - **Header:** repository, branch, model, context used, and engine and VM status.
  - Mockups first (three options, Noah picks), then build. The board, workflows, metrics and admin screens stay
    reachable.
- **Context readout.** Each turn's `finish` carries usage; the header shows tokens used against the engine's
  limit (262144 for Gemma).
- `POST /v1/chat/completions` and the existing `/v1/airlie/chat/attachments` and `/v1/airlie/chat/skills`
  routes stay.

### Hooks

The turn handler runs fixed hooks so policy, superpowers, and logging sit in one place:

| Hook | Purpose |
|---|---|
| `session_start` | Bind the kind, repository and VM; inject the superpowers bootstrap and the slash-command list |
| `before_turn` | Kind guardrails, attachment limits, ensure and hold the role's engine and the VM |
| `after_tool` | Audit log, UI side effects (todos, job and story cards) |
| `on_finish` | Persist the turn and release holders; the idle timers start |

### Memory and the doctor

- The doctor (fixed on 2026-11-11: MTP heads, one live sequence per other engine, KV scaled by `kv_bits`)
  projects two numbers per profile: the **pinned floor** (pinned engines only) and **everything resident**. The
  floor must fit the wired limit; everything resident may exceed it, and the doctor says the manager will queue
  or evict.
- With the defaults above (QAT 8-bit Gemma at 262144, reviewer and small on demand) every model resident is
  about 111 GB of the 124 GB limit, inside which the doctor keeps a 12 GB reserve.
- **VMs share the machine.** The wired limit caps GPU memory; a VM's RAM comes out of what is left of the 128 GB.
  The manager counts each running VM's configured memory, and the doctor shows models plus a default VM count
  against physical memory.
- **Raise the GPU limit** (Noah 2026-11-11). `iogpu.wired_limit_mb` reads 0 on this Mac today (the system
  default; MLX recommends about 115 GB), while `airlie.yaml` sets 126976 MB (124 GB). The sysctl needs sudo and
  resets on reboot, so phase 0 adds a task that sets it now and installs a LaunchDaemon that sets it at boot from
  `memory.wired_limit_mb`, and corrects the README's stale 106496.
- Two processes never share weights (MLX 0.32.4 reads safetensors into each process's own Metal buffers), so the
  manager's arithmetic counts each engine's weights in full.

## Configurable behavior

Merged in the usual order (`includes` → root → `airlie.local.yaml` → topology profile; profiles carry no engine
overrides). Expected keys, exact schema left to the builder:

- `engines.<name>.lifecycle` (`pinned` | `on_demand`) and `engines.<name>.idle_unload_s`.
- `models.<name>.mtp.estimated_memory_gb` (added on the doctor branch).
- `manager`: budget margin, start and download timeouts.
- `boxes`: the VM backend (`tart`), default image, VM memory and CPUs, idle stop, network allow list.
- `chat.kinds`: the tool lists for plain and repository chats, and which tools ask first (extends today's `chat:`
  section, which has only `attachments`).
- `chat.superpowers`: on/off and the vendored skills path.
- `roles.chat.max_context: 262144`, `engines.gemma.max_kv_size: 262144`.

## Testing

Every change passes the gate in [`AGENTS.md`](../../../AGENTS.md) (`uv sync --frozen`, ruff, ruff format, mypy,
`pytest -n auto`, `task web:gate`). Unit tests over 500 ms or on the real clock go in
`tests/unit/quarantine.toml`.

Unit tests use fakes, never real engines or VMs:

- **Manager:** ensure (one start for concurrent callers), hold/release, idle unload on a fake clock, pinned
  engines never stop, LRU eviction of idle engines and VMs only, waiting with a reason when nothing can be
  evicted, adopting an engine already on its port.
- **Downloads:** progress events from a fake tqdm, dry-run sizing, one download per snapshot, failure and retry,
  disk-space refusal.
- **Turn route:** the UI message stream chunks for text, reasoning, tool input and output, data parts, and
  errors; plain chats never get file or shell tools; confirmations enforced server-side.
- **VMs:** a fake box backend: start, hold, idle stop with disk kept, delete; tools routed to the box; push goes
  through the host; no credential in the box's environment; memory counted in the budget.
- **Superpowers harness:** bootstrap injected once per session and not into subagents; `skill` returns the file
  and siblings; `todo`; `task` runs a fresh context in the session's VM and holds its engine.
- **Web:** `task web:gate` covers the left nav, the thread's part rendering, the status line, and the
  permission prompt.

Live checks (outside the default gate): MTP acceptance on the QAT pairing at short and long context; a cold start
of Muse through a review; a download of a small model with progress in the UI; a repository chat that boots a
Tart Linux VM, clones, runs tests, and pushes a branch through the host; one superpowers flow (brainstorm → plan →
one subagent task) in that chat.

## Order of work

| Phase | Scope |
|---|---|
| **0 — Gemma at 262k, GPU limit** | `max_kv_size` and `chat.max_context` to 262144; MTP acceptance check on the QAT pairing; the GPU-limit task and LaunchDaemon; the doctor's pinned-floor and everything-resident rows. |
| **1 — Model manager** | `lifecycle`, `ensure`, `hold`, `idle_unload_s`, budget with LRU eviction and waiting, adoption of running engines; every engine path goes through it; reviewer and small on demand. |
| **2 — Downloads and status** | `snapshot_download` with progress for serve and `task models:pull`; the status GET and events. |
| **3 — Turn route and plain chat** | `POST /v1/airlie/chat/turn` with the UI message stream and a server-side tool loop; plain chat on it with web, `skill`, `todo`; the superpowers bootstrap and mapping. |
| **4 — Repository chats in VMs** | The Tart box backend behind the shell seam, a VM per session under the manager, the recipe and default image, clone in and push out through the host, builtin tools and `task` in the VM, confirmations. |
| **5 — Redesign** | Mockups (three options, Noah picks), then the left nav with pinned repositories and the Vue thread on `@ai-sdk/vue`: tool blocks, diffs, todos, permission prompts, status and context in the header. |
| **Later** | Compaction (summarize and continue), the visual companion, macOS VMs. |

The redesign mockups (phase 5) can start in parallel with phases 1–4; the build waits for Noah's pick and for the
turn route.

## Left to the builder

The exact UI-message envelope fields; the manager's module layout and how the orchestrator's engines get it
injected; whether on-demand engines use APC's warm-disk tier; which story tools need a confirmation in v1; numeric
defaults for the manager's margin and timeouts; how the checkout gets into the VM (git bundle, shared folder, or a
fetch through a host proxy) and the branch back out; the default image and VM size; how the `task` tool picks a
role when a skill does not say.

## Review comments (2026-11-11)

Internal design review (Iroh). Severity: **blocker** stops planning without a decision; **should-fix** will
bite implementation or ops; **nit** is clarity or test hygiene.

### Scope and phase ordering

1. **should-fix — Single effort, six phases, one doctor story.** Lifecycle, admission, router, turn API, Tart,
   shell, composer JIT, and HUD share `engine/process.py`, serve routing, and config schema. Phase 0 is the
   real critical path; phases 1–2 should not land UI (assistant-ui thread) until holder/pin semantics and
   `GET /v1/airlie/chat/context` exist, or the HUD and OpenCode clients will ship against stubs. Consider
   spelling a **minimum vertical slice** per phase (config + doctor + one pytest file) before web work.

2. **blocker — Phase 1 vs phase 3 ordering for repo-scoped mode.** Phase 1 promises repo-scoped enforcement and
   hooks; phase 3 adds Tart and confirms. If phase 1 can select a repo, the spec must say whether repo-scoped
   tools are **hard-disabled until Tart** or seatbelt-only with the unboxed warning—not silent host execution.
   Anti-goals forbid the latter without warning; the phase table does not say which gate applies when.

3. **should-fix — Phase 4 (shell/nav) after phase 3 (Tart).** Users can hit repo-scoped chat from the turn API
   before left-nav **+** exists. Either accept API-only repo chat in phases 1–3 or move a minimal repo picker
   into phase 1.

4. **should-fix — Phase 5 (composer JIT) without compaction (phase 6).** Per-turn skill injection can blow the
   128k budget quickly; the router in phase 2 may promote to `gemma_long` while phase 5 still adds tokens every
   turn. State whether composer JIT is **token-budget-aware** in phase 5 or deferred until phase 6.

### Dual Gemma, catalog, and doctor

5. **blocker — Two engines, one catalog model.** Today `engine_for_model` returns the **first** engine whose
   `models` list contains the name (`schema.py`). Two entries both serving `gemma4` makes routing
   **nondeterministic** (dict iteration order) unless you add an explicit rule: alias model names
   (`gemma4` vs `gemma4_long`), a `primary_engine_for_model` map, or a documented exception that
   `gemma_long` is **never** resolved via `engine_for_model` and is chat-router-only. Builder TBD must be
   decided before phase 2, not left open.

6. **should-fix — Doctor projection vs one-hot.** `project_memory_gb` sums **every** resident mlx-vlm engine's
   weights, KV, and prefix cache. With `gemma` pinned and `gemma_long` on promote, the wired limit can appear
   exceeded even when policy says one-hot. The spec mentions a **migration window** holding both; doctor needs
   rules: count at most one Gemma KV slot for chat by default, add the second only when a holder flag says
   migration, or admission will refuse studio work incorrectly.

7. **should-fix — Duplicate prefix cache.** `gemma` already sets `cache_memory_gb: 4`. A second process at 8085
   duplicates weights **and** cache reservation. Doctor rows should show **incremental** cost of `gemma_long`
   (delta KV + optional shared-weight accounting), not pretend the second process is free aside from KV.

8. **nit — Token numbers.** OpenCode `limit.context: 128000` vs `max_kv_size: 131072` is a 3072-token gap. Say
   whether `effective_limit` exposes 131072 for the 128k backend and 128000 is only OpenAPI rounding, or adjust
   one side so clients and router thresholds use the same integer.

### Router, cold prefill, and mlx-vlm constraints

9. **blocker — Cold prefill is still builder TBD but phase 2 depends on it.** Promotion without a specified
   transcript transfer (full replay vs summary slice vs prefix-cache reuse across processes) has no latency or
   correctness budget. mlx-vlm prefix cache does **not** cross processes; cold promote always pays full prefill on
   8085. Spec should require: max promote latency target, behavior when prefill fails mid-turn, and whether the
   user sees a visible “switching to long context” gap.

10. **should-fix — “Cannot fit the next message in gemma's KV”.** Needs an operational definition: tokenizer count,
    attachment expansion, tool-result bytes, composer JIT from phase 5, and MTP draft head overhead. Without that,
    hysteresis unit tests are injectable integers only—they won't match production.

11. **should-fix — In-flight turn during demote/promote.** One-hot says new prefill goes to the promoted engine; it
    does not say what happens to an **active SSE stream** on the demoted process. Serve must either drain, cancel,
    or finish on the old backend before demote—document the rule.

12. **should-fix — Jev/chief vs context router naming.** Studio router picks specialists; this router picks Gemma
    backend. Implementation should use distinct types/config keys (`chat.context_router` vs `router.kind`) so logs,
    metrics, and tests do not conflate them.

### Admission, QoS, and testability

13. **should-fix — Soft admission lacks observable contracts.** “Queue, delay, or refuse with a structured reason”
    needs a stable reason enum or code (for tests and OpenCode). Who consumes the queue (orchestrator lanes, engine
    `ensure`, Tart box start)? Without seams, QoS tests stay fake-clock theater.

14. **should-fix — “Interactive chat is active”.** Defined as web turn in flight or OpenCode stream registered on
    serve—registration mechanism is unspecified (per-connection table? completion id?). Admission tests need a
    fake registry API on the same seam the scheduler uses.

15. **should-fix — Chat-preferring scheduler vs existing line fanout.** Studio already runs planning/build lanes
    with consult fanout (`live_kv_gb` for studio topology). Spec should say whether chat preference **pauses** lane
    starts, **delays** `ensure` on main/reviewer, or only affects **new** gemma_long promotes—otherwise two QoS
    stories fight in one process.

16. **nit — Config keys are named but not bounded.** `chat.admission.max queue wait` should clarify units (seconds?),
    default, and whether refuse returns HTTP 429, 503, or a job-card “waiting for memory” state.

### OpenCode, limits, and HUD

17. **should-fix — 128k advertised vs 262k effective.** Roles stay at 128000 until promotion; clients that ignore
    `GET /v1/airlie/chat/context` will trim or error on long threads. Spec should require serve to inject
    **usage** into streaming chunks (or a standard header) so OpenCode plugins can promote without a second poll
    channel—optional but reduces “opaque headroom” complaints.

18. **should-fix — HUD phase 6 vs endpoint phase 2.** Context endpoint ships in phase 2; HUD in phase 6. OpenCode
    and power users depend on phase-2 behavior alone for months—document minimal JSON stability guarantees
    (version field?) so clients do not break at phase 6 compaction.

19. **should-fix — Compaction engine.** Phase 6 “summarize-and-continue” needs a model/seat (summarizer role? same
    Gemma?) and whether compaction counts as a holder on `gemma_long`. Missing that invites unbounded summarize loops
    under admission.

### Tart, isolation, and security

20. **should-fix — One box per chat session vs one box per job (remote studio).** Remote studio queues macOS VMs at
    two; chat multiplies sessions per user. Spec mentions queue at capacity as builder TBD—needs a **user-visible**
    policy (block new repo chat, queue, or general-mode fallback) before phase 3.

21. **should-fix — Seatbelt fallback.** Allowed with warning, but remote-studio anti-goals treat host execution as
    failure for **remote** work. Clarify whether repo-scoped **web chat on the Studio host** may use seatbelt when
    Tart is down, or repo-scoped chat must hard-fail—same words, different threat model.

22. **should-fix — Credentials in the box.** Host holds GitHub App and API keys; repo-scoped tools include `git` and
    `write_file`. Align with remote-studio: which credentials enter the box, how `run_tests` reaches the host gate
    vs box-only commands, and whether `jobs_start` always stays host-side.

23. **nit — `onFinish` box release vs `idle_unload_s`.** Session end should drop Tart and engine holders in a
    defined order so a closed tab does not leave a box running while `gemma_long` unloads.

### Transport, hooks, and web stack

24. **should-fix — Two transports, one router.** Web uses `/v1/airlie/chat/turn` + hooks; OpenCode uses
    `/v1/chat/completions`. Router and admission must attach to **both** entry points with shared session/correlation
    ids—builder TBD list should include how OpenCode threads map to session id for context GET.

25. **should-fix — Confirmation UX on OpenCode.** Confirmations are specified for repo-scoped web; OpenCode on the
    same serve path may invoke tools without the Vue modal. Either non-goal for OpenCode confirms or a serve-side
    tool-approval protocol for MCP—otherwise policy diverges by client.

26. **nit — Testing section and `task web:gate`.** Unit list is solid for Python seams; no mention of Vue tests for
    mode indicators, confirm modals, or assistant-ui integration. At least one `web:gate` test (or documented manual
    check) for streaming tool parts should be called out, or phase 1 ships UI without CI cover.

### Contradictions and underspecified edges

27. **should-fix — “Unless migration window explicitly holds both”.** Nothing in configurable behavior names this
    window (duration, max concurrent holders, user-visible behavior). Tie it to `chat.context_router` or engine
    `hold` refs.

28. **should-fix — General mode “optional web search/fetch when configured”.** Same admission/QoS as studio search
    (`web.search` / searxng)? If search hits external network, mode gating and observability should match repo tools.

29. **nit — Status line says Approach C merged same day.** Implementation plans should reference this file as the
    single source for chat + lifecycle so older chat-only specs are not followed in parallel.

### Suggested decisions before phase 2 coding

- Resolve **catalog/dual-engine routing** (#5) and **doctor one-hot accounting** (#6–7).
- Lock **cold prefill semantics** (#9) and **in-flight stream on backend switch** (#11).
- Lock **repo-scoped behavior before Tart** (#2) and **OpenCode tool confirms** (#25).
- Publish **admission reason codes + interactive-chat registry** (#13–14) for the listed unit tests to assert real
  behavior, not only injected stubs.

## Review resolutions (2026-11-11 revision)

How the revision above answers each comment. "Moot" means the comment was about the two-Gemma router, which the
revision removes.

1. **Phases and slices.** Reordered: the engine work (phases 0–2) lands before any UI change; each phase is its
   own vertical slice with config, doctor rows, and tests.
2. **Repo-scoped before Tart.** Decided: repository chats exist only with their VM (phase 4); phase 3 ships
   plain chat, which runs no commands. No host execution and no seatbelt interim for chat.
3. **Navigation after Tart.** Until the redesign's left nav (phase 5), a repository chat starts from the board
   card or a repository selector on the chat screen.
4. **Composer JIT vs compaction.** "Composer JIT" is dropped as a term (it collided with the workflow composer
   and with the chat input). Per-turn skill loading is the `skill` tool; with 262k the budget pressure is lower,
   and compaction stays later.
5. **Two engines, one catalog model.** Moot: one Gemma engine.
6. **Doctor vs one-hot.** Moot for Gemma. The doctor gains pinned-floor and everything-resident rows instead.
7. **Duplicate prefix cache.** Moot: one Gemma process.
8. **128000 vs 131072.** Moot for this effort (OpenCode is out of scope); the chat limit is the engine's 262144.
9. **Cold prefill.** Moot: no backend switch.
10. **"Cannot fit in gemma's KV".** Moot; the only limit is the server's context check at 262144, and the turn
    route reports usage on every `finish`.
11. **In-flight stream on switch.** Moot: no switch. The manager never stops an engine that has a holder, which
    covers a streaming turn.
12. **Router naming.** Moot: there is no context router; the studio router (`router.kind`) is unchanged.
13. **Admission contracts.** The manager's waits carry a reason (shown in the UI and in the status events); the
    reason codes are part of the status API.
14. **"Interactive chat is active".** Replaced by holders: an open turn holds its engine and VM through the
    manager, the same seam the story line uses.
15. **Chat preference vs lane fanout.** No separate QoS scheduler: the manager admits or waits by memory, and
    never evicts a held engine. The story line's own slot limits are unchanged.
16. **Units and refusal codes.** Left to the builder with the manager's numeric defaults; waiting is shown, not
    refused, unless a timeout passes.
17. **128k advertised vs 262k.** Moot for OpenCode; the web chat reads usage from each `finish`.
18. **Context endpoint stability.** Replaced by usage on each turn's `finish` and the engine status API.
19. **Compaction engine.** Later; not in this effort.
20. **Box per session vs capacity.** Linux Tart guests have no framework cap (the two-VM limit is macOS guests
    only); the manager admits VMs by memory, stops idle ones first, and shows a wait otherwise.
21. **Seatbelt fallback threat model.** Chat has no host fallback: without Tart, repository chat is unavailable
    and says how to install it. Seatbelt stays the story line's confinement.
22. **Credentials in the box.** No push or merge credential enters the VM; the host fetches the code in and the
    branch out, and pushes with the GitHub App, as in remote studio. Setup secrets exist only during setup.
23. **`on_finish` vs idle unload.** Each turn's end releases its engine and VM holders; both idle timers run on
    their own; deleting the chat deletes its VM.
24. **Two transports, one router.** Moot: `/v1/chat/completions` stays as it is; only the turn route is new.
25. **Confirmations on OpenCode.** Out of scope (OpenCode).
26. **Web tests.** Phase 5's build must add `web:gate` coverage for message parts, the status line, and
    permission prompts.
27. **Migration window.** Moot: no second Gemma.
28. **Web search in general mode.** Same tools and logging as today's `web_search` / `web_fetch` roles.
29. **Single source.** This file is the source for chat, the model manager, and long context.
