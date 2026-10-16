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
