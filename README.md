
<p align="center">
  <img src="docs/readme/wordart.png" alt="airlie" width="420" />
</p>

airlie is an agentic workflow platform. Today it runs development workflows end to end — from a board card
through planning, build, review to PR — with specialist agents that share what they learn on one
Apple Silicon Mac. Dynamic workflows are next: the same line, for work that isn't only software.

`task serve` hosts the web app. The tabs:

### Chat

Ask what's running, how airlie works, or type `/` for commands such as `/story`.

<p align="center">
  <img src="docs/readme/chat.png" alt="Chat: ask airlie or type / for commands" width="720" />
</p>

The Chat screen still talks to `/v1/chat/completions`; it moves to the turn route below with the chat redesign.

#### The chat turn route

`POST /v1/airlie/chat/turn` runs one chat turn on the `chat` role and streams it as an AI SDK UI message stream
(SSE, header `x-vercel-ai-ui-message-stream: v1`, ending `data: [DONE]`). Body: `{"id": "<thread id>", "kind":
"plain", "messages": [{"role": "user", "parts": [{"type": "text", "text": "…"}]}], "skill": "<optional>"}`. The
server keeps the conversation, so only the newest user message is read (its `text` parts and `file` parts naming
`/v1/airlie/uploads/<id>`); the rest of `messages` and any `tools` list are ignored. The stream carries the
standard chunks (`start`, `start-step`, `text-*`, `reasoning-*`, `tool-input-*`, `tool-output-*`, `finish-step`,
`finish`, `error`) plus `data-engine` status parts ("Starting gemma…" while a VM or job comes up).
`finish` carries `messageMetadata.usage` with `prompt_tokens` and `max_context`, the context readout.

| Route | Does |
| --- | --- |
| `POST /v1/airlie/chat/turn` | one turn; 401 not signed in, 400 a refused body (an unknown kind, no user message) or too many attachments, 404 a thread that is not yours, 409 the thread already has a turn running, 503 no `chat` role or engine; a `before_turn` guardrail may refuse with its own status |
| `GET /v1/airlie/chat/threads[?kind=]` | your threads, newest first |
| `GET /v1/airlie/chat/threads/{id}` | one thread with its messages and todos; 404 |
| `DELETE /v1/airlie/chat/threads/{id}` | 204; 409 while a turn is running; 404 |
| `POST /v1/airlie/chat/threads/{id}/stop` | cancels the running turn and saves the partial reply as `interrupted`; 204 (also when nothing is running); 404 |

A client that disconnects does not stop the turn: it finishes, is saved to the thread and notifies `on_finish`.
Stop is the explicit call above. Threads live on the server (sqlite, per signed-in person), so a reload or another
device reads the same conversation.

Only the `plain` kind exists (`repository` answers 400 until plan 4). A plain chat has no repository: its tools are
`web_search` and `web_fetch` (when the web tools are configured), `skill` and `todo`; there is no file or shell
tool. `chat.kinds.<kind>.tools` names them (`ask_first` marks tools to confirm), and `chat.max_steps` caps the
tool rounds in a turn.

The session prompt carries the [superpowers](https://github.com/obra/superpowers) bootstrap, and `skill` loads any
of its skills (`brainstorming`, `writing-plans`, …) on demand. They are vendored at 7.0.0 in `vendor/superpowers/`
(`chat.superpowers.enabled`, `chat.superpowers.path`), copied byte for byte and never edited; `airlie-tools.md`
there is airlie's own tool mapping. `vendor/superpowers/PIN.md` records the pin. `task superpowers:verify` checks
the tree against `MANIFEST.sha256`; to update, re-copy a newer release, edit `PIN.md` and regenerate the manifest.

### Board

The local board (initiative → epic → story → subtask) with columns from To do through Done. Create a card when you want something on the line; open one for its acceptance criteria, the layer that ran, and the comments the agents leave.

<p align="center">
  <img src="docs/readme/board.png" alt="Board: Kanban of stories across To do, Approved, Doing, Review, Blocked, Done" width="720" />
</p>

<p align="center">
  <img src="docs/readme/create.png" alt="Create: pick initiative, epic, story or subtask" width="720" />
</p>

<p align="center">
  <img src="docs/readme/story.png" alt="Story: acceptance criteria, workflow status, agent comments" width="720" />
</p>

### Workflows

What the line is building right now, every story's stage, and — for one story — the pipeline from intake through the layers, with what each role was given and what it produced.

<p align="center">
  <img src="docs/readme/workflows.png" alt="Workflows: running progress and stories by stage" width="720" />
</p>

<p align="center">
  <img src="docs/readme/workflow.png" alt="One workflow: pipeline stages and the coder's build input and output" width="720" />
</p>

### Metrics

Where to look when something feels off: generation and prefill speed, stories in flight, fix rounds per layer.

<p align="center">
  <img src="docs/readme/metrics.png" alt="Metrics: tok/s, stories, fix rounds, generation speed by role" width="720" />
</p>

### Admin

Who can sign in, their role, passkeys and Tailscale login, and an Engines table (each engine's state: `stopped`,
`downloading`, `starting`, `ready`, `waiting`, `stopping`, `failed`, who holds it and why a caller is waiting). First admin and invites:
`task auth:admin` / `task auth:invite` ([`docs/integrations/sign-in.md`](docs/integrations/sign-in.md)).

<p align="center">
  <img src="docs/readme/admin.png" alt="Admin: people, roles, passkeys, invites" width="720" />
</p>

## Install

Three tools, then everything else is a task (node 26 builds the web app in `web/`, and must be the first `node` on
your `PATH`: an nvm node that comes first stops the web tasks with a message naming it):

```bash
brew install go-task uv node@26
```

```bash
task setup
```

`task setup` installs the pinned Python and every dependency into a project-local environment managed by uv, and
builds the web app (`npm ci` and Vite under `web/`, into `src/airlie/serve/web_next`). You never run pip, activate a
venv, or install anything else by hand. Then:

```bash
task doctor
```

airlie runs no model. Every catalog model is served by a remote HTTP endpoint (the `workflow` engine in
`airlie.yaml`, OpenAI or Anthropic wire format). Point it at yours in the environment (or `.env` beside
`airlie.yaml`) before `task serve`, and let `task doctor` check that it answers:

```bash
export AIRLIE_WORKFLOW_BASE_URL=https://inference.example.com/v1
export AIRLIE_WORKFLOW_API_KEY=...   # optional: unset means none
```

## Phone

airlie is an installable web app. On an iPhone, open the Studio's tailnet URL in Safari, tap Share, then Add to Home
Screen; it opens full-screen with the dark status bar. Sign in with your passkey once: a sign-in lasts
`auth.session_days` (30 by default) idle, and each visit past half of that renews it, so a phone you use stays signed
in. With the Studio unreachable the app opens its cached shell, or says it can't reach the Studio (the API and live
feed are never cached). When a new version is ready the app offers a Reload.

