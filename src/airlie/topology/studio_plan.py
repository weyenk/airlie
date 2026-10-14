"""Chief protocol (design §5): the chief emits one JSON object per turn: plan, synthesis, delegate, answer."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from airlie.tools.jsonargs import repair_json_args


class PlanError(ValueError):
    """A plan the scheduler cannot run; the message names the subtask."""


@dataclass(frozen=True)
class Subtask:
    id: str
    brief: str
    role_hint: str | None = None
    depends_on: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    pinned: bool = False
    """The role was chosen by a person or a pipeline: the router does not override it."""


@dataclass(frozen=True)
class Plan:
    assumptions: tuple[str, ...]
    subtasks: tuple[Subtask, ...]


@dataclass(frozen=True)
class Synthesis:
    answer: str
    open_questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Delegate:
    subtask: Subtask


@dataclass(frozen=True)
class Answer:
    text: str


def extract_json(text: str) -> dict[str, Any] | None:
    """The first JSON object in `text` (prose before or after is ignored), repaired when possible."""
    start = text.find("{")
    if start < 0:
        return None
    fixed = repair_json_args(text[start:])
    try:
        obj = json.loads(fixed)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _subtask(d: Any) -> Subtask | None:
    if not isinstance(d, dict) or not d.get("id") or not d.get("brief"):
        return None
    return Subtask(
        id=str(d["id"]),
        brief=str(d["brief"]),
        role_hint=str(d["role"]) if d.get("role") else None,
        depends_on=tuple(str(x) for x in d.get("depends_on") or []),
        inputs=tuple(str(x) for x in d.get("inputs") or []),
    )


def parse_chief(text: str) -> Plan | Synthesis | Delegate | Answer | None:
    obj = extract_json(text)
    if obj is None:
        return None
    kind = obj.get("type")
    if kind == "plan":
        subs = [_subtask(d) for d in obj.get("subtasks") or []]
        if not subs or any(s is None for s in subs):
            return None
        return Plan(tuple(str(a) for a in obj.get("assumptions") or []), tuple(s for s in subs if s))
    if kind == "synthesis":
        return Synthesis(str(obj.get("answer", "")), tuple(str(q) for q in obj.get("open_questions") or []))
    if kind == "delegate":
        s = _subtask(obj.get("subtask"))
        return Delegate(s) if s else None
    if kind == "answer":
        return Answer(str(obj.get("answer", "")))
    return None


_OPENER = re.compile(r"```json[ \t]*\r?\n", re.I)


def extract_json_block(text: str) -> dict[str, Any] | None:
    """The object under the last ```json fence in `text`, else the first bare object (`extract_json`). Roles
    that think aloud put braces in their prose, so the last fence marks the answer, and the object's own end,
    not a closing fence, ends it: a fence quoted in a JSON string follows backslash-n, so it never opens."""
    openers = list(_OPENER.finditer(text))
    if not openers:
        return extract_json(text)
    start = openers[-1].end()
    body = text[start:].lstrip()
    return extract_json(body) if body.startswith("{") else None


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    """The chief's own plan shape, so a person reads and edits what the chief writes."""
    return {
        "type": "plan",
        "assumptions": list(plan.assumptions),
        "subtasks": [
            {
                "id": s.id,
                "role": s.role_hint,
                "brief": s.brief,
                "depends_on": list(s.depends_on),
                "inputs": list(s.inputs),
            }
            for s in plan.subtasks
        ],
    }


def plan_from_dict(obj: dict[str, Any]) -> Plan:
    """A plan a person or a pipeline wrote (the chief's shape); PlanError says what to fix."""
    raw = obj.get("subtasks")
    if not isinstance(raw, list) or not raw:
        raise PlanError("a plan needs a non-empty 'subtasks' list")
    subs: list[Subtask] = []
    for n, d in enumerate(raw, 1):
        s = _subtask(d)
        if s is None:
            raise PlanError(f"subtask {n} needs an 'id' and a 'brief'")
        subs.append(s)
    return Plan(tuple(str(a) for a in obj.get("assumptions") or []), tuple(subs))


def pin(plan: Plan) -> Plan:
    return Plan(plan.assumptions, tuple(replace(s, pinned=True) for s in plan.subtasks))


def schedule(plan: Plan, *, satisfied: frozenset[str] = frozenset()) -> list[list[Subtask]]:
    """Waves in dependency order; a wave's subtasks are independent of each other. `satisfied` names
    subtasks that already ran (a chief delegate may depend on them)."""
    ids = [s.id for s in plan.subtasks]
    if len(set(ids)) != len(ids):
        raise PlanError(f"duplicate subtask id in {ids}")
    by = {s.id: s for s in plan.subtasks}
    for s in plan.subtasks:
        for d in s.depends_on:
            if d not in by and d not in satisfied:
                raise PlanError(f"subtask '{s.id}' depends on '{d}', which is not in the plan")
    done: set[str] = set(satisfied)
    waves: list[list[Subtask]] = []
    remaining = list(plan.subtasks)
    while remaining:
        wave = [s for s in remaining if all(d in done for d in s.depends_on)]
        if not wave:
            raise PlanError(f"dependency cycle among {[s.id for s in remaining]}")
        waves.append(wave)
        done |= {s.id for s in wave}
        remaining = [s for s in remaining if s.id not in done]
    return waves
