"""A story as a pipeline: the stages left to right, a lane of jobs per layer, each job with its status,
attempts, tokens and speed. Derived from a story's state.json and events.jsonl and the studio runs they name
(their step events and trace), so a story built before this existed shows what it recorded and says what it
did not."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from airlie.serve.jobs import read_events
from airlie.story.state import STAGES

STAGE_ROLES = {"refine": "product", "survey": "architect", "layers": "architect"}
"""The role a stage job runs as, when its steps do not say (a run that has not stepped yet)."""

CHECK_KINDS = frozenset(
    {
        "test_checks_passed",
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
"""What the story's events say about the pre-code check on a layer's tests."""

BUILD_KINDS = frozenset({"fix_round", "gate_failed", "write_deadline"})
"""What a layer's studio run says about its coder having to be sent back."""

OBSERVATION_CHARS = 2000
_SAFE = re.compile(r"[A-Za-z0-9._-]+")


class StoryNotFound(LookupError):
    """No story of that key, or no such node or job in it."""


def _name(value: str, what: str) -> str:
    """`value` as one path component, or StoryNotFound: it names a directory and must not leave its parent."""
    if not _SAFE.fullmatch(value) or value in (".", ".."):
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
    if not run or not _SAFE.fullmatch(run):
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
    return {
        "id": name,
        "label": label,
        "kind": "stage",
        "status": status,
        "jobs": [job] if status != "skipped" else [],
    }


def _subtask(run_events: list[dict[str, Any]], team: str, kind: str) -> dict[str, Any] | None:
    found = [e for e in run_events if e.get("kind") == kind and e.get("subtask") == team]
    return found[-1] if found else None


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

    t_start, t_done = (
        _subtask(run_events, "tests", "subtask_start"),
        _subtask(run_events, "tests", "subtask_done"),
    )
    tests_status = _status_of(t_start, t_done)
    if run_events and t_start is None and _subtask(run_events, "build", "subtask_start") is not None:
        tests_status = "skipped"  # a run with no tester (before story.tester_role): the coder wrote its own
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

    checks = [e for e in mine if e.get("kind") in CHECK_KINDS]
    attempts = max((int(e["attempt"]) for e in checks if isinstance(e.get("attempt"), int)), default=0)
    passed = any(e["kind"] == "test_checks_passed" for e in checks)
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
                {"kind": e["kind"], "detail": _detail(e)} for e in checks if e["kind"] != "test_checks_passed"
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
    trace = state_dir / "runs" / run / "trace" / team if run and _SAFE.fullmatch(run) else None
    prompt = None
    if trace is not None and (trace / "prompt.json").is_file():
        try:
            prompt = json.loads((trace / "prompt.json").read_text())
        except (ValueError, OSError):
            prompt = None
    note = None
    if prompt is None:
        note = "airlie's own check, no model" if job_id == "checks" else "opening prompt not recorded"
    steps = _steps(trace) if trace is not None and trace.is_dir() else []
    output: dict[str, Any] = {
        "final_text": next((s["assistant_text"] for s in reversed(steps) if s["assistant_text"]), "")
    }
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
