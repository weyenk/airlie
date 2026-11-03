"""A story as a pipeline: the stages left to right, a lane of jobs per layer, each job with its status,
attempts, tokens and speed. Derived from a story's state.json and events.jsonl and the studio runs they name
(their step events and trace), so a story built before this existed shows what it recorded and says what it
did not."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from airlie.board import BoardStore
from airlie.serve.flow_parts import (
    STAGE_ROLES,
    StoryNotFound,
    detail,
    job,
    load,
    name,
    run_events,
    safe,
    status_of,
    timing,
    tokens,
)
from airlie.serve.flow_plan import plan_nodes, stage_node
from airlie.story.budgets import stage_figures
from airlie.story.costs import queue_hours
from airlie.story.diagnosis import latest
from airlie.story.epic import epic_of
from airlie.story.epic_plan import StoryPlan
from airlie.story.initiative import Initiative
from airlie.story.initiative_state import waiting_on
from airlie.story.model import StoryError

CHECK_KINDS = frozenset(
    {
        "test_checks_passed",
        "package_returned",
        "package_ready",
        "package_placed",
        "file_exists",
        "file_missing",
        "covers_missing",
        "import_unresolved",
        "unknown_method",
        "test_too_large",
        "assertion_free_test",
        "unparseable_test",
        "test_type_error",
        "existing_test_changed",
        "suite_did_not_run",
        "test_checks_retry",
        "test_checks_skipped",
        "tester_lint_fixed",
        "tester_lint_round",
        "lint_fails",
        "no_new_test",
        "passes_before_code",
        "test_not_run",
        "existing_test_fails",
        "existing_tests_fail_at_base",
        "locked_tests_fail",
        "tests_not_mended",
        "tests_mend_undone",
        "tests_mended",
    }
)
"""What the story's events say about the readiness check on a layer's package (and, in a story recorded
before P1, the pre-code check on the tester's tests)."""

CHECK_DONE = frozenset({"test_checks_passed", "package_ready", "package_placed"})
"""The check events that are no retry: the outcome."""

BUILD_KINDS = frozenset({"fix_round", "gate_failed", "write_deadline"})
"""What a layer's studio run says about its coder having to be sent back."""

OBSERVATION_CHARS = 2000


def _subtask(run_events: list[dict[str, Any]], team: str, kind: str) -> dict[str, Any] | None:
    found = [e for e in run_events if e.get("kind") == kind and e.get("subtask") == team]
    return found[-1] if found else None


def _package_job(events: list[dict[str, Any]], built: bool, failed: bool, state_dir: Path) -> dict[str, Any]:
    """The architect's package for a layer: one attempt per `package_start`, passed on `package_ready`, failed
    when every attempt came back and the layer stopped, running while one is out; its tokens are the last
    attempt's ask (the studio run its `package_ready` or `package_returned` names)."""
    starts = [e for e in events if e["kind"] == "package_start"]
    ready = [e for e in events if e["kind"] == "package_ready"]
    returned = [e for e in events if e["kind"] == "package_returned"]
    ended = (ready or returned)[-1] if ready or returned else None
    run = ended.get("studio_run") if ended else None
    if ready or built:
        status = "passed"
    elif not starts:
        status = "pending"
    elif len(returned) >= len(starts) and failed:
        status = "failed"
    else:
        status = "running"
    first = starts[0].get("ts") if starts else None
    last = ready[-1].get("ts") if ready else None
    took = (
        round(last - first, 1) if isinstance(first, float | int) and isinstance(last, float | int) else None
    )
    used = tokens(run_events(state_dir, run), "package")
    return job(
        "package",
        "Package",
        (used or {}).get("role") or STAGE_ROLES["package"],
        status,
        attempts=len(starts),
        studio_run=run,
        tokens=used,
        started=first,
        ended=last,
        duration_s=took,
        retries=[{"kind": e["kind"], "detail": ", ".join(e.get("kinds") or [])} for e in returned],
    )


def _layer_node(
    i: int,
    layer: dict[str, Any],
    st: dict[str, Any],
    events: list[dict[str, Any]],
    state_dir: Path,
    first_open: int | None,
    blocked: bool,
) -> dict[str, Any]:
    nn = f"{i + 1:02d}"
    mine = [e for e in events if e.get("layer") == nn]
    starts = [e for e in mine if e.get("kind") == "layer_start"]
    built = nn in (st.get("built") or {})
    if built:
        status = "passed"
    elif first_open == i:
        status = "failed" if blocked else "running" if starts else "pending"
    else:
        status = "pending"
    run = starts[-1]["studio_run"] if starts else (st.get("built") or {}).get(nn, {}).get("run_id")
    run_log = run_events(state_dir, run)
    jobs: list[dict[str, Any]] = []

    package_events = [
        e for e in mine if e.get("kind") in ("package_start", "package_returned", "package_ready")
    ]
    legacy = (
        not package_events and bool(run_log) and any(e.get("kind") == "subtask_start" for e in run_log)
    )  # a layer run before P1: the tester's `tests` subtask, or none (the coder wrote its own tests)
    if legacy:
        t_start, t_done = (
            _subtask(run_log, "tests", "subtask_start"),
            _subtask(run_log, "tests", "subtask_done"),
        )
        tests_status = status_of(t_start, t_done)
        if t_start is None and _subtask(run_log, "build", "subtask_start") is not None:
            tests_status = (
                "skipped"  # a run with no tester (before story.tester_role): the coder wrote its own
            )
        jobs.append(
            job(
                "tests",
                "Tests",
                "tester",
                tests_status,
                studio_run=run,
                tokens=tokens(run_log, "tests"),
                **timing(t_start, t_done),
            )
        )
    else:
        jobs.append(_package_job(package_events, built, status == "failed", state_dir))

    checks = [e for e in mine if e.get("kind") in CHECK_KINDS]
    attempts = max((int(e["attempt"]) for e in checks if isinstance(e.get("attempt"), int)), default=0)
    passed = any(e["kind"] in ("test_checks_passed", "package_ready") for e in checks)
    checks_status = (
        "passed"
        if passed or built
        else "failed"
        if checks and status == "failed"
        else "running"
        if checks
        else "pending"
    )
    jobs.append(
        job(
            "checks",
            "Test checks",
            None,
            checks_status,
            attempts=attempts or (1 if passed else 0),
            retries=[{"kind": e["kind"], "detail": detail(e)} for e in checks if e["kind"] not in CHECK_DONE],
        )
    )

    b_start, b_done = (
        _subtask(run_log, "build", "subtask_start"),
        _subtask(run_log, "build", "subtask_done"),
    )
    build_status = "passed" if built else status_of(b_start, b_done)
    jobs.append(
        job(
            "build",
            "Build",
            "coder",
            build_status,
            studio_run=run,
            tokens=tokens(run_log, "build"),
            fix_rounds=int((b_done or {}).get("fix_rounds") or 0),
            retries=[
                {"kind": e["kind"], "detail": detail(e)} for e in run_log if e.get("kind") in BUILD_KINDS
            ],
            **timing(b_start, b_done),
        )
    )
    return {
        "id": f"layer-{nn}",
        "label": f"Layer {nn}",
        "kind": "layer",
        "title": layer.get("title"),
        "scope": layer.get("scope"),
        "acceptance": list(layer.get("acceptance") or []),
        "attempts": len(starts),
        "status": status,
        "jobs": jobs,
    }


def story_flow(state_dir: Path, key: str) -> dict[str, Any]:
    """The pipeline of one story: `nodes` left to right, each with its `jobs`; the acceptance criteria and
    the reason it stopped, if it did."""
    _, st, events = load(state_dir, key)
    blocked = bool(st.get("blocked"))
    card = st["card"] if isinstance(st.get("card"), dict) else {}
    story = st["story"] if isinstance(st.get("story"), dict) else {}
    layers = [x for x in st.get("layers") or [] if isinstance(x, dict)]
    built = st.get("built") or {}
    first_open = next((i for i in range(len(layers)) if f"{i + 1:02d}" not in built), None)
    nodes = plan_nodes(state_dir, st, events)
    nodes += [_layer_node(i, x, st, events, state_dir, first_open, blocked) for i, x in enumerate(layers)]
    nodes += [
        stage_node("publish", "Publish", st, events, state_dir, blocked),
        stage_node("review", "Review", st, events, state_dir, blocked),
    ]
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


def job_detail(state_dir: Path, key: str, node_id: str, job_id: str) -> dict[str, Any]:
    """One job of the flow: its summary, what it was given, what it produced and the steps it took."""
    _, st, _ = load(state_dir, key)
    flow = story_flow(state_dir, key)
    node = next((n for n in flow["nodes"] if n["id"] == node_id), None)
    job = next((j for j in (node or {}).get("jobs", []) if j["id"] == job_id), None)
    if node is None or job is None:
        raise StoryNotFound(f"no job '{job_id}' in '{node_id}' of {key}")
    team = job_id if node["kind"] == "layer" else node_id
    run = job.get("studio_run")
    prompt, steps = trace_of(state_dir, run, team) if isinstance(run, str) else (None, [])
    note = None
    if prompt is None:
        note = "airlie's own check, no model" if job_id == "checks" else "opening prompt not recorded"
    output: dict[str, Any] = {"final_text": final_text(steps)}
    if node["kind"] == "layer":
        i = int(node_id.split("-")[1]) - 1
        layers = st.get("layers") or []
        if 0 <= i < len(layers):
            output["layer"] = layers[i]
        built = (st.get("built") or {}).get(node_id.split("-")[1])
        if job_id == "build" and built:
            output["built"] = built
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
