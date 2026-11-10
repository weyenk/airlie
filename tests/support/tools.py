"""Shared helpers extracted from tests/unit/test_tools.py for tests.support.tools."""

from __future__ import annotations

from pathlib import Path

from airlie.tools import ToolContext


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
