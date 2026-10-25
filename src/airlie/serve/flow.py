"""A story as a pipeline: the stages left to right, a lane of jobs per layer, each job with its status,
attempts, tokens and speed. Derived from a story's state.json and events.jsonl and the studio runs they name
(their step events and trace), so a story built before this existed shows what it recorded and says what it
did not."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from airlie.board import BoardStore
from airlie.serve.jobs import read_events
from airlie.story.costs import queue_hours
from airlie.story.epic import epic_of
from airlie.story.epic_plan import StoryPlan
from airlie.story.initiative import Initiative
from airlie.story.initiative_state import waiting_on
from airlie.story.model import StoryError
from airlie.story.state import STAGES

STAGE_ROLES = {
    "refine": "product",
    "survey": "architect",
    "layers": "architect",
    "package": "architect",
    "meeting": "amigos",
}
"""The role a stage job runs as, when its steps do not say (a run that has not stepped yet)."""

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
        "assertion_free_test",
        "unparseable_test",
        "existing_test_changed",
        "suite_did_not_run",
        "test_checks_retry",
        "test_checks_skipped",
        "tester_lint_fixed",
        "tester_lint_round",
        "lint_fails",
        "no_new_test",
        "passes_before_code",
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
_SAFE = re.compile(r"[A-Za-z0-9._-]+")


class StoryNotFound(LookupError):
    """No story of that key, or no such node or job in it."""


def _safe(value: str) -> bool:
    """Whether `value` is one path component that stays inside its parent directory."""
    return _SAFE.fullmatch(value) is not None and value not in (".", "..")


def _name(value: str, what: str) -> str:
    """`value` as one path component, or StoryNotFound: it names a directory and must not leave its parent."""
    if not _safe(value):
        raise StoryNotFound(f"no {what} '{value}'")
    return value


def _load(state_dir: Path, key: str) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    d = state_dir / "stories" / _name(key, "story")
    f = d / "state.json"
    if not f.is_file():
        raise StoryNotFound(f"no story '{key}'")
    try:
        st = json.loads(f.read_text())
    except (ValueError, OSError) as e:
        raise StoryNotFound(f"story '{key}' has a damaged state.json: {e}") from e
    if not isinstance(st, dict):
        raise StoryNotFound(f"story '{key}' has a damaged state.json")
    events, _ = read_events(d / "events.jsonl", 0)
    return d, st, events


def _run_events(state_dir: Path, run: str | None) -> list[dict[str, Any]]:
    if not run or not _safe(run):
        return []
    return read_events(state_dir / "runs" / run / "events.jsonl", 0)[0]


def _tokens(events: list[dict[str, Any]], team: str) -> dict[str, Any] | None:
    """What the steps of one subtask used, and the speed they generated at (completion tokens per second of
    generation); None when it took no step."""
    steps = [e for e in events if e.get("kind") == "step" and e.get("team_id") == team]
    if not steps:
        return None
    prompt = sum(int(e.get("prompt_tokens") or 0) for e in steps)
    completion = sum(int(e.get("completion_tokens") or 0) for e in steps)
    gen_s = sum(float(e.get("gen_ms") or 0) for e in steps) / 1000
    prefill_s = sum(float(e.get("prefill_ms") or 0) for e in steps) / 1000
    tool_s = sum(float(e.get("tool_ms") or 0) for e in steps) / 1000
    return {
        "steps": len(steps),
        "prompt": prompt,
        "completion": completion,
        "gen_s": round(gen_s, 1),
        "prefill_s": round(prefill_s, 1),
        "tool_s": round(tool_s, 1),
        "tps": round(completion / gen_s, 1) if gen_s else None,
        "effective_tps": round(completion / (gen_s + prefill_s), 1) if gen_s + prefill_s else None,
        "prefill_tps": round(prompt / prefill_s, 1) if prefill_s else None,
        "role": next((e["role"] for e in steps if e.get("role")), None),
        "model": next((e["model"] for e in steps if e.get("model")), None),
    }


def _detail(e: dict[str, Any]) -> str:
    for k in ("error", "why", "reason"):
        if e.get(k):
            return str(e[k])
    if "findings" in e:
        return f"{e['findings']} finding(s)"
    if "attempt" in e:
        return f"attempt {e['attempt']}"
    return ""


def _job(
    id: str,
    label: str,
    role: str | None,
    status: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "id": id,
        "label": label,
        "role": role,
        "status": status,
        "attempts": 1 if status != "pending" else 0,
        "fix_rounds": 0,
        "retries": [],
        "studio_run": None,
        "started": None,
        "ended": None,
        "duration_s": None,
        "tokens": None,
        **fields,
    }


def _stage_node(
    name: str,
    label: str,
    st: dict[str, Any],
    events: list[dict[str, Any]],
    state_dir: Path,
    blocked: bool,
) -> dict[str, Any]:
    ran = [e for e in events if e.get("kind") == "run" and e.get("stage") == name]
    finished = [e for e in events if e.get("kind") == "stage" and e.get("stage") == name]
    retries = [e for e in events if e.get("kind") in ("retry", "plan_fix") and e.get("stage") == name]
    done = STAGES.index(st.get("stage", "new")) >= STAGES.index(name) if name in STAGES else False
    if name == "survey":
        done = any(e.get("kind") == "survey_done" for e in events)
        failed = any(e.get("kind") == "survey_failed" for e in events)
        status = "failed" if failed else "passed" if done else "running" if ran and not blocked else "skipped"
    elif done:
        status = "passed"
    elif blocked and STAGES.index(st.get("stage", "new")) + 1 == STAGES.index(name):
        status = "failed"
    else:
        status = "pending"
    if status == "pending" and name != "layers" and ran and not blocked:
        status = "running"
    job = _job(name, label, STAGE_ROLES.get(name), "pending" if status == "skipped" else status)
    if ran:
        run = ran[-1]["studio_run"]
        run_events = _run_events(state_dir, run)
        tokens = _tokens(run_events, name)
        start = ran[0].get("ts")
        end = finished[-1].get("ts") if finished and done else None
        job.update(
            studio_run=run,
            attempts=len(retries) + 1,
            retries=[{"kind": e["kind"], "detail": _detail(e)} for e in retries],
            tokens=tokens,
            role=(tokens or {}).get("role") or job["role"],
            started=start,
            ended=end,
            duration_s=round(end - start, 1)
            if isinstance(start, float | int) and isinstance(end, float | int)
            else None,
        )
    elif name == "intake" and status == "passed":
        job["attempts"] = 1
    jobs = [job] if status != "skipped" else []
    if name == "layers":
        meeting = _meeting_job(events, blocked)
        if meeting is not None:
            jobs.append(meeting)
            if status == "pending" and not blocked:
                status = "running"  # the layers are split and the three are agreeing them
    return {
        "id": name,
        "label": label,
        "kind": "stage",
        "status": status,
        "jobs": jobs,
    }


def _meeting_job(events: list[dict[str, Any]], blocked: bool) -> dict[str, Any] | None:
    """The story meeting (P2, 2026-10-24) as a job of the `layers` stage, from the story's `amigos_*` events:
    the turns taken and what they cost, the meeting's own objections (a package critic's, which carry
    `source: package`, belong to a layer), and how it ended: agreed, or run out of turns with the parts
    their owners decided and the objections left open. None for a story that held no meeting."""
    turns = [e for e in events if e.get("kind") == "amigos_turn"]
    agreed = [e for e in events if e.get("kind") == "amigos_agreed"]
    decided = [e for e in events if e.get("kind") == "amigos_owner_decided"]
    if not turns and not agreed and not decided:
        return None
    ended = (agreed or decided)[-1] if agreed or decided else None
    status = "passed" if ended is not None else "failed" if blocked else "running"
    start = turns[0].get("ts") if turns else None
    end = ended.get("ts") if ended is not None else None
    prompt = sum(int(e.get("prompt_tokens") or 0) for e in turns)
    completion = sum(int(e.get("completion_tokens") or 0) for e in turns)
    return _job(
        "meeting",
        "Three amigos",
        STAGE_ROLES["meeting"],
        status,
        started=start,
        ended=end,
        duration_s=round(end - start, 1)
        if isinstance(start, float | int) and isinstance(end, float | int)
        else None,
        tokens={"steps": len(turns), "prompt": prompt, "completion": completion},
        turns=len(turns),
        objections=sum(
            1 for e in events if e.get("kind") == "amigos_objection" and e.get("source") != "package"
        ),
        resolved=sum(1 for e in events if e.get("kind") == "amigos_resolved"),
        unresolved=sum(1 for e in events if e.get("kind") == "amigos_unresolved"),
        owner_decided=list(decided[-1].get("parts") or []) if decided else [],
    )


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
    tokens = _tokens(_run_events(state_dir, run), "package")
    return _job(
        "package",
        "Package",
        (tokens or {}).get("role") or STAGE_ROLES["package"],
        status,
        attempts=len(starts),
        studio_run=run,
        tokens=tokens,
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
    run_events = _run_events(state_dir, run)
    jobs: list[dict[str, Any]] = []

    package_events = [
        e for e in mine if e.get("kind") in ("package_start", "package_returned", "package_ready")
    ]
    legacy = (
        not package_events and bool(run_events) and any(e.get("kind") == "subtask_start" for e in run_events)
    )  # a layer run before P1: the tester's `tests` subtask, or none (the coder wrote its own tests)
    if legacy:
        t_start, t_done = (
            _subtask(run_events, "tests", "subtask_start"),
            _subtask(run_events, "tests", "subtask_done"),
        )
        tests_status = _status_of(t_start, t_done)
        if t_start is None and _subtask(run_events, "build", "subtask_start") is not None:
            tests_status = (
                "skipped"  # a run with no tester (before story.tester_role): the coder wrote its own
            )
        jobs.append(
            _job(
                "tests",
                "Tests",
                "tester",
                tests_status,
                studio_run=run,
                tokens=_tokens(run_events, "tests"),
                **_timing(t_start, t_done),
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
        _job(
            "checks",
            "Test checks",
            None,
            checks_status,
            attempts=attempts or (1 if passed else 0),
            retries=[
                {"kind": e["kind"], "detail": _detail(e)} for e in checks if e["kind"] not in CHECK_DONE
            ],
        )
    )

    b_start, b_done = (
        _subtask(run_events, "build", "subtask_start"),
        _subtask(run_events, "build", "subtask_done"),
    )
    build_status = "passed" if built else _status_of(b_start, b_done)
    jobs.append(
        _job(
            "build",
            "Build",
            "coder",
            build_status,
            studio_run=run,
            tokens=_tokens(run_events, "build"),
            fix_rounds=int((b_done or {}).get("fix_rounds") or 0),
            retries=[
                {"kind": e["kind"], "detail": _detail(e)} for e in run_events if e.get("kind") in BUILD_KINDS
            ],
            **_timing(b_start, b_done),
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


def _status_of(start: dict[str, Any] | None, done: dict[str, Any] | None) -> str:
    if done is not None:
        return "passed" if done.get("ok") else "failed"
    return "running" if start is not None else "pending"


def _timing(start: dict[str, Any] | None, done: dict[str, Any] | None) -> dict[str, Any]:
    a, b = (start or {}).get("ts"), (done or {}).get("ts")
    took = round(b - a, 1) if isinstance(a, float | int) and isinstance(b, float | int) else None
    return {"started": a, "ended": b, "duration_s": took}


def story_flow(state_dir: Path, key: str) -> dict[str, Any]:
    """The pipeline of one story: `nodes` left to right, each with its `jobs`; the acceptance criteria and
    the reason it stopped, if it did."""
    _, st, events = _load(state_dir, key)
    blocked = bool(st.get("blocked"))
    card = st["card"] if isinstance(st.get("card"), dict) else {}
    story = st["story"] if isinstance(st.get("story"), dict) else {}
    layers = [x for x in st.get("layers") or [] if isinstance(x, dict)]
    built = st.get("built") or {}
    first_open = next((i for i in range(len(layers)) if f"{i + 1:02d}" not in built), None)
    nodes = [
        _stage_node("intake", "Intake", st, events, state_dir, blocked),
        _stage_node("refine", "Refine", st, events, state_dir, blocked),
        _stage_node("survey", "Survey", st, events, state_dir, blocked),
        _stage_node("layers", "Plan layers", st, events, state_dir, blocked),
    ]
    nodes += [_layer_node(i, x, st, events, state_dir, first_open, blocked) for i, x in enumerate(layers)]
    nodes += [
        _stage_node("publish", "Publish", st, events, state_dir, blocked),
        _stage_node("review", "Review", st, events, state_dir, blocked),
    ]
    return {
        "key": key,
        "title": card.get("title") or story.get("title"),
        "summary": story.get("summary"),
        "stage": st.get("stage"),
        "blocked": st.get("blocked"),
        "acceptance": list(story.get("acceptance") or []),
        "nodes": nodes,
    }


def _story_json(state_dir: Path, key: str | None) -> dict[str, Any]:
    """The state.json of the story `key` as a dict; {} when there is none (it has not started) or it cannot
    be read."""
    if not key or not _safe(key):
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
    f = state_dir / "epics" / _name(key, "epic") / "state.json"
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
    f = state_dir / "initiatives" / _name(key, "initiative") / "state.json"
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
    if not (_safe(run) and _safe(team)):
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
    _, st, _ = _load(state_dir, key)
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
        if job_id == "meeting":
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
