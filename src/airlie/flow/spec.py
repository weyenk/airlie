"""Node and workflow models, and the static checks run when the config loads.

A workflow is a graph of typed nodes joined by lines: a line carries the artifacts its source produces to
the node it names, with an optional condition and loop bound. A model node (ask, conversation, write,
meeting, team) is configuration only; a check or gate names an operation by `run`; a bundle runs another
workflow as one step. `check_workflows` catches wiring mistakes (unknown nodes, a missing field for a
node's type, an input nothing produces, a cycle without a bound) before anything runs. This module imports
nothing from `airlie.config.schema` (which imports it)."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Literal

from pydantic import Field, model_validator

from airlie.flow.outputs import Format, OutputSpec, Strict

__all__ = ["Format", "Strict"]  # re-exported: readers of the specs import them from here

NodeType = Literal["ask", "conversation", "write", "meeting", "team", "check", "gate", "bundle"]

RESERVED = ("path", "artifacts", "workflow")
"""Names of the story state's own bookkeeping that no node output may take: `path` (where the state lives,
never saved), `artifacts` (the store of a workflow's own outputs, D15) and `workflow` (the card's snapshot,
D21). `artifacts.<name>` is a name inside the store, and is fine; nothing may write inside `path` or
`workflow`."""

_OLD_NODE_FIELDS = ("parse", "after", "done", "when", "next", "on", "branch", "loop", "nodes", "over")
_OLD_WORKFLOW_FIELDS = ("start", "loose")
_OLD_TYPES = ("parallel", "each")


def _old(what: str, then: str = "write a `to` line") -> str:
    return f"{what} is the old schema; run the workflow upgrade or {then}"


def reserved_output(name: str) -> bool:
    """`name` would overwrite the state's own bookkeeping (`RESERVED`)."""
    head, dot, _ = name.partition(".")
    return head in RESERVED and (not dot or head != "artifacts")


def _old_node_fields(raw: Mapping[str, Any]) -> list[str]:
    found = [_old(f"field '{f}'") for f in _OLD_NODE_FIELDS if f in raw]
    if isinstance(raw.get("on_fail"), Mapping):
        found.append(_old("field 'on_fail'", "draw a loop line"))
    if raw.get("type") in _OLD_TYPES:
        found.append(_old(f"type '{raw['type']}'"))
    return found


def _old_workflow_fields(raw: Mapping[str, Any]) -> list[str]:
    return [_old(f"field '{f}'") for f in _OLD_WORKFLOW_FIELDS if f in raw]


def old_schema_problems(raw: Mapping[str, Any]) -> list[str]:
    """What in a raw workflow mapping, its nodes included, is the old schema: "field 'start' is the old
    schema ..." or "node a: field 'next' is the old schema ...". Empty when it is the current schema."""
    found = _old_workflow_fields(raw)
    nodes = raw.get("nodes")
    for nname, node in nodes.items() if isinstance(nodes, Mapping) else ():
        if isinstance(node, Mapping):
            found += [f"node {nname}: {what}" for what in _old_node_fields(node)]
    return found


class LoopBound(Strict):
    max: int | str  # a count, or a dotted config path such as "story.layer_attempts"
    carry: str | None = None  # output name copied into the store as "feedback.<target>"


class Line(Strict):
    """An artifact going from the node it is listed on to `node`. `when` is a condition; `loop` marks a line
    drawn back to an earlier node, with its bound."""

    node: str
    when: str | None = None
    loop: LoopBound | None = None

    @model_validator(mode="before")
    @classmethod
    def _bare_name(cls, data: Any) -> Any:
        return {"node": data} if isinstance(data, str) else data


class TeamRole(Strict):
    supervisor: str
    worker: str


