---
trigger: always
---

# CONTINUE.md — airlie project guide

> This file is loaded into context for every conversation in this workspace.
> Keep it accurate; verify anything marked "needs verification" before relying on it.

## 1. Project Overview

**airlie** is an *agentic workflow platform*: it runs software-development workflows
end to end — from a board card (initiative → epic → story → subtask) through planning,
build, review, and PR — using specialist LLM agents ("roles") that share what they learn
on a single Apple Silicon Mac. Today it runs development workflows; the same
"line" concept is intended to generalize to non-software work later.

### Key technologies

| Layer | Tech |
| --- | --- |
| Language | Python **3.12** (`>=3.12,<3.13`), managed with **uv** — never install deps by hand |
| Task runner | **Taskfile** (`Taskfile.yml`) — *every* action is a `task` target; run `task --list` |
| Engines | `mlx` / `mlx-vlm` (in-process MLX engines on Apple Silicon) or any `openai_http` server |
| Serving | FastAPI + uvicorn (`task serve` → OpenAI-compatible endpoint on `127.0.0.1:8090`) |
| Web app | Vue 3 + TypeScript + Vite + Pinia in `web/` (node **26** pinned; must be first `node` on `PATH`) |
| Testing | pytest (layers under `tests/`), mutmut mutation testing, vitest for `web/` |
| Typing/lint | mypy **strict**, ruff (lint + format) |
| Config | YAML: `airlie.yaml` + `config/*.yaml` fragments (see §3) |

### High-level architecture

```
prompts/ & config/         → declarative config + Markdown prompts (data, not code)
src/airlie/
  cli.py                   → typer CLI (`airlie …`); Taskfile targets call it
  config/                  → YAML validation → AirlieConfig (schema.py, load.py)
  engine/                  → Engine seam: mlx_vlm / http_engine / routed / fake…
  orchestrator.py          → ParallelAgentTeamOrchestrator: K teams on one engine
  teams/                   → native + AutoGen team loops, worktrees, aggregation
  topology/                → studio / fork / diversify / review flows
  tools/                   → tools agents call (fs, shell, sandbox, compute, web, MCP…)
  story/                   → story pipeline: cards → layers → draft PRs
  serve/                   → FastAPI app: chat turn route, board, workflows, jobs
  lts/                     → Learning-to-Share shared memory + admission controller
  train/                   → LTS controller training (LoRA on Qwen3-0.6B; spec 03)
  manager/                 → engine lifecycle (pinned vs on_demand), leases, VMs
web/                       → Vue SPA served from src/airlie/serve/web_next
tests/<layer>/             → unit, functional, contract, integration, e2e, …
vendor/superpowers/        → pinned, byte-exact copy of superpowers skills (never edit)
```

Key ideas:

- **Config is data.** `airlie.yaml` (root) `includes:` `config/models.yaml`,
  `config/roles.yaml`, `config/profiles.yaml`, and `config/workflows/*.yaml`.
  Merge order: `includes` → root keys → `airlie.local.yaml` (gitignored, NOT
  re-processed for `includes:`) → selected topology profile. Paths in fragments
  resolve against the **root** file's directory. Edit the fragment that owns a
  section rather than inlining it back into `airlie.yaml`.
- **Engines have a `lifecycle`:** `pinned` (`task up` starts; e.g. `main`,
  `gemma`, `decision`) vs `on_demand` (`small`, `reviewer`; started on first use,
  unloaded after `idle_unload_s`). Role calls hold their engine — in `task serve`
  directly, elsewhere via lease from serve.
- **Roles are prompts.** A role's instructions live in `prompts/roles/<role>.md`;
  hub rules/steering texts in `src/airlie/prompts/{rules,steering}/` ship with the
  code that sends them. A workflow (`config/workflows/*.yaml`) is nodes + lines:
  model/check/gate/bundle nodes wired by `to:` edges; the composer in serve runs
  the story workflow when `story.composer` is on, or any workflow via a card's
  `workflow:<name>` label.

## 2. Getting Started

### Prerequisites

```bash
brew install go-task uv node@26   # three tools; everything else is a task
```

### Install & verify

```bash
task setup      # installs pinned Python + deps into uv-managed env; builds web/
task doctor     # validates airlie.yaml, checks RAM limit, tools, models, memory projection
task models:pull  # download pinned model weights
```

### Common entries

```bash
task --list                       # show every task
task serve [-- --port 8090]     # OpenAI-compatible endpoint + web app
task up | down | restart | status | logs   # engine lifecycle
task story -- AIR-7 [--repo PATH]  # run a story through to draft PRs
task studio -- "goal" --repo PATH  # studio topology (the supported path)
```

