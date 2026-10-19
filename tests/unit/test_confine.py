"""Commands a model runs cannot write outside its worktree (airlie.tools.confine), through every tool that
runs one: shell, git, run_tests and its gate steps.

2026-10-19: a studio coder whose `python` failed re-pointed the person's ~/.asdf shims, made directories in
/opt/homebrew/Cellar and pip-installed pytest into the person's own Python. `outside` stands in for those."""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from airlie.config.schema import ToolsSpec
from airlie.engine import ToolCall
from airlie.teams.worktree import add_worktree
from airlie.tools import confine
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.registry import ToolRegistry

pytestmark = pytest.mark.skipif(not confine.available(), reason="sandbox-exec is macOS's")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=r, check=True)
    (r / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True)
    return r


@pytest.fixture
def outside() -> Iterator[Path]:
    """The person's own files: not the worktree, nor a temporary or cache directory every program writes (a
    test's tmp_path is one of those)."""
    d = Path.home() / "Library" / "Caches" / "airlie-confine-tests" / uuid.uuid4().hex
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def call(ctx: ToolContext, name: str, args: dict[str, object]) -> ToolResult:
    out = await ToolRegistry.builtin([name], ctx).call(ToolCall("1", name, json.dumps(args)))
    assert isinstance(out, ToolResult)
    return out


async def shell(ctx: ToolContext, cmd: str) -> ToolResult:
    return await call(ctx, "shell", {"cmd": cmd})


async def test_a_shell_command_writes_in_the_worktree_and_not_outside_it(repo: Path, outside: Path) -> None:
    ctx = ToolContext(repo)
    assert (await shell(ctx, "echo hi > made.txt && mkdir -p d/e && touch d/e/f")).ok
    assert (repo / "made.txt").read_text() == "hi\n" and (repo / "d/e/f").exists()
    for cmd in (
        f"echo x > {outside}/planted",
        f"mkdir -p {outside}/libexec/bin",
        f"ln -sf /bin/sh {outside}/python",
        f"cd {outside}/.. && touch {outside.name}/escaped",
    ):
        r = await shell(ctx, cmd)
        assert not r.ok and confine.REFUSED in r.output and confine.NOTE in r.output, cmd
    assert list(outside.iterdir()) == []


async def test_changing_or_removing_a_file_outside_is_refused_too(repo: Path, outside: Path) -> None:
    (outside / "shim").write_text("original\n")
    ctx = ToolContext(repo)
    for cmd in (f"echo changed > {outside}/shim", f"rm {outside}/shim", f"chmod 000 {outside}/shim"):
        assert not (await shell(ctx, cmd)).ok, cmd
    assert (outside / "shim").read_text() == "original\n"


async def test_git_commits_in_a_linked_worktree_whose_objects_live_in_the_main_repository(
    repo: Path, tmp_path: Path
) -> None:
    wt = add_worktree(repo, tmp_path / "runs" / "team", branch="airlie/t")
    ctx = ToolContext(wt)
    assert (await shell(ctx, "echo b > b.txt")).ok
    assert (await call(ctx, "git", {"args": ["add", "-A"]})).ok
    assert (await call(ctx, "git", {"args": ["commit", "-qm", "b"]})).ok
    log = subprocess.run(["git", "log", "--oneline", "airlie/t"], cwd=repo, capture_output=True, text=True)
    assert log.stdout.splitlines()[0].endswith(" b")


async def test_temporary_files_may_be_written_where_macos_puts_them(repo: Path) -> None:
    r = await shell(
        ToolContext(repo), 'f=$(mktemp) && echo ok > "$f" && rm "$f" && touch /tmp/airlie-confine-$$'
    )
    assert r.ok, r.output


async def test_a_directory_in_tools_writable_may_be_written(repo: Path, outside: Path) -> None:
    ctx = ToolContext(repo, writable=(outside,))
    assert (await shell(ctx, f"echo x > {outside}/allowed")).ok
    assert (outside / "allowed").read_text() == "x\n"


async def test_with_the_sandbox_off_a_command_writes_where_it_likes(repo: Path, outside: Path) -> None:
    r = await shell(ToolContext(repo, sandbox=False), f"echo x > {outside}/free")
    assert r.ok and (outside / "free").exists()


async def test_a_gate_step_that_changes_directory_is_still_confined_by_the_worktree(
    repo: Path, outside: Path
) -> None:
    (repo / "sub").mkdir()
    steps = ("cd sub", "touch ../top.txt", f"touch {outside}/nope")
    r = await call(ToolContext(repo, test_steps=steps), "run_tests", {})
    assert (repo / "top.txt").exists() and not (outside / "nope").exists()
    assert "2 of 3 gate commands pass" in r.output and confine.NOTE in r.output


def test_writable_roots_are_real_paths_and_never_the_whole_disk(repo: Path) -> None:
    roots = confine.writable_roots(repo, (Path("/tmp/x"), Path("/")))
    assert repo.resolve() in roots and (repo / ".git").resolve() in roots
    assert Path("/private/tmp") in roots and Path("/private/tmp/x") in roots and Path("/") not in roots
    assert len(roots) == len(set(roots))


def test_the_note_is_added_only_to_a_failure_on_a_refused_write() -> None:
    refused = "touch: /x: Operation not permitted\n"
    assert confine.explain(refused, 1).endswith(confine.NOTE + "\n")
    assert confine.explain(refused, 0) == refused
    assert confine.explain("boom\n", 1) == "boom\n"


def test_the_config_turns_it_off_or_adds_directories() -> None:
    assert ToolsSpec().confinement() == {"sandbox": True, "writable": ()}
    spec = ToolsSpec(sandbox=False, writable=[Path("~/.npm")])
    assert spec.confinement() == {"sandbox": False, "writable": (Path("~/.npm"),)}


def test_inside_a_sandbox_already_commands_run_under_that_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox cannot be applied inside another: sandbox-exec exits 71 (airlie's own tool tests, run by a
    story's gate inside the coder's confined run_tests)."""
    confine.available.cache_clear()
    monkeypatch.setattr(confine.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 71))
    try:
        assert confine.available() is False
    finally:
        confine.available.cache_clear()
