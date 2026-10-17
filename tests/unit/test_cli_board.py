"""`airlie board export`: the local board as JSON next to board.sqlite, or where --out says. `airlie board
import-jira`: the Jira project onto that board, with --dry-run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.board import BoardStore
from airlie.story import jira
from airlie.story.board import BoardError
from tests.unit.test_config import MINIMAL, write
from tests.unit.test_jira_import import HDS, FakeClient

runner = CliRunner()


def config_with_state(tmp_path: Path, **board: object) -> tuple[Path, Path]:
    state = tmp_path / "state"
    data = {**MINIMAL, "observability": {"state_dir": str(state)}, "board": board}
    return write(tmp_path, data), state


def test_export_writes_board_json_next_to_the_store(tmp_path: Path) -> None:
    cfg_path, state = config_with_state(tmp_path, prefix="HDS")
    s = BoardStore(state / "board.sqlite", prefix="HDS")
    s.create(level="initiative", title="i")
    s.create(level="initiative", title="j")
    s.close()
    r = runner.invoke(cli.app, ["board", "export", "--config", str(cfg_path)])
    assert r.exit_code == 0, r.output
    out = state / "board.json"
    assert r.output.strip() == str(out)
    data = json.loads(out.read_text())
    assert data["prefix"] == "HDS" and [i["key"] for i in data["items"]] == ["HDS-1", "HDS-2"]


def test_export_honours_out_and_an_empty_board(tmp_path: Path) -> None:
    cfg_path, state = config_with_state(tmp_path)
    out = tmp_path / "elsewhere" / "b.json"
    r = runner.invoke(cli.app, ["board", "export", "--out", str(out), "--config", str(cfg_path)])
    assert r.exit_code == 0, r.output
    assert json.loads(out.read_text()) == {"prefix": "AL", "items": []}
    assert (state / "board.sqlite").exists()


def test_board_export_is_a_task() -> None:
    tasks = yaml.safe_load(Path("Taskfile.yml").read_text())["tasks"]
    assert "airlie board export" in tasks["board:export"]["cmds"][0]


# ---- import-jira ----


def test_import_jira_imports_the_project_into_the_state_dirs_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, state = config_with_state(tmp_path, prefix="HDS")
    client = FakeClient(HDS)
    monkeypatch.setattr(jira, "jira_board", lambda cfg: client)
    r = runner.invoke(cli.app, ["board", "import-jira", "--config", str(cfg_path)])
    assert r.exit_code == 0, r.output
    assert "HDS-14: created as HDS-14" in r.output and r.output.strip().endswith(
        "5 created, 0 updated, 0 unchanged"
    )
    s = BoardStore(state / "board.sqlite", prefix="HDS")
    assert [i.key for i in s.list()] == ["HDS-5", "HDS-7", "HDS-9", "HDS-14", "HDS-16"]
    s.close()


def test_import_jira_dry_run_prints_the_report_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, state = config_with_state(tmp_path)  # prefix AL: keys would be allocated
    monkeypatch.setattr(jira, "jira_board", lambda cfg: FakeClient(HDS))
    r = runner.invoke(cli.app, ["board", "import-jira", "--dry-run", "--config", str(cfg_path)])
    assert r.exit_code == 0, r.output
    assert "HDS-14: would create AL-?" in r.output and "dry run: 5 would be created" in r.output
    assert "board.prefix is AL, not HDS" in r.output  # how to keep Jira's numbers
    s = BoardStore(state / "board.sqlite")
    assert s.list() == []
    s.close()


def test_import_jira_without_a_jira_project_or_with_a_broken_one_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path, _ = config_with_state(tmp_path)
    monkeypatch.setattr(jira, "jira_board", lambda cfg: None)
    r = runner.invoke(cli.app, ["board", "import-jira", "--config", str(cfg_path)])
    assert r.exit_code == 1 and "ATLASSIAN_PROJECT_KEY" in r.output and "Traceback" not in r.output

    def broken(cfg: object) -> None:
        raise BoardError("Jira needs your account email and an API token")

    monkeypatch.setattr(jira, "jira_board", broken)
    r = runner.invoke(cli.app, ["board", "import-jira", "--config", str(cfg_path)])
    assert r.exit_code == 1 and "API token" in r.output and "Traceback" not in r.output


def test_board_import_jira_is_a_task() -> None:
    tasks = yaml.safe_load(Path("Taskfile.yml").read_text())["tasks"]
    assert "airlie board import-jira" in tasks["board:import-jira"]["cmds"][0]
