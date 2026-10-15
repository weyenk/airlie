# airlie

Parallel agent teams on one Apple Silicon Mac, sharing what they learn. airlie runs K independent agent teams
against a [vllm-mlx](https://github.com/waybarrios/vllm-mlx) engine it manages for you, with a Learning-to-Share
memory bank between the teams and an EdgeAgent-style scheduler, exactly as laid out in
[`docs/wiki/build-spec`](docs/wiki/build-spec) (the source of truth). Design notes:
[`docs/superpowers/specs/2026-10-12-airlie-design.md`](docs/superpowers/specs/2026-10-12-airlie-design.md).

## Install

Two tools, then everything else is a task:

```bash
brew install go-task uv
```

```bash
task setup
```

`task setup` installs the pinned Python and every dependency into a project-local environment managed by uv. You
never run pip, activate a venv, or install anything else by hand. Then:

```bash
task doctor
```

```bash
task models:pull
```

## Tasks

Run `task` to list them. The ones that exist today:

| Task | Does |
| --- | --- |
| `task setup` | one-time, idempotent setup |
| `task doctor` | validates `airlie.yaml`, checks the wired-memory limit, RAM, tools, vllm-mlx, model weights, and the spec's memory projection; exit 1 on any red row |
| `task config` | prints the fully resolved configuration (`airlie.yaml` + `airlie.local.yaml`) |
| `task models:list` / `task models:pull [-- name…]` | show the catalog with pinned revisions / download exactly those revisions |
| `task up` / `task down` / `task restart` / `task status` / `task logs [-- -f]` | start/stop every managed engine in `airlie.yaml`: the vllm-mlx process (`main`, all catalog models in its registry) and the llama-server process serving Kev (`decision`, `/v1/systemone`); status shows pid and per-model readiness |
| `task run -- "goal" --repo PATH [--topology fork] [--k 3] [--test-cmd "pytest -q"]` | run the configured topology on a goal; every team/branch works in its own git worktree under the run directory and the report lists each one's outcome, branch and trace |
| `task eval -- lang_detect [--topology fork] [--k 1]` | materialize a graded task from `evals/tasks/`, run it, grade every worktree with the hidden tests |
| `task serve [-- --port 8090]` | OpenAI-compatible endpoint: roles are models (`/v1/models`, `/v1/chat/completions` with the role prompt prepended), plus `POST /v1/airlie/run {goal, repo, k}`; jobs: `POST /v1/airlie/plans`, `/runs` or `/stories`, then `GET /v1/airlie/jobs/{id}?since=&wait=` (long poll), `DELETE` to cancel |
| `task studio -- "goal" --repo PATH [--image shot.png] [--test-cmd "pytest -q"]` | the studio topology: the chief plans in JSON, Kev routes each subtask to a specialist role (and a skill), specialists run on one worktree, a cross-family reviewer gets one fix round, the chief synthesises; `report.md` and `routing.jsonl` land in the run directory |
| `task story -- ISSUE [--repo PATH] [--test-cmd CMD]` / `task story -- --text "request" …` | a Jira issue (a key like `AIR-7`, or its link) or a written request → product's story → architect's layers → each layer built test-first by the studio on its own branch → stacked draft PRs (gh stack) → the issue in *In Review*; only a human merges; a stopped story resumes from its last finished stage; an issue is built in the repository its `repo:<owner>/<name>` label names (its clone in `~/git`, `story.repos_root`), and every layer must pass the `## Test gate` of that repository's `AGENTS.md`; Jira comes from the `ATLASSIAN_*` variables in `~/.config/airlie/.env` (or a `.env` beside `airlie.yaml`), which airlie reads itself and never exports; with `story.github_app.app_id` set, a GitHub App's bot pushes, opens the PRs and authors the layer commits with a token scoped to the story's one repository, never your gh login or SSH key (`docs/integrations/story.md`) |
| `task story:tick` / `task story:check [-- ISSUE]` | one scheduler beat (what the launchd agent runs): build the top *To Do* issue unless a story is in flight / check roles, gh + gh-stack, the Jira credentials, project and statuses, and an issue's label, checkout, trunk and test gate |
| `task story:launchd:install` / `task story:launchd:uninstall` | load / remove the launchd agent that runs the story tick (`airlie story tick`) every 15 minutes |
| `task opencode:install` | copy the OpenCode chief, its subagents, `/studio` and `/story` into `~/.config/opencode` and register `airlie mcp`, an MCP server (stdio) that gives OpenCode the tools `airlie_plan`, `airlie_run`, `airlie_status`, `airlie_cancel` and `airlie_story` over `task serve`'s jobs API, in its `opencode.json` (merged into an entry you already have; after a backup when it changes the file; `docs/integrations/local-agents.md`) |
| `task route:eval -- --min-accuracy 0.9 --out docs/bench/routing.json` | score the Kev router on `evals/routing.jsonl` (54 labelled tasks seeded from ai-mode) against the role criteria in `evals/routing-roles.yaml`: accuracy, mean confidence, wrong-but-clear, confusions |
| `task bench:topology -- --topologies single,teams,fork --repeats 3` | one agent vs K teams vs fork-when-stuck on the graded tasks: wall-clock, steps, forks, winner graded by the hidden tests; verdict against the single-agent baseline |
| `task bench:lts -- --tasks lang_detect --repeats 2` | memory-free baseline vs the configured controller on graded tasks: wall-clock, steps, admissions, retrievals, pass rate, and the spec's ≥25 % verdict |
| `task controller:dataset` / `task controller:train [-- --epochs 5]` / `task controller:eval` | build the training set from every run's admission log; train the spec 03 §2 controller (LoRA on Qwen3-0.6B + projections) with the spec 03 §3 REINFORCE objective; replay held-out decisions against the logged controller |
| `task memory -- RUN_ID [--full]` | show a run's shared memory bank: keys, owning team, cross-team reads |
| `task bench [-- --model NAME --ctx 4k,32k --n 1,2,4]` | engine throughput through the seam: TTFT, prefill and decode tok/s, aggregate at N streams, and whether turn 2 of a conversation hits the prefix cache |
| `task bench:models` | the same for every catalog model marked `bench: true`, one table |
| `task bench:compare -- a.json b.json` | per-cell deltas between two saved runs |
| `task test` / `task test:live` / `task lint` / `task typecheck` / `task check` | unit tests / tests against the running engine / ruff / mypy / lint+types+unit |
| `task static` | lint + typecheck: stage 1 of the pipeline and the pre-push gate (`task hooks:install`, run by `task setup`) |
| `task test:unit` / `test:functional` / `test:contract` / `test:integration` / `test:e2e` / `test:journey` / `test:performance` / `test:security` | one layer of the test pyramid, `tests/<layer>/`; a layer with no tests yet succeeds |
| `task mutate [-- --max-children 6]` / `task mutate:results` | mutation-test every enrolled module (`[tool.airlie.mutation].enrolled`); an uncaught mutant exits 1 |
| `task mutate:changed [-- --base origin/main]` | the pipeline's mutation step: mutants of the enrolled files changed since the base ref, plus every function a changed or new test executes (`--list` previews) |
| `task mutate:project` | mutation-test all of `src/airlie` (the weekly run): enrolled modules gate, the rest is printed as a score table, the backlog |
| `task fmt` | auto-format and auto-fix lint |

Coming per the roadmap: `task events`, `task trace`, `task bench:gaia` (M4b).

## Pipeline

`.github/workflows/pipeline.yml` runs one stage per level of the test pyramid; a stage starts only when the one
before it is green, and nothing in it builds or serves the application:

1. **static**: `task static` (ruff + mypy). The same command runs before every push to origin (`.githooks/pre-push`).
2. **lowest level**: `task test:unit`, then `task mutate:changed`. Mutation testing comes after the unit tests
   and only covers what the change touches: the enrolled source files it modified, and every function that a
   changed or new test executes, so a new test is held to "the code it runs has its mutants caught".
3. **units in concert**: functional, contract, integration (`tests/functional`, `tests/contract`, `tests/integration`).
4. **behavioural**: e2e, journey.
5. **environmental**: performance, security.

Stages 3 to 5 pass while their `tests/<layer>/` directory is empty and start enforcing as soon as it has tests.
`.github/workflows/mutation-weekly.yml` runs `task mutate:project` every Monday over the whole project and
uploads the per-module score table; enrolled modules must be clean, the other modules are the backlog. To
enrol a module, add its path to `[tool.airlie.mutation].enrolled` once `task mutate:changed` is clean for it.

## Using airlie from another client

Terminal agents (OpenCode, Claude Code, Qwen Code) call the engine directly: see
[`docs/integrations/local-agents.md`](docs/integrations/local-agents.md). VS Code's chat (Local session) and
Cursor (through a tunnel) are in `docs/integrations/` too.


`task serve` starts an OpenAI-compatible server on `127.0.0.1:8090`. Point Claude Code, Qwen Code, Cursor or
`curl` at `http://127.0.0.1:8090/v1` and pick a role as the model name; airlie prepends the role prompt and
forwards to the engine. `POST /v1/airlie/run` with `{"goal": ..., "repo": "/path", "k": 3}` runs the parallel
teams and returns the aggregated answer and winning branch. The jobs API (`/v1/airlie/plans`, `/runs`, `/stories`)
starts studio plans, studio runs and stories in the background and is followed by long polls
([`docs/integrations/story.md`](docs/integrations/story.md)). **Without `--api-key`, any program on this machine can
call every route, including the ones that start stories, which push branches and open draft PRs:** run
`task serve -- --api-key SECRET` (or set `AIRLIE_SERVE_API_KEY`) to require `Authorization: Bearer SECRET`. A
`task serve` that is already running must be restarted to get the jobs API.

## How a run works

`task run` spawns K teams (`teams.k`). Each team is the built-in ReAct loop (`teams.orchestrator: native`, the
default since 2026-10-13; `autogen` selects the spec 01 §2 AutoGen `AssistantAgent` over the same seams) talking to the engine through the OpenAI endpoint, with sandboxed
tools (`read_file`, `write_file`, `edit_file`, `list_dir`, `grep`, `shell`, `run_tests`, `git`, plus any MCP
servers under `mcp:`) confined to its own worktree. Generation slots are accounted per engine; a team inside a
tool call holds no slot (spec 04 §2 suspend-and-yield), and the engine's prefix cache makes its next turn cheap.
When the teams finish, the aggregator (spec 01 §4 step 9; `aggregator.kind: llm` or `test_score`) runs the test
command in every worktree, picks one winning branch (`airlie/<run_id>/team-N`) and writes the final answer to
`runs/<run_id>/report.md`. With `teams.stop_on_first_pass: true` the first team whose tests pass cancels the
others. Every step is written to `~/.local/state/airlie/runs/<run_id>/events.jsonl` and `trace/`.

The EdgeAgent HAL scheduler (spec 04 §1) runs alongside: accepted-draft counts reported by the engine update each
team's EMA and re-allocate the 16-slot draft budget after every step (`hal` events). vllm-mlx 0.5.0 cannot take a
per-request draft length, so the budgets are computed and logged but only *applied* on an engine with
`engines.<name>.per_request_draft: true`.

## Topologies

`topology.kind` picks how branches are arranged over the engine (design: `docs/superpowers/specs/2026-10-13-fork-when-stuck-design.md`):

- **`single`**: one agent, no shared memory (`teams.k` forced to 1, LTS off). **The default since the 2026-10-13
  benchmark** (`docs/bench/2026-10-13-topology-bench.md`): it matched K=3 on pass rate at 45 % of the wall-clock.
- **`teams`** (the build spec): K independent teams from the start, sharing through the LTS memory bank, one
  winner picked by the aggregator (`teams.k: 3`, `lts.enabled: true`).
- **`fork`** (fork-when-stuck): one trunk runs with the whole engine budget until it is stuck (two consecutive
  failing `run_tests`, or a repeated action). Its working tree is committed, `fork.width` children get worktrees
  at that commit and the trunk's conversation verbatim (a prefix-cache hit) plus one nudge each: a distinct
  root-cause hypothesis from the `hypothesizer` role (`fork.diversify: hypotheses`) or a temperature spread. The
  first child whose tests pass cancels its siblings and continues as the trunk; with no pass, the child with the
  fewest failing tests continues, up to `fork.max_rounds`. Branches are `trunk`, `r1-1`, `r1-2`, `r2-1`, … and
  the report names the winning git branch. Events: `fork_point`, `fork_resolved`, `branch_done`.

Every topology injects a deterministic repository map (tree, sizes, Python signatures; `repo_map.*`, cached under
`<state_dir>/repo_maps/`) before the goal, so no branch re-discovers the layout.

- **`studio`** (design: `docs/superpowers/specs/2026-10-13-studio-topology-design.md`): one generalist
  (`chief`, Qwen3.8-27B, also the vision model) scopes the request into a JSON plan of subtasks; the router
  (Kev on llama-server, or the chief's own hint with `router.kind: chief`) assigns each subtask a specialist
  role from the roles whose prompts carry a `summary:` line, and for roles with a skill library (`qa` →
  `skills/qa/*.md`) picks the guideline to inject; specialists are prompts over three models (generalist, the
  coder for `coder`/`qa`/`security`, Muse Glimmer for the `reviewer`) and run as single agents on one worktree,
  writers one at a time, readers in parallel; after a code subtask the reviewer (a different model family, on
  purpose: judges favour their own family's text) returns a JSON verdict and blocking findings give the coder
  one fix round; the chief synthesises. Run it with `task studio -- "goal" --repo . [--image shot.png]` after
  `task restart -- --topology studio` (the profile preloads the generalist and the reviewer; the server keeps
  parsing the coder family's tool calls and airlie parses what it leaves raw, such as Muse's channels). Every routing decision is logged (`runs/<id>/routing.jsonl`) and scorable with
  `task route:eval`.

**Profiles.** `topology.profiles.<kind>` is a partial config deep-merged over `airlie.yaml` when that kind is
selected (`extends: <profile>` chains), so each topology declares its own team count, memory settings, contexts
and models. `--topology` on `task run`/`task eval` and `topology` in `POST /v1/airlie/run` select the profile for
one run; a profile name that is not a kind (a named variant such as `studio-qwen38`, or a parent other
profiles `extends`) can be selected the same way and takes its kind from its own `topology.kind` or the file's.
`--k N` with `N > 1` under the `single` kind selects the `teams` profile. `task doctor` projects memory per
profile across every managed engine. A profile that changes engine settings (`max_kv_size`, `server_parsers`,
the resident model set) takes effect at the next `task restart`.

## Learning to Share

After every step a team's (action, observation) pair is summarized into a one-line key by the `summarizer` role
and offered to the admission controller (`lts.controller.kind`: `heuristic` now, `judge` for a small-model
YES/NO, `learned` once the spec 03 §2 controller is trained in M5, `always`/`never` for baselines). Admitted
entries go into the shared memory bank. Summarize + decide run in the background (`lts.background_admission`),
so a team never waits on admission. Before each model call a team sees the keys ranked for its current step
under `[Shared Memory Keys Available]` (`lts.ranking`, `lts.max_keys`; its own keys excluded unless
`lts.inject_own`), the top `lts.inline_top` carry their value inline once, and any full value is one
`retrieve_value` call away. `task memory -- RUN_ID` shows the bank; `runs/<id>/lts/admission.jsonl` and
`retrieval.jsonl` are the training log.

### Training the controller

Every run logs each admission decision with the context the controller saw, whether a peer later retrieved the
entry, and the team's outcome. `task controller:dataset` turns those logs into examples, `task controller:train`
trains the spec 03 §2 model (frozen Qwen3-0.6B embedder, three projection layers, LoRA r=16 α=16) with the spec
03 §3 objective (group-relative advantages per task, usage-aware shaping β=0.5, sparsity λ=0.05), and
`task controller:eval` replays held-out decisions. To switch the live system to it, set
`lts.controller: {kind: learned, adapter_path: ~/.local/state/airlie/controller/latest}`.

## Choosing the team model

`task doctor` warns when a catalog model uses hybrid (linear/SSM) attention: vllm-mlx 0.5.0 cannot keep prefix-cache
entries for those, so every agent turn re-prefills the whole conversation (measured: a 4k-token prefix cost 4.3 s on
every turn with Qwen3.8-27B, versus 72 ms on turn 2 with a standard-attention model). Keep `roles.coder.model` on a
standard-attention model; the numbers are in [`docs/bench/`](docs/bench/).

## Configuration

Everything is in [`airlie.yaml`](airlie.yaml); copy `airlie.local.example.yaml` to `airlie.local.yaml` for local
overrides (deep-merged, gitignored). Every seam is a key there: engines (`vllm_mlx` managed process, a
`llama_server` managed process for one GGUF such as Kev, or any `openai_http` server), the model catalog (pinned
Hugging Face revisions; `bench: true` entries are compared by `task bench:models`; `tool_call_parser` /
`reasoning_parser` name the parser classes, vllm-mlx's or airlie's own (`qwen3_xml_raw`, `muse_glimmer`, which
the managed engine loads at start), that run on each model's output, in the engine or, when
`engines.<e>.server_parsers` is false, in airlie), roles, tools and MCP servers, the router (`jev` on Kev's `/v1/systemone`
or `chief`), the LTS admission controller, summarizer, scheduler, teams, aggregator, topology profiles, and the
`studio` block (chief role, parallelism, the review: which roles are reviewed, how many fix rounds). A role may
declare `tools` (a subset of the builtins plus `decide` and `search_knowledge`), `skills` (a directory of
guidelines with `summary:` frontmatter, routed by Kev), `knowledge` (a directory the `search_knowledge` tool
indexes with BM25), `vision` (receives the run's images) and `max_steps`. Unknown keys are errors, and every
error names the key path.

Swapping the team model is one line: point `roles.coder.model` (and `roles.aggregator.model`) at another catalog
entry.

## Hardware

Built for a Mac Studio M5 Max with 128 GB unified memory. Set the wired limit once (spec 01 §1):

```bash
sudo sysctl iogpu.wired_limit_mb=106496
```

`task doctor` tells you if it is missing.

## License

airlie is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE). You may use, change and
share it for any noncommercial purpose: personal projects, hobby work, research, education, or a charity or
public body. Using it in or for a company needs a separate commercial license; ask via
[github.com/weyenk](https://github.com/weyenk).
