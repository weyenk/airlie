"""Run one layer of the test pyramid: tests/<layer>/ under pytest, or say so and succeed when it is empty.

    uv run python scripts/run_layer.py functional [pytest args]

The layers, lowest first: unit; then functional, contract, integration (units in concert); then e2e,
journey (behavioural); then performance, security (environmental). A layer with no tests yet is not a
failure, so the pipeline can carry every stage before every stage has tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYERS = ("unit", "functional", "contract", "integration", "e2e", "journey", "performance", "security")


def has_tests(layer_dir: Path) -> bool:
    return layer_dir.is_dir() and any(layer_dir.rglob("test_*.py"))


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in LAYERS:
        print(f"usage: run_layer.py {{{'|'.join(LAYERS)}}} [pytest args]", file=sys.stderr)
        return 2
    layer, rest = argv[0], argv[1:]
    layer_dir = ROOT / "tests" / layer
    if not has_tests(layer_dir):
        print(f"no {layer} tests yet (tests/{layer}/); nothing to run")
        return 0
    cmd = [sys.executable, "-m", "pytest", str(layer_dir), *rest]
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
