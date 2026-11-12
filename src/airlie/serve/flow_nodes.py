"""A card on any workflow as the pipeline screen draws it (D22): one box per node of the workflow it
snapshotted, in the order the file lists them, with the status the composer's node events give it. The lines
(`to`) are the composer's business and are not drawn. A `bundle` node is drawn through the workflow it runs:
with `each`, a lane per item it has reached, holding that workflow's nodes; without, those nodes flat, in
place. The story's own screen (flow_plan, flow_build) is not drawn from here."""

from __future__ import annotations

from typing import Any

from airlie.flow.spec import NodeSpec, TeamRole, WorkflowSpec
from airlie.serve.flow_parts import Shape, card, job, shaped, step_row, tag

_MODEL = frozenset({"ask", "conversation", "write", "meeting"})
_KINDS = ("node_started", "node_finished", "node_skipped")
Key = tuple[str, str, str | None]
"""Whose events: workflow, node, each-item (None outside a bundle)."""


def _attributed(events: list[dict[str, Any]], root: str) -> dict[Key, list[dict[str, Any]]]:
    """The node events by workflow, node and item. A `node_finished` names them as a start does; one from
    before the composer named them names only the node, and closes the latest open start of that name (a
    bundle's items run one after another)."""
    out: dict[Key, list[dict[str, Any]]] = {}
    open_: dict[str, list[Key]] = {}
    for e in events:
        kind, node = e.get("kind"), e.get("node")
        if kind not in _KINDS or not isinstance(node, str):
            continue
        if kind == "node_finished":
            starts = open_.get(node, [])
            if e.get("workflow"):
                item = e.get("item")
                key: Key = (str(e["workflow"]), node, item if isinstance(item, str) else None)
                if key in starts:
                    starts.remove(key)
            else:
                key = starts.pop() if starts else (root, node, None)
        else:
            item = e.get("item")
            key = (str(e.get("workflow") or root), node, item if isinstance(item, str) else None)
            if kind == "node_started":
                open_.setdefault(node, []).append(key)
        out.setdefault(key, []).append(e)
    return out


def _status(events: list[dict[str, Any]]) -> str:
    """The last run of a node: skipped, running (started, not finished), passed or failed; pending if none."""
    last = events[-1] if events else None
    if last is None:
        return "pending"
    if last["kind"] == "node_skipped":
        return "skipped"
    if last["kind"] == "node_started":
        return "running"
    return "passed" if last.get("outcome") == "done" else "failed"


def _ts(e: dict[str, Any] | None) -> float | None:
    v = (e or {}).get("ts")
    return float(v) if isinstance(v, float | int) else None


def _row(e: dict[str, Any]) -> dict[str, Any]:
    kind = str(e["kind"])
    if kind == "node_finished":
        status = "passed" if e.get("outcome") == "done" else "failed"
    else:
        status = "running" if kind == "node_started" else "skipped"
    ms = e.get("ms")
    parts = [kind, str(e["outcome"]) if e.get("outcome") else "", f"{ms} ms" if ms is not None else ""]
    return step_row(status, " ".join(p for p in parts if p))


def _shape(name: str, spec: NodeSpec) -> Shape:
    if spec.type in _MODEL:
        return "model"
    return "milestone" if spec.type == "check" and name == "planned" else "code"


def _job(name: str, spec: NodeSpec, events: list[dict[str, Any]]) -> dict[str, Any]:
    status = _status(events)
    starts = [e for e in events if e["kind"] == "node_started"]
    start = starts[-1] if starts else None
    end = events[-1] if start is not None and events[-1]["kind"] == "node_finished" else None
    a, b = _ts(start), _ts(end)
    r = spec.role
    role = (
        ", ".join(r)
        if isinstance(r, list)
        else f"{r.supervisor} / {r.worker}"
        if isinstance(r, TeamRole)
        else r or ""
    )
    box = job(
        name,
        name,
        role,
        status,
        started=a,
        ended=b,
        duration_s=round(b - a, 1) if a is not None and b is not None else None,
    )
    return shaped(
        box,
        _shape(name, spec),
        live=spec.type if status == "running" else None,
        tags=[tag("gate", "chip")] if spec.type == "gate" else (),
        card=card(name, [_row(e) for e in events]),
    )


def _stage(box: dict[str, Any]) -> dict[str, Any]:
    return {"id": box["id"], "label": box["label"], "kind": "stage", "status": box["status"], "jobs": [box]}


def _lane_status(jobs: list[dict[str, Any]]) -> str:
    statuses = {j["status"] for j in jobs}
    if "failed" in statuses:
        return "failed"
    if "running" in statuses:
        return "running"
    if statuses <= {"passed", "skipped"}:
        return "passed"
    return "pending" if statuses == {"pending"} else "running"  # between two of its steps


def _lanes(
    name: str, spec: NodeSpec, specs: dict[str, WorkflowSpec], by: dict[Key, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """A bundle with `each`: one lane per item it has reached, in the order reached."""
    bundle = specs.get(spec.workflow or "")
    if bundle is None:
        return []
    items = list(dict.fromkeys(i for (w, _, i) in by if w == spec.workflow and i is not None))
    lanes = []
    for item in items:
        jobs = [
            _job(n, s, by.get((spec.workflow or "", n, item), []))
            for n, s in bundle.nodes.items()
            if s.type != "bundle"
        ]
        status = _lane_status(jobs)
        row = {"kind": "cycle", "id": "c1", "label": item, "status": status, "jobs": jobs}
        lanes.append(
            {
                "id": f"{name}:{item}",
                "label": item,
                "kind": "layer",
                "title": name,
                "scope": None,
                "acceptance": [],
                "attempts": 0,
                "status": status,
                "tags": [],
                "rows": [row],
                "jobs": jobs,
            }
        )
    return lanes


def node_flow(st: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The nodes of the card's snapshotted workflow (`st["workflow"]`) for the pipeline screen, in the root
    workflow's order: a stage per node with its one job, a bundle's nodes flat in its place, or, with `each`,
    its lanes (`kind: "layer"`) per item. Statuses and hover cards come from the node events."""
    snap = st["workflow"]
    root = str(snap["name"])
    specs = {n: WorkflowSpec.model_validate(d) for n, d in snap["specs"].items()}
    by = _attributed(events, root)
    return _draw(root, specs, by, ())


def _draw(
    name: str, specs: dict[str, WorkflowSpec], by: dict[Key, list[dict[str, Any]]], seen: tuple[str, ...]
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for node, spec in specs[name].nodes.items():
        if spec.type != "bundle":
            nodes.append(_stage(_job(node, spec, by.get((name, node, None), []))))
        elif spec.each:
            nodes += _lanes(node, spec, specs, by)
        elif spec.workflow in specs and spec.workflow not in (*seen, name):
            nodes += _draw(spec.workflow, specs, by, (*seen, name))
    return nodes
