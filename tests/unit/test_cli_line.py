"""`airlie line why`: a block's diagnosis, printed, or only its paths (planning-line P5a)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from airlie import cli
from tests.unit.git_helpers import git
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


def clone_behind(tmp_path: Path) -> Path:
    """A home checkout on main one commit behind its remote."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    git(tmp_path, "clone", "-q", str(remote), str(work))
    for cmd in (("config", "user.email", "t@example.com"), ("config", "user.name", "t")):
        git(work, *cmd)
    (work / "a").write_text("a")
    git(work, "add", ".")
    git(work, "commit", "-qm", "A")
    git(work, "push", "-q", "origin", "HEAD:main")
    home = tmp_path / "home"
    git(tmp_path, "clone", "-q", str(remote), str(home))
    (work / "b").write_text("b")
    git(work, "add", ".")
    git(work, "commit", "-qm", "B")
    git(work, "push", "-q", "origin", "HEAD:main")
    return home


def test_line_deploy_pulls_the_home_trunk_and_says_when_serve_is_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from airlie.story import fixes

    home = clone_behind(tmp_path)
    monkeypatch.setattr(fixes, "home_checkout", lambda: home)
    res = invoke(tmp_path, "deploy", "--serve-url", "http://127.0.0.1:1")
    assert res.exit_code == 0, res.stdout
    assert (home / "b").exists()  # fast-forwarded
    assert "nothing to restart" in res.stdout  # nothing answers /health there


def test_line_deploy_refuses_a_checkout_that_is_not_on_the_trunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from airlie.story import fixes

    home = clone_behind(tmp_path)
    git(home, "checkout", "-qb", "feature")
    monkeypatch.setattr(fixes, "home_checkout", lambda: home)
    res = invoke(tmp_path, "deploy")
    assert res.exit_code == 1 and not (home / "b").exists()
