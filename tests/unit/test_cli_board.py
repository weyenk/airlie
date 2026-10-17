"""`airlie board export`: the local board as JSON next to board.sqlite, or where --out says."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.board import BoardStore
from tests.unit.test_config import MINIMAL, write

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
