# airlie

Python 3.12 with uv; every action is a Taskfile task (`task --list`). Read `README.md` first.

## Configuration

Committed config is split for readability:

- [`airlie.yaml`](airlie.yaml) — root index (`includes:`, engines, tools, story, …)
- [`config/models.yaml`](config/models.yaml), [`config/roles.yaml`](config/roles.yaml), [`config/profiles.yaml`](config/profiles.yaml) — included fragments

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