Notifications (Web Push) need a contact for the push services, in `airlie.local.yaml`:

```yaml
notifications:
  contact: "mailto:you@example.com"   # or https://your.host; a mailto needs a dotted domain
```

Restart `task serve`; `task doctor` reports `notifications` as `push on` once it is set. Then, in the installed app,
open the account menu, choose Notifications, tap Turn on notifications and allow the prompt (iOS asks only in the
installed app). Tick the kinds you want (`notifications.default_kinds` sets what is on until you choose). Today the
Studio sends a push when a story, epic or initiative is blocked, when a story is published for review, when mockups
are ready to pick, and for Send a test; a chat reply, a model or VM ready and something needing your OK are marked
"coming soon" (chat replies come with the chat redesign, model/VM ready and confirmations later). Notifications
belong to a signed-in person, so an API key cannot set them up. The Studio POSTs each push to the phone's push service, so it needs outbound internet;
`notifications.push_hosts` lists the services a browser may subscribe through (Apple, Google, Mozilla, Windows by
default). The VAPID key is made when serve starts with `notifications.contact` set, in
`observability.state_dir/vapid.pem` (`notifications.vapid_key` moves it).

## Tasks

Run `task` to list them. The ones that exist today:

| Task | Does |
| --- | --- |
| `task setup` | one-time, idempotent setup |
| `task doctor` | validates `airlie.yaml`, checks the wired-memory limit, RAM, tools (node against the pin in `web/package.json`: the first `node` on your `PATH` must be that major), and that each engine's `base_url` answers `GET …/models` over HTTP; exit 1 on any red row |
| `task config` | prints the fully resolved configuration (`airlie.yaml` + `airlie.local.yaml`) |
| `task searxng:up` / `:status` / `:down` / `:destroy` | SearXNG for the web roles' `web_search`, in a Tart Linux VM reached at `127.0.0.1:8888` (the first `up` downloads a few GB and installs it); `docs/integrations/web-tools.md` |
| `task web:e2e [-- --roles chief,ux]` | asks every web-enabled role (chief, research, ux, design, reviewer) about something after its training data and checks it searched and cited what it found |
| `task models:list` | show the model catalog and the engine serving each model (airlie starts no inference server: the engines are remote HTTP endpoints, see *Engines*) |
| `task gpu:limit` | set `iogpu.wired_limit_mb` to `memory.wired_limit_mb` now and at every boot (a LaunchDaemon; asks for your password) |
| `task run [-- WORKFLOW] --repo PATH` | run a workflow from `config/workflows/` via the generic engine (default workflow name: `story`); one run at a time per repo (`airlie.workflow.start_workflow`) |
| `task eval -- lang_detect [--topology fork] [--k 1]` | materialize a graded task from `evals/tasks/`, run it, grade every worktree with the hidden tests (same topology note as `task run`) |
| `task serve [-- --port 8090]` | OpenAI-compatible endpoint: roles are models (`/v1/models`, `/v1/chat/completions` with the role prompt prepended), plus `POST /v1/airlie/run {goal, repo, k}`; jobs: `POST /v1/airlie/plans`, `/runs` or `/stories`, then `GET /v1/airlie/jobs/{id}?since=&wait=` (long poll), `DELETE` to cancel |
| `task web:install` / `web:check` / `web:build` / `web:gate` / `web:dev` | the Vue app in `web/` (node 26, pinned in `web/package.json`): `npm ci` / typecheck + lint + tests / build into `src/airlie/serve/web_next` (`task serve` and `task setup` run it) / check then build, skipped while nothing under `web/` changed since it last passed / Vite dev server with `/v1` proxied to `task serve` |
| `task supervisor:log` / `task supervisor:stats` / `task supervisor:verdict -- ID right\|wrong [NOTE]` | read the supervisor's ledger (Muse's advice at a story's trouble points, advisory only): the newest interventions / how often they helped, by trigger and category / your verdict on one (`docs/integrations/story.md`, *The supervisor (trial)*) |
| `task supervisor:try -- --worktree PATH --base SHA [--run RUN_ID]` | Muse's triage of a worktree as a story's first pre-code retry would ask for it, with no ledger row (`docs/integrations/story.md`) |
| `task prompts:audit [-- --role NAME]...` | advisory: Muse reads each role's full instructions (its role prompt, then every rule and steering text that reaches it, in `src/airlie/prompts/`) and lists the ones that contradict, steering against rules included; writes `prompts-audit-<UTC time>.md` under the state directory's `runs/` and prints its path and each role's first line. Needs the engine, is not deterministic and fails nothing; the gate's check is `tests/unit/test_prompt_contradictions.py` |
| `task prompts:subjects` | every `governs` subject in use, one per line, with the hub texts and role prompts that tag it, so a near-duplicate subject (`tests.location` against `test.location`) shows; no engine needed |
| `task board:export [-- --out FILE]` / `task board:import-jira [-- --dry-run]` | write the local board (initiative → epic → story → subtask, `/v1/board`, the default source of stories) as JSON / import the Jira project onto it, each issue keeping its number under `board.prefix` (`HDS-5` → `AIR-5`) (`docs/integrations/story.md`, *Boards*) |
| `task initiative -- --text "..." --repo owner/name [--budget HOURS]` | make an initiative card from a loose ask, approve it and run the specs phase's first turn: product asks you a question as a comment on the card, imagines the work with the specialists, and delivers a core epic and extensions priced in hours, with three mockups for web work (`docs/integrations/story.md`, *Initiatives*) |
| `task initiative:turn -- AIR-40` | run the specs phase's next turn now, after you answered its question on the board while `task serve` was down |
| `task epic:add -- --file spec.md --repo owner/name` | make an epic card on the local board from a Markdown spec (its `## Acceptance criteria` bullets become the epic's criteria); approve it and the tick plans it into prioritized stories (`docs/integrations/story.md`, *Epics*) |
| `task epic:replan -- AIR-3 [--reason TEXT]` | ask the architect to revise a planned epic's stories now: started, in-review and merged stories are kept, unstarted ones changed or dropped, new ones made, a stopped story with no layer built retired (the tick does this by itself when a story stops again after its one resume) (`docs/integrations/story.md`, *Epics*) |
| `task serve:launchd:install` / `task serve:launchd:uninstall` | KeepAlive launchd agent for `task serve` (log `~/.local/state/airlie/serve.log`); stop any terminal `task serve` first |
| `task auth:admin` / `auth:invite` / `auth:users` / `auth:revoke` | first admin invite link / invite a person (passkey) / list people / revoke; Admin in the web app; `docs/integrations/sign-in.md` |
| `task tunnel` | Cloudflare quick tunnel for `task serve` on 8090 (Cursor and other cloud clients); run serve with `--api-key` first |
| `task bridge` / `bridge:launchd:install` / `bridge:launchd:uninstall` | Job Bus worker: claim/result to the hub, studio and llm jobs on loopback `task serve` (`src/airlie/serve/bridge/README.md`) |
| `task hub:install` / `hub:check` / `hub:gate` | Job Bus hub npm deps / typecheck + contract tests / the hub gate |
| `task opencode:install` | copy the OpenCode chief, its subagents, `/studio` and `/story` into `~/.config/opencode` and register `airlie mcp`, an MCP server (stdio) that gives OpenCode the tools `airlie_plan`, `airlie_run`, `airlie_status`, `airlie_cancel` and `airlie_story` over `task serve`'s jobs API, in its `opencode.json` (merged into an entry you already have; after a backup when it changes the file; `docs/integrations/local-agents.md`) |
| `task bench:topology -- --topologies single,teams,fork,studio --repeats 3` | experimental topologies and studio on the graded tasks: wall-clock, steps, forks, winner graded by the hidden tests |
| `task bench:lts -- --tasks lang_detect --repeats 2` | memory-free baseline vs the configured controller on graded tasks: wall-clock, steps, admissions, retrievals, pass rate, and the spec's ≥25 % verdict (experimental `teams` / LTS path) |
| `task bench:story` / `bench:story-compare` | a fixed story built end to end, measured and graded / deltas between two story bench records |
| `task controller:dataset` / `task controller:train [-- --epochs 5]` / `task controller:eval` | build the training set from every run's admission log; train the spec 03 §2 controller (LoRA on Qwen3-0.6B + projections) with the spec 03 §3 REINFORCE objective; replay held-out decisions against the logged controller |
| `task memory -- RUN_ID [--full]` | show a run's shared memory bank: keys, owning team, cross-team reads (experimental `teams` / LTS path) |
| `task bench [-- --model NAME --ctx 4k,32k --n 1,2,4]` | engine throughput through the seam: TTFT, prefill and decode tok/s, aggregate at N streams, and whether turn 2 of a conversation hits the prefix cache |
| `task bench:models` | the same for every catalog model marked `bench: true`, one table |
| `task bench:compare -- a.json b.json` | per-cell deltas between two saved runs |
| `task test` / `task test:live` / `task test:mac` / `task lint` / `task typecheck` / `task check` | unit tests / tests against the configured HTTP engine / mac-only unit tests / ruff / mypy / lint+types+unit |
| `task static` | lint + typecheck: part of pipeline stage 1 and the pre-push gate (`task hooks:install`, run by `task setup`) |
| `task test:unit` / `test:functional` / `test:contract` / `test:integration` / `test:e2e` / `test:journey` / `test:performance` / `test:security` | one layer of the test pyramid, `tests/<layer>/`; a layer with no tests yet succeeds |
| `task mutate [-- --max-children 6]` / `task mutate:results` | mutation-test every enrolled module (`[tool.airlie.mutation].enrolled`); an uncaught mutant exits 1 |
| `task mutate:changed [-- --base origin/main]` | mutants of the functions of enrolled files the change touched since the base ref, plus every function a changed or new test executes (`--list` previews); the pipeline runs it with `--gate-only --defer-setup`, in shards |
| `task mutate:project` | mutation-test all of `src/airlie` (the weekly run on the Mac): enrolled modules gate, the rest is printed as a score table, the backlog |
| `task mutate:local [-- --max-children 4]` | the weekly whole-project run by hand: waits for the line's lanes, mutates a fresh worktree of `origin/main`, writes `~/.local/state/airlie/mutation/<date>.md` |
| `task mutate:launchd:install` / `task mutate:launchd:uninstall` | load / remove the launchd agent that runs `task mutate:local`'s run every Sunday at 01:00 |
| `task mutate:merge -- --merge DIR --expect N` | gate on the `--results` files of N sharded runs (`--shard I/N`) together: CI's matrix ends with this (`--survivors` writes the gating survivors as JSON) |
| `task fmt` | auto-format and auto-fix lint |

Coming per the roadmap: `task events`, `task trace`, `task bench:gaia` (M4b).

## Pipeline

`.github/workflows/pipeline.yml` runs one stage per level of the test pyramid; a stage starts only when the one
before it is green:

1. **static**: `task static` (ruff + mypy; also the pre-push gate via `.githooks/pre-push`), then `task web:gate`
   (Vue typecheck, lint, tests and build) and `task hub:gate`.
2. **lowest level**: `task test:unit`, then mutation testing of the functions of enrolled modules the change
   touched (`task mutate:changed -- --gate-only --defer-setup`): a PR is held to the code it changed, and a change
   to the mutation setup (`pyproject.toml`, `tests/unit/conftest.py`) is left to the nightly sweep. It runs as a
   matrix of 8 shards (`--shard I/N`: whole functions, dealt out by size) and a `mutation-gate` job merges them
   into one verdict (`task mutate:merge`); a survivor fails it. Mac-only unit tests and mutants (`task test:mac`,
   `mutation-mac`) run beside the Linux matrix. A story airlie builds gets mutation on each of its PRs, like any
   other change. Run `task mutate:changed` locally for the wider set: it also mutates every function a changed or
   new test executes.
3. **units in concert**: functional, contract, integration (`tests/functional`, `tests/contract`, `tests/integration`).
4. **behavioural**: e2e (the web app in a container), journey.
5. **environmental**: performance, security.

Stages 3 to 5 pass while their `tests/<layer>/` directory is empty and start enforcing as soon as it has tests.
`.github/workflows/mutation-nightly.yml` runs `task mutate` (`--scope enrolled`) every night over every enrolled
module, in 8 Linux shards and a mac job for the sources that need macOS, and uploads the merged report and the
surviving mutants; airlie's line turns those survivors into fix cards (`line.mutation_cards`). The whole project,
the modules that are not enrolled too, is mutated weekly on the Mac by a launchd agent (`task
mutate:launchd:install`), which writes the per-module score table: the backlog. To enrol a module,
add its path to `[tool.airlie.mutation].enrolled` once `task mutate:changed` is clean for it.

## Using airlie from another client

Terminal agents (OpenCode, Claude Code, Qwen Code) call the engine directly: see
[`docs/integrations/local-agents.md`](docs/integrations/local-agents.md). VS Code's chat (Local session) and
Cursor (`task tunnel`, with serve's `--api-key`) are in `docs/integrations/` too. People in the web app:
[`docs/integrations/sign-in.md`](docs/integrations/sign-in.md) (passkeys, Tailscale, `task auth:admin`).

`task serve` starts an OpenAI-compatible server on `127.0.0.1:8090` and hosts the web app. Point Claude Code,
Qwen Code, Cursor or `curl` at `http://127.0.0.1:8090/v1` and pick a role as the model name; airlie prepends
the role prompt and forwards to the engine. `POST /v1/airlie/run` with `{"goal": ..., "repo": "/path", "k": 3}`
runs a topology (studio, or an experimental one) and returns the aggregated answer and winning branch. The jobs
API (`/v1/airlie/plans`, `/runs`, `/stories`) starts studio plans, studio runs and stories in the background and
is followed by long polls ([`docs/integrations/story.md`](docs/integrations/story.md)). **Without `--api-key`,
any program on this machine can call every route, including the ones that start stories, which push branches and
open draft PRs:** run `task serve -- --api-key SECRET` (or set `AIRLIE_SERVE_API_KEY`) to require
`Authorization: Bearer SECRET`. A `task serve` that is already running must be restarted to pick up a new key.

## Topologies

**`studio`** is the topology the line and the web app run (design:
`docs/superpowers/specs/2026-10-13-studio-topology-design.md`). One generalist (`chief`, Qwen3.8-27B, also the
vision model) scopes the request into a JSON plan of subtasks; the router (`router.kind`: the chief's
own hint, or a catalog model) assigns each subtask a specialist role from the roles whose prompts carry a
`summary:` line, and for roles with a skill library (`skills/qa/*.md`, folded into the coder's test brief) picks
the guideline to inject; specialists are prompts over the catalog (generalist for most seats, Gemma for
the `reviewer`) and run as single agents on one worktree, writers one at a time, readers in parallel; after a
code subtask the reviewer (Gemma, a different lab than the coder's Qwen, on purpose: judges favour their own family's text) returns
a JSON verdict and blocking findings give the coder one fix round; the chief synthesises. Run experimental studio topology jobs through serve (`POST /v1/airlie/runs`); the committed `workflow` engine serves every model the studio's roles name (see *Engines*). Every routing decision is logged
(`runs/<id>/routing.jsonl`) and scorable with `task route:eval`.

Every topology injects a deterministic repository map (tree, sizes, Python signatures; `repo_map.*`, cached under
`<state_dir>/repo_maps/`) before the goal, so no branch re-discovers the layout.

### Experimental topologies

`single`, `teams` and `fork` remain for research and benches (`task run`, `task eval`, `task bench:topology`).
They are not what the line builds with. The committed `airlie.yaml` still defaults `topology.kind` to `single`
for those tasks; pass `--topology studio` on `task eval` or use serve's run route for studio jobs. Design notes:
`docs/superpowers/specs/2026-10-13-fork-when-stuck-design.md`,
`docs/bench/2026-10-13-topology-bench.md`.

- **`single`**: one agent, no shared memory (`teams.k` forced to 1, LTS off). Won the 2026-10-13 topology bench
  on wall-clock (matched K=3 on pass rate at 45 % of the time).
- **`teams`** (the build-spec layout): K independent teams from the start, sharing through the LTS memory bank,
  one winner picked by the aggregator (`teams.k: 3`, `lts.enabled: true`). `task run` spawns the teams; each is
  the built-in ReAct loop (`teams.orchestrator: native`; `autogen` selects the AutoGen `AssistantAgent`) with
  sandboxed tools confined to its own worktree. The aggregator (`aggregator.kind: llm` or `test_score`) picks a
  winning branch; with `teams.stop_on_first_pass: true` the first pass cancels the others. Learning to Share
  (below) applies here.
- **`fork`** (fork-when-stuck): one trunk runs with the whole engine budget until it is stuck (two consecutive
  failing `run_tests`, or a repeated action). Its working tree is committed, `fork.width` children get worktrees
  at that commit and the trunk's conversation verbatim (a prefix-cache hit) plus one nudge each: a distinct
  root-cause hypothesis from the `hypothesizer` role (`fork.diversify: hypotheses`) or a temperature spread. The
  first child whose tests pass cancels its siblings and continues as the trunk; with no pass, the child with the
  fewest failing tests continues, up to `fork.max_rounds`. Branches are `trunk`, `r1-1`, `r1-2`, `r2-1`, …;
  events: `fork_point`, `fork_resolved`, `branch_done`.

The EdgeAgent HAL scheduler (spec 04 §1) can run alongside multi-team layouts: accepted-draft counts update each
team's EMA and re-allocate the 16-slot draft budget after every step (`hal` events). a remote endpoint cannot take a
per-request draft length, so the budgets are computed and logged but not applied. Every step of a run is written to
`~/.local/state/airlie/runs/<run_id>/events.jsonl` and `trace/`.

**Profiles.** `topology.profiles.<kind>` is a partial config deep-merged over `airlie.yaml` when that kind is
selected (`extends: <profile>` chains), so each topology declares its own team count, memory settings, contexts
and models. `--topology` on `task eval` and `topology` in `POST /v1/airlie/run` select
the profile for one run; a profile name that is not a kind (a named variant, or a parent other profiles
`extends`) can be selected the same way and takes its kind from its own `topology.kind` or the file's.
`--k N` with `N > 1` under the `single` kind selects the `teams` profile. `task doctor` projects memory per
profile. Profiles carry no engine flags: the engines come from the base config.


## Engines

airlie never starts or stops an inference server. `engines:` in `airlie.yaml` lists remote HTTP endpoints; the
committed one is `workflow`:

- `base_url` and `api_key` come from `AIRLIE_WORKFLOW_BASE_URL` and `AIRLIE_WORKFLOW_API_KEY` (an unset key is none);
  a person's own endpoint goes in `airlie.local.yaml`.
- `format` is the wire dialect (`openai` or `anthropic`); `models` are the catalog names the endpoint serves;
  `passthru` renames a catalog name to the one the server expects.
- Roles pick a catalog model and the engine is derived from it, so swapping endpoints never touches the roles.
- `task serve` is airlie's own OpenAI-compatible endpoint on top of those engines (roles and catalog models).
- `task doctor` asks each engine's `GET <base_url>/models`: an unreachable or failing one is a red row naming the
  engine and the URL; a 401 or 403 counts as reachable and says to check the key.

## Repository chats

The chat panel lists the repositories each signed-in person can reach on GitHub when
`chat.sources` includes `github_app` (as in the committed `airlie.yaml`). Until they link,
**Connect GitHub** shows the device-flow code and a link to GitHub; after approval the panel lists
their installations' repositories, plus any **extras** that are local checkouts only (pinned by hand).
Set `story.github_app.client_id` in `airlie.local.yaml`, turn **Device flow** on for the App, and put the
App's client secret at `story.github_app.client_secret_file` (0600) so refresh and revocation work;
linked tokens are sealed with `story.github_app.token_key_file` (losing that file drops every link — people
connect again). See [`docs/integrations/story.md`](docs/integrations/story.md) (*Publishing as a bot*).

A repository chat works on a checkout inside its own Tart VM, one VM per chat. The model's tools (`shell`,
`run_tests`, file edits, `compute`) run in the VM, never on the host. There is no host fallback: when Tart is
missing or the VM cannot start, the chat says so and stops. Install Tart with
`brew install cirruslabs/cli/tart`, and softnet, which filters the VM's network after setup (`allow_list` and
`off` both need it; without it a repository chat is refused with this line, and `task doctor` has a `softnet`
row): `brew install openai/tools/softnet`, then `sudo chown root:wheel "$(brew --prefix)/bin/softnet"` and `sudo chmod u+s
"$(brew --prefix)/bin/softnet"` (softnet's README asks for a SUID bit on the binary or passwordless sudo, and gives no
command; it needs macOS 26 or later; there is no `softnet install`).

- **Image.** `boxes.image` (default `ghcr.io/cirruslabs/ubuntu:latest`; `tart pull` it once, or a chat's first
  start downloads it, several GB, with no progress shown). A repository with a recipe that has `setup` is built
  once into a cached image, `airlie-env-<key>`, and every chat clones from it. The key covers the base, the setup
  lines and the variables (names and values). A `:latest` base is keyed by its name: a moved tag reuses the
  cached image until the recipe changes.
- **Recipe.** `.airlie/environment.json` in the repository (see
  [`docs/integrations/environment.example.json`](docs/integrations/environment.example.json)). Keys: `base` (a
  registry image, `host/name[:tag]`, or `boxes.image`; never a local VM, so a recipe cannot start from another
  repository's cached image or another chat's disk), `setup` (shell lines run once with the network open), `env`
  (variables of every command; the values are in git, so no secrets), `test` (what `run_tests` runs; without it
  `run_tests` says how to add it) and `workdir` (default `/home/admin/work`). An unknown key is an error that
  names it.
- **Secrets.** Setup secrets stay on the host in `<state_dir>/boxes/secrets/<owner>__<name>.env`, `KEY=VALUE`
  lines, mode 600 (a file others can read is refused). They reach the VM only while setup runs, on stdin, and are
  not in the cached key. Building an image while secrets exist asks first (`build_image`). What setup writes to
  disk with them (a `~/.npmrc` token, a logged-in registry) stays in the cached image and in every chat cloned
  from it: such an image is named per repository and never shared, but a chat's model can read it.
- **Network.** Open during setup. After it, `boxes.network.after_setup` is `allow_list` (default: only
  `boxes.network.allow_hosts`, resolved to IPv4 addresses at each start) or `off`. In `allow_list` the VM reaches only those addresses:
  no DNS server and no gateway (so the host's sshd or a container's published port is not reachable). The host
  writes the resolved name and address pairs into the guest's `/etc/hosts` after boot, between airlie's marker
  lines (replaced each boot), so the names resolve there. A name that does not resolve fails the start and is
  named. A CDN whose addresses rotate mid-session needs a VM restart (a new resolution); it can fail an install
  until then. IPv6 is not used (the guest has no global IPv6 address).
- **Code in, branch out.** The host puts the checkout in and takes commits out as a git bundle over `tart exec`
  (no shared folder). A bundle over `boxes.bundle_max_mb` (default 512) is refused. The VM holds no token and no
  remote; the host pushes with the GitHub App.
- **Always asks.** `push_branch`, `open_pr` and `build_image` need a person's yes whatever the config says: the
  server holds the call until the chat's approval arrives, a no is spent once, and a yes that goes unused expires
  with its turn. `chat.kinds.repository.ask_first` may add tools, but a config that drops these three fails to load.
  A push is asked about after its commits are out of the VM: the request names the branch, the head commit, the
  commits and the changed paths, and says when they touch `.github/workflows/` (then the push's token may change
  workflows). The yes is for that head; a commit made after it is asked about again. `open_pr` is asked about
  with its title, branch and the head that was pushed.
- **`task` tool.** A fresh-context subagent on one of `chat.task.roles` (default `coder`, `reviewer`, `tester`,
  `architect`), in the same VM, holding its engine through the manager. It has no `task`, push or PR tool.
- **`boxes:` keys.** `backend`, `image`, `memory_gb` (8), `cpus` (4), `disk_gb` (40), `idle_stop_s` (900; the VM
  stops, its disk stays), `setup_timeout_s`, `boot_timeout_s`, `bundle_max_mb`, `network`. Deleting the chat
  deletes the VM; when `tart delete` fails the chat stays and the delete says which VM, so no disk is left
  without a chat. A running VM counts its `memory_gb` in `task doctor`. While a VM starts, the chat shows why it
  waits ("building image", "starting VM", "waiting for memory: ..."). Serve's shutdown stops the chat VMs (the
  disks stay), and a VM is synced before every stop, so the clone survives it.

