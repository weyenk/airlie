"""What a story is doing right now, and how it left: whether it is live (a killed process draws as stopped,
not running), the now bar, and the exit row at the end of the flow (blocked, waiting, cancelled)."""

from __future__ import annotations

import time
from collections.abc import Collection
from pathlib import Path
from typing import Any

from airlie.serve.flow_parts import card, job, run_events, shaped, text_row
from airlie.serve.running import ACTIVE_S
from airlie.story.diagnosis import latest

STOPS = ("story_done", "blocked", "waiting", "cancelled")
"""The events that end a story's turn: after one, a quiet log is a story at rest, not one that died."""

LANES = {"plan": "Plan lane", "build": "Build lane"}


def _ts(e: dict[str, Any]) -> float | None:
    v = e.get("ts")
    return float(v) if isinstance(v, float | int) else None


def last_ts(events: list[dict[str, Any]]) -> float | None:
    """The newest `ts` any of `events` carries; None when none does."""
    return max((t for e in events if (t := _ts(e)) is not None), default=None)


def story_live(events: list[dict[str, Any]], run_last_ts: float | None, *, held: bool, now: float) -> bool:
    """Whether the story is being worked on now. A job this server holds is live however quiet. Otherwise
    it is live unless its log ends on a stop, and only while something (its log, or the studio run its
    deepest running job names) wrote within ACTIVE_S: a process killed mid-box leaves no stop event."""
    if held:
        return True
    if events and events[-1].get("kind") in STOPS:
        return False
    newest = last_ts(events)
    stamps = [t for t in (newest, run_last_ts) if t is not None]
    return bool(stamps) and now - max(stamps) <= ACTIVE_S


def _boxes(node: dict[str, Any]) -> list[dict[str, Any]]:
    """A node's jobs and, for a layer, the jobs of every cycle row."""
    rows = [j for r in node.get("rows") or () if r.get("kind") == "cycle" for j in r["jobs"]]
    return [*node.get("jobs", []), *rows]


def settle(nodes: list[dict[str, Any]], *, live: bool, blocked: bool) -> None:
    """Turn every `running` job, cycle and node into `failed` when the story is blocked, or `stopped` when
    it is not live: nothing is running in a story that nobody is working on."""
    if live and not blocked:
        return
    settled = "failed" if blocked else "stopped"
    for node in nodes:
        for box in (node, *_boxes(node), *(r for r in node.get("rows") or () if r.get("kind") == "cycle")):
            if box.get("status") == "running":
                box["status"] = settled


def active(
    nodes: list[dict[str, Any]], statuses: Collection[str]
) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """The last node whose status is one of `statuses`, with its deepest job of such a status (None when
    only the node is); None when no node is."""
    for node in reversed(nodes):
        if node.get("status") in statuses:
            found = next((j for j in reversed(node.get("jobs", [])) if j.get("status") in statuses), None)
            return node, found
    return None


def run_last_ts(state_dir: Path, nodes: list[dict[str, Any]]) -> float | None:
    """The last event's `ts` in the studio run the deepest running job names; None when there is none."""
    hit = active(nodes, {"running"})
    run = hit[1].get("studio_run") if hit and hit[1] else None
    return last_ts(run_events(state_dir, run)) if isinstance(run, str) else None


def _path(node: dict[str, Any]) -> list[str]:
    """Where a node sits: its lane, its label and, for a layer, its latest cycle."""
    path = [LANES.get(str(node.get("lane")), ""), str(node.get("label"))]
    cycles = [r for r in node.get("rows") or () if r.get("kind") == "cycle"]
    if cycles:
        path.append(str(cycles[-1]["label"]))
    return [p for p in path if p]


def _box_path(node: dict[str, Any], box: dict[str, Any] | None) -> list[str]:
    """Where a running box sits: its lane, its node, the layer's latest cycle when the layer has more than
    one, and the box itself (unless it is the node's own, a stage's one job)."""
    path = [LANES.get(str(node.get("lane")), ""), str(node.get("label"))]
    cycles = [r for r in node.get("rows") or () if r.get("kind") == "cycle"]
    if len(cycles) > 1:
        path.append(str(cycles[-1]["label"]))
    if box is not None and box.get("label") != node.get("label"):
        path.append(str(box.get("label")))
    return [p for p in path if p]


