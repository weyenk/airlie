"""Builtin tools are sandboxed to the team worktree, timeboxed and output-capped."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from airlie.engine import ToolCall
from airlie.tools import ToolContext, ToolRegistry, ToolResult, resolve
from tests.unit.proc_helpers import eventually, running, unique_sleep


def ctx(tmp_path: Path, **kw) -> ToolContext:
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    return ToolContext(
        worktree=wt,
        shell_timeout_s=kw.pop("timeout", 5.0),
        output_cap_chars=kw.pop("cap", 2000),
        test_cmd=kw.pop("test_cmd", "echo tests-ok"),
        test_steps=tuple(kw.pop("test_steps", ())),
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


async def test_a_call_to_a_registry_with_no_tools_says_there_are_none(tmp_path: Path) -> None:
    r = await ToolRegistry.builtin([], ctx(tmp_path)).call(ToolCall("1", "Read", "{}"))
    assert not r.ok and "no tools" in r.output and "available: " not in r.output


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


def shell_call(cmd: str) -> ToolCall:
    return ToolCall("1", "shell", json.dumps({"cmd": cmd}))


async def test_a_shell_timeout_kills_what_the_command_started(tmp_path: Path) -> None:
    sleeper = unique_sleep()
    started = time.monotonic()
    reg = ToolRegistry.builtin(["shell"], ctx(tmp_path, timeout=1))
    r = await asyncio.wait_for(reg.call(shell_call(f"{sleeper} & wait")), 20)
    assert not r.ok and "timed out after 1s" in r.output
    assert time.monotonic() - started < 5  # it did not sit out the sleep, which holds the output pipe
    assert await eventually(lambda: not running(sleeper))  # and the sleep is not left running


async def test_cancelling_a_shell_call_kills_what_the_command_started(tmp_path: Path) -> None:
    sleeper = unique_sleep()
    reg = ToolRegistry.builtin(["shell"], ctx(tmp_path, timeout=60))
    task = asyncio.create_task(reg.call(shell_call(f"{sleeper} & wait")))
    assert await eventually(lambda: running(f"^{sleeper}"))  # the sleep itself is up, not only its shell
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)  # at once, not when the sleep has run its 30 s out
    assert await eventually(lambda: not running(sleeper))


@pytest.mark.parametrize("sandbox", [False, True])
async def test_a_cancel_while_the_command_is_still_being_started_kills_it_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox: bool
) -> None:
    """The child runs before asyncio has connected its pipes: a cancel in that moment (PR 105's CI, a slow
    runner) used to leave the caller without the process to kill, and the command ran on."""
    started = asyncio.Event()

    def slow(real):  # type: ignore[no-untyped-def]
        async def spawn(*args, **kwargs):  # type: ignore[no-untyped-def]
            proc = await real(*args, **kwargs)
            started.set()
            await asyncio.sleep(2)  # the cancel lands here, with the child already running
            return proc

        return spawn

    monkeypatch.setattr(asyncio, "create_subprocess_shell", slow(asyncio.create_subprocess_shell))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow(asyncio.create_subprocess_exec))
    sleeper = unique_sleep()
    reg = ToolRegistry.builtin(["shell"], replace(ctx(tmp_path, timeout=60), sandbox=sandbox))
    task = asyncio.create_task(reg.call(shell_call(f"{sleeper} & wait")))
    await asyncio.wait_for(started.wait(), 20)
    assert await eventually(lambda: running(f"^{sleeper}"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    assert await eventually(lambda: not running(sleeper))


async def test_a_command_reads_no_stdin_so_it_never_waits_on_the_persons_terminal(tmp_path: Path) -> None:
    read, write = os.pipe()  # airlie's stdin: one that never ends, as a terminal nobody types into
    saved = os.dup(0)
    os.dup2(read, 0)
    try:
        reg = ToolRegistry.builtin(["shell"], ctx(tmp_path, timeout=5))
        r = await asyncio.wait_for(reg.call(shell_call("cat; echo read to the end; echo to stderr >&2")), 20)
    finally:
        os.dup2(saved, 0)
        for fd in (saved, read, write):
            os.close(fd)
    assert r.ok and r.output == "read to the end\nto stderr\n"  # stderr is part of the output


async def test_run_tests_uses_configured_command(tmp_path: Path) -> None:
    reg = ToolRegistry.builtin(["run_tests"], ctx(tmp_path, test_cmd="echo tests-ok && exit 3"))
    r = await reg.call(ToolCall("1", "run_tests", "{}"))
    assert not r.ok and "tests-ok" in r.output and "exit 3" in r.output


async def test_run_tests_runs_every_gate_command_and_reports_each(tmp_path: Path) -> None:
    steps = ["echo one", "echo ruff says no; exit 3", "echo 12 passed"]
    reg = ToolRegistry.builtin(["run_tests"], ctx(tmp_path, test_cmd="unused", test_steps=steps))
    r = await reg.call(ToolCall("1", "run_tests", "{}"))
    assert not r.ok
    lines = r.output.splitlines()
    assert lines[0] == "2 of 3 gate commands pass; failed: [2/3]"
    assert "[1/3] PASS: echo one" in lines
    assert "[2/3] FAIL (exit 3): echo ruff says no; exit 3" in lines and "ruff says no" in lines
    # the command after the failure still ran, and its result comes through
    assert "[3/3] PASS: echo 12 passed" in lines and lines[-1] == "12 passed"


async def test_run_tests_passes_when_every_gate_command_does(tmp_path: Path) -> None:
    steps = ["mkdir -p sub && cd sub", "pwd"]  # a cd carries over, as it does in the story's gate
    reg = ToolRegistry.builtin(["run_tests"], ctx(tmp_path, test_steps=steps))
    r = await reg.call(ToolCall("1", "run_tests", "{}"))
    assert r.ok and r.output.splitlines()[0] == "2 of 2 gate commands pass"
    assert r.output.rstrip().endswith("/wt/sub")


async def test_run_tests_caps_a_failing_commands_output(tmp_path: Path) -> None:
    steps = ["python3 -c \"print('x' * 5000)\"; exit 1"]
    reg = ToolRegistry.builtin(["run_tests"], ctx(tmp_path, cap=500, test_steps=steps))
    r = await reg.call(ToolCall("1", "run_tests", "{}"))
    assert not r.ok and "truncated" in r.output and len(r.output) < 800


async def test_run_tests_goes_on_after_a_command_that_times_out(tmp_path: Path) -> None:
    steps = ["echo one", "sleep 10", "echo after"]
    reg = ToolRegistry.builtin(["run_tests"], ctx(tmp_path, timeout=1.0, test_steps=steps))
    r = await reg.call(ToolCall("1", "run_tests", "{}"))
    assert not r.ok
    assert "[1/3] PASS: echo one" in r.output
    assert "[2/3] FAIL (timed out after 1s): sleep 10" in r.output and "[3/3] PASS: echo after" in r.output


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


@pytest.mark.parametrize(
    "args",
    [
        ["-c", "alias.p=push", "p"],  # an alias the deny list never sees
        ["-c", "core.sshCommand=sh -c id", "fetch"],
        ["-C", "..", "push"],
        ["--git-dir=../other/.git", "status"],
        ["--no-pager", "log"],
        ["--", "status"],
        ["-"],
    ],
)
async def test_git_refuses_its_own_options_before_the_subcommand(tmp_path: Path, args: list[str]) -> None:
    """The deny list reads the subcommand: anything in front of it would get a denied one past it."""
    c = ctx(tmp_path)
    os.system(f"cd {c.worktree} && git init -q")
    r = await ToolRegistry.builtin(["git"], c).call(ToolCall("1", "git", json.dumps({"args": args})))
    assert r == ToolResult(False, "error: git's own options are not allowed: start with the subcommand")


async def test_git_takes_options_after_the_subcommand(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    os.system(
        f"cd {c.worktree} && git init -q && git -c user.name=t -c user.email=t@t commit -q --allow-empty -m i"
    )
    r = await ToolRegistry.builtin(["git"], c).call(
        ToolCall("1", "git", '{"args": ["log", "-1", "--format=%s"]}')
    )
    assert r.ok and r.output.strip() == "i"


def test_schemas_expose_every_registered_tool(tmp_path: Path) -> None:
    reg = ToolRegistry.builtin(["read_file", "shell"], ctx(tmp_path))
    names = [s.name for s in reg.schemas()]
    assert names == ["read_file", "shell"]
    assert reg.schemas()[0].parameters["required"] == ["path"]


async def test_registry_names_missing_required_args_and_cut_off_arguments(tmp_path: Path) -> None:
    """Parity with the AutoGen path (2026-10-13): clear, actionable errors the model can act on."""
    reg = ToolRegistry.builtin(["read_file", "write_file"], ctx(tmp_path))
    r = await reg.call(ToolCall("1", "read_file", "{}"))
    assert not r.ok and r.output == (
        'error: read_file requires: path. A correct call looks like: read_file(path="src/app.py")'
    )
    r = await reg.call(ToolCall("2", "write_file", '{"path": "a.py", "content": "x = 1' + "0" * 300))
    assert not r.ok and "cut off" in r.output and "shorter" in r.output and "'0000'" in r.output


async def test_a_missing_argument_error_shows_a_correct_call_built_from_the_schema(tmp_path: Path) -> None:
    """HDS-5 2026-10-16: Qwen3-Coder sent empty calls; a bare "requires: path" did not bring it back."""
    reg = ToolRegistry.builtin(["write_file", "shell", "grep", "git"], ctx(tmp_path))
    r = await reg.call(ToolCall("1", "write_file", '{"path": "a.py"}'))
    assert r.output == (
        "error: write_file requires: content. A correct call looks like: "
        'write_file(path="src/app.py", content="...")'
    )
    r = await reg.call(ToolCall("2", "shell", "{}"))
    assert r.output.endswith('A correct call looks like: shell(cmd="ls")')
    r = await reg.call(ToolCall("3", "grep", "{}"))
    assert r.output.endswith('A correct call looks like: grep(pattern="...")')
    r = await reg.call(ToolCall("4", "git", "{}"))
    assert r.output.endswith('A correct call looks like: git(args=["..."])')


async def test_read_file_takes_a_line_range_and_numbers_the_lines_as_in_the_file(tmp_path: Path) -> None:
    """AIR-7 2026-10-18: the coder asked read_file for offset 438 three times, for the part of a file the
    cap had cut, and got the first 20,000 chars back each time: it ignored arguments it did not declare."""
    c = ctx(tmp_path)
    (c.worktree / "big.py").write_text("".join(f"line {i}\n" for i in range(1, 11)))
    reg = ToolRegistry.builtin(["read_file"], c)
    r = await reg.call(ToolCall("1", "read_file", '{"path": "big.py", "offset": 4, "limit": 3}'))
    assert r.ok and r.output == "4: line 4\n5: line 5\n6: line 6"
    r = await reg.call(
        ToolCall("2", "read_file", '{"path": "big.py", "offset": "9", "limit": "5"}')
    )  # as strings
    assert r.ok and r.output == "9: line 9\n10: line 10"
    r = await reg.call(ToolCall("3", "read_file", '{"path": "big.py", "offset": 40}'))
    assert r.ok and "offset 40 is past the end: big.py has 10 lines" in r.output
    r = await reg.call(ToolCall("4", "read_file", '{"path": "big.py", "offset": "x"}'))
    assert not r.ok and "offset" in r.output


async def test_a_cut_read_says_how_to_read_the_rest(tmp_path: Path) -> None:
    c = ctx(tmp_path, cap=60)
    (c.worktree / "big.py").write_text("".join(f"line {i} {'x' * 10}\n" for i in range(1, 21)))
    reg = ToolRegistry.builtin(["read_file"], c)
    r = await reg.call(ToolCall("1", "read_file", '{"path": "big.py"}'))
    assert r.ok
    shown = [ln for ln in r.output.splitlines() if ln[:1].isdigit()]
    assert shown and all(len(ln) > 0 for ln in shown)
    last = int(shown[-1].split(":")[0])
    assert f"[truncated: lines {last + 1}-20 not shown; read on with offset={last + 1}]" in r.output
    assert "… [truncated" in r.output  # the shape every capped observation has
    again = await reg.call(ToolCall("2", "read_file", json.dumps({"path": "big.py", "offset": last + 1})))
    assert again.ok and again.output.startswith(f"{last + 1}: line {last + 1}")
