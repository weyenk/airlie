"""Node and workflow models, and the static checks run when the config loads.

A workflow is a graph of typed nodes. `check_workflows` catches wiring mistakes (unknown nodes, a missing
field for a node's type, an input nothing produces) before anything runs. This module imports nothing from
`airlie.config.schema` (which imports it), so it declares its own strict base."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


NodeType = Literal["ask", "conversation", "write", "meeting", "check", "gate", "parallel", "each"]
Format = Literal[
    "json", "markdown", "text", "bool", "list", "ref:branch", "ref:worktree", "ref:pr", "file", "session"
]


class LoopSpec(Strict):
    to: str  # node to run again
    max: int | str  # a count, or a dotted config path such as "story.layer_attempts"
    when: str | None = None  # registry predicate id; None means "when the node failed"
    carry: str | None = None  # output name copied into the store as "feedback.<to>"


class NodeSpec(Strict):
    type: NodeType
    role: str | list[str] | None = None
    prompt: str | None = None  # registry id: brief builder (ask/conversation/write), opening brief
    turn: str | None = None  # registry id: a conversation's turn prompt
    parse: str | None = None  # registry id: reply parser
    run: str | None = None  # registry id: a check's or gate's callable
    after: str | None = None  # registry id: side effect after the outputs land (a board comment)
    inputs: list[str] = []
    outputs: dict[str, Format] = {}
    steps: int | str | None = None
    tools: list[str] | None = None
    done: str | None = None  # registry predicate id
    on_fail: Literal["block", "continue"] | LoopSpec = "block"
    when: str | None = None  # registry predicate id; false skips the node
    next: str | None = None
    on: str | None = None  # branch: which output
    branch: dict[str, str] | None = None  # output value -> node; "*" is the default
    loop: LoopSpec | None = None
    nodes: list[str] = []  # parallel
    over: str | None = None  # each: list artifact
    workflow: str | None = None  # each: bundle name
    args: dict[str, Any] = {}  # extra values handed to the callable
    session: str | None = None  # conversation/write: the session this node continues
    advisor: str | None = None  # write, Part 2


class WorkflowSpec(Strict):
    start: str
    loose: list[str] = []  # inputs the caller supplies: card, repo
    nodes: dict[str, NodeSpec]


def _requirements(node: NodeSpec) -> str | None:
    """The per-type message for a node missing a field its type needs, else None."""
    t = node.type
    if t in ("ask", "conversation", "write"):
        return f"{t} needs role and prompt" if not (node.role and node.prompt) else None
    if t == "meeting":
        return "meeting needs role and run" if not (node.role and node.run) else None
    if t in ("check", "gate"):
        return f"{t} needs run" if not node.run else None
    if t == "each":
        return "each needs over and workflow" if not (node.over and node.workflow) else None
    return "parallel needs nodes" if not node.nodes else None


def _produced(w: WorkflowSpec) -> set[str]:
    names: set[str] = set()
    for n in w.nodes.values():
        names.update(n.outputs)
        for loop in (n.loop, n.on_fail if isinstance(n.on_fail, LoopSpec) else None):
            if loop is not None and loop.carry:
                names.add(f"feedback.{loop.to}")
    return names


def check_workflows(workflows: Mapping[str, WorkflowSpec]) -> list[str]:
    """Return one problem string per mistake: "workflow <w>: node <n>: <what>". Empty means consistent."""
    problems: list[str] = []
    callers: dict[str, list[str]] = {}  # bundle -> workflows whose `each` nodes call it
    for wname, w in workflows.items():
        for n in w.nodes.values():
            if n.type == "each" and n.workflow is not None and n.workflow in workflows:
                callers.setdefault(n.workflow, []).append(wname)
    for wname, w in workflows.items():
        known = set(w.nodes)
        available = _produced(w) | set(w.loose)
        for caller in callers.get(wname, []):
            available |= _produced(workflows[caller]) | set(workflows[caller].loose)
        if w.start not in known:
            problems.append(f"workflow {wname}: start names unknown node '{w.start}'")
        for nname, n in w.nodes.items():

            def add(what: str, _w: str = wname, _n: str = nname) -> None:
                problems.append(f"workflow {_w}: node {_n}: {what}")

            if (missing := _requirements(n)) is not None:
                add(missing)
            for name in n.inputs:
                if name not in available:
                    add(f"input '{name}' is not produced by any node and not loose")
            if n.next is not None and n.next not in known:
                add(f"next names unknown node '{n.next}'")
            for target in (n.branch or {}).values():
                if target not in known:
                    add(f"branch names unknown node '{target}'")
            for target in n.nodes:
                if target not in known:
                    add(f"nodes names unknown node '{target}'")
            for loop in (n.loop, n.on_fail if isinstance(n.on_fail, LoopSpec) else None):
                if loop is not None and loop.to not in known:
                    add(f"loop names unknown node '{loop.to}'")
            if n.workflow is not None and n.workflow not in workflows:
                add(f"workflow names unknown workflow '{n.workflow}'")
    return problems
