"""Node and workflow models, and the static checks run when the config loads.

A workflow is a graph of typed nodes. `check_workflows` catches wiring mistakes (unknown nodes, a missing
field for a node's type, an input nothing produces) before anything runs. This module imports nothing from
`airlie.config.schema` (which imports it), so it declares its own strict base."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class Layout(Strict):
    x: float
    y: float


class WorkflowSpec(Strict):
    type: str | None = None  # the card type this workflow serves; the composer ignores it
    start: str
    loose: list[str] = []  # inputs the caller supplies: card, repo
    nodes: dict[str, NodeSpec]
    layout: dict[str, Layout] = Field(default_factory=dict)  # canvas positions; the composer ignores them


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


def _prompt_file(node: NodeSpec, readable: set[str], prompts: Collection[str]) -> list[str]:
    """What is wrong with the prompt file a node's `prompt: "prompt:<id>"` names: an id that is neither a
    hub rule nor in `prompts`, or a field the node cannot read (the hub's rules always, a draft's when
    `prompts` maps its ids to their parsed files)."""
    from airlie.flow.prompt_files import FEEDBACK, PREFIX, rule_ids
    from airlie.prompts.hub import PromptText, entry

    if node.prompt is None or not node.prompt.startswith(PREFIX):
        return []
    id = node.prompt.removeprefix(PREFIX)
    parsed: PromptText | None
    if id in rule_ids():  # the hub first (D19)
        parsed = entry(id)
    elif id in prompts:
        parsed = prompts[id] if isinstance(prompts, Mapping) else None
    else:
        return [f"unknown prompt file '{id}'"]
    fields = parsed.fields if parsed is not None else ()
    return [
        f"prompt file '{id}' reads '{name}', which is not an input of the node"
        for name in fields
        if name != FEEDBACK and name not in readable
    ]


def check_workflows(workflows: Mapping[str, WorkflowSpec], *, prompts: Collection[str] = ()) -> list[str]:
    """Return one problem string per mistake: "workflow <w>: node <n>: <what>". Empty means consistent.
    `prompts` are the ids of the prompt files the drafts carry, beside the hub's rules, that a node's
    `prompt: "prompt:<id>"` may name; a mapping of them to their parsed files (`PromptText`) has their
    fields checked as well."""
    problems: list[str] = []
    callers: dict[str, list[str]] = {}  # bundle -> workflows whose `each` nodes call it
    for wname, w in workflows.items():
        for n in w.nodes.values():
            if n.type == "each" and n.workflow is not None and n.workflow in workflows:
                callers.setdefault(n.workflow, []).append(wname)
    for wname, w in workflows.items():
        known = set(w.nodes)
        children = {c for n in w.nodes.values() if n.type == "parallel" for c in n.nodes}
        available = _produced(w) | set(w.loose)
        for caller in callers.get(wname, []):
            available |= _produced(workflows[caller]) | set(workflows[caller].loose)
        if w.start not in known:
            problems.append(f"workflow {wname}: start names unknown node '{w.start}'")
        for lname in w.layout:
            if lname not in known:
                problems.append(f"workflow {wname}: layout names unknown node '{lname}'")
        for nname, n in w.nodes.items():

            def add(what: str, _w: str = wname, _n: str = nname) -> None:
                problems.append(f"workflow {_w}: node {_n}: {what}")

            if (missing := _requirements(n)) is not None:
                add(missing)
            for what in _prompt_file(n, set(n.inputs) | set(w.loose), prompts):
                add(what)
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
            if nname in children and (
                n.loop is not None
                or n.branch is not None
                or n.next is not None
                or isinstance(n.on_fail, LoopSpec)
            ):
                add("a parallel child may not use loop, branch, next or an on_fail loop")
            for loop in (n.loop, n.on_fail if isinstance(n.on_fail, LoopSpec) else None):
                if loop is not None and loop.to not in known:
                    add(f"loop names unknown node '{loop.to}'")
            if n.workflow is not None and n.workflow not in workflows:
                add(f"workflow names unknown workflow '{n.workflow}'")
    return problems
