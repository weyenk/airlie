"""Builtin tools are sandboxed to the team worktree, timeboxed and output-capped."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from airlie.engine import ToolCall
from airlie.tools import ToolContext, ToolRegistry, ToolResult, resolve
from airlie.tools import confine as confine_mod
from airlie.tools.shell import run_cmd
from tests.unit.proc_helpers import StartGate, cancel_phases, eventually, running, unique_sleep


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


# ---- how run_cmd starts a command: confined or not, through sh or not ----

needs_sandbox = pytest.mark.skipif(
    not confine_mod.available(), reason="no sandbox-exec here (or already in one)"
)


async def test_an_unconfined_command_runs_in_the_worktree_without_a_shell(tmp_path: Path) -> None:
    c = replace(ctx(tmp_path), sandbox=False)
    r = await run_cmd(["pwd"], c, shell=False)
    assert r.ok and Path(r.output.strip()).resolve() == c.worktree.resolve()
    missing = await run_cmd(["no-such-program-airlie"], c, shell=False)
    assert missing.ok is False and "No such file or directory" in missing.output


def venv_tool(c: ToolContext) -> None:
    """A program only the worktree's .venv/bin has: agent_env puts that directory first on PATH."""
    bin_dir = c.worktree / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "only-in-the-venv").write_text("#!/bin/sh\necho from-the-venv\n")
    (bin_dir / "only-in-the-venv").chmod(0o755)


@pytest.mark.parametrize("sandbox", [False, pytest.param(True, marks=needs_sandbox)])
async def test_a_command_is_found_on_the_worktrees_path(tmp_path: Path, sandbox: bool) -> None:
    c = replace(ctx(tmp_path), sandbox=sandbox)
    venv_tool(c)
    r = await run_cmd(["only-in-the-venv"], c, shell=False)
    assert r.ok and r.output.strip() == "from-the-venv"


@needs_sandbox
async def test_a_confined_command_that_is_not_there_fails_as_an_unconfined_one_does(tmp_path: Path) -> None:
    r = await run_cmd(["no-such-program-airlie"], ctx(tmp_path), shell=False)
    assert r.ok is False and "No such file or directory" in r.output and "exit 71" not in r.output


REFUSED_SAYS = "echo 'touch: /etc/x: Operation not permitted'"


async def test_the_sandbox_note_is_for_a_confined_command_that_failed_on_a_refused_write(
    tmp_path: Path,
) -> None:
    unconfined = replace(ctx(tmp_path), sandbox=False)
    r = await run_cmd(f"{REFUSED_SAYS}; exit 1", unconfined, shell=True)
    assert r.ok is False and confine_mod.NOTE not in r.output  # not confined: not airlie's sandbox
    if not confine_mod.available():
        return
    confined = ctx(tmp_path)
    assert confine_mod.NOTE in (await run_cmd(f"{REFUSED_SAYS}; exit 1", confined, shell=True)).output
    ok = await run_cmd(REFUSED_SAYS, confined, shell=True)  # it says so, but it did not fail
    assert ok.ok and confine_mod.NOTE not in ok.output


@pytest.mark.parametrize("sandbox", [False, pytest.param(True, marks=needs_sandbox)])
@pytest.mark.parametrize(("hold", "cancel"), cancel_phases())
async def test_cancelling_a_shell_call_kills_what_the_command_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox: bool, hold: bool, cancel: Any
) -> None:
    """Mid-start (the child runs, its pipes not yet connected) or once running: either way nothing the
    command started outlives the cancel. The test, not the machine's speed, says which (StartGate)."""
    from airlie.tools import shell as shell_mod

    gate = StartGate(monkeypatch, shell_mod, hold=hold)
    sleeper = unique_sleep()
    reg = ToolRegistry.builtin(["shell"], replace(ctx(tmp_path, timeout=60), sandbox=sandbox))
    task = asyncio.create_task(reg.call(shell_call(f"{sleeper} & wait")))
    await cancel(task, gate, sleeper)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)  # at once, not when the sleep has run its 30 s out
    assert await eventually(lambda: not running(sleeper))
