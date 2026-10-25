"""The line, as rows for the Vue app (planning-line P5b, D11, 2026-10-25): `GET /v1/airlie/line` and its event
tail. Nothing here changes anything: it reads what the beat and the lanes leave on disk (`line/status.json`,
`line/lanes/<lane>.json`, the phase file, the cards' records, the diagnoses) and the board, so a row is true
whichever process ticked (serve's ticker or the launchd tick).

A lane's record is believed only while the lane's lock is held: a dead process leaves a file, no lock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airlie.board import BoardStore
from airlie.config import AirlieConfig
from airlie.story import factory
from airlie.story.budgets import card_dir
from airlie.story.costs import queue_hours, size_of_story
from airlie.story.diagnosis import latest
from airlie.story.phase import decode_state, paused
from airlie.story.state import lock_held

GATES = ("engine", "profile_match", "github")


def _json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _events(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def line_events_tail(state_dir: Path, limit: int = 100) -> list[dict[str, Any]]:
    """The last `limit` events of the line's own log, oldest first."""
    return _events(state_dir / "line" / "events.jsonl")[-max(1, limit) :]


def _lane(cfg: AirlieConfig, store: BoardStore, lane: str, now: float) -> dict[str, Any] | None:
    state = cfg.observability.state_dir.expanduser()
    if not lock_held(factory.stories_dir(cfg), lane):  # type: ignore[arg-type]
        return None
    record = _json(state / "line" / "lanes" / f"{lane}.json")
    key = record.get("key")
    if not isinstance(key, str):
        return {"card": None, "title": None, "stage": None, "elapsed_s": None}  # held by an older tick
    try:
        title = store.get(key).title
    except KeyError:
        title = None
    folder = card_dir(state, key)
    stage = _json(folder / "state.json").get("stage") if folder is not None else None
    started = record.get("started")
    row: dict[str, Any] = {
        "card": key,
        "title": title,
        "stage": stage,
        "elapsed_s": round(now - float(started), 1) if isinstance(started, int | float) else None,
    }
    if lane == "build":
        layers = [
            e for e in _events(state / "stories" / key / "events.jsonl") if e.get("kind") == "layer_start"
        ]
        decoding = decode_state(factory.stories_dir(cfg)) or {}
        size = size_of_story(state, key)
        row |= {
            "layer": layers[-1].get("layer") if layers else None,
            "step": decoding.get("step"),
            "tps": decoding.get("tps"),
            "estimate_h": factory.make_costs(cfg).median_hours(size)[0] if size is not None else None,
        }
    return row


def line_row(cfg: AirlieConfig, store: BoardStore, *, last_tick: float | None, now: float) -> dict[str, Any]:
    """`{paused, gates: {engine, profile_match, github}, lanes: {build, plan}, queue_hours, blocked,
    last_tick}`:
    a lane is null while it is free; a gate is `ok`, `down`, `mismatch` or `unknown` (not asked yet, or the
    beat stopped before it); `blocked` lists the stories in blocked with their diagnosis's class and kind and
    the fix card they wait for."""
    state = cfg.observability.state_dir.expanduser()
    status = _json(state / "line" / "status.json")
    raw = status.get("gates")
    gates: dict[str, Any] = raw if isinstance(raw, dict) else {}
    ticked = [t for t in (status.get("ts"), last_tick) if isinstance(t, int | float)]
    blocked: list[dict[str, Any]] = []
    for story in store.list(level="story", status="blocked"):
        d = latest(state, story.key)
        fix = next((x[len("blocked_by:") :] for x in story.labels if x.startswith("blocked_by:")), None)
        blocked.append(
            {
                "key": story.key,
                "class": d.classified.class_ if d else None,
                "kind": d.classified.kind if d else None,
                "fix": fix,
            }
        )
    return {
        "paused": paused(state),
        "gates": {g: gates.get(g, "unknown") for g in GATES},
        "lanes": {"build": _lane(cfg, store, "build", now), "plan": _lane(cfg, store, "plan", now)},
        "queue_hours": queue_hours(store, state),
        "blocked": blocked,
        "last_tick": max(ticked) if ticked else None,
        "line": status.get("line"),
    }
