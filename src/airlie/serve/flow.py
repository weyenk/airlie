"""A story as a pipeline: the stages left to right, a lane of jobs per layer, each job with its status,
attempts, tokens and speed. Derived from a story's state.json and events.jsonl and the studio runs they name
(their step events and trace), so a story built before this existed shows what it recorded and says what it
did not."""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from airlie.board import BoardStore
from airlie.serve.flow_build import build_nodes
from airlie.serve.flow_now import run_last_ts, settle, story_exit, story_live, story_now
from airlie.serve.flow_parts import StoryNotFound, load, name, safe
from airlie.serve.flow_plan import plan_nodes
from airlie.story.budgets import stage_figures
from airlie.story.costs import queue_hours
from airlie.story.diagnosis import latest
from airlie.story.epic import epic_of
from airlie.story.epic_plan import StoryPlan
from airlie.story.initiative import Initiative
from airlie.story.initiative_state import waiting_on
from airlie.story.model import StoryError

OBSERVATION_CHARS = 2000


def story_flow(state_dir: Path, key: str, *, held: bool = False, now: float | None = None) -> dict[str, Any]:
    """The pipeline of one story: `nodes` left to right, each with its `jobs`; the acceptance criteria and
    the reason it stopped, if it did; what it is doing `now` and how it left (`exit`). `held`: the server
    runs it (running however quiet its log); `now` is the clock, for tests. A box recorded as running that
    nobody works on any more is drawn stopped, or failed when the story is blocked."""
    _, st, events = load(state_dir, key)
    at = time.time() if now is None else now
    card = st["card"] if isinstance(st.get("card"), dict) else {}
    story = st["story"] if isinstance(st.get("story"), dict) else {}
    nodes = plan_nodes(state_dir, st, events) + build_nodes(state_dir, st, events)
    run_ts = run_last_ts(state_dir, nodes)
    live = story_live(events, run_ts, held=held, now=at)
    settle(nodes, live=live, blocked=bool(st.get("blocked")))
    return {
        "key": key,
        "title": card.get("title") or story.get("title"),
        "summary": story.get("summary"),
        "stage": st.get("stage"),
        "blocked": st.get("blocked"),
        "diagnosis": _diagnosis_row(state_dir, key),
        "budgets": stage_figures(state_dir / "stories" / key),
        "acceptance": list(story.get("acceptance") or []),
        "nodes": nodes,
        "now": story_now(nodes, st, events, live=live, run_last_ts=run_ts),
        "exit": story_exit(state_dir, key, st, events, nodes, live=live),
    }


def _diagnosis_row(state_dir: Path, key: str) -> dict[str, Any] | None:
    """The latest block's diagnosis for the row (planning-line P5b, D11): its class, kind and signature, and
    how many blocks the story has had. None for a story that never blocked, or whose diagnosis cannot be
    read."""
    d = latest(state_dir, key)
    if d is None:
        return None
    return {
        "class": d.classified.class_,
        "kind": d.classified.kind,
        "signature": d.classified.signature,
        "n": d.pack.n,
    }


