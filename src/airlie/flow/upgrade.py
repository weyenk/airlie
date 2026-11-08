"""An old-schema workflow mapping read as the current one (nodes-are-config design, D21): the wiring becomes
lines, and what named code is dropped with a problem that says what a person must write in its place.

The old schema ran nodes from `start` in their listed order, or by `next`; `branch` picked the next node by an
output's value (`*` the default), `loop` and an `on_fail` mapping jumped back with a bound, `parallel` ran
its `nodes` together, `each` ran a bundle per item of `over`, `loose` named what the caller supplies and a
node's `when` was a predicate in code. `upgrade_v1` turns each into lines (`to`), a `bundle` node and the
workflow's `inputs`. A mapping with none of the old keys is the current schema and passes through unchanged.

The problems name the node (`node <n>: ...`); the caller says which workflow. A node with a problem is left
in the result, so the builder can show it, and `story.workflows.workflow_problems` keeps it unrunnable until
a person fixes it."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from airlie.flow.spec import old_schema_problems

if TYPE_CHECKING:
    from airlie.flow.catalog import Catalog

__all__ = ["upgrade_v1"]

_MODEL_TYPES = ("ask", "conversation", "write", "meeting", "team")
_CODE_ON_MODEL = ("parse", "after", "done", "turn", "run", "args")
"""Old model-node fields that named code (a parser, a side effect, a done predicate, a turn builder, a runner)
or handed values to it: dropped, with `write this node's prompt`."""
_DROPPED = ("parse", "after", "done", "when", "next", "on", "branch", "loop", "nodes", "over")
_BARE = re.compile(r"[A-Za-z0-9_.\-]+")
_PROMPT_FILE = "prompt:"


def _set(value: Any) -> bool:
    """A field an old snapshot dumped with its default (None, [], {}) was never set."""
    return value not in (None, [], {}, "")


def _literal(value: str) -> str:
    return value if _BARE.fullmatch(value) and value not in ("and", "or", "not", "exists") else f'"{value}"'


def _old_loop(node: str, loop: Mapping[str, Any], problems: list[str], *, when: bool) -> dict[str, Any]:
    """A loop line from an old `loop` (or `on_fail`) mapping. An old loop's `when` was a predicate in code;
    `on_fail` ignored it."""
    line: dict[str, Any] = {"node": str(loop["to"]), "loop": {"max": loop["max"]}}
    if _set(loop.get("carry")):
        line["loop"]["carry"] = loop["carry"]
    if when and _set(loop.get("when")):
        problems.append(f"node {node}: condition '{loop['when']}' was code; write it as an expression")
    return line


def _forward(node: Mapping[str, Any], after: str | None) -> list[dict[str, Any]]:
    """The forward lines out of an old node: one per branch value (`<on> == <value>`), the default (`*`,
    else the next node) taken when none of them holds; else `next`; else the node listed after it."""
    fallthrough = str(node["next"]) if _set(node.get("next")) else after
    branch = node.get("branch") if _set(node.get("on")) else None
    if not isinstance(branch, Mapping) or not branch:
        return [{"node": fallthrough}] if fallthrough is not None else []
    on = str(node["on"])
    lines: list[dict[str, Any]] = []
    tests: list[str] = []
    for value, target in branch.items():
        if str(value) == "*":
            continue
        test = f"{on} == {_literal(str(value))}"
        tests.append(test)
        lines.append({"node": str(target), "when": test})
    default = str(branch["*"]) if "*" in branch else fallthrough
    if default is not None:
        line: dict[str, Any] = {"node": default}
        if tests:
            line["when"] = f"not ({' or '.join(tests)})"
        lines.append(line)
    return lines


def _reached(start: str | None, lines: Mapping[str, list[dict[str, Any]]]) -> set[str]:
    seen: set[str] = set()
    todo = [start] if start is not None else []
    while todo:
        at = todo.pop()
        if at in seen or at not in lines:
            continue
        seen.add(at)
        todo += [ln["node"] for ln in lines[at]]
    return seen


def upgrade_v1(
    data: Mapping[str, Any], *, catalog: Catalog | None = None
) -> tuple[dict[str, Any], list[str]]:
    """`data` (one workflow's mapping) in the current schema, and the problems the upgrade leaves:

    - `next`, `branch`/`on`, `loop` and an `on_fail` mapping become `to` lines: a branch value `v` a line
      with `when: "<on> == v"`, the default (`*`, or the node after) a line taken when none of them holds,
      listed last; a loop a line with its bound and carry. A node with none of them gets a line to the node
      listed after it, as the old schema ran them in order.
    - a `parallel` node goes: its children get the lines into it and the lines out of it.
    - an `each` node becomes a `bundle` node with `each: <over>`, which it reads as an input.
    - `loose` becomes `inputs`; `start` goes (a node with no line in is where a run starts), and a node the
      old start never reached is a problem.
    - a node's `when` (a predicate in code) goes, with the problem `node <n>: condition '<id>' was code;
      write it as an expression`.
    - a model node's code (a `prompt` that is no `prompt:<id>` file, `parse`, `after`, `done`, `turn`, `run`,
      `args`) goes, with the problem `node <n>: write this node's prompt`.
    - with a `catalog`, a check or gate node also reads every input its operation declares (the old schema
      read them from the story's state unnamed).

    A mapping with none of the old schema's keys is returned as it is (a copy), with no problems."""
    if not old_schema_problems(data):
        return dict(data), []
    problems: list[str] = []
    raw_nodes = data.get("nodes")
    nodes: dict[str, Mapping[str, Any]] = (
        {str(k): v for k, v in raw_nodes.items() if isinstance(v, Mapping)}
        if isinstance(raw_nodes, Mapping)
        else {}
    )
    groups = {
        name: [str(c) for c in n.get("nodes") or []]
        for name, n in nodes.items()
        if n.get("type") == "parallel"
    }
    children = {c for kids in groups.values() for c in kids}
    order = [n for n in nodes if n not in children]

    def into(target: str) -> list[str]:
        return groups.get(target, [target])

    def redirected(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**ln, "node": t} for ln in lines for t in into(ln["node"])]

    out_lines: dict[str, list[dict[str, Any]]] = {}
    for i, name in enumerate(order):
        node = nodes[name]
        lines: list[dict[str, Any]] = []
        if isinstance(node.get("loop"), Mapping):
            lines.append(_old_loop(name, node["loop"], problems, when=True))
        if isinstance(node.get("on_fail"), Mapping):
            lines.append(_old_loop(name, node["on_fail"], problems, when=False))
        after = order[i + 1] if i + 1 < len(order) else None
        lines += _forward(node, after)
        out_lines[name] = redirected(lines)
    for parent, kids in groups.items():
        for kid in kids:
            out_lines[kid] = [dict(ln) for ln in out_lines.get(parent, [])]
        out_lines.pop(parent, None)

    upgraded: dict[str, dict[str, Any]] = {}
    for name, node in nodes.items():
        if name in groups:
            if _set(node.get("when")):
                problems.append(
                    f"node {name}: condition '{node['when']}' was code; write it as an expression"
                )
            continue
        upgraded[name] = _node(name, node, out_lines.get(name, []), problems, catalog)

    start = str(data["start"]) if _set(data.get("start")) else None
    if start is not None and start not in nodes:
        problems.append(f"start names unknown node '{start}'")
    elif start is not None:
        reached: set[str] = set()
        for first in into(start):
            reached |= _reached(first, {n: upgraded[n].get("to", []) for n in upgraded})
        problems += [
            f"node {n}: the old workflow started at '{start}' and never reached it; draw a line into it or "
            "remove it"
            for n in upgraded
            if n not in reached
        ]

    out: dict[str, Any] = {
        k: v for k, v in data.items() if k not in ("start", "loose", "nodes", "inputs", "layout")
    }
    inputs = [*(data.get("inputs") or []), *(data.get("loose") or [])]
    if inputs:
        out["inputs"] = list(dict.fromkeys(inputs))
    out["nodes"] = upgraded
    layout = data.get("layout")
    if isinstance(layout, Mapping):
        out["layout"] = {k: v for k, v in layout.items() if k not in groups}
    return out, problems


def _node(
    name: str,
    node: Mapping[str, Any],
    lines: list[dict[str, Any]],
    problems: list[str],
    catalog: Catalog | None,
) -> dict[str, Any]:
    new = {k: v for k, v in node.items() if k not in _DROPPED and k != "to"}
    if isinstance(node.get("on_fail"), Mapping):
        new.pop("on_fail")  # a spent loop blocks, the current default
    if _set(node.get("when")):
        problems.append(f"node {name}: condition '{node['when']}' was code; write it as an expression")
    kind = node.get("type")
    if kind == "each":
        new["type"] = "bundle"
        over = node.get("over")
        if _set(over):
            new["each"] = over
            new["inputs"] = list(dict.fromkeys([str(over), *(node.get("inputs") or [])]))
    if kind in _MODEL_TYPES:
        prompt = node.get("prompt")
        code = [f for f in _CODE_ON_MODEL if _set(node.get(f))]
        if _set(prompt) and not str(prompt).startswith(_PROMPT_FILE):
            code.insert(0, "prompt")
        for field in code:
            new.pop(field, None)
        if code:
            problems.append(f"node {name}: write this node's prompt")
    if kind in ("check", "gate") and catalog is not None and str(node.get("run")) in catalog:
        declared = catalog[str(node["run"])].inputs
        new["inputs"] = list(dict.fromkeys([*(node.get("inputs") or []), *declared]))
    new = {k: v for k, v in new.items() if _set(v) or k == "type"}
    if lines:
        new["to"] = [dict(ln) for ln in lines]
    return new
