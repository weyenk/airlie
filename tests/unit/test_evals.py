"""Graded eval tasks: the studio_feature task loads and its hidden tests exercise the package it asks for."""

from pathlib import Path

from airlie.evals import load_task

ROOT = Path(__file__).resolve().parents[2]


def test_studio_feature_task_loads_and_targets_the_ledger_package() -> None:
    task = load_task(ROOT / "evals/tasks/studio_feature.yaml")
    assert task.id == "studio_feature" and task.run_cmd == "python -m pytest -q"
    assert "ledger/" in task.requirement and "README" in task.requirement
    hidden = "".join(task.hidden_tests.values())
    assert "from ledger import Ledger" in hidden and "remove" in hidden and "by_category" in hidden
