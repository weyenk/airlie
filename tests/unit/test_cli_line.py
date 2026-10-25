"""`airlie line why`: a block's diagnosis, printed, or only its paths (planning-line P5a)."""

from __future__ import annotations

import shutil
from pathlib import Path

from typer.testing import CliRunner

from airlie import cli
from tests.unit.test_studio_orchestrator import cfg_for

runner = CliRunner()
BLOCKS = Path(__file__).parent / "data" / "blocks"


def state_with(tmp_path: Path, case: str) -> Path:
    """A copy of a recorded block's state directory: the command writes a diagnosis into it."""
    state = tmp_path / "state"
    shutil.copytree(BLOCKS / case, state)
    return state


def invoke(tmp_path: Path, *args: str):  # type: ignore[no-untyped-def]
    cfg = cfg_for(tmp_path, extra={"observability": {"state_dir": str(tmp_path / "state")}})
    assert cfg.source_path is not None
    return runner.invoke(cli.app, ["line", *args, "--config", str(cfg.source_path)])


def test_line_why_prints_the_latest_diagnosis_and_only_the_paths_with_the_flag(tmp_path: Path) -> None:
    state = state_with(tmp_path, "hold_trap")
    res = invoke(tmp_path, "why", "AIR-17")
    assert res.exit_code == 0, res.stdout
    assert "AIR-17 blocked: process / hold_trap" in res.stdout
    assert "## The class" in res.stdout and "write_deadline:tests" in res.stdout
    assert (state / "stories" / "AIR-17" / "diagnosis" / "1.md").is_file()  # a past block gets one made
    only = invoke(tmp_path, "why", "AIR-17", "--paths").stdout.splitlines()
    assert only[0].endswith("events.jsonl") and any("/runs/20261020-210203-3cdf" in p for p in only)
    assert all("blocked:" not in p and "##" not in p for p in only)
    assert any(p.endswith(".json") and "step-" in p for p in only)


def test_line_why_asks_for_the_nth_block_and_says_when_there_is_none(tmp_path: Path) -> None:
    state_with(tmp_path, "gate_red")
    assert "(block 8)" in invoke(tmp_path, "why", "AIR-9").stdout  # the fixture holds the story up to its 8th
    missing = invoke(tmp_path, "why", "AIR-9", "--n", "9")
    assert missing.exit_code == 1
    assert invoke(tmp_path, "why", "AIR-404").exit_code == 1
