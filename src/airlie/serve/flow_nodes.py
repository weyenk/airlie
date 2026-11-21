# mypy: ignore-errors
"""A card on any workflow as the pipeline screen draws it (D22): one box per node of the workflow it
snapshotted, in the order the file lists them, with the status the composer's node events give it. The lines
are the composer's business and are not drawn. The retired bundle kind is not expanded here."""

from __future__ import annotations

from typing import Any

from airlie.serve.flow_parts import Shape, card, job, shaped, step_row, tag
from airlie.workflow.spec import NodeSpec, WorkflowSpec

_KINDS = ("node_started", "node_finished", "node_skipped")
Key = tuple[str, str, str | None]
"""Whose events: workflow, node, each-item (None outside a former bundle)."""


def _attributed(events: list[dict[str, Any]], root: str) -> dict[Key, list[dict[str, Any]]]:
    """The node events by workflow, node and item. A `node_finished` names them as a start does; one from
    before the composer named them names only the node, and closes the latest open start of that name."""
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
    # Four-kind model: WORK with a shell `run` is code; prompt WORK / meetings are model boxes.
    if getattr(spec, "kind", None) == "WORK" and getattr(spec, "run", None):
        return "code"
    if getattr(spec, "kind", None) == "WORK" and name == "planned":
        return "milestone"
    return "model"


def _role(spec: NodeSpec) -> str:
    kind = getattr(spec, "kind", None)
    if kind == "ROUND_ROBIN":
        return ", ".join(getattr(spec, "roles", None) or [])
    if kind == "SUPERVISOR_WORKERS":
        return f"{spec.supervisor} / {spec.worker}"
    r = getattr(spec, "role", None)
    return r or ""


def _job(name: str, spec: NodeSpec, events: list[dict[str, Any]]) -> dict[str, Any]:
    status = _status(events)
    starts = [e for e in events if e["kind"] == "node_started"]
    start = starts[-1] if starts else None
    end = events[-1] if start is not None and events[-1]["kind"] == "node_finished" else None
    a, b = _ts(start), _ts(end)
    kind = getattr(spec, "kind", None) or "WORK"
    box = job(
        name,
        name,
        _role(spec),
        status,
        started=a,
        ended=b,
        duration_s=round(b - a, 1) if a is not None and b is not None else None,
    )
    return shaped(
        box,
        _shape(name, spec),
        live=kind if status == "running" else None,
        tags=[tag("gate", "chip")] if getattr(spec, "gate", None) is not None else (),
        card=card(name, [_row(e) for e in events]),
    )


def _stage(box: dict[str, Any]) -> dict[str, Any]:
    return {"id": box["id"], "label": box["label"], "kind": "stage", "status": box["status"], "jobs": [box]}


def node_flow(st: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pipeline boxes for the card's snapshotted workflow (`st["workflow"]`).

    One stage per root node, in file order. Statuses and hover cards come from the node events.
    """
    snap = st["workflow"]
    root = str(snap["name"])
    specs = {n: WorkflowSpec.model_validate(d) for n, d in snap["specs"].items()}
    by = _attributed(events, root)
    return [
        _stage(_job(node, spec, by.get((root, node, None), []))) for node, spec in specs[root].nodes.items()
    ]