### Running tests

Gate — run **in order from the repo root**:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q -n auto --maxprocesses 8
task web:gate
```

Slow/clock-dependent tests are quarantined in `tests/unit/quarantine.toml`
(`task test:quarantine` runs only those; they must still pass). The pipeline
(`Taskfile.yml` / `.github/workflows/pipeline.yml`) runs the five-stage test pyramid; `task test:<layer>` runs one layer.

## 3. Project Structure

| Path | Purpose |
| --- | --- |
| `airlie.yaml` | root config index (engines, tools, story, chat, …) |
| `config/{models,roles,profiles}.yaml`, `config/workflows/*.yaml` | included fragments (see §1) |
| `Taskfile.yml` | every runnable action |
| `src/airlie/` | the package; key modules listed in §1 |
| `src/airlie/cli.py` | typer CLI — humans use `task`, which calls `airlie …` subcommands |
| `src/airlie/prompts/` | hub texts (rules/steering) with YAML front matter |
| `prompts/` | (repo root) role prompts chosen by `roles.<role>.prompt` |
| `web/` | Vue SPA; build output goes to `src/airlie/serve/web_next` |
| `hub/` | Job Bus hub (npm project; `task hub:gate`) |
| `tests/` | pyramid layers + `conftest.py`, `fixtures/`, `support/` |
| `docs/superpowers/specs/` | design docs (studio topology, prompt hub, fork-when-stuck, …) — read before related work |
| `docs/bench/`, `evals/` | benchmark records + graded tasks |
| `ops/test`, `ops/e2e`, `ops/searxng` | Containerfiles for CI/test images |
| `vendor/superpowers/` | pinned skills copy (`PIN.md`, `MANIFEST.sha256`) — verify with `task superpowers:verify`, never edit |
| `.claude/skills/using-superpowers/SKILL.md`, `src/airlie/skills/` | skills used by the chat `skill` tool |

## 4. Development Workflow

- **Everything is a task.** Check `task --list`; do not run pip/npm/mutmut directly
  unless a task wraps it. `task setup` is idempotent.
- **Style:** ruff lint (`E,F,I,UP,B,SIM`) + ruff format; mypy `strict = true` over
  `src/airlie`. `task fmt` auto-fixes; `task lint`/`typecheck`/`static` check.
- **Tests:** every change must pass the §2 gate. New tests >500 ms or clock-based →
  quarantine file with a `slow:`/`clock:` reason. Match the existing test layer
  (`tests/unit/` by default).
- **Config edits:** edit the owning fragment (`config/…`), don't inline into
  `airlie.yaml`; copy `airlie.local.example.yaml` → `airlie.local.yaml` for local
  overrides (gitignored). Set `story.github_app.app_id: null` in `airlie.local.yaml`
  to publish with your own gh login.
- **Prompts:** to change wording, edit the hub file, then regenerate snapshots with
  `AIRLIE_WRITE_PROMPT_TEXTS=1 uv run pytest tests/unit/test_prompt_texts.py`
  (rerun without the flag; review the JSON diff). `task prompts:subjects` lists
  near-duplicate `governs:` subjects; `task prompts:audit` (engine needed, not
  deterministic) is advisory.
- **Don't edit `vendor/superpowers/`** — re-copy a release to update.
- **Commits/PRs:** stories built by airlie go to draft PRs (`story.publish`:
  `single` = one PR per top layer; `stack` = one per layer via gh-stack); only a
  human merges. Pre-push gate: `task static` via `.githooks/pre-push`
  (`task hooks:install`).
- **Vendored/serial work:** each team/branch works in its own git worktree under
  the run directory; `task story:restack`/`story:restart` move or restart a stopped
  story onto current trunk.

## 5. Key Concepts

- **Line** — the automation loop: serve runs a "beat" every minute (launchd agent
  every 15 min: `task line:tick`); gates + bookkeeping, then one job in the
  planning lane and one in the build lane. `task line:pause|resume|stats|why`.
- **Card hierarchy** — initiative → epic → story → subtask on the local board
  (`task board:export|import-jira`; default source of stories).
- **Studio topology** (supported): `chief` (Qwen, generalist) plans JSON subtasks →
  router (decision model, or `router.kind: chief`) assigns specialist roles →
  specialists run on one worktree (writers serial, readers parallel) → cross-family
  reviewer gets one fix round → chief synthesizes. `task studio`.
- **Topologies** `single`/`teams`/`fork` are experimental (research/bench only).
- **Roles** (`config/roles.yaml`): named seats (`chief`, `coder`, `reviewer`,
  `tester`, `architect`, `decider-2b`…) each with a prompt, model, tools, optional
  `skills`/`knowledge`, `max_tokens`/`thinking` — a role must have
  `max_tokens` > its thinking budget or config is refused.
- **LTS (Learning to Share)** — shared-memory bank keyed by one-line summaries;
  admission controller (`heuristic`/`judge`/`learned`/`always`/`never`) decides
  what gets admitted; `task memory -- RUN_ID` inspects a bank.
- **Workflow graph** — nodes (model/check/gate/bundle) + edges (`to:` with `when:`
  or `loop:`); edited visually in the web workflow builder, saved as drafts the
  board can adopt.
- **Tart VMs** — repository chats run inside their own VM (`boxes.image`,
  `airlie-env-<key>` cached images); tools run in the VM, never the host; needs
  `tart` + `softnet` (needs macOS ≥26, SUID install).

## 6. Common Tasks

- **Change config (models/roles/profiles/workflows):** edit the owning file under
  `config/`; verify with `task config` (resolved output) and `task doctor`.
- **Add/modify a prompt:** edit/`src/airlie/prompts/<rules|steering>/<id>.md` (front
  matter: `kind`, `roles`, `fields`, `when`, `governs`, `yields_to`, `why`), then
  regenerate prompt-text snapshots (§4) and run
  `tests/unit/test_prompt_contradictions.py` (conflicts need a `yields_to` chain
  or an `ACCEPTED` entry).
- **Add a task:** add to `Taskfile.yml`, run it, and check against the §2 gate.
- **Build a feature:** brainstorm → plan (`writing-plans` skill) → write failing
  test first (test-driven-development) → implement → review
  (`requesting-code-review`) → finish (`finishing-a-development-branch`).
- **Run a story locally:** `task story -- <ISSUE-or-text>`; restarts/restacks via
  `task story:restart`/`task story:restack`.
- **Call the engine from another client:** `task serve` → point clients at
  `http://127.0.0.1:8090/v1`, pick a role as model name. Without `--api-key` any
  local process can call every route — run `task serve -- --api-key SECRET` (or set
  `AIRLIE_SERVE_API_KEY`) to lock down.

## 7. Troubleshooting

- **"an nvm node" error / wrong node:** install node 26 and ensure it's first on
  `PATH`; `task web:*` tasks check this against `web/package.json`.
- **Engine won't start / memory pressure:** `task doctor` shows the memory
  projection (pinned set = floor, on-demand on top); `task gpu:limit` sets
  `iogpu.wired_limit_mb` now and at boot (needs password). `task status`/`logs`.
- **`task story`/`line:tick` can't reach Jira:** needs `ATLASSIAN_*` in
  `~/.config/airlie/.env` or a `.env` beside `airlie.yaml`; `task story:check` names
  what's missing.
- **Chat can't reach network / VM won't start:** install `tart` and `softnet` per
  §5; `task doctor` has a `softnet` row.
- **Web app out of sync with backend:** rebuild with `task web:build` (skipped when
  nothing under `web/` changed); dev server: `task web:dev` (proxies `/v1` → 8090).
- **Config loads but behaves oddly:** remember merge order (§1); confirm with
  `task config`. Errors name the key path — unknown keys fail with path shown.
- **Long/serial test timeouts:** quarantined tests run only under
  `task test:quarantine`; check that list before assuming a suite is slow.

## 8. References

- `README.md` — full user-facing docs (start here).
- `AGENTS.md` — agent-facing rules (config split, test gate).
- `docs/superpowers/specs/` — design docs, e.g.
  `2026-10-13-studio-topology-design.md`, `2026-10-22-prompt-hub-design.md`,
  `2026-10-13-fork-when-stuck-design.md`.
- `docs/integrations/` — `story.md`, `sign-in.md`, `web-tools.md`,
  `local-agents.md`, `environment.example.json`, `bridge-contract.md`.
- `Taskfile.yml` — authoritative task list (`task --list`).
- `airlie.yaml` + `config/*` — configuration of record.
- `vendor/superpowers/PIN.md` + `MANIFEST.sha256` — pinned skills.
- `Taskfile.yml` pipeline stage docs (inline comments) and `docs/bench/` for
  benchmark records.

### Needs verification

- Exact current pinned model names/revisions → check `config/models.yaml` +
  `task models:list` before editing role→model mappings.
- `config/workflows/` contents and whether `story.composer` is on → inspect both.
- `tests/unit/quarantine.toml` entries → check before assuming a test must stay
  in the normal run.