## Learning to Share

Used by the experimental `teams` topology (and off under `single`). After every step a team's (action,
observation) pair is summarized into a one-line key by the `summarizer` role and offered to the admission
controller (`lts.controller.kind`: `heuristic` now, `judge` for a small-model YES/NO, `learned` once the spec
03 §2 controller is trained in M5, `always`/`never` for baselines). Admitted entries go into the shared memory
bank. Summarize + decide run in the background (`lts.background_admission`), so a team never waits on admission.
Before each model call a team sees the keys ranked for its current step under
`[Shared Memory Keys Available]` (`lts.ranking`, `lts.max_keys`; its own keys excluded unless `lts.inject_own`),
the top `lts.inline_top` carry their value inline once, and any full value is one `retrieve_value` call away.
`task memory -- RUN_ID` shows the bank; `runs/<id>/lts/admission.jsonl` and `retrieval.jsonl` are the training
log.

### Training the controller

Every run logs each admission decision with the context the controller saw, whether a peer later retrieved the
entry, and the team's outcome. `task controller:dataset` turns those logs into examples, `task controller:train`
trains the spec 03 §2 model (frozen Qwen3-0.6B embedder, three projection layers, LoRA r=16 α=16) with the spec
03 §3 objective (group-relative advantages per task, usage-aware shaping β=0.5, sparsity λ=0.05), and
`task controller:eval` replays held-out decisions. To switch the live system to it, set
`lts.controller: {kind: learned, adapter_path: ~/.local/state/airlie/controller/latest}`.

