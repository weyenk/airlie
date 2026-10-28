"""What each stage of the line costs, recorded against a budget (planning-line P5b, D8, 2026-10-25).

`line.budgets.<stage>` carries `wall_s` and `tokens` for each of ten stages (`STAGES`). A `StageMeter` folds a
card's own events and the step events of the studio runs they name into the stage's figures, writes them to
`stages/<stage>.json` in the card's directory, and emits `budget_warning` at 80 percent and `budget_exceeded`
at 100 percent of either budget, each at most once per stage per card. Nothing stops: a stage's own caps
(steps, turns, attempts) stay the only stops. The figures are what `line:stats` and the flow rows read, so
the whole getting cheaper when planning time moved upward can be shown.

Tokens are the tokens a stage processed, prompt and completion, summed over its steps; wall-clock is the span
of each studio run's own log (the meeting's turns and the publish step are timed by their own events)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from airlie.config.schema import BUDGET_STAGES, BudgetSpec

STAGES = BUDGET_STAGES
"""The ten stages a card's cost is kept for, in the order the line meets them."""

REPEATING = frozenset({"package", "layer", "review"})
"""The stages a story runs once for each layer: a budget is what one costs."""

WARN_AT = 0.8
Emit = Callable[..., None]
State = Literal["ok", "warning", "exceeded"]

_ASK_STAGES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), stage)
    for pattern, stage in (
        (r"survey", "survey"),
        (r"refine", "refine"),
        (r"questions", "questions"),
        (r"(imagine|consult:.*|directions:.*|render:.*|read:.*|revise:.*)", "imagine"),
        (r"(plan|replan|layers)", "plan"),
        (r"(package|critic|proof)", "package"),
    )
)
"""An ask's stage (the `stage` of a `run` event) to the budget stage it is part of: the story's layer split
and the epic's planning are both `plan`; a package and the critic's read of it are `package`; the
imagining's consultations, directions, renders and reads are `imagine`."""


def stage_of(ask_stage: str) -> str | None:
    """The budget stage of an ask's stage, or None for one that costs nothing a budget names."""
    for pattern, stage in _ASK_STAGES:
        if pattern.fullmatch(ask_stage):
            return stage
    return None


@dataclass(frozen=True)
class Figures:
    tokens_in: int = 0
    tokens_out: int = 0
    wall_s: float = 0.0
    steps: int = 0
    attempts: int = 0

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def __add__(self, other: Figures) -> Figures:
        return Figures(
            self.tokens_in + other.tokens_in,
            self.tokens_out + other.tokens_out,
            self.wall_s + other.wall_s,
            self.steps + other.steps,
            self.attempts + other.attempts,
        )


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


def card_dir(state_dir: Path, key: str) -> Path | None:
    """The directory a card keeps its record in: a story's, an epic's or an initiative's."""
    for kind in ("stories", "epics", "initiatives"):
        found = state_dir / kind / key
        if found.is_dir():
            return found
    return None


def run_dir_of(state_dir: Path, key: str, run_id: str) -> Path | None:
    for candidate in (state_dir / "runs" / run_id, state_dir / "stories" / key / "runs" / run_id):
        if candidate.is_dir():
            return candidate
    return None


def run_figures(
    run_events: list[dict[str, Any]], *, reviewers: frozenset[str] = frozenset()
) -> dict[str, Figures]:
    """A studio run's cost, split in two: the steps of a role in `reviewers` are the review's and every other
    step the run's own. Wall-clock is the span of the run's log, attributed to the run's own."""
    own, review = Figures(attempts=1), Figures()
    stamps = [float(e["ts"]) for e in run_events if isinstance(e.get("ts"), int | float)]
    span = (max(stamps) - min(stamps)) if len(stamps) > 1 else 0.0
    for e in run_events:
        if e.get("kind") != "step":
            continue
        step = Figures(int(e.get("prompt_tokens") or 0), int(e.get("completion_tokens") or 0), 0.0, 1, 0)
        if e.get("role") in reviewers:
            review += step
        else:
            own += step
    return {"own": Figures(own.tokens_in, own.tokens_out, span, own.steps, 1), "review": review}


