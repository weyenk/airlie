"""What is running now, whoever started it (the web app, the launchd story tick in another process, a `task`
command): one row per story or standalone run in progress, each with a progress bar. Read from the Studio's
state directory alone, so a story the tick is building shows here though this server never started it; the
jobs this server holds only keep a quiet one on the list."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airlie.serve.jobs import read_events
from airlie.serve.listing import stories
from airlie.serve.tree import build_tree

ACTIVE_S: float = 3600.0
"""How long a story or run may go without writing and still count as running. A story or run that crashed
leaves no end marker (no `run_done`, a stage that never finishes), so silence this long is what ends it. A
job this server holds is running however quiet: a chief thinking for an hour writes nothing."""

_PRE = {"new": 0, "intake": 1, "refine": 2}
"""How many of the stages before the layers (intake, refine, the layer plan) a story has finished, by the
stage it is at: any later stage, and any airlie does not know, has finished all three."""


def story_progress(stage: str, layers: int, built: int) -> float:
    """How far a story is, 0 to 1, counting steps: intake, refine and the layer plan (3), one build per layer
    and publish. `stage` is the last one finished, `layers` how many layers it planned (none yet counts as
    one, so the bar does not jump once they are known) and `built` how many are built; review is the end of
    publish, so it adds no step."""
    pre = _PRE.get(stage, 3)
    total = 3 + max(layers, 1) + 1
    done = pre + built + (1 if stage in ("publish", "review") else 0)
    return min(1.0, round(done / total, 3))  # built can pass layers when the layers were planned again


def run_progress(tree: dict[str, Any]) -> float | None:
    """The share of a run's tree nodes that are finished (done or failed), 0 to 1. None, an indeterminate
    bar, for a tree of one node or none: a single agent has no steps to count."""
    nodes = tree["nodes"]
    if len(nodes) <= 1:
        return None
    finished = sum(1 for n in nodes if n["state"] in ("done", "failed"))
    return round(finished / len(nodes), 3)


def _fresh(touched: float, now: float) -> bool:
    return now - touched <= ACTIVE_S


def _state(state_dir: Path, key: str) -> dict[str, Any]:
    """The story's state.json as an object; {} when it is not there, not readable or not an object."""
    try:
        st = json.loads((state_dir / "stories" / key / "state.json").read_text())
    except (ValueError, OSError):
        return {}
    return st if isinstance(st, dict) else {}


def _story_row(
    row: dict[str, Any], st: dict[str, Any], live: frozenset[str], now: float
) -> dict[str, Any] | None:
    """The running row of the story `row` (from listing.stories) with state `st`, or None when it is not
    running: finished (review), blocked, a stage that is no text, or silent for ACTIVE_S with no job of this
    server (`story-<key>`). A story with no state yet is at "new"."""
    stage = "new" if row["stage"] is None else row["stage"]
    if not isinstance(stage, str) or stage == "review" or row["blocked"]:
        return None
    if not (_fresh(row["last_activity"], now) or f"story-{row['key']}" in live):
        return None
    layers, built = st.get("layers"), st.get("built")
    progress = story_progress(
        stage, len(layers) if isinstance(layers, list) else 0, len(built) if isinstance(built, dict) else 0
    )
    return {
        "kind": "story",
        "key": row["key"],
        "title": row["title"],
        "topology": None,  # a story is a pipeline of runs, with no topology of its own
        "progress": progress,
        "last_activity": row["last_activity"],
    }


def _run_row(log: Path, run_id: str, touched: float) -> dict[str, Any] | None:
    """The running row of run `run_id`, whose event log `log` last changed at `touched`: None when the log
    has no `run_start` or has a `run_done`, or cannot be read or drawn as a tree (a damaged log skips
    its run; it must not take the list down with it)."""
    try:
        events, _ = read_events(log, 0)
        kinds = [e.get("kind") for e in events]
        if "run_start" not in kinds or "run_done" in kinds:
            return None
        tree = build_tree(events)
        return {
            "kind": "run",
            "key": run_id,
            "title": tree["goal"],
            "topology": tree["topology"],
            "progress": run_progress(tree),
            "last_activity": touched,
        }
    except Exception:  # noqa: BLE001 - one run's bad log is that run's problem, not the list's
        return None


def _run_rows(
    state_dir: Path, owned: set[str], live: frozenset[str], now: float, newest: int
) -> list[dict[str, Any]]:
    root = state_dir / "runs"
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    # ids sort by time: the newest `newest` directories, as listing.recent_runs takes them
    for d in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)[:newest]:
        if d.name in owned:
            continue
        try:
            touched = (d / "events.jsonl").stat().st_mtime
        except OSError:  # no log (yet)
            continue
        if not (_fresh(touched, now) or d.name in live):
            continue
        row = _run_row(d / "events.jsonl", d.name, touched)
        if row is not None:
            out.append(row)
    return out


def running(
    state_dir: Path, *, live: frozenset[str] = frozenset(), now: float, recent_runs: int = 100
) -> list[dict[str, Any]]:
    """Every story and standalone run in progress, newest activity first (ties by key); a row is
    `{"kind": "story" | "run", "key", "title", "topology", "progress", "last_activity"}`. `live` holds the ids
    of the jobs this server is running (`story-<KEY>` for a story, the run's id for a plan or a run): they
    stay listed however quiet. `now` is seconds since the epoch.

    A story is running when its stage is not `review`, it is not blocked, and it wrote within ACTIVE_S of
    `now` or is live. A run is running when it is among the newest `recent_runs` run directories, has a
    `run_start` and no `run_done`, belongs to no story, and its log changed within ACTIVE_S of `now` or it is
    live. The runs a story names are its own work (stages show in its row, not as rows of their own): the
    names are only compared with run directories, never joined into a path."""
    rows: list[dict[str, Any]] = []
    owned: set[str] = set()
    for row in stories(state_dir):
        st = _state(state_dir, row["key"])
        names = st.get("runs")
        if isinstance(names, list):
            owned.update(n for n in names if isinstance(n, str))
        story = _story_row(row, st, live, now)
        if story is not None:
            rows.append(story)
    rows += _run_rows(state_dir, owned, live, now, recent_runs)
    rows.sort(key=lambda r: (-r["last_activity"], r["key"]))
    return rows
