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


def test_grade_dirs_uses_the_winner_directory_when_teams_share_a_worktree(tmp_path: Path) -> None:
    from airlie.evals import grade_dirs

    (tmp_path / "studio").mkdir()
    (tmp_path / "team-1").mkdir()
    assert grade_dirs(tmp_path, ["fix", "note"], "studio") == ["studio"]
    assert grade_dirs(tmp_path, ["team-1", "team-2"], "team-1") == ["team-1"]
