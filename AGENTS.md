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