def _story_json(state_dir: Path, key: str | None) -> dict[str, Any]:
    """The state.json of the story `key` as a dict; {} when there is none (it has not started) or it cannot
    be read."""
    if not key or not safe(key):
        return {}
    try:
        data = json.loads((state_dir / "stories" / key / "state.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def epic_flow(state_dir: Path, key: str, statuses: Mapping[str, str] | None = None) -> dict[str, Any]:
    """An epic as the web app draws it (D9 of the plans phase): the plan as the architect wrote it (summary,
    constraints, risks), the epic's criteria, each story of the plan in dependency order with its board key,
    its dependencies, size and epic criteria, where it stands (`status`: the board's, from `statuses`, None
    when not given; `stage` and `blocked`: its own state's, None before it starts), the cards a re-plan left
    out, and the re-plans. StoryNotFound for an epic with no record or a damaged one."""
    f = state_dir / "epics" / name(key, "epic") / "state.json"
    if not f.is_file():
        raise StoryNotFound(f"no epic '{key}'")
    try:
        st = json.loads(f.read_text())
        raw_plan = st.get("plan")
        plan = StoryPlan.from_dict(raw_plan) if isinstance(raw_plan, dict) else None
        card = st["card"] if isinstance(st.get("card"), dict) else {}
        made = {str(k): str(v) for k, v in (st.get("stories") or {}).items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise StoryNotFound(f"epic '{key}': state.json is damaged") from None
    try:
        spec = epic_of(str(card.get("description") or ""))
    except StoryError:
        spec = None
    stories: list[dict[str, Any]] = []
    for ps in plan.order() if plan else ():
        card_key = made.get(ps.id)
        saved = _story_json(state_dir, card_key)
        stories.append(
            {
                "id": ps.id,
                "key": card_key,
                "title": ps.title,
                "size": ps.size,
                "depends_on": list(ps.depends_on),
                "epic_criteria": list(ps.epic_criteria),
                "status": (statuses or {}).get(card_key or ""),
                "stage": saved.get("stage"),
                "blocked": saved.get("blocked"),
            }
        )
    in_plan = {ps.id for ps in plan.stories} if plan else set()
    return {
        "key": key,
        "title": card.get("title"),
        "repo": st.get("repo"),
        "stage": st.get("stage"),
        "blocked": st.get("blocked"),
        "summary": plan.summary if plan else None,
        "constraints": list(plan.constraints) if plan else [],
        "risks": [{"risk": r.risk, "pinned": r.pinned} for r in plan.risks] if plan else [],
        "criteria": [{"id": c.id, "text": c.text} for c in spec.criteria] if spec else [],
        "stories": stories,
        "left_out": [k for sid, k in made.items() if sid not in in_plan],
        "replans": [r for r in st.get("replans") or () if isinstance(r, dict)],
    }


def initiative_epic_row(store: BoardStore, entry: Mapping[str, Any]) -> dict[str, Any]:
    """One epic of an initiative as the app draws it: its draft's id, kind and title, and, once it is a card,
    its key and board status with the price, the verdict, the mockup pages and the pick, read from the card's
    own block (what a person may have changed since). An epic not yet carded has `key` None and no price."""
    key = entry.get("key")
    row: dict[str, Any] = {
        "key": key,
        "id": entry.get("id"),
        "kind": entry.get("kind"),
        "title": entry.get("title"),
        "status": None,
        "hours": None,
        "basis": None,
        "revised": None,
        "actual": None,
        "verdict": (entry.get("viability") or {}).get("verdict"),
        "options": [],
        "picked": None,
        "depends_on": [],
    }
    if not isinstance(key, str):
        return row
    try:
        item = store.get(key)
    except KeyError:
        return row
    row["status"], row["title"] = item.status, item.title
    try:
        spec = epic_of(item.description)
    except StoryError:
        spec = None
    if spec is not None:
        row["depends_on"] = list(spec.depends_on)
        if spec.estimate is not None:
            est = spec.estimate
            row.update(hours=est.hours, basis=est.basis, revised=est.revised, actual=est.actual)
        if spec.viability is not None:
            row["verdict"] = spec.viability.verdict
        if spec.mockups is not None:
            row["options"], row["picked"] = list(spec.mockups.options), spec.mockups.picked
    return row


def initiative_flow(state_dir: Path, store: BoardStore, key: str) -> dict[str, Any]:
    """An initiative as the web app draws it (D9 of the specs phase): the dialogue with the person, product's
    assumptions, every consultation, the epics (draft, card, price, verdict, mockup pages, pick) and the
    hours already queued, with what the initiative waits on (`waiting_on`: the person or the line) and the
    pending question. The epics' statuses and prices are the board's, read now. StoryNotFound for an
    initiative with no record or a damaged one."""
    f = state_dir / "initiatives" / name(key, "initiative") / "state.json"
    if not f.is_file():
        raise StoryNotFound(f"no initiative '{key}'")
    try:
        st = json.loads(f.read_text())
        qa = [q for q in st.get("qa") or () if isinstance(q, dict)]
        stage = str(st["stage"])
        epics = [e for e in st.get("epics") or () if isinstance(e, dict)]
        consultations = [c for c in st.get("consultations") or () if isinstance(c, dict)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise StoryNotFound(f"initiative '{key}': state.json is damaged") from None
    pending = qa[-1] if qa and qa[-1].get("a") is None else None
    budget = None
    with contextlib.suppress(KeyError, StoryError):
        budget = Initiative.from_item(store.get(key)).budget_hours
    card = st["card"] if isinstance(st.get("card"), dict) else {}
    return {
        "key": key,
        "title": card.get("title"),
        "stage": stage,
        "blocked": st.get("blocked"),
        "waiting_on": waiting_on(stage, pending is not None),
        "question": pending.get("q") if pending else None,
        "budget_hours": budget,
        "summary": st.get("summary") or "",
        "qa": [{k: q.get(k) for k in ("q", "a", "asked_at", "answered_at")} for q in qa],
        "assumptions": [str(a) for a in st.get("assumptions") or ()],
        "consultations": [
            {k: c.get(k) for k in ("seat", "question", "answer", "seconds", "round")} for c in consultations
        ],
        "epics": [initiative_epic_row(store, e) for e in epics],
        "queue_hours": queue_hours(store, state_dir),
    }


def _steps(trace: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    folders = [(0, trace)] + sorted(
        (int(p.name.split("-")[1]), p) for p in trace.glob("round-*") if p.name.split("-")[1].isdigit()
    )
    for attempt, folder in folders:
        for f in sorted(folder.glob("step-*.json")):
            try:
                d = json.loads(f.read_text())
            except (ValueError, OSError):
                continue
            call = d.get("u_t") or {}
            out.append(
                {
                    "attempt": attempt,
                    "n": int(f.stem.split("-")[1]),
                    "tool": call.get("name"),
                    "arguments": call.get("arguments"),
                    "ok": d.get("ok"),
                    "observation": str(d.get("o_t") or "")[:OBSERVATION_CHARS],
                    "assistant_text": d.get("assistant_text"),
                    "reasoning": d.get("reasoning"),
                }
            )
    return out


def trace_of(state_dir: Path, run: str, team: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """(prompt, steps): what subtask `team` of run `run` was opened with (its prompt.json; None when that was
    not recorded or is not an object) and the steps it took in every attempt, from runs/<run>/trace/<team>/.
    (None, []) when either name is not one path component: both come from event logs and URLs."""
    if not (safe(run) and safe(team)):
        return None, []
    trace = state_dir / "runs" / run / "trace" / team
    prompt: Any = None
    if (trace / "prompt.json").is_file():
        try:
            prompt = json.loads((trace / "prompt.json").read_text())
        except (ValueError, OSError):
            prompt = None
    steps = _steps(trace) if trace.is_dir() else []
    return (prompt if isinstance(prompt, dict) else None), steps


def final_text(steps: list[dict[str, Any]]) -> str:
    """The last thing a job said in its steps (`steps` as trace_of reads them); "" when it said nothing."""
    return next((s["assistant_text"] for s in reversed(steps) if s["assistant_text"]), "")


_CYCLE_PREFIX = re.compile(r"c\d+-")


def _find_job(
    flow: dict[str, Any], node_id: str, job_id: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(node, job) for `job_id` of `node_id`: a node's own jobs, a layer's cycle rows (an earlier cycle's ids
    are `c{n}-{job}`) or the exit row (node `exit`). None when there is none."""
    node: dict[str, Any] | None
    if node_id == "exit":
        node = {"id": "exit", "kind": "exit", "jobs": (flow["exit"] or {}).get("jobs", [])}
    else:
        node = next((n for n in flow["nodes"] if n["id"] == node_id), None)
    if node is None:
        return None
    rows = [j for r in node.get("rows") or () if r.get("kind") == "cycle" for j in r["jobs"]]
    found = next((j for j in [*node.get("jobs", []), *rows] if j["id"] == job_id), None)
    return (node, found) if found is not None else None


def job_detail(
    state_dir: Path, key: str, node_id: str, job_id: str, *, held: bool = False, now: float | None = None
) -> dict[str, Any]:
    """One job of the flow: its summary, what it was given, what it produced and the steps it took."""
    _, st, _ = load(state_dir, key)
    flow = story_flow(state_dir, key, held=held, now=now)
    hit = _find_job(flow, node_id, job_id)
    if hit is None:
        raise StoryNotFound(f"no job '{job_id}' in '{node_id}' of {key}")
    node, job = hit
    plain = _CYCLE_PREFIX.sub("", job_id)
    team = plain if node["kind"] in ("layer", "exit") else node_id
    run = job.get("studio_run")
    prompt, steps = trace_of(state_dir, run, team) if isinstance(run, str) else (None, [])
    note = None
    if prompt is None:
        note = "airlie's own check, no model" if job.get("shape") == "code" else "opening prompt not recorded"
    output: dict[str, Any] = {"final_text": final_text(steps)}
    if node["kind"] == "layer":
        i = int(node_id.split("-")[1]) - 1
        layers = st.get("layers") or []
        if 0 <= i < len(layers):
            output["layer"] = layers[i]
        built = (st.get("built") or {}).get(node_id.split("-")[1])
        if plain == "build" and built:
            output["built"] = built
    elif node_id == "exit" and job_id == "diagnose":
        d = latest(state_dir, key)
        output["diagnosis"] = d.to_dict() if d else None
    elif node_id == "refine":
        output["story"] = st.get("story")
    elif node_id == "layers":
        output["layers"] = st.get("layers")
    elif node_id == "amigos":
        output["layers"] = st.get("layers")
        output["agreement"] = st.get("agreement")
        note = "a meeting of product, the architect and the tester; its turns are in the story's events"
    elif node_id == "survey":
        output["survey"] = st.get("survey")
    return {
        "node": node_id,
        "job": job,
        "input": prompt,
        "input_note": note,
        "output": output,
        "steps": steps,
    }
