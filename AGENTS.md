# airlie

Python 3.12 with uv; every action is a Taskfile task (`task --list`). Read `README.md` first.

## Configuration

Committed config is split for readability:

- [`airlie.yaml`](airlie.yaml) — root index (`includes:`, engines, tools, story, …)
- [`config/models.yaml`](config/models.yaml), [`config/roles.yaml`](config/roles.yaml), [`config/profiles.yaml`](config/profiles.yaml) — included fragments
- [`config/workflows/*.yaml`](config/workflows/) — included by that glob (sorted; a glob that matches nothing is an error): the story workflow ([`story.yaml`](config/workflows/story.yaml)), its layer bundle ([`layer.yaml`](config/workflows/layer.yaml)) and any workflow the builder published. The composer runs the story's when `story.composer` is on; a card's `workflow:<name>` label runs any of them through the composer regardless of `story.composer`

A workflow is nodes and lines: a node's `to` lists the nodes its outputs go to (each line may carry a `when`
condition or a `loop` bound), and a `bundle` node runs another workflow, once per item with `each`. A model
node's `prompt` is the text itself or `prompt:<id>` for a prompt file; a `check` or `gate` node's `run` names
an operation from the catalog, and `airlie workflows catalog` prints it (name, title, inputs, outputs,
transient, every_run).

The web builder (`/workflows/build/<name>`) edits a workflow as the engine reads it: one box per node, a port per
output, a line per `to` entry (its condition and loop live on the line; a line that only orders carries no
artifact). Drag a port to another box to connect; double-click a box (or Edit on its card) for its fields in a
modal, a line for its wire modal, and the Workflow button for the workflow's type, inputs and outputs. A bundle
box opens its workflow in place on a double-click, with a breadcrumb back that restores the parent's unsaved
edits. Save draft writes a draft the board can choose; a draft in the old schema is read upgraded and says so
until saved. A story's screen and card offer "Use the current workflow" to adopt the published one.

Merge order: `includes` → root keys → `airlie.local.yaml` → topology profile. Role prompts, model paths, and similar data paths resolve against the root file's directory, not the fragment's. Prefer editing the fragment that owns a section rather than inlining it back into the root.

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