## Choosing the team model

The default team / chief model is `generalist` (Qwen3.8-27B, hybrid attention). Swap with one line:
`roles.coder.model` (and `roles.aggregator.model`) at another catalog entry; numbers in
[`docs/bench/`](docs/bench/).

## Configuration

Everything is in [`airlie.yaml`](airlie.yaml); the committed file `includes:` [`config/models.yaml`](config/models.yaml),
[`config/roles.yaml`](config/roles.yaml), [`config/profiles.yaml`](config/profiles.yaml) and the glob
`config/workflows/*.yaml` (the workflows a card's `workflow:<name>` label runs; paths relative to the file that lists
them). A glob expands to its matching files in sorted order, and one that matches nothing is an error. Each included
fragment is deep-merged in list order, then the root file's other keys merge on
top, then `airlie.local.yaml` (deep-merged, gitignored, and **not** processed for `includes:`), then the selected
topology profile. Role prompts, model paths, `story.repo`, and the like resolve against the **root** config file's
directory (`airlie.yaml`'s parent), not the including fragment's directory. Copy `airlie.local.example.yaml` to
`airlie.local.yaml` for local overrides. Every seam is a key there: engines (remote HTTP endpoints, `workflow` by default), the model catalog (names and labels;
`bench: true` entries are compared by `task bench:models`), roles, tools and MCP servers, the router, the LTS admission controller, summarizer, scheduler, teams, aggregator, topology profiles, and the
`studio` block (chief role, parallelism, the review: which roles are reviewed, how many fix rounds; `done_check`:
a coder that stops on a brief with acceptance criteria ticks each one against its diff before review, and
`done_check_steps` is what a miss may cost). A role may
declare `tools` (a subset of the builtins plus `decide` and `search_knowledge`), `skills` (a directory of
guidelines with `summary:` frontmatter, routed by the decision model), `knowledge` (a directory the `search_knowledge` tool
indexes with BM25), `vision` (receives the run's images) and `max_steps`. Unknown keys are errors, and every
error names the key path.

A workflow is nodes and lines: a node's `to` names the nodes its outputs feed, a model node's `prompt` is text or
`prompt:<id>`, and a `check`/`gate` node's `run` comes from the catalog that `airlie workflows catalog` prints.

The web app's workflow builder (Workflows, then a name or New) draws that file: a box per node, a port per output,
a line per `to` entry with its condition or loop on it. A click selects a box and shows a small card; a double-click
opens the box's fields (a check or gate picks its `run` from the catalog by title). A bundle box opens its own
workflow in place with a breadcrumb back. Validate runs the composer's checks; Save draft stores the workflow
as a draft a card can choose, and the story screen can adopt the current workflow with one confirm.

A role that thinks (`thinking: true` with a `thinking_budget`) needs `max_tokens` larger than the budget: `max_tokens`
caps the thinking and the answer together, so the config is refused otherwise, naming the role and both numbers.
A reply the engine cuts off at `max_tokens` ends an agent's turn as `cut_off`, never as a finished answer; the
studio chief refuses one it cannot parse, and the reviewer one with no text.

## Prompts

What a model is told lives in Markdown files, not in Python. A role's own prompt is `prompts/roles/<role>.md`,
chosen by `roles.<role>.prompt` in `airlie.yaml`. Everything else is a hub file in the package, one text each, loaded
by id (`from airlie.prompts import text`): `src/airlie/prompts/rules/` holds the standing instructions placed in a
brief or goal message, and `src/airlie/prompts/steering/` the nudges sent partway through a run when something
goes wrong. They ship with the code they are sent from, so there is no config key and no override directory.

A hub file is YAML front matter, then the exact text sent as a format string (`{name}` is filled in, `{{` and `}}`
write a literal brace). The id is the file name:

| Key | Holds |
| --- | --- |
| `kind` | `rule` or `steering`, the folder it is in |
| `roles` | the roles that can receive it under `airlie.yaml`'s tool lists and flags (`write-now` only a role that can write and has no `write_schedule`), or `any` |
| `fields` | the `{placeholders}` the body uses; the loader checks they match exactly |
| `when` | steering only: the condition that sends it, in a sentence |
| `governs` | the subjects it instructs on, each with its stance (`tests.location: new-file`) |
| `yields_to` | ids it gives way to at runtime when they disagree: precedence, not an accepted conflict |
| `why` | the incident behind the wording: what went wrong, the run, the date |

The contradiction check runs in `pytest` (`tests/unit/test_prompt_contradictions.py`): two rules, or two steering
texts, that reach the same role with different stances on one subject fail the gate unless one `yields_to` the
other or the conflict is in that test's `ACCEPTED` list. Steering outranks rules, so it is never compared with a
rule; that is its job, as when "reading is switched off" overrides a role prompt that says to read first.
`yields_to` is for runtime precedence only: a stage's brief over the role prompt's general default (the layer's
named test file over `coder.md`'s new one), a later nudge over an earlier one. It skips the whole pair, and the
gate fails on an entry that settles no live disagreement. A real conflict that stands for now goes in `ACCEPTED`
instead, one entry per role and subject with both sources and a reason; that entry covers that subject alone, and
the gate fails once it no longer matches a conflict. `task prompts:subjects` lists every subject with the texts that
tag it, so a near-duplicate subject shows. `task prompts:audit` is the model's read of the same ground (above).
The team loop, studio, fork and the story pipeline record each steering text they send as a `steer` event, with
its `id`, the `role` and where it went (`team_id` and `step`, a `subtask`, or a story's `layer` or planning
`round`), so those events show which nudges still fire. The single-call loop behind a role's one-shot calls
(`tools/loop.py`) has no stream of its own: it tells its caller through `on_steer`, and the studio's chief and
reviewer and the supervisor's triage record `no-more-tools` from it. `airlie supervisor try` has no stream, so
there it goes unrecorded.

Role prompts take part in the check as rules, through flat front-matter lines in `prompts/roles/<role>.md`: a
`governs.<subject>: <value>` line per subject and `yields_to: a, b`. Other texts name a role prompt as
`role:<name>`.

To change a wording on purpose, edit the hub file, then regenerate the golden snapshot of what every text renders
to: `AIRLIE_WRITE_PROMPT_TEXTS=1 uv run pytest tests/unit/test_prompt_texts.py`. That run fails on purpose after it
writes `tests/unit/data/prompt_texts.json`; rerun without the flag and review the JSON diff, which is exactly what
the models will now read differently.

To add a nudge or a rule, add a file with a non-empty `governs` (the gate fails on one without). A nudge is sent
through its module's `_steer` helper (the fork emits the event itself), so it is recorded as a `steer` event.
`test_prompt_contradictions.py` then checks the new text against the others that reach the same role.

Design: `docs/superpowers/specs/2026-10-22-prompt-hub-design.md`.

## Hardware

Built for a Mac Studio M5 Max with 128 GB unified memory. Set the wired limit (`memory.wired_limit_mb`) now and at every boot:

```bash
task gpu:limit
```

`task doctor` tells you if it is missing.

## License

airlie is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE). You may use, change and
share it for any noncommercial purpose: personal projects, hobby work, research, education, or a charity or
public body. Using it in or for a company needs a separate commercial license; ask via
[github.com/weyenk](https://github.com/weyenk).