class NodeSpec(Strict):
    type: NodeType
    role: str | list[str] | TeamRole | None = None
    prompt: str | None = None  # the text, or "prompt:<id>" for a prompt file
    turn: str | None = None  # a conversation's turn text
    worker: str | None = None  # a team's worker template text
    inputs: list[str] = []
    outputs: dict[str, OutputSpec] = {}
    steps: int | str | None = None
    tools: list[str] | None = None
    on_fail: Literal["block", "continue"] = "block"
    events: dict[Literal["done", "failed"], str] = {}  # legacy event kinds the node's outcomes emit (D14)
    session: str | None = None  # the session input this node continues
    advisor: str | None = None  # write, Part 2
    workers: int | None = None  # team: the cap
    run: str | None = None  # check/gate: the catalog operation
    args: dict[str, Any] = {}  # check/gate: extra values handed to the operation
    workflow: str | None = None  # bundle: the workflow it runs
    each: str | None = None  # bundle: the list input run once per item
    to: list[Line] = []

    @model_validator(mode="before")
    @classmethod
    def _refuse_old_schema(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and (old := _old_node_fields(data)):
            raise ValueError("; ".join(old))
        return data


class Layout(Strict):
    x: float
    y: float


class WorkflowSpec(Strict):
    type: str | None = None  # the card type this workflow serves; the composer ignores it
    inputs: list[str] = []  # what the caller supplies: card, repo, or a bundle's inputs
    outputs: list[str] = []  # what a bundle hands back
    nodes: dict[str, NodeSpec]
    layout: dict[str, Layout] = Field(default_factory=dict)  # canvas positions; the composer ignores them

    @model_validator(mode="before")
    @classmethod
    def _refuse_old_schema(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and (old := _old_workflow_fields(data)):
            raise ValueError("; ".join(old))
        return data


_MODEL_TYPES = ("ask", "conversation", "write", "meeting", "team")
_CODE_FIELDS = ("run", "args", "workflow", "each")
_MODEL_FIELDS = ("prompt", "turn", "worker", "session", "workers")


def _set(node: NodeSpec, names: tuple[str, ...]) -> list[str]:
    return [f for f in names if getattr(node, f) not in (None, {}, [])]


def _requirements(node: NodeSpec) -> str | None:
    """The per-type message for a node missing a field its type needs, else None."""
    t = node.type
    if t in ("ask", "conversation", "write"):
        return (
            None if isinstance(node.role, str) and node.role and node.prompt else f"{t} needs role and prompt"
        )
    if t == "meeting":
        ok = isinstance(node.role, list) and node.role and node.prompt
        return None if ok else "meeting needs role (a list) and prompt"
    if t == "team":
        ok = isinstance(node.role, TeamRole) and node.prompt and node.worker
        return None if ok else "team needs role (supervisor and worker), prompt and worker"
    if t == "bundle":
        return None if node.workflow else "bundle needs workflow"
    return None if node.run else f"{t} needs run"


def _wrong_fields(node: NodeSpec) -> str | None:
    """A model node that names code, or a check/gate that takes model fields, else None."""
    if node.type in _MODEL_TYPES and (code := _set(node, _CODE_FIELDS)):
        return f"{node.type} names code ({', '.join(code)}); a model node is configuration only"
    if node.type in ("check", "gate") and (model := _set(node, _MODEL_FIELDS)):
        return f"{node.type} takes no model fields ({', '.join(model)})"
    return None


def _produced(w: WorkflowSpec) -> set[str]:
    """Every name a node of `w` may read: its inputs, every output, `feedback` and `feedback.<node>` for
    each loop line that carries something."""
    names: set[str] = set(w.inputs) | {"feedback"}
    for n in w.nodes.values():
        names.update(n.outputs)
        names.update(f"feedback.{line.node}" for line in n.to if line.loop is not None and line.loop.carry)
    return names


def _carried_into(w: WorkflowSpec, target: str) -> set[str]:
    """What the lines into `target` carry: the outputs of the nodes that list it in `to`, and the feedback a
    loop line into it carries."""
    names: set[str] = set(w.inputs)
    for source in w.nodes.values():
        for line in source.to:
            if line.node == target:
                names.update(source.outputs)
                if line.loop is not None and line.loop.carry:
                    names.add(f"feedback.{target}")
    return names


def _closing_lines(w: WorkflowSpec) -> list[tuple[str, str]]:
    """The (source, target) of every line without a loop bound that closes a cycle of such lines."""
    edges = {
        name: [ln.node for ln in n.to if ln.loop is None and ln.node in w.nodes]
        for name, n in w.nodes.items()
    }

    def reaches(start: str, goal: str) -> bool:
        seen: set[str] = set()
        todo = [start]
        while todo:
            at = todo.pop()
            if at == goal:
                return True
            if at not in seen:
                seen.add(at)
                todo.extend(edges[at])
        return False

    return [(src, dst) for src, targets in edges.items() for dst in targets if reaches(dst, src)]


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


def _bundle_problems(
    w: WorkflowSpec, nname: str, n: NodeSpec, workflows: Mapping[str, WorkflowSpec]
) -> list[str]:
    """What is wrong with a bundle node's boundary: an input no line carries, an `each` that is not an input,
    a named workflow that is unknown or reads an input the node does not have."""
    found: list[str] = []
    carried = _carried_into(w, nname)
    found += [
        f"input '{name}' is not carried by a line into the node"
        for name in n.inputs
        if name not in carried and not name.startswith("feedback")
    ]
    if n.each is not None and n.each not in n.inputs:
        found.append(f"each '{n.each}' is not among the node's inputs")
    if n.workflow is not None:
        if n.workflow not in workflows:
            found.append(f"workflow names unknown workflow '{n.workflow}'")
        else:
            found += [
                f"workflow '{n.workflow}' reads input '{name}', which is not an input of the node"
                for name in workflows[n.workflow].inputs
                if name not in n.inputs
            ]
    return found


def _session_problems(w: WorkflowSpec, n: NodeSpec) -> list[str]:
    """A session input whose producer has another role than `n` (D25)."""
    found: list[str] = []
    for name in dict.fromkeys([*n.inputs, *([n.session] if n.session else [])]):
        for pname, p in w.nodes.items():
            out = p.outputs.get(name)
            if out is not None and out.format == "session" and p.role != n.role:
                role = p.role if isinstance(p.role, str) else repr(p.role)
                found.append(
                    f"input '{name}' is a session of node '{pname}' (role {role}); "
                    "a session continues only between nodes of the same role"
                )
    return found


def check_workflows(workflows: Mapping[str, WorkflowSpec], *, prompts: Collection[str] = ()) -> list[str]:
    """Return one problem string per mistake: "workflow <w>: node <n>: <what>". Empty means consistent.
    `prompts` are the ids of the prompt files the drafts carry, beside the hub's rules, that a node's
    `prompt: "prompt:<id>"` may name; a mapping of them to their parsed files (`PromptText`) has their
    fields checked as well."""
    problems: list[str] = []
    for wname, w in workflows.items():
        known = set(w.nodes)
        available = _produced(w)
        for lname in w.layout:
            if lname not in known:
                problems.append(f"workflow {wname}: layout names unknown node '{lname}'")
        closing = _closing_lines(w)
        for nname, n in w.nodes.items():
            found: list[str] = []
            if (missing := _requirements(n)) is not None:
                found.append(missing)
            if (wrong := _wrong_fields(n)) is not None:
                found.append(wrong)
            found += [f"output '{out}' is reserved" for out in n.outputs if reserved_output(out)]
            found += _prompt_file(n, set(n.inputs) | set(w.inputs), prompts)
            found += [
                f"input '{name}' is not produced by any node and not a workflow input"
                for name in n.inputs
                if name not in available
            ]
            found += [f"to names unknown node '{line.node}'" for line in n.to if line.node not in known]
            found += [
                f"the line to '{dst}' closes a cycle and has no loop bound"
                for src, dst in closing
                if src == nname
            ]
            if n.type == "bundle":
                found += _bundle_problems(w, nname, n, workflows)
            found += _session_problems(w, n)
            problems += [f"workflow {wname}: node {nname}: {what}" for what in found]
    return problems
