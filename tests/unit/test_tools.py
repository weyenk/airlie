"""Builtin tools are sandboxed to the team worktree, timeboxed and output-capped."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from airlie.engine import ToolCall
from airlie.tools import ToolContext, ToolRegistry, resolve


def ctx(tmp_path: Path, **kw) -> ToolContext:
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return ToolContext(
        worktree=wt,
        shell_timeout_s=kw.pop("timeout", 5.0),
        output_cap_chars=kw.pop("cap", 2000),
        test_cmd=kw.pop("test_cmd", "echo tests-ok"),
    )


def test_resolve_keeps_paths_inside_worktree(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    assert resolve(c, "a/b.py") == c.worktree / "a" / "b.py"
    assert resolve(c, "./x") == c.worktree / "x"


@pytest.mark.parametrize(
    "bad", ["../etc/passwd", "/etc/passwd", "a/../../x", ".env", "keys/id_rsa", "cert.pem"]
)
def test_fs_rejects_escape_and_secrets(tmp_path: Path, bad: str) -> None:
    with pytest.raises(PermissionError):
        resolve(ctx(tmp_path), bad)


def test_fs_rejects_symlink_escape(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    (c.worktree / "link").symlink_to(tmp_path)
    with pytest.raises(PermissionError):
        resolve(c, "link/secret.txt")


async def test_read_write_edit_list_grep(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    reg = ToolRegistry.builtin(["read_file", "write_file", "edit_file", "list_dir", "grep"], c)
    assert (
        await reg.call(ToolCall("1", "write_file", '{"path": "pkg/a.py", "content": "x = 1\\ny = 2\\n"}'))
    ).ok
    r = await reg.call(ToolCall("2", "read_file", '{"path": "pkg/a.py"}'))
    assert r.ok and "x = 1" in r.output
    r = await reg.call(ToolCall("3", "edit_file", '{"path": "pkg/a.py", "old": "y = 2", "new": "y = 3"}'))
    assert r.ok and (c.worktree / "pkg/a.py").read_text() == "x = 1\ny = 3\n"
    r = await reg.call(ToolCall("4", "edit_file", '{"path": "pkg/a.py", "old": "nope", "new": "z"}'))
    assert not r.ok and "not found" in r.output
    r = await reg.call(ToolCall("5", "list_dir", '{"path": "pkg"}'))
    assert r.ok and "a.py" in r.output
    r = await reg.call(ToolCall("6", "grep", '{"pattern": "y = ", "path": "pkg"}'))
    assert r.ok and "a.py:2:" in r.output


async def test_unknown_tool_and_bad_json_are_observations_not_exceptions(tmp_path: Path) -> None:
    reg = ToolRegistry.builtin(["read_file"], ctx(tmp_path))
    r = await reg.call(ToolCall("1", "fly", "{}"))
    assert not r.ok and "unknown tool 'fly'" in r.output
    r = await reg.call(ToolCall("2", "read_file", "{not json"))
    assert not r.ok and "arguments" in r.output


async def test_shell_runs_in_worktree(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    r = await ToolRegistry.builtin(["shell"], c).call(ToolCall("1", "shell", '{"cmd": "pwd"}'))
    assert r.ok and str(c.worktree.resolve()) in r.output


async def test_shell_caps_output(tmp_path: Path) -> None:
    r = await ToolRegistry.builtin(["shell"], ctx(tmp_path, cap=50)).call(
        ToolCall("1", "shell", '{"cmd": "seq 1 100"}')
    )
    assert r.ok
    assert len(r.output) <= 50 + 80 and "truncated" in r.output


async def test_shell_timeout(tmp_path: Path) -> None:
    reg = ToolRegistry.builtin(["shell"], ctx(tmp_path, timeout=0.3))
    r = await reg.call(ToolCall("1", "shell", '{"cmd": "sleep 5"}'))
    assert not r.ok and "timed out after 0.3" in r.output


async def test_run_tests_uses_configured_command(tmp_path: Path) -> None:
    reg = ToolRegistry.builtin(["run_tests"], ctx(tmp_path, test_cmd="echo tests-ok && exit 3"))
    r = await reg.call(ToolCall("1", "run_tests", "{}"))
    assert not r.ok and "tests-ok" in r.output and "exit 3" in r.output


async def test_git_status_and_diff(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    init = "git init -q && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m init"
    os.system(f"cd {c.worktree} && {init}")
    reg = ToolRegistry.builtin(["git", "write_file"], c)
    await reg.call(ToolCall("1", "write_file", '{"path": "n.txt", "content": "hi\\n"}'))
    r = await reg.call(ToolCall("2", "git", '{"args": ["status", "--short"]}'))
    assert r.ok and "n.txt" in r.output
    r = await reg.call(ToolCall("3", "git", '{"args": ["push"]}'))
    assert not r.ok and "not allowed" in r.output


def test_schemas_expose_every_registered_tool(tmp_path: Path) -> None:
    reg = ToolRegistry.builtin(["read_file", "shell"], ctx(tmp_path))
    names = [s.name for s in reg.schemas()]
    assert names == ["read_file", "shell"]
    assert reg.schemas()[0].parameters["required"] == ["path"]
