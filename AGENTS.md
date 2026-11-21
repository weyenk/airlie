# airlie

Python 3.12 with uv; every action is a Taskfile task (`task --list`). Product overview: [`README.md`](README.md).

This file is already in session context for Qwen Code and VS Code chat. Do not re-read it with tools, and do not explore the tree looking for project rules — use the map below, then open only the files the task needs.

## Layout

| Path | What it is |
| --- | --- |
| `src/airlie/` | Python package (serve, workflows, chat, board, …) |
| `web/` | Vue Studio UI (built into `src/airlie/serve/web_next`) |
| `hub/` | Job Bus API (Vercel); Mac bridge claims work for `task serve` |
| `config/` | `models.yaml`, `roles.yaml`, `profiles.yaml`; optional `workflows/*.yaml` |
| `airlie.yaml` | Root config (`includes:`); overrides in gitignored `airlie.local.yaml` |
| `tests/` | Pyramid under `tests/<layer>/`; quarantine in `tests/unit/quarantine.toml` |
| `docs/` | Deep dives — open only when the task needs them |
| `vendor/` | Pinned third-party trees (byte-for-byte; never edit in place) |

## Configuration

Merge order: `includes` → root keys → `airlie.local.yaml` → topology profile. Paths resolve against the root file's directory. Edit the fragment that owns a section.

- Engines are remote HTTP only (`kind: http`). The committed `workflow` engine takes `AIRLIE_WORKFLOW_BASE_URL` / optional `AIRLIE_WORKFLOW_API_KEY` (or the same keys in `airlie.local.yaml`). Airlie never starts an inference server; `task doctor` probes `GET <base_url>/models`. Point at omlx, llama.cpp, or any OpenAI/Anthropic-compatible API — real URLs stay out of git.
- Catalog models are names/labels in [`config/models.yaml`](config/models.yaml); roles pick a model and the engine is derived.
- Workflows (when you have them) live under [`config/workflows/`](config/workflows/) and are pulled in by an `includes:` glob (empty match is an error — omit the include until a file exists). Production ships none; test copies of story/layer are under `tests/fixtures/workflows/`. Four node kinds: `WORK`, `ROUND_ROBIN`, `SUPERVISOR_WORKERS`, `K_INDEPENDENT_TEAMS`, wired by `edges` (`when` / `loop` on the wire). A `WORK` node has exactly one of `prompt` / `prompt_id` / `run`.
- Chat: `chat.kinds.<plain|repository>.tools` / `ask_first`, `chat.max_steps`; repository tools only on repository kind.
- `task serve`'s manager still owns on-demand resources it registers (repository-chat VMs under `boxes`). SearXNG for `web_search` is outside the manager (`task searxng:up`).

## Test gate

Every change must pass these commands, run in order from the repository root:

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q -n auto --maxprocesses 8
task web:gate
```

Unit tests over 500 ms or on the real clock are quarantined in [`tests/unit/quarantine.toml`](tests/unit/quarantine.toml) and left out of that run; `task test:quarantine` runs only them (they must still pass). CI's unit job sets `AIRLIE_TEST_BUDGET_MS=500` and fails on a new test over it: make it fast, or list it there with a `slow:` or `clock:` reason.
