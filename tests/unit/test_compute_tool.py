"""The compute tool (tools/compute.py): a short Python program's output, for roles that may only read; it
cannot write outside its own temporary directory or reach the network, and a time limit ends it."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.tools import compute, confine
from airlie.tools.compute import Compute
from airlie.tools.protocol import ToolContext

needs_sandbox = pytest.mark.skipif(not confine.available(), reason="macOS sandbox-exec only")


async def run(code: str, tmp_path: Path) -> tuple[bool, str]:
    result = await Compute().run({"code": code}, ToolContext(worktree=tmp_path))
    return result.ok, result.output


async def test_it_returns_what_the_program_prints(tmp_path: Path) -> None:
    code = (
        "vals = [(1000,'M'),(900,'CM'),(500,'D'),(400,'CD'),(100,'C'),(90,'XC'),(50,'L'),(40,'XL'),"
        "(10,'X'),(9,'IX'),(5,'V'),(4,'IV'),(1,'I')]\n"
        "def r(n):\n    s=''\n    for v,c in vals:\n        while n>=v: s+=c; n-=v\n    return s\n"
        "print(max(len(r(n)) for n in range(1, 4000)))"
    )
    assert await run(code, tmp_path) == (True, "15\n")


async def test_a_failing_program_shows_its_error_and_exit(tmp_path: Path) -> None:
    ok, out = await run("int('M')", tmp_path)
    assert not ok and "ValueError" in out and "[exit 1]" in out


async def test_a_program_that_prints_nothing_is_told_so(tmp_path: Path) -> None:
    assert await run("x = 2 + 2", tmp_path) == (True, "(it printed nothing: print what you need)")


async def test_an_empty_program_is_refused(tmp_path: Path) -> None:
    assert await run("  ", tmp_path) == (False, "error: no code given")


async def test_a_program_that_runs_too_long_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compute, "TIMEOUT_S", 0.5)
    ok, out = await run("while True: pass", tmp_path)
    assert not ok and "ran past 0.5s" in out


@needs_sandbox
async def test_it_cannot_write_outside_its_own_directory(tmp_path: Path) -> None:
    target = tmp_path / "written.txt"
    ok, out = await run(f"open({str(target)!r}, 'w').write('x')", tmp_path)
    assert not ok and "Operation not permitted" in out and not target.exists()
    assert (await run("open('scratch.txt', 'w').write('x'); print('ok')", tmp_path)) == (True, "ok\n")


@needs_sandbox
async def test_it_cannot_reach_the_network(tmp_path: Path) -> None:
    code = "import socket\nsocket.create_connection(('1.1.1.1', 80), timeout=3)\nprint('connected')"
    ok, out = await run(code, tmp_path)
    assert not ok and "connected" not in out


def test_the_profile_without_network_denies_it() -> None:
    assert "(deny network*)" in confine.profile(1, network=False)
    assert "network" not in confine.profile(1)


async def test_files_a_program_writes_stay_for_the_next_call_and_calls_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bundle 6 run 3 (2026-10-30): 40 calls, 72,000 tokens, each writing the same reference again. A module
    saved once is imported after; past CALLS the tool says to answer."""
    monkeypatch.setattr(compute, "CALLS", 2)
    tool, ctx = Compute(), ToolContext(worktree=tmp_path)
    saved = await tool.run({"code": "open('ref.py', 'w').write('def double(n):\\n    return 2 * n\\n')"}, ctx)
    used = await tool.run({"code": "from ref import double\nprint(double(21))"}, ctx)
    past = await tool.run({"code": "print(1)"}, ctx)
    assert saved.ok and (used.ok, used.output) == (True, "42\n")
    assert not past.ok and "answer from what they showed" in past.output
