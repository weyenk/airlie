"""Shared helpers extracted from tests/unit/test_flow_nodes.py for tests.support.flow_nodes."""

from __future__ import annotations

from typing import Any

from airlie.flow.spec import Line, NodeSpec, WorkflowSpec


def snapshot(name: str, specs: dict[str, WorkflowSpec]) -> dict[str, Any]:
    return {
        "workflow": {
            "name": name,
            "specs": {n: s.model_dump(mode="json") for n, s in specs.items()},
            "prompts": {},
        }
    }


def bugfix() -> dict[str, Any]:
    root = WorkflowSpec(
        inputs=["card"],
        nodes={
            "intake": NodeSpec(type="check", run="story.intake", to=[Line(node="reproduce")]),
            "reproduce": NodeSpec(type="ask", role="architect", prompt="p", to=[Line(node="fix_layers")]),
            "fix_layers": NodeSpec(type="bundle", each="layers", workflow="fix", to=[Line(node="review")]),
            "review": NodeSpec(type="gate", run="story.review"),
        },
    )
    fix = WorkflowSpec(
        nodes={
            "write": NodeSpec(type="write", role="coder", prompt="p", to=[Line(node="gate")]),
            "gate": NodeSpec(type="gate", run="story.layer_gate"),
        },
    )
    return snapshot("bugfix", {"bugfix": root, "fix": fix})


def ev(
    ts: float, kind: str, node: str, type: str, workflow: str, item: str | None = None, **f: Any
) -> dict[str, Any]:
    e: dict[str, Any] = {"ts": ts, "run_id": "r1", "kind": kind, "node": node, "type": type}
    if workflow:  # a finish from before the composer named its workflow and item carries neither
        e.update(workflow=workflow, item=item)
    return {**e, **f}


def done(
    ts: float,
    node: str,
    type: str,
    outcome: str = "done",
    ms: int = 1000,
    *,
    workflow: str = "",
    item: str | None = None,
) -> dict[str, Any]:
    return ev(ts, "node_finished", node, type, workflow, item, outcome=outcome, ms=ms)


EVENTS = [
    ev(1.0, "node_started", "intake", "check", "bugfix"),
    done(2.0, "intake", "check"),
    ev(3.0, "node_started", "reproduce", "ask", "bugfix"),
    done(5.0, "reproduce", "ask"),
    ev(6.0, "node_started", "fix_layers", "each", "bugfix"),
    ev(7.0, "node_started", "write", "write", "fix", "01"),
    done(8.0, "write", "write"),
    ev(9.0, "node_started", "gate", "gate", "fix", "01"),
    done(10.0, "gate", "gate"),
    ev(11.0, "node_started", "write", "write", "fix", "02"),
]
