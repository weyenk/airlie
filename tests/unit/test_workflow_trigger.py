from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from airlie.workflow.spec import WorkflowSpec
from airlie.workflow.trigger import start_workflow
from airlie.workflow.worktree import LOCK_NAME


class Fake:
    """An Engine double: one reply per generate call, cycling the given texts (last repeats)."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls: list[str] = []

    async def generate(self, request: object) -> SimpleNamespace:
        self.calls.append(request.role)
        return SimpleNamespace(text=self.texts[(len(self.calls) - 1) % len(self.texts)])

    async def yes_no(self, role: str, messages: list) -> tuple[float, float]:
        return (1.0, 1.0)


def _spec() -> WorkflowSpec:
    return WorkflowSpec(
        title="t",
        inputs=[],
        nodes={"a": {"kind": "WORK", "prompt": "x", "outputs": {"r": {"format": "text"}}}},
        edges={"a": []},
        outputs=["r"],
    )


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(workflows={"mywf": _spec()})


async def test_start_workflow_runs(tmp_path: Path) -> None:
    result = await start_workflow("mywf", cfg=_cfg(), engine=Fake(["done"]), workdir=tmp_path, card={})
    assert result.outputs["r"] == "done"
    assert not (tmp_path / LOCK_NAME).exists()  # the lock is released when the run ends


async def test_start_workflow_refuses_when_locked(tmp_path: Path) -> None:
    (tmp_path / LOCK_NAME).write_text("999")
    result = await start_workflow("mywf", cfg=_cfg(), engine=Fake(["done"]), workdir=tmp_path, card={})
    assert result.outputs == {}
    assert result.events == [{"kind": "already_running"}]


async def test_start_workflow_unknown_name(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        await start_workflow("nope", cfg=_cfg(), engine=Fake(["done"]), workdir=tmp_path, card={})
