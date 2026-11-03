"""shell._run's choice of how a command starts, on any platform: confined where the context asks for the
sandbox and it is available, unconfined otherwise. tests/unit/test_confine.py runs the sandbox itself, on
macOS only; here the confined spawn is stood in for, so the Linux mutation shards hold the choice too (PR 212,
2026-11-02: `confined = None` survived there, where confine.available() is always false)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from airlie.tools import shell
from airlie.tools.protocol import ToolContext


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The confined spawn, stood in for: each call is noted, and the command runs unconfined."""
    calls: list[str] = []

    async def fake(
        cmd: list[str] | str, ctx: ToolContext, env: dict[str, str], spawn: dict[str, Any], *, shell: bool
    ) -> asyncio.subprocess.Process:
        calls.append(str(cmd))
        return await asyncio.create_subprocess_shell(str(cmd), **spawn)

    monkeypatch.setattr(shell, "_spawn_confined", fake)
    monkeypatch.setattr(shell.confine, "explain", lambda text, rc: f"{text}[explained {rc}]")
    return calls


@pytest.mark.parametrize(
    ("sandbox", "available", "confined"), [(True, True, True), (True, False, False), (False, True, False)]
)
async def test_a_command_runs_confined_only_where_the_sandbox_is_asked_for_and_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spawned: list[str],
    sandbox: bool,
    available: bool,
    confined: bool,
) -> None:
    monkeypatch.setattr(shell.confine, "available", lambda: available)
    result = await shell.run_cmd("echo hi", ToolContext(tmp_path, sandbox=sandbox), shell=True)
    assert result.ok and result.output.startswith("hi\n")
    assert spawned == (["echo hi"] if confined else [])
    assert result.output.endswith("[explained 0]") is confined  # the sandbox's explanation only when confined
