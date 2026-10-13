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
| `task up` / `task down` / `task restart` / `task status` / `task logs [-- -f]` | start/stop the vllm-mlx engine declared in `airlie.yaml` (one process, all catalog models in its registry); status shows pid and per-model readiness |
| `task run -- "goal" --repo PATH [--topology fork] [--k 3] [--test-cmd "pytest -q"]` | run the configured topology on a goal; every team/branch works in its own git worktree under the run directory and the report lists each one's outcome, branch and trace |
| `task eval -- lang_detect [--topology fork] [--k 1]` | materialize a graded task from `evals/tasks/`, run it, grade every worktree with the hidden tests |
| `task serve [-- --port 8090]` | OpenAI-compatible endpoint: roles are models (`/v1/models`, `/v1/chat/completions` with the role prompt prepended), plus `POST /v1/airlie/run {goal, repo, k}` |
| `task bench:topology -- --topologies single,teams,fork --repeats 3` | one agent vs the spec's K teams vs fork-when-stuck on the graded tasks: wall-clock, steps, forks, winner graded by the hidden tests; verdict against the single-agent baseline |
| `task bench:lts -- --tasks lang_detect --repeats 2` | memory-free baseline vs the configured controller on graded tasks: wall-clock, steps, admissions, retrievals, pass rate, and the spec's ≥25 % verdict |
| `task controller:dataset` / `task controller:train [-- --epochs 5]` / `task controller:eval` | build the training set from every run's admission log; train the spec 03 §2 controller (LoRA on Qwen3-0.6B + projections) with the spec 03 §3 REINFORCE objective; replay held-out decisions against the logged controller |
| `task memory -- RUN_ID [--full]` | show a run's shared memory bank: keys, owning team, cross-team reads |
| `task bench [-- --model NAME --ctx 4k,32k --n 1,2,4]` | engine throughput through the seam: TTFT, prefill and decode tok/s, aggregate at N streams, and whether turn 2 of a conversation hits the prefix cache |
| `task bench:models` | the same for every catalog model marked `bench: true`, one table |
| `task bench:compare -- a.json b.json` | per-cell deltas between two saved runs |
| `task test` / `task test:live` / `task lint` / `task typecheck` / `task check` | unit tests / tests against the running engine / ruff / mypy / lint+types+unit |
| `task fmt` | auto-format and auto-fix lint |

Coming per the roadmap: `task events`, `task trace`, `task bench:gaia` (M4b).

## Using airlie from another client

`task serve` starts an OpenAI-compatible server on `127.0.0.1:8090`. Point Claude Code, Qwen Code, Cursor or
`curl` at `http://127.0.0.1:8090/v1` and pick a role as the model name; airlie prepends the role prompt and
forwards to the engine. `POST /v1/airlie/run` with `{"goal": ..., "repo": "/path", "k": 3}` runs the parallel
teams and returns the aggregated answer and winning branch.

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

- **`teams`** (the build spec): K independent teams from the start, sharing through the LTS memory bank, one
  winner picked by the aggregator. `teams.k: 1` with `lts.enabled: false` is the single agent, **the default since
  the 2026-10-13 benchmark** (`docs/bench/2026-10-13-topology-bench.md`): it matched K=3 on pass rate at 45 % of
  the wall-clock. The spec's K=3 + LTS is `teams.k: 3`, `lts.enabled: true`.
- **`fork`** (fork-when-stuck): one trunk runs with the whole engine budget until it is stuck (two consecutive
  failing `run_tests`, or a repeated action). Its working tree is committed, `fork.width` children get worktrees
  at that commit and the trunk's conversation verbatim (a prefix-cache hit) plus one nudge each: a distinct
  root-cause hypothesis from the `hypothesizer` role (`fork.diversify: hypotheses`) or a temperature spread. The
  first child whose tests pass cancels its siblings and continues as the trunk; with no pass, the child with the
  fewest failing tests continues, up to `fork.max_rounds`. Branches are `trunk`, `r1-1`, `r1-2`, `r2-1`, … and
  the report names the winning git branch. Events: `fork_point`, `fork_resolved`, `branch_done`.

Every topology injects a deterministic repository map (tree, sizes, Python signatures; `repo_map.*`, cached under
`<state_dir>/repo_maps/`) before the goal, so no branch re-discovers the layout. `--topology` on `task run`/`task
eval` and `topology` in `POST /v1/airlie/run` override the config for one run; `task bench:topology` compares them.

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
overrides (deep-merged, gitignored). Every seam is a key there: engines (`vllm_mlx` managed process or any
`openai_http` server), the model catalog (pinned Hugging Face revisions; `bench: true` entries are compared by
`task bench:models`), roles, tools and MCP servers, the LTS admission controller, summarizer, scheduler, teams,
aggregator. Unknown keys are errors, and every error names the key path.

Swapping the team model is one line: point `roles.coder.model` (and `roles.aggregator.model`) at another catalog
entry.

## Hardware

Built for a Mac Studio M5 Max with 128 GB unified memory. Set the wired limit once (spec 01 §1):

```bash
sudo sysctl iogpu.wired_limit_mb=106496
```

`task doctor` tells you if it is missing.