class StageMeter:
    """One card's stage costs. `budgets` is `line.budgets` (a stage without an entry has no budget: it is
    recorded and never warned), `emit` appends an event to the card's own log."""

    def __init__(
        self,
        state_dir: Path,
        key: str,
        budgets: Mapping[str, BudgetSpec],
        emit: Emit,
        *,
        reviewers: frozenset[str] = frozenset(),
    ) -> None:
        self.state_dir = state_dir
        self.key = key
        self.budgets = budgets
        self.emit = emit
        self.reviewers = reviewers
        self.fresh = False
        """Whether the latest `observe` crossed a line for the first time: the pipeline posts its one note at
        an overrun when this is set."""

    @property
    def folder(self) -> Path | None:
        return card_dir(self.state_dir, self.key)

    def runs(self, stage: str) -> list[Figures]:
        """One `Figures` for each time `stage` ran on this card, oldest first: each studio run of an ask or of
        a layer's build (its steps' tokens, its log's span); the meeting's turns are folded into one, and the
        publish is one."""
        folder = self.folder
        if folder is None:
            return []
        events = _events(folder / "events.jsonl")
        if stage == "amigos":
            turns = [
                Figures(
                    int(e.get("prompt_tokens") or 0),
                    int(e.get("completion_tokens") or 0),
                    float(e.get("wall_ms") or 0) / 1000.0,
                    1,
                    0,
                )
                for e in events
                if e.get("kind") == "amigos_turn"
            ]
            return [sum(turns, Figures(attempts=1))] if turns else []
        if stage == "publish":
            marks = {e.get("stage"): float(e["ts"]) for e in events if e.get("kind") == "stage" and "ts" in e}
            if "publish" in marks and "build" in marks:
                return [Figures(wall_s=max(0.0, marks["publish"] - marks["build"]), attempts=1)]
            return []
        out: list[Figures] = []
        seen: set[str] = set()
        for e in events:
            run = e.get("studio_run")
            if not isinstance(run, str) or run in seen:
                continue
            kind = e.get("kind")
            if kind == "run" and stage_of(str(e.get("stage"))) == stage:
                part = "own"
            elif kind == "layer_start" and stage in ("layer", "review"):
                part = "own" if stage == "layer" else "review"
            else:
                continue
            seen.add(run)
            run_dir = run_dir_of(self.state_dir, self.key, run)
            if run_dir is not None:
                out.append(run_figures(_events(run_dir / "events.jsonl"), reviewers=self.reviewers)[part])
        return out

    def fold(self, stage: str) -> Figures:
        """The figures `stage` is judged by. A stage that repeats on a card (a package for each layer, a
        layer's build and its review) is judged by its latest run, so a budget means what one layer costs
        however many a story has; the rest (a survey, the refine, the imagining, the meeting, the plan with
        its retries, the publish) by all their runs."""
        runs = self.runs(stage)
        if not runs:
            return Figures()
        return runs[-1] if stage in REPEATING else sum(runs, Figures())

    def observe(self, stage: str) -> State:
        """Fold `stage`, write `stages/<stage>.json`, and say where it stands against its budget: `warning` at
        80 percent of either, `exceeded` at 100. The event for each is emitted once per stage per card."""
        self.fresh = False
        folder = self.folder
        figures = self.fold(stage)
        if folder is None or not (figures.steps or figures.wall_s):
            return "ok"  # the stage did not run on this card: nothing to record
        budget = self.budgets.get(stage)
        used = {
            "wall_s": figures.wall_s / budget.wall_s if budget else 0.0,
            "tokens": figures.tokens / budget.tokens if budget else 0.0,
        }
        worst = max(used.values())
        state: State = "exceeded" if worst >= 1.0 else ("warning" if worst >= WARN_AT else "ok")
        if folder is None:
            return state
        path = folder / "stages" / f"{stage}.json"
        before = _read(path)
        record = {
            **asdict(figures),
            "tokens": figures.tokens,
            "budget": budget.model_dump() if budget else None,
            "used": {k: round(v, 3) for k, v in used.items()},
            "state": state,
            "warned": bool(before.get("warned")) or state in ("warning", "exceeded"),
            "exceeded": bool(before.get("exceeded")) or state == "exceeded",
        }
        fields = {"stage": stage, **asdict(figures), "budget": budget.model_dump() if budget else None}
        if state in ("warning", "exceeded") and not before.get("warned"):
            self.emit("budget_warning", **fields, used=record["used"])
        if state == "exceeded" and not before.get("exceeded"):
            self.emit("budget_exceeded", **fields, used=record["used"])
            self.fresh = True
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record))
        except OSError:
            pass  # figures are a record: a folder that cannot be written is no reason to stop a stage
        return state


def check(meter: StageMeter, stage: str) -> str | None:
    """`meter.observe(stage)`, never raising (the figures are a record, not a gate), and the note to post on
    the card when the stage has just crossed its budget: its cost against the budget, once."""
    try:
        state = meter.observe(stage)
        if state != "exceeded" or not meter.fresh:
            return None
        figures, budget = meter.fold(stage), meter.budgets[stage]
    except Exception:  # noqa: BLE001 - a record that cannot be kept is no reason to stop a stage
        return None
    return (
        f"the {stage} stage cost {figures.wall_s / 60:.0f} min and {figures.tokens:,} tokens against a "
        f"budget of {budget.wall_s / 60:.0f} min and {budget.tokens:,}: nothing stops it, its own caps do"
    )


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def stage_figures(folder: Path) -> dict[str, dict[str, Any]]:
    """Every stage's recorded figures of a card, for the flow rows and the stats."""
    out: dict[str, dict[str, Any]] = {}
    for stage in STAGES:
        found = _read(folder / "stages" / f"{stage}.json")
        if found:
            out[stage] = found
    return out
