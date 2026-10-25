"""The price of work, from the ledger (D6 of the specs phase, P4 of the planning line, 2026-10-25).

An epic is first a price next to a value, so the price must be one a person can check. It is read from the
stories the line has already published: a story's wall-clock is the sum over its runs (split at each
`story_start`) of the last event's time minus the first's; its size is the one its card names (P3's story
card), else by the layers it built. The median by size prices a sketch; a size with too few samples falls back
to the overall median scaled by `size_scale`, and with no ledger at all to `default_hours`, and the basis says
which. A calibration factor, the ratio of actual to estimated hours over finished epics (1.0 until three
have finished), is applied and named. Nothing here is a model: every number is arithmetic on files.

The files are read as data: a story whose record cannot be read counts for nothing, it never stops a price."""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from airlie.board import BoardStore
from airlie.story.epic import SIZES, Estimate, Size, StorySketch, split_block

__all__ = ["SIZES", "Estimate", "Size", "StoryCosts", "StorySketch", "queue_hours", "read_estimate"]
CALIBRATION_MIN_EPICS = 3
"""Finished epics with an actual before the calibration is anything but 1.0."""


def _events(path: Path) -> list[dict[str, Any]] | None:
    """The event log's objects, or None when it cannot be read. A torn line is skipped."""
    try:
        text = path.read_text()
    except (OSError, ValueError):
        return None
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def story_wall_clock(events: Path) -> float | None:
    """The hours a published story took: the sum over its runs (each from a `story_start` to the next) of the
    last event's time minus the first's. None when the story never reached `review` (its state.json, beside
    the log) or the log cannot be read: only a finished story is a sample."""
    state = _json(events.parent / "state.json")
    if state is None or state.get("stage") != "review":
        return None
    recs = _events(events)
    if recs is None:
        return None
    runs: list[list[float]] = []
    for rec in recs:
        ts = rec.get("ts")
        if not isinstance(ts, int | float):
            continue
        if rec.get("kind") == "story_start" or not runs:
            runs.append([])
        runs[-1].append(float(ts))
    if not runs:
        return None
    return sum(max(r) - min(r) for r in runs) / 3600.0


def size_of_story(state_dir: Path, key: str) -> Size | None:
    """A story's size: the one its card names when it has a story card block, else by the layers it built
    (one or two S, three or four M, five and more L). None with neither."""
    state = _json(state_dir / "stories" / key / "state.json")
    if state is None:
        return None
    card = state.get("card")
    description = card.get("description") if isinstance(card, dict) else None
    if isinstance(description, str):
        _, obj = split_block(description)
        if obj is not None and obj.get("type") == "story_card" and obj.get("size") in SIZES:
            return obj["size"]  # type: ignore[no-any-return]
    built = state.get("built")
    layers = len(built) if isinstance(built, dict) else 0
    if layers == 0:
        return None
    return "S" if layers <= 2 else "M" if layers <= 4 else "L"


def read_estimate(state_dir: Path, key: str) -> Estimate | None:
    """The estimate recorded for epic `key` (epics/<key>/state.json), or None."""
    state = _json(state_dir / "epics" / key / "state.json")
    raw = state.get("estimate") if state else None
    if not isinstance(raw, dict):
        return None
    try:
        return Estimate.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return None


def _hours(n: float) -> str:
    return f"{n:.1f}"


class StoryCosts:
    """Prices a story sketch from the stories `state_dir` (observability.state_dir) holds."""

    def __init__(
        self,
        state_dir: Path,
        *,
        min_samples: int,
        default_hours: Mapping[str, float],
        size_scale: Mapping[str, float],
        now: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = state_dir
        self.min_samples = min_samples
        self.default_hours = dict(default_hours)
        self.size_scale = dict(size_scale)
        self.now = now

    def samples(self) -> dict[Size, list[float]]:
        """Each published story's hours, by its size. A story with no size or no wall-clock is left out."""
        found: dict[Size, list[float]] = {"S": [], "M": [], "L": []}
        root = self.state_dir / "stories"
        if not root.is_dir():
            return found
        for events in sorted(root.glob("*/events.jsonl")):
            hours = story_wall_clock(events)
            size = size_of_story(self.state_dir, events.parent.name)
            if hours is not None and size is not None:
                found[size].append(hours)
        return found

    def _price(self, size: Size, samples: Mapping[Size, list[float]]) -> tuple[float, str, str]:
        """(hours, how in full, how in a word) for one story of `size`."""
        own = samples[size]
        if len(own) >= self.min_samples:
            return statistics.median(own), f"median of {len(own)} {size} stories", f"median of {len(own)}"
        everything = [h for hs in samples.values() for h in hs]
        if everything:
            scale = self.size_scale[size]
            return (
                statistics.median(everything) * scale,
                f"overall median x {scale:g}",
                f"overall median of {len(everything)} x {scale:g}",
            )
        return self.default_hours[size], "default", "default"

    def median_hours(self, size: Size) -> tuple[float, str]:
        """(hours, how): the median of this size's published stories when there are `min_samples`, else the
        overall median scaled for the size, else the default."""
        hours, how, _ = self._price(size, self.samples())
        return hours, how

    def calibration(self) -> tuple[float, str]:
        """The ratio of the actual hours to the estimated over the epics that have an actual; (1.0,
        "uncalibrated") under three such epics."""
        root = self.state_dir / "epics"
        done: list[Estimate] = []
        if root.is_dir():
            for state in sorted(root.glob("*/state.json")):
                est = read_estimate(self.state_dir, state.parent.name)
                if est is not None and est.actual is not None and est.hours > 0:
                    done.append(est)
        if len(done) < CALIBRATION_MIN_EPICS:
            return 1.0, "uncalibrated"
        return sum(e.actual or 0.0 for e in done) / sum(e.hours for e in done), f"{len(done)} epics"

    def estimate(self, sketch: Sequence[StorySketch]) -> Estimate:
        """The sketch's price: the calibrated sum of each size's count times its median, with the arithmetic
        in one line a person can check."""
        at = datetime.fromtimestamp(self.now(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not sketch:
            return Estimate(0.0, "no stories sketched", at)
        samples = self.samples()
        counts = Counter(s.size for s in sketch)
        total = 0.0
        parts: list[str] = []
        for size in SIZES:
            n = counts.get(size, 0)
            if not n:
                continue
            hours, _, short = self._price(size, samples)
            total += n * hours
            parts.append(f"{n} {size} at {_hours(hours)} h ({short})")
        factor, how = self.calibration()
        basis = ", ".join(parts) + f", x{factor:.1f} calibration ({how})"
        return Estimate(round(total * factor, 1), basis, at)


def queue_hours(store: BoardStore, state_dir: Path) -> float:
    """The hours still ahead: for every epic approved or doing, its latest estimate times the share of its
    stories (its children on the board) not done; an epic with no story yet counts whole. 0.0 with none."""
    total = 0.0
    for status in ("approved", "doing"):
        for epic in store.list(level="epic", status=status):
            est = read_estimate(state_dir, epic.key)
            if est is None:
                continue
            children = store.list(level="story", parent=epic.key)
            share = 1.0 if not children else sum(1 for c in children if c.status != "done") / len(children)
            total += est.latest * share
    return round(total, 1)
