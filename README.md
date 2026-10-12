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
| `task bench [-- --model NAME --ctx 4k,32k --n 1,2,4]` | engine throughput through the seam: TTFT, prefill and decode tok/s, aggregate at N streams, and whether turn 2 of a conversation hits the prefix cache |
| `task bench:models` | the same for every catalog model marked `bench: true`, one table |
| `task bench:compare -- a.json b.json` | per-cell deltas between two saved runs |
| `task test` / `task test:live` / `task lint` / `task typecheck` / `task check` | unit tests / tests against the running engine / ruff / mypy / lint+types+unit |
| `task fmt` | auto-format and auto-fix lint |

Coming per the roadmap: `task run -- "goal"`, `task serve`, `task bench:lts`, `task events`, `task trace`,
`task memory`, `task controller:*`.

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