def _clock(ts: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts is not None else "the start"


def _exit_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last of `waiting` / `cancelled` / `story_start` when it is a waiting or cancelled: how the story's
    last turn ended. None when it was started again since."""
    last = next(
        (e for e in reversed(events) if e.get("kind") in ("waiting", "cancelled", "story_start")), None
    )
    return last if last is not None and last.get("kind") != "story_start" else None


def diagnosing(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The `diagnosis_start` of a block Muse is reading now: the last of it, `blocked`, `cancelled` and
    `story_start` when it is that. None otherwise, and always for a story logged before the event."""
    kinds = ("diagnosis_start", "blocked", "cancelled", "story_start")
    last = next((e for e in reversed(events) if e.get("kind") in kinds), None)
    return last if last is not None and last.get("kind") == "diagnosis_start" else None


def _last_passed(nodes: list[dict[str, Any]]) -> str | None:
    """The last box in pipeline order that passed, as its node and its label; None when none did."""
    for node in reversed(nodes):
        box = next((j for j in reversed(node.get("jobs", [])) if j.get("status") == "passed"), None)
        if box is not None:
            return " › ".join(dict.fromkeys(str(x) for x in (node.get("label"), box.get("label"))))
    return None


def _idle_text(
    st: dict[str, Any], events: list[dict[str, Any]], nodes: list[dict[str, Any]], *, live: bool
) -> str:
    if st.get("blocked"):
        hit = active(nodes, {"failed"})
        return f"Blocked at {' › '.join(_path(hit[0]))}" if hit else "Blocked"
    ended = _exit_event(events)
    if ended is not None and ended["kind"] == "waiting":
        return f"Waiting on {ended.get('on')}"
    if ended is not None:
        return f"Cancelled during {ended.get('during') or ended.get('stage')}"
    if st.get("stage") == "review":
        n = len(st.get("prs") or [])
        return f"In review · {n} PR{'' if n == 1 else 's'}"
    work = [e for e in events if e.get("kind") != "story_done"]
    if work and work[-1].get("kind") == "story_planned":
        return "Planned · waiting for the build lane"
    after = _last_passed(nodes) if live else None
    return f"Between steps · after {after}" if after else "Not started"


def _working_text(path: list[str], *, live: bool, last: float | None) -> str:
    where = " › ".join(path)
    return f"Running {where}" if live else f"Stopped at {where} · no event since {_clock(last)}"


def story_now(
    nodes: list[dict[str, Any]],
    st: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    live: bool,
    run_last_ts: float | None = None,
) -> dict[str, Any]:
    """The now bar: `status` running (a box is working), stopped (it was, and nothing has written since) or
    idle; the `path` to the box, its node and job ids, its `inner` step, role, step count and start; the
    `last_event` time (data only: no threshold) and the `text` the bar says. Call after `settle`."""
    newest = last_ts(events)
    last = max((t for t in (newest, run_last_ts) if t is not None), default=None)
    out: dict[str, Any] = {
        "status": "idle",
        "path": [],
        "node": None,
        "job": None,
        "inner": None,
        "role": None,
        "steps": None,
        "started": None,
        "last_event": last,
        "text": "",
    }
    opened = diagnosing(events) if st.get("blocked") else None
    if opened is not None:  # the block is being read: the exit row's Diagnose is the running box
        out.update(
            status="running" if live else "stopped",
            path=["Blocked", "Diagnose"],
            node="exit",
            job="diagnose",
            role="muse",
            started=_ts(opened),
        )
        out["text"] = _working_text(out["path"], live=live, last=last)
        return out
    left = st.get("blocked") or _exit_event(events)  # a story that left is at rest
    hit = None if left else active(nodes, {"stopped" if not live else "running"})
    if hit is None:
        out["text"] = _idle_text(st, events, nodes, live=live)
        return out
    node, box = hit
    out.update(status="running" if live else "stopped", path=_box_path(node, box), node=node["id"])
    if box is not None:
        out.update(
            job=box["id"],
            inner=box.get("live"),
            role=box.get("role"),
            steps=(box.get("tokens") or {}).get("steps"),
            started=box.get("started"),
        )
    out["text"] = _working_text(out["path"], live=live, last=last)
    return out


def _fix_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The events of the latest block's handling: after the block before it, up to the end."""
    blocks = [i for i, e in enumerate(events) if e.get("kind") == "blocked"]
    return events[blocks[-2] + 1 :] if len(blocks) > 1 else events


def _diagnosing_jobs(
    events: list[dict[str, Any]], opened: dict[str, Any], *, live: bool
) -> list[dict[str, Any]]:
    """The block Muse is reading now (after its `diagnosis_start`): Diagnose running (stopped when nobody
    works on the story), and the fix card it has made so far, else pending. The diagnosis on disk and the
    fix events before the start are the previous block's."""
    handled = events[next(i for i, e in enumerate(events) if e is opened) + 1 :]
    failed = any(e.get("kind") == "diagnosis_failed" for e in handled)
    status = "failed" if failed else "running" if live else "stopped"
    diagnose = shaped(job("diagnose", "diagnose", "muse", status, started=_ts(opened)), "model")
    made = [e for e in handled if e.get("kind") in ("fix_filed", "fix_linked", "fix_noted")]
    untaken = any(e.get("kind") == "fix_did_not_take" for e in handled)
    fix = job("fix", "fix", None, "failed" if untaken else "passed" if made else "pending")
    fix = shaped(fix, "code", sub=str(made[-1].get("fix")) if made and made[-1].get("fix") else None)
    return [diagnose, fix]


def _blocked_jobs(state_dir: Path, key: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The three boxes after a block: the diagnosis (a model), the fix card it filed or linked (airlie's own
    step) and the story's card moving to blocked."""
    handled = _fix_events(events)
    d = latest(state_dir, key)
    failed = any(e.get("kind") == "diagnosis_failed" for e in handled)
    diagnose = job("diagnose", "diagnose", "muse", "failed" if failed else "passed" if d else "skipped")
    rows = (
        [text_row("class", d.classified.class_), text_row("signature", d.classified.signature)] if d else []
    )
    diagnose = shaped(diagnose, "model", card=card("Diagnose the block", rows) if rows else None)
    made = [e for e in handled if e.get("kind") in ("fix_filed", "fix_linked", "fix_noted")]
    untaken = any(e.get("kind") == "fix_did_not_take" for e in handled)
    fix = job("fix", "fix", None, "failed" if untaken else "passed" if made else "skipped")
    fix = shaped(fix, "code", sub=str(made[-1].get("fix")) if made and made[-1].get("fix") else None)
    return [diagnose, fix]


def story_exit(
    state_dir: Path,
    key: str,
    st: dict[str, Any],
    events: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    *,
    live: bool = True,
) -> dict[str, Any] | None:
    """How the story's last turn ended, drawn after the lanes: `kind` blocked, waiting or cancelled, `where`
    it stopped, how many `times` it has, `at` when, the `reason` and what it waits `on`, and its `jobs`
    (blocked: diagnose, fix, card; the others only the card moving back to To Do). None for a story that is
    running, finished, or started again after a stop. While Muse reads a new block (its `diagnosis_start` is
    open) the row is that block: Diagnose running, the rest pending, counted in `times`."""
    blocked = st.get("blocked")
    ended = _exit_event(events)
    if not blocked and ended is None:
        return None
    opened = diagnosing(events) if blocked else None
    if opened is not None:
        hit = active(nodes, {"failed"})
        jobs = _diagnosing_jobs(events, opened, live=live)
        jobs.append(shaped(job("card", "card", None, "pending"), "code", sub="→ blocked"))
        return {
            "kind": "blocked",
            "where": " › ".join(_path(hit[0])) if hit else str(opened.get("stage") or ""),
            "times": sum(1 for e in events if e.get("kind") == "blocked") + 1,
            "at": _ts(opened),
            "reason": blocked,
            "on": None,
            "jobs": jobs,
        }
    if blocked:
        kind, wanted = "blocked", [e for e in events if e.get("kind") == "blocked"]
        hit = active(nodes, {"failed"})
        where = " › ".join(_path(hit[0])) if hit else str((wanted[-1] if wanted else {}).get("stage") or "")
        jobs = _blocked_jobs(state_dir, key, events)
        arrow = "→ blocked"
    else:
        assert ended is not None
        kind, wanted = str(ended["kind"]), [e for e in events if e.get("kind") == ended["kind"]]
        where = str(ended.get("during") or ended.get("stage") or "")
        jobs, arrow = [], "→ To Do"
    event = wanted[-1] if wanted else {}
    jobs.append(shaped(job("card", "card", None, "passed", started=_ts(event)), "code", sub=arrow))
    return {
        "kind": kind,
        "where": where,
        "times": len(wanted) or 1,
        "at": _ts(event),
        "reason": blocked if blocked else event.get("reason"),
        "on": event.get("on"),
        "jobs": jobs,
    }
