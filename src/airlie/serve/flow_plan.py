"""The plan lane of a story's flow: a box per step before the build (intake, base, gate, survey, refine, plan
layers, the three amigos, PLANNED), each with its status, its inner rounds and turns, and the hover card the
web workflow screen draws. Derived from the story's state.json and events, and the studio runs they name."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from airlie.serve.flow_parts import (
    STAGE_ROLES,
    card,
    detail,
    job,
    run_events,
    squares,
    step_row,
    tag,
    text_row,
    tokens,
    votes,
)
from airlie.story.state import STAGES

Shape = Literal["code", "model", "milestone"]

MEETING_ROUND = ("product", "architect", "tester", "airlie")
"""The order the three amigos and airlie's checker take their turns in."""

VOTERS = ("product", "architect", "tester")
"""The roles that vote; airlie's checker takes turns but has no verdict."""


def _stage_index(st: dict[str, Any]) -> int:
    return STAGES.index(st.get("stage", "new")) if st.get("stage", "new") in STAGES else 0


def _span(start: Any, end: Any) -> float | None:
    if isinstance(start, float | int) and isinstance(end, float | int):
        return round(end - start, 1)
    return None


def _shaped(
    box: dict[str, Any],
    shape: Shape,
    *,
    live: str | None = None,
    tags: Sequence[dict[str, Any]] = (),
    card: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """`box` (a job) with the fields the workflow screen draws a plan box by."""
    box.update(shape=shape, live=live, tags=list(tags), card=card, **fields)
    return box


def stage_node(
    name: str,
    label: str,
    st: dict[str, Any],
    events: list[dict[str, Any]],
    state_dir: Path,
    blocked: bool,
) -> dict[str, Any]:
    """A stage of the story as a node with one job (none when the survey never ran): passed once the story is
    past it, failed when the story is blocked right before it, running while its studio run is out."""
    ran = [e for e in events if e.get("kind") == "run" and e.get("stage") == name]
    finished = [e for e in events if e.get("kind") == "stage" and e.get("stage") == name]
    retries = [e for e in events if e.get("kind") in ("retry", "plan_fix") and e.get("stage") == name]
    index = _stage_index(st)
    done = index >= STAGES.index(name) if name in STAGES else False
    if name == "layers" and any(e.get("kind") == "plan_done" for e in events):
        done = True
    if name == "survey":
        done = any(e.get("kind") == "survey_done" for e in events)
        failed = any(e.get("kind") == "survey_failed" for e in events)
        status = "failed" if failed else "passed" if done else "running" if ran and not blocked else "skipped"
    elif done:
        status = "passed"
    elif blocked and index + 1 == STAGES.index(name):
        status = "failed"
    elif ran and not blocked:
        status = "running"
    else:
        status = "pending"
    box = job(name, label, STAGE_ROLES.get(name), "pending" if status == "skipped" else status)
    if ran:
        run = ran[-1]["studio_run"]
        toks = tokens(run_events(state_dir, run), name)
        start = ran[0].get("ts")
        end = finished[-1].get("ts") if finished and done else None
        box.update(
            studio_run=run,
            attempts=len(retries) + 1,
            retries=[{"kind": e["kind"], "detail": detail(e)} for e in retries],
            tokens=toks,
            role=(toks or {}).get("role") or box["role"],
            started=start,
            ended=end,
            duration_s=_span(start, end),
        )
    elif name == "intake" and status == "passed":
        box["attempts"] = 1
    return {
        "id": name,
        "label": label,
        "kind": "stage",
        "status": status,
        "jobs": [box] if status != "skipped" else [],
    }


def _run_card(
    title: str, box: dict[str, Any], rows: list[dict[str, Any]], tags: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """The card of a box that asks a model: `rows` first, then who ran it and for how long. None when there
    is nothing to say."""
    toks = box.get("tokens") or {}
    if toks:
        rows = [*rows, text_row("steps", f"{toks['steps']} step(s), {toks['completion']} tokens out")]
    if not rows and not tags:
        return None
    who = box.get("role")
    took = box.get("duration_s")
    foot = " · ".join(str(x) for x in (who, f"{took}s" if took is not None else None) if x) or None
    return card(title, rows, tags=tags, foot=foot)


def _code_job(
    id: str, label: str, status: str, events: list[dict[str, Any]], **fields: Any
) -> dict[str, Any]:
    """A step airlie takes itself, with no model: it has an instant, not a run."""
    box = job(id, label, None, status)
    ts = events[-1].get("ts") if events else None
    box.update(started=ts, ended=ts if status == "passed" else None)
    return _shaped(box, "code", **fields)


def _intake_node(st: dict[str, Any], events: list[dict[str, Any]], blocked: bool) -> dict[str, Any]:
    started = [e for e in events if e.get("kind") == "story_start"]
    index = _stage_index(st)
    if index >= STAGES.index("intake"):
        status = "passed"
    elif blocked and started:
        status = "failed"
    elif started:
        status = "running"
    else:
        status = "pending"
    crossed = [e for e in events if e.get("kind") == "stage" and e.get("stage") == "intake"]
    box = _code_job("intake", "Intake", status, crossed or started)
    box["attempts"] = 1 if status != "pending" else 0
    return _plan_node("intake", "Intake", status, [box])


def _plan_node(id: str, label: str, status: str, jobs: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    return {
        "id": id,
        "label": label,
        "kind": "stage",
        "lane": "plan",
        "status": status,
        "jobs": jobs,
        **fields,
    }


def _base_node(st: dict[str, Any], events: list[dict[str, Any]], blocked: bool) -> dict[str, Any]:
    base = st.get("base") if isinstance(st.get("base"), dict) else None
    intake_done = _stage_index(st) >= STAGES.index("intake")
    if base:
        status = "passed"
    elif blocked and intake_done:
        status = "failed"
    elif intake_done:
        status = "running"
    else:
        status = "pending"
    box = _code_job("base", "Base", status, [])
    if base:
        box["sub"] = f"{base.get('ref', '')} {str(base.get('sha', ''))[:7]}".strip()
        box["card"] = card(
            "Base", [text_row("ref", str(base.get("ref", ""))), text_row("commit", str(base.get("sha", "")))]
        )
    return _plan_node("base", "Base", status, [box])


def _gate_node(st: dict[str, Any], events: list[dict[str, Any]], blocked: bool) -> dict[str, Any]:
    gate = st.get("gate") if isinstance(st.get("gate"), dict) else None
    logged = [e for e in events if e.get("kind") == "gate"]
    if gate or logged:
        status = "passed"
    elif blocked and st.get("stage") == "intake":
        status = "failed"
    else:
        status = "pending"
    box = _code_job("gate", "Gate", status, logged)
    commands = list((gate or {}).get("commands") or [])
    commit = (gate or {}).get("agents_md") or (logged[-1].get("agents_md") if logged else None)
    rows = [step_row("passed", str(c)) for c in commands]
    if commit:
        rows.append(text_row("AGENTS.md", str(commit)))
    if rows:
        box["card"] = card("Gate", rows)
    return _plan_node("gate", "Gate", status, [box])


def _decorated(node: dict[str, Any], **fields: Any) -> dict[str, Any]:
    node.update(lane="plan", **fields)
    return node


def _survey_node(
    st: dict[str, Any], events: list[dict[str, Any]], state_dir: Path, blocked: bool
) -> dict[str, Any]:
    node = stage_node("survey", "Survey", st, events, state_dir, blocked)
    for box in node["jobs"]:
        _shaped(box, "model", card=_run_card("Survey", box, [], ()))
    return _decorated(node, group="same-time")


def _refine_node(
    st: dict[str, Any], events: list[dict[str, Any]], state_dir: Path, blocked: bool
) -> dict[str, Any]:
    node = stage_node("refine", "Refine", st, events, state_dir, blocked)
    for box in node["jobs"]:
        _shaped(box, "model", card=_run_card("Refine", box, [], ()))
    return _decorated(node, group="same-time")


def _layers_node(
    st: dict[str, Any], events: list[dict[str, Any]], state_dir: Path, blocked: bool
) -> dict[str, Any]:
    node = stage_node("layers", "Plan layers", st, events, state_dir, blocked)
    rounds = [
        "passed" if e["kind"] == "plan_round" else "failed"
        for e in events
        if e.get("kind") in ("plan_round", "plan_round_unusable") and e.get("stage") == "layers"
    ]
    fixes = ["warn" for e in events if e.get("kind") in ("retry", "plan_fix") and e.get("stage") == "layers"]
    done = next((e for e in reversed(events) if e.get("kind") == "plan_done"), None)
    over = any(e.get("kind") == "budget_exceeded" and e.get("stage") == "plan" for e in events)
    tags = [tag("over budget", "warn")] if over else []
    rows: list[dict[str, Any]] = []
    if rounds:
        rows.append(squares("rounds", rounds))
    if fixes:
        rows.append(squares("fixes", fixes))
    if done is not None:
        rows.append(text_row("result", f"{done.get('reason')} after {done.get('rounds')} round(s)"))
    for box in node["jobs"]:
        _shaped(box, "model", tags=tags, card=_run_card("Plan layers", box, rows, tags))
    return _decorated(node)


def _next_in_round(turns: list[dict[str, Any]]) -> str:
    """The role whose turn is out: the one after the last turn's role in the round (`product` before any)."""
    if not turns:
        return MEETING_ROUND[0]
    last = turns[-1].get("role")
    if last not in MEETING_ROUND:
        return MEETING_ROUND[0]
    return MEETING_ROUND[(MEETING_ROUND.index(last) + 1) % len(MEETING_ROUND)]


def _meeting_card(
    turns: list[dict[str, Any]],
    events: list[dict[str, Any]],
    skill: dict[str, Any] | None,
    ended: dict[str, Any] | None,
    box: dict[str, Any],
) -> dict[str, Any]:
    """The meeting's card: its turns, each voter's last verdict, the objections and how it ended, and the
    skill pick that chose it."""
    rows: list[dict[str, Any]] = [
        squares("turns", ["code-passed" if t.get("role") == "airlie" else "passed" for t in turns])
    ]
    last: dict[str, dict[str, Any]] = {}
    for t in turns:
        if t.get("role") in VOTERS and isinstance(t.get("verdict"), dict):
            last[str(t["role"])] = t["verdict"]
    if last:
        rows.append(
            votes(
                [
                    {
                        "role": r,
                        "requirements": last[r].get("requirements"),
                        "changes": last[r].get("changes"),
                        "proof": last[r].get("proof"),
                    }
                    for r in VOTERS
                    if r in last
                ]
            )
        )
    rows.append(text_row("objections", f"{box['objections']} raised, {box['resolved']} resolved"))
    if ended is not None:
        if ended["kind"] == "amigos_agreed":
            rows.append(text_row("ended", f"agreed after {ended.get('turns')} turn(s)"))
        else:
            parts = ", ".join(str(p) for p in ended.get("parts") or [])
            rows.append(text_row("ended", f"owners decided {parts} after {ended.get('turns')} turn(s)"))
    rows.append(text_row("skill", str((skill or {}).get("chosen") or "none")))
    tags = [tag("owner decided", "warn")] if box["owner_decided"] else []
    took = box.get("duration_s")
    return card("Three amigos", rows, tags=tags, foot=f"{took}s" if took is not None else None)


def _meeting_job(events: list[dict[str, Any]], blocked: bool) -> dict[str, Any] | None:
    """The story meeting (P2, 2026-10-24) as a job, from the story's `amigos_*` events: the turns taken and
    what they cost, the meeting's own objections (a package critic's, which carry `source: package`, belong to
    a layer), and how it ended: agreed, or run out of turns with the parts their owners decided and the
    objections left open. It runs from `amigos_start`, or, in a story logged before that, from the skill pick
    that chose it. None for a story that held no meeting."""
    turns = [e for e in events if e.get("kind") == "amigos_turn"]
    agreed = [e for e in events if e.get("kind") == "amigos_agreed"]
    decided = [e for e in events if e.get("kind") == "amigos_owner_decided"]
    started = next((e for e in events if e.get("kind") == "amigos_start"), None)
    skill = next(
        (e for e in reversed(events) if e.get("kind") == "skill" and e.get("subtask") == "amigos"), None
    )
    if started is None and skill is None and not (turns or agreed or decided):
        return None
    ended = (agreed or decided)[-1] if agreed or decided else None
    status = "passed" if ended is not None else "failed" if blocked else "running"
    begin = (started or skill or (turns[0] if turns else {})).get("ts")
    end = ended.get("ts") if ended is not None else None
    prompt = sum(int(e.get("prompt_tokens") or 0) for e in turns)
    completion = sum(int(e.get("completion_tokens") or 0) for e in turns)
    box = job(
        "meeting",
        "Three amigos",
        STAGE_ROLES["meeting"],
        status,
        started=begin,
        ended=end,
        duration_s=_span(begin, end),
        tokens={"steps": len(turns), "prompt": prompt, "completion": completion},
        turns=len(turns),
        objections=sum(
            1 for e in events if e.get("kind") == "amigos_objection" and e.get("source") != "package"
        ),
        resolved=sum(1 for e in events if e.get("kind") == "amigos_resolved"),
        unresolved=sum(1 for e in events if e.get("kind") == "amigos_unresolved"),
        owner_decided=list(decided[-1].get("parts") or []) if decided else [],
    )
    tags = [tag("owner decided", "warn")] if box["owner_decided"] else []
    return _shaped(
        box,
        "model",
        live=_next_in_round(turns) if status == "running" else None,
        tags=tags,
        card=_meeting_card(turns, events, skill, ended, box),
    )


def _amigos_node(events: list[dict[str, Any]], blocked: bool) -> dict[str, Any]:
    meeting = _meeting_job(events, blocked)
    return _plan_node(
        "amigos",
        "Three amigos",
        meeting["status"] if meeting else "pending",
        [meeting] if meeting else [],
    )


def _planned_node(st: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    logged = [e for e in events if e.get("kind") == "story_planned"]
    agreed = bool(st.get("agreement")) and _stage_index(st) >= STAGES.index("layers")
    status = "passed" if logged or agreed else "pending"
    box = job("planned", "Planned", None, status)
    ts = logged[-1].get("ts") if logged else None
    box.update(started=ts, ended=ts)
    if isinstance(ts, float | int):
        box["sub"] = time.strftime("%H:%M", time.localtime(ts))
    return _plan_node("planned", "Planned", status, [_shaped(box, "milestone")])


def plan_nodes(state_dir: Path, st: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The nodes before the build, in order: intake, base, gate, survey and refine (at the same time), plan
    layers, the three amigos, PLANNED. A node with no job (no survey, no meeting) stays in the list empty."""
    blocked = bool(st.get("blocked"))
    return [
        _intake_node(st, events, blocked),
        _base_node(st, events, blocked),
        _gate_node(st, events, blocked),
        _survey_node(st, events, state_dir, blocked),
        _refine_node(st, events, state_dir, blocked),
        _layers_node(st, events, state_dir, blocked),
        _amigos_node(events, blocked),
        _planned_node(st, events),
    ]
