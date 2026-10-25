"""`airlie epic add`: a Markdown spec becomes an epic card on the local board."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from airlie import cli
from airlie.board import BoardStore
from airlie.story import factory
from airlie.story.epic import epic_of
from tests.unit.test_cli_story import story_cfg
from tests.unit.test_config import MINIMAL, write
from tests.unit.test_story_epic import SPEC

runner = CliRunner()


def config_with_state(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "state"
    return write(tmp_path, {**MINIMAL, "observability": {"state_dir": str(state)}}), state


def test_epic_add_makes_an_epic_card_with_its_repo_label_and_prints_the_key(tmp_path: Path) -> None:
    cfg_path, state = config_with_state(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC)
    r = runner.invoke(
        cli.app, ["epic", "add", "--file", str(spec), "--repo", "o/r", "--config", str(cfg_path)]
    )
    assert r.exit_code == 0, r.output
    assert r.output.strip() == "AIR-1"
    store = BoardStore(state / "board.sqlite")
    [item] = store.list(level="epic")
    assert (item.key, item.title, item.status, item.labels) == ("AIR-1", "Avatars", "todo", ("repo:o/r",))
    epic = epic_of(item.description)
    assert epic is not None and [c.id for c in epic.criteria] == ["EC1", "EC2"]
    store.close()


def test_epic_add_takes_a_title(tmp_path: Path) -> None:
    cfg_path, state = config_with_state(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text(SPEC)
    args = [
        "epic",
        "add",
        "--file",
        str(spec),
        "--repo",
        "o/r",
        "--title",
        "Faces",
        "--config",
        str(cfg_path),
    ]
    r = runner.invoke(cli.app, args)
    assert r.exit_code == 0, r.output
    store = BoardStore(state / "board.sqlite")
    assert store.get("AIR-1").title == "Faces"
    store.close()


def test_a_spec_without_criteria_makes_no_card_and_says_why(tmp_path: Path) -> None:
    cfg_path, state = config_with_state(tmp_path)
    spec = tmp_path / "spec.md"
    spec.write_text("# Nothing\n\nNo criteria.\n")
    r = runner.invoke(
        cli.app, ["epic", "add", "--file", str(spec), "--repo", "o/r", "--config", str(cfg_path)]
    )
    assert r.exit_code == 1
    assert "Acceptance criteria" in r.output
    store = BoardStore(state / "board.sqlite")
    assert store.list() == []
    store.close()


def test_a_missing_file_is_named(tmp_path: Path) -> None:
    cfg_path, _ = config_with_state(tmp_path)
    missing = tmp_path / "nope.md"
    r = runner.invoke(
        cli.app, ["epic", "add", "--file", str(missing), "--repo", "o/r", "--config", str(cfg_path)]
    )
    assert r.exit_code == 1 and "nope.md" in r.output


def test_epic_add_is_a_task() -> None:
    tasks = yaml.safe_load(Path("Taskfile.yml").read_text())["tasks"]
    assert "airlie epic add" in tasks["epic:add"]["cmds"][0]


# ---- epic replan ----


def replan_cfg(tmp_path: Path) -> Path:
    return story_cfg(tmp_path)


def test_epic_replan_is_a_task() -> None:
    tasks = yaml.safe_load(Path("Taskfile.yml").read_text())["tasks"]
    assert "airlie epic replan" in tasks["epic:replan"]["cmds"][0]


def test_epic_replan_names_an_item_that_is_not_an_epic_and_one_that_is_not_there(tmp_path: Path) -> None:
    cfg_path = str(replan_cfg(tmp_path))
    state = tmp_path / "state"
    store = BoardStore(state / "board.sqlite")
    story = store.create(level="story", title="a story").key
    store.close()
    r = runner.invoke(cli.app, ["epic", "replan", story, "--config", cfg_path])
    assert r.exit_code == 1 and f"{story} is a story, not an epic" in r.output
    r = runner.invoke(cli.app, ["epic", "replan", "AIR-99", "--config", cfg_path])
    assert r.exit_code == 1 and "no item AIR-99" in r.output


def test_epic_replan_runs_the_replan_and_prints_its_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, str]] = []

    async def fake(cfg: object, key: str, reason: str) -> str:
        seen.append((key, reason))
        return f"epic {key} re-planned: 1 new, 0 changed, 0 retired, 0 dropped"

    monkeypatch.setattr(factory, "replan_one", fake)
    cfg_path = str(replan_cfg(tmp_path))
    r = runner.invoke(cli.app, ["epic", "replan", "AIR-3", "--reason", "too big", "--config", cfg_path])
    assert r.exit_code == 0, r.output
    assert "epic AIR-3 re-planned: 1 new" in r.output and seen == [("AIR-3", "too big")]
