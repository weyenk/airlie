from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from airlie.workflow.runner import Stalled, StoryBlocked, run_workflow
from airlie.workflow.spec import LoopBound, WireSpec, WorkflowSpec


class FakeCfg:
    """A stand-in for AirlieConfig: enough attributes for config-path counts/roles."""

    class loop:
        max = 8


class Fake:
    """An Engine double: one reply per generate call, cycling the given texts (last repeats)."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.calls: list[str] = []

    async def generate(self, request: object) -> SimpleNamespace:
        self.calls.append(request.role)
        text = self.texts[(len(self.calls) - 1) % len(self.texts)]
        return SimpleNamespace(text=text)

    async def yes_no(self, role: str, messages: list) -> tuple[float, float]:
        return (1.0, 1.0)


def _run(spec: WorkflowSpec, engine: Fake, tmp_path: Path):
    return run_workflow(spec, cfg=FakeCfg(), engine=engine, workdir=tmp_path, inputs={})


async def test_two_node_plan_build(tmp_path: Path) -> None:
    spec = WorkflowSpec(
        title="pb",
        inputs=[],
        nodes={
            "plan": {"kind": "WORK", "prompt": "plan", "outputs": {"plan": {"format": "text"}}},
            "build": {
                "kind": "WORK",
                "prompt": "build the plan",
                "inputs": ["plan"],
                "outputs": {"code": {"format": "text"}},
            },
        },
        edges={"plan": [WireSpec(target="build", carries=["plan"])]},
        outputs=["code"],
    )
    result = await _run(spec, Fake(["done"]), tmp_path)
    assert result.outputs["code"] == "done"


async def test_when_false_kills_branch(tmp_path: Path) -> None:
    spec = WorkflowSpec(
        title="br",
        inputs=[],
        nodes={
            "a": {"kind": "WORK", "prompt": "x", "outputs": {"flag": {"format": "bool"}}},
            "b": {"kind": "WORK", "prompt": "y", "inputs": ["flag"], "outputs": {}},
        },
        edges={"a": [WireSpec(target="b", when="flag")]},
    )
    engine = Fake(["no"])
    result = await _run(spec, engine, tmp_path)
    skipped = {e["node"] for e in result.events if e["kind"] == "node_skipped"}
    assert "b" in skipped
    assert engine.calls == ["coder"]


async def test_loop_retries_until_done(tmp_path: Path) -> None:
    """The check loops back to the builder while its verdict is not done, then stops."""
    spec = WorkflowSpec(
        title="lp",
        inputs=[],
        nodes={
            "build": {"kind": "WORK", "run": "true"},
            "check": {
                "kind": "WORK",
                "prompt": "check",
                "outputs": {"ok": {"format": "bool"}},
                "on_fail": "continue",
            },
        },
        edges={
            "build": [WireSpec(target="check")],
            "check": [WireSpec(target="build", when="not ok", loop=LoopBound(max=8))],
        },
        outputs=["ok"],
    )
    engine = Fake(["no", "yes"])
    result = await _run(spec, engine, tmp_path)
    assert result.outputs["ok"] is True
    assert engine.calls == ["coder", "coder"]  # the check ran twice, the builder was a shell node


async def test_all_false_downstream_die(tmp_path: Path) -> None:
    spec = WorkflowSpec(
        title="d",
        inputs=[],
        nodes={
            "start": {"kind": "WORK", "prompt": "s", "outputs": {"flag": {"format": "bool"}}},
            "build": {"kind": "WORK", "prompt": "b", "inputs": ["flag"]},
            "gate": {"kind": "WORK", "prompt": "g", "inputs": ["build_out"]},
            "desc": {"kind": "WORK", "prompt": "d", "inputs": ["gate_out"]},
            "end": {"kind": "WORK", "prompt": "e"},
        },
        edges={
            "start": [WireSpec(target="build", when="flag"), WireSpec(target="end")],
            "build": [WireSpec(target="gate")],
            "gate": [WireSpec(target="desc")],
            "desc": [WireSpec(target="end")],
        },
    )
    result = await _run(spec, Fake(["no"]), tmp_path)
    assert result.ended == "end"
    skipped = {e["node"] for e in result.events if e["kind"] == "node_skipped"}
    assert {"build", "gate", "desc"} <= skipped


async def test_failed_block_raises(tmp_path: Path) -> None:
    spec = WorkflowSpec(
        title="blk",
        inputs=[],
        nodes={
            "start": {"kind": "WORK", "run": "true"},
            "build": {"kind": "WORK", "run": "false", "on_fail": "block"},
        },
        edges={"start": [WireSpec(target="build")]},
    )
    with pytest.raises(StoryBlocked):
        await _run(spec, Fake(["unused"]), tmp_path)


async def test_work_shell(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("hello")
    spec = WorkflowSpec(
        title="sh",
        inputs=[],
        nodes={"a": {"kind": "WORK", "run": "test -f file", "outputs": {"ok": {"format": "text"}}}},
        edges={"a": [WireSpec(target="a", loop=LoopBound(max=2))]},  # a retry-on-fail bound; run succeeds
        outputs=["ok"],
    )
    result = await _run(spec, Fake(["unused"]), tmp_path)
    assert result.outputs.get("ok") is None
    assert result.ended == "a"


async def test_round_robin(tmp_path: Path) -> None:
    spec = WorkflowSpec(
        title="rr",
        inputs=[],
        nodes={
            "agree": {
                "kind": "ROUND_ROBIN",
                "roles": ["product", "architect"],
                "prompt": "align",
                "agreement_when": "align",
                "steps": 3,
                "outputs": {"agreement": {"format": "text"}},
            },
            "done": {"kind": "WORK", "prompt": "d", "inputs": ["agreement"], "outputs": {}},
        },
        edges={"agree": [WireSpec(target="done")]},
        outputs=["agreement"],
    )
    engine = Fake(["nope", "align"])
    result = await _run(spec, engine, tmp_path)
    assert result.outputs["agreement"] == "align"
    assert engine.calls[:2] == ["product", "architect"]


async def test_supervisor_workers_single(tmp_path: Path) -> None:
    spec = WorkflowSpec(
        title="sw",
        inputs=[],
        nodes={
            "a": {
                "kind": "SUPERVISOR_WORKERS",
                "supervisor": "arch",
                "worker": "code",
                "workers": 1,
                "prompt": "split",
                "outputs": {"out": {"format": "text"}},
            },
            "done": {"kind": "WORK", "prompt": "d", "inputs": ["out"], "outputs": {}},
        },
        edges={"a": [WireSpec(target="done")]},
        outputs=["out"],
    )
    engine = Fake(['["do it"]', "worker result", "synthesis"])
    result = await _run(spec, engine, tmp_path)
    assert result.outputs["out"] == "synthesis"
    assert engine.calls[0] == "arch" and "code" in engine.calls


async def test_k_independent_single(tmp_path: Path) -> None:
    spec = WorkflowSpec(
        title="kin",
        inputs=[],
        nodes={
            "t": {
                "kind": "K_INDEPENDENT_TEAMS",
                "teams": 1,
                "roles": ["architect"],
                "prompt": "solve",
                "outputs": {"winner": {"format": "text"}},
            },
            "done": {"kind": "WORK", "prompt": "d", "inputs": ["winner"], "outputs": {}},
        },
        edges={"t": [WireSpec(target="done")]},
        outputs=["winner"],
    )
    engine = Fake(["blueprint"])
    result = await _run(spec, engine, tmp_path)
    assert result.outputs["winner"] == "blueprint"
    assert engine.calls[0] == "architect"


async def test_stall_when_input_never_produced(tmp_path: Path) -> None:
    spec = WorkflowSpec(
        title="stall",
        inputs=[],
        nodes={
            "a": {"kind": "WORK", "prompt": "a"},
            "b": {"kind": "WORK", "prompt": "b", "inputs": ["missing"]},
        },
        edges={"a": [WireSpec(target="b")]},
    )
    with pytest.raises(Stalled):
        await _run(spec, Fake(["x"]), tmp_path)
