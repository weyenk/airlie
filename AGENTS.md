# airlie

Python 3.12 with uv; every action is a Taskfile task (`task --list`). Read `README.md` first.

## Test gate

Every change must pass these commands, run in order from the repository root:

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

## Mutation gate

A story built by airlie runs this once, over the whole story's change against its base (`{base}`), after
its last layer passes the test gate and before its PRs open:

```sh
uv run python scripts/mutate_changed.py --base {base} --max-children 6
```
