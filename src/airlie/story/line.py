"""The line's scheduler (planning-line P5b, 2026-10-25): one beat over every level, in two lanes.

A beat runs the gates (the engine is up, GitHub answers, the line is not paused), the bookkeeping that needs
no model (fix releases and transient retries, promotions, epic and initiative statuses), then one job per
lane:

- the **planning lane** (`plan.lock`): the oldest ready of an initiative's turn or an epic's revision, an
  epic to plan or re-plan, a story to plan (refine, the meeting, the layer split);
- the **build lane** (`build.lock`): a replay of a dependent story whose base moved, then the top planned
  story's layers, review and publish.

A job takes its lane's lock for its whole run, whether this beat, another process's beat (the serve ticker
and the launchd agent are two schedulers over the same locks), a serve route or `task story` started it.
`StoryPipeline.plan` and `build` are the two halves of a story; the planning lane's long turns wait for the
build lane's gaps (`build.phase`, task 7) and a paused line starts nothing (task 9).

The helpers that do the work (`_plan_epic`, `_initiative_turn`, the story pipelines) are the factory's and are
called through its module, so the tests that replace them replace them here too."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from airlie.board import Item
from airlie.config import AirlieConfig
from airlie.doctor import RealProbe, SystemProbe, engine_matches_profile
from airlie.story import factory
from airlie.story.board import Board, BoardError, Card, NullBoard
from airlie.story.epic_pipeline import EpicPipeline, epics_dir
from airlie.story.epic_plan import after_keys
from airlie.story.fixes import GhRun, bookkeep, line_event
from airlie.story.initiative_pipeline import InitiativePipeline
from airlie.story.initiative_state import initiatives_dir
from airlie.story.local_board import LocalBoard
from airlie.story.model import PLANNED, StoryError, StorySetupError
from airlie.story.phase import (
    Phase,
    decoding,
    fanout,
    paused,
    read_phase,
    set_paused,
    wait_for_gap,
    write_phase,
)
from airlie.story.pipeline import StoryPipeline
from airlie.story.state import Lane, StoryState, lane_lock, lock_held

Spawn = Callable[["Job"], None]
"""Starts a lane's job in the background and returns at once (the serve process registers it in its jobs)."""

log = logging.getLogger(__name__)

LANES: tuple[Lane, ...] = ("plan", "build")


@dataclass(frozen=True)
class Pipelines:
    """The pipelines the lanes run, as factories (the tests pass fakes). `story` makes the pipeline of a
    story by the checkout it is built in and the GitHub repository its label names."""

    story: Callable[[Path, str | None], StoryPipeline]
    epic: Callable[[], EpicPipeline]
    initiative: Callable[[], InitiativePipeline]

    @classmethod
    def of(cls, cfg: AirlieConfig, board: Board) -> Pipelines:
        """The factory's pipelines over `board`. Resolved at the call, not here, so a test that replaces a
        factory function replaces it for the line too."""

        def local() -> LocalBoard:
            if not isinstance(board, LocalBoard):
                raise BoardError("epics and initiatives are on airlie's own board (story.board: local)")
            return board

        return cls(
            story=lambda repo, github_repo: factory.make_pipeline(
                cfg, board=board, repo=repo, test_cmd=None, github_repo=github_repo
            ),
            epic=lambda: factory.make_epic_pipeline(cfg, local()),
            initiative=lambda: factory.make_initiative_pipeline(cfg, local()),
        )


@dataclass
class Job:
    """One lane's piece of work for this beat. `work` takes the lane's lock itself and returns the line the
    beat logs; `events` is the log a person can follow."""

    lane: Lane
    key: str
    what: str
    events: Path
    work: Callable[[], Coroutine[Any, Any, str]]
    planned: str | None = None  # the story this job plans: the build lane may take it in the same beat
    planned_card: Card | None = field(default=None, init=False)  # set when it did plan it
    result: str | None = field(default=None, init=False)

    @property
    def id(self) -> str:
        return f"line-{self.lane}-{self.key}"


def busy_line(lane: Lane) -> str:
    return f"busy: the {'build' if lane == 'build' else 'planning'} lane is in use"


class Line:
    """One scheduler over a board. `beat()` is a tick."""

    def __init__(
        self,
        cfg: AirlieConfig,
        *,
        board: Board,
        state_dir: Path,
        pipelines: Pipelines | None = None,
        gh: GhRun | None = None,
        now: Callable[[], float] = time.time,
        serve_commit: str | None = None,
        probe: SystemProbe | None = None,
    ) -> None:
        self.probe = probe
        self.gates: dict[str, str] = {}
        """The latest beat's gates: engine, profile_match, github (ok, down, mismatch or unknown)."""
        """What the engine gate reads the running engine with (the host's `ps`); tests pass a fake."""
        self.cfg = cfg
        self.board = board
        self.state_dir = state_dir
        self.pipes = pipelines or Pipelines.of(cfg, board)
        self.gh = gh
        self.now = now
        self.serve_commit = serve_commit
        self.stories = factory.stories_dir(cfg)
        self.lanes: tuple[Lane, ...] = LANES if isinstance(board, LocalBoard) else ("build",)
        """The lanes this board has: a Jira board has no epics or initiatives to plan, so one lane (the
        story's whole run) is all there is."""

    # ---- the beat ----

    async def beat(self, spawn: Spawn | None = None) -> str:
        """One tick: the gates, the bookkeeping, one job per lane. Returns the line for the log, naming what
        each lane did; both lanes idle is `idle: no issue matches the intake query`. With `spawn` (the serve
        process's), a lane's job is handed to it to run in the background and the beat returns at once,
        saying what it started; without it (the CLI's), the beat runs its jobs to their ends."""
        board = self.board
        if isinstance(board, NullBoard):
            return "no board configured (ATLASSIAN_BASE_URL and ATLASSIAN_PROJECT_KEY): nothing to pick up"
        self.gates = dict.fromkeys(("engine", "profile_match", "github"), "unknown")
        line = await self._beat(spawn)
        self._record(line)
        return line

    async def _beat(self, spawn: Spawn | None) -> str:
        notes = await self._bookkeeping()
        busy = [lane for lane in self.lanes if lock_held(self.stories, lane)]
        if len(busy) == len(self.lanes):  # nothing to start: the gates are not asked (no engine, no GitHub)
            return self._join(notes, [busy_line(lane) for lane in busy])
        refused = await self._gates()
        if refused is not None:
            line_event(self.state_dir, "gate_failed", reason=refused)
            return self._join(notes, [refused])
        if paused(self.state_dir):  # the gates and the bookkeeping were done; a running job finishes
            line_event(self.state_dir, "paused")
            return self._join(notes, ["paused"])
        lines = await self._lanes(busy, spawn)
        return self._join(notes, lines)

    @staticmethod
    def _join(notes: list[str], lines: list[str]) -> str:
        return " ".join("; ".join([*notes, *lines]).split())

    async def _bookkeeping(self) -> list[str]:
        """What a beat does with no model: the fixes' releases and the transient retries (P5a), then the
        epics' and initiatives' statuses and the promotions (P3, P4). A board that fails is no reason to fail
        the beat."""
        notes: list[str] = []
        board = self.board
        if isinstance(board, LocalBoard):
            try:
                notes += await bookkeep(self.cfg, board.store, serve_commit=self.serve_commit, gh=self.gh)
            except Exception as e:  # noqa: BLE001 - the beat goes on whatever the bookkeeping did
                notes.append(f"bookkeeping failed ({factory._why(e)})")
            notes += await asyncio.to_thread(factory._follow, self.cfg, board)
        elif self.cfg.story.board == "jira":
            factory.log.info(
                "story.board is jira: the line only reads it, so epics are neither planned nor followed"
            )
        return notes

    async def _gates(self) -> str | None:
        """Why no job starts this beat, or None: the engine is down, or GitHub does not answer (an issue taken
        now would stop at its first publish step)."""
        try:
            healthy = bool((await factory._engine(self.cfg).health()).ok)
        except Exception:  # noqa: BLE001 - any failure to reach the engine means it is down
            healthy = False
        self.gates["engine"] = "ok" if healthy else "down"
        if not healthy:
            return "engine down: start it with `task restart -- --topology studio`"
        down = await factory.github_down(self.cfg)
        self.gates["github"] = "ok" if down is None else "down"
        if down is not None:
            return " ".join(f"github down: {down}".split())  # GitHub's own message may hold a line break
        return await self._profile_gate()

    async def _profile_gate(self) -> str | None:
        """The engine must be running the story profile (D10): a KV smaller than the profile's rotates a long
        prompt and wastes a layer, so a mismatch starts nothing this beat (the bookkeeping has run). The
        line never restarts the engine: the log line names the command for a person."""
        profile = self.cfg.story.topology
        try:
            probe = self.probe or RealProbe()
            found = await asyncio.to_thread(engine_matches_profile, self.cfg, probe, profile)
        except Exception as e:  # noqa: BLE001 - a check that cannot run is no mismatch
            factory.log.warning("engine profile check failed: %s", e)
            return None
        self.gates["profile_match"] = {"ok": "ok", "fail": "mismatch"}.get(found.status, "unknown")
        if found.status != "fail":
            return None
        line_event(self.state_dir, "engine_mismatch", profile=profile, detail=found.detail)
        return " ".join(f"engine mismatch: {found.detail}".split())

    # ---- the lanes ----

    async def _lanes(self, busy: list[Lane], spawn: Spawn | None = None) -> list[str]:
        """Pick a job for each lane that is free and run them side by side; the line of each. The build lane
        takes the story the planning lane has just planned when it had nothing of its own (the one-shot
        beat's hand-over: a fresh story is built by the beat that planned it, not by the next)."""
        jobs: list[Job] = []
        lines = [busy_line(lane) for lane in busy]
        for lane in self.lanes:
            if lane in busy:
                continue
            job = await self._pick(lane)
            if job is not None:
                jobs.append(job)
        if spawn is not None:  # the next beat, a minute on, takes what this one planned
            for job in jobs:
                spawn(job)
                lines.append(f"started {job.lane} {job.what} {job.key}")
            if not jobs and not busy:
                lines.append("idle: no issue matches the intake query")
            return lines
        lines += await asyncio.gather(*(self.run_job(job) for job in jobs))
        if not any(job.lane == "build" for job in jobs) and "build" not in busy:
            for job in jobs:
                follow = self._handover(job)
                if follow is not None:
                    lines.remove(job.result or "")
                    lines.append(await self.run_job(follow))
        if not jobs and not busy:
            lines.append("idle: no issue matches the intake query")
        return lines

    def _handover(self, job: Job) -> Job | None:
        """The build job for the story `job` just planned, when the build lane is free and has the story."""
        if job.planned is None or job.planned_card is None or lock_held(self.stories, "build"):
            return None
        board = self.board
        assert isinstance(board, LocalBoard)
        try:
            item = board.item(job.planned)
        except BoardError:
            return None
        if item.status not in ("approved", "doing") or board.is_held(item):
            return None
        return self._building_job(
            job.planned, "story", self.stories / job.planned / "events.jsonl", item, taken_from="approved"
        )

    def _record(self, line: str) -> None:
        """The beat's latest word, for the line row to read from any process: when, the gates and the line."""
        with contextlib.suppress(OSError):
            folder = self.state_dir / "line"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "status.json").write_text(
                json.dumps({"ts": self.now(), "gates": self.gates, "line": line})
            )

    def _lane_record(self, job: Job, *, on: bool) -> None:
        """`line/lanes/<lane>.json` names what the lane is doing (and since when) while its job runs; the row
        believes it only while the lane's lock is held."""
        path = self.state_dir / "line" / "lanes" / f"{job.lane}.json"
        with contextlib.suppress(OSError):
            if on:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"key": job.key, "what": job.what, "started": self.now(), "pid": os.getpid()})
                )
            else:
                path.unlink(missing_ok=True)

    async def run_job(self, job: Job) -> str:
        """Run a job to its end. A failure is that lane's line, never the other lane's."""
        line_event(self.state_dir, "lane_start", lane=job.lane, key=job.key, what=job.what)
        self._lane_record(job, on=True)
        try:
            line = await job.work()
        except (BoardError, StorySetupError, StoryError) as e:
            line = f"{job.what} {job.key}: failed ({factory._why(e)})"
        finally:
            self._lane_record(job, on=False)
        job.result = " ".join(line.split())
        line_event(self.state_dir, "lane_done", lane=job.lane, key=job.key, line=job.result)
        return job.result

    async def _pick(self, lane: Lane) -> Job | None:
        """The lane's job. The board reads are SQLite and file reads: they run in a worker thread, so the
        serve process's loop (the engine proxy, the pages) is never held by a beat."""
        if lane == "plan":
            return await asyncio.to_thread(self._pick_plan)
        if isinstance(self.board, LocalBoard):
            return await asyncio.to_thread(self._pick_local_build, self.board)
        card = await self.board.next_card()
        if card is None:
            return None
        return self._building_job(card.key, "story", self.stories / card.key / "events.jsonl", card)

    # ---- the planning lane ----

    def _pick_plan(self) -> Job | None:
        """The planning lane's oldest ready job, in a fixed order: a person is waiting on the first two (an
        initiative's turn, an epic's revision), an epic's planning feeds the board, and a story's planning
        feeds the build lane."""
        board, cfg = self.board, self.cfg
        if not isinstance(board, LocalBoard):
            return None
        specs = factory._specs_pick(cfg, board)
        if specs is not None:
            return self._specs_job(board, *specs)
        epic = board.next_epic()
        if epic is not None:
            return self._epic_job(board, epic)
        target = factory._replan_target(cfg, board)
        if target is not None:
            return self._replan_job(board, *target)
        item = board.next_unplanned_story()
        if item is not None:
            card = board.card_of(item)
            held: list[Job] = []

            async def plan_story() -> str:
                line, st = await self._story_line(card, "plan")
                if st is not None and st.blocked is None and st.done(PLANNED):
                    held[0].planned_card = card  # the one-shot beat hands it to the build lane
                return line

            job = self._planning_job(
                item.key,
                "story",
                self.stories / item.key / "events.jsonl",
                plan_story,
                planned=item.key,
            )
            held.append(job)
            return job
        return None

    def _specs_job(self, board: LocalBoard, how: Literal["turn", "revise"], item: Item) -> Job:
        initiative = item.key if how == "turn" else (self._initiative_of(item) or item.key)
        events = initiatives_dir(self.cfg) / initiative / "events.jsonl"

        async def body() -> str:
            if how == "turn":
                return await factory._initiative_turn(self.cfg, board, item, self.pipes.initiative)
            return await factory._revise_epic(self.cfg, board, item, self.pipes.initiative)

        return self._planning_job(item.key, "initiative" if how == "turn" else "revision", events, body)

    def _epic_job(self, board: LocalBoard, epic: Item) -> Job:
        return self._planning_job(
            epic.key,
            "epic",
            epics_dir(self.cfg) / epic.key / "events.jsonl",
            lambda: factory._plan_epic(self.cfg, board, epic, self.pipes.epic),
        )

    def _replan_job(self, board: LocalBoard, epic: Item, story: Item) -> Job:
        return self._planning_job(
            epic.key,
            "replan",
            epics_dir(self.cfg) / epic.key / "events.jsonl",
            lambda: factory._replan_epic(self.cfg, board, epic, story, self.pipes.epic),
        )

    @staticmethod
    def _initiative_of(item: Item) -> str | None:
        from airlie.story.initiative import initiative_key_of

        return initiative_key_of(item.labels)

    def _planning_job(
        self,
        key: str,
        what: str,
        events: Path,
        body: Callable[[], Awaitable[str | None]],
        *,
        planned: str | None = None,
    ) -> Job:
        async def work() -> str:
            with lane_lock(self.stories, "plan") as held:
                if not held:
                    return busy_line("plan")
                return (await body()) or f"{what} {key}: nothing to do"

        return Job("plan", key, what, events, work, planned=planned)

    # ---- the build lane ----

    def _pick_local_build(self, board: LocalBoard) -> Job | None:
        """The build lane's job: the top planned story. A dependent story whose base moved is replayed onto it
        first, in this lane's lock: the replay moves branches the build would build on."""
        item = board.next_planned_story()
        if item is None:
            if not self._replay_due(board):
                return None
            return self._building_job("replay", "replay", self.stories, None)
        return self._building_job(item.key, "story", self.stories / item.key / "events.jsonl", item)

    def _replay_due(self, board: LocalBoard) -> bool:
        """A started story that stacks on another has its base recorded as the other's branch: the replay
        decides whether that branch moved, in the build lane's lock."""
        for item in board.store.list(level="story"):
            if item.status not in ("approved", "doing") or board.is_held(item) or not after_keys(item.labels):
                continue
            saved = factory._saved(self.cfg, item.key)
            if saved is not None and (saved.get("base") or {}).get("story"):
                return True
        return False

    def _building_job(
        self, key: str, what: str, events: Path, item: Item | Card | None, *, taken_from: str | None = None
    ) -> Job:
        board = self.board

        async def work() -> str:
            with lane_lock(self.stories, "build") as held:
                if not held:
                    return busy_line("build")
                notes: list[str] = []
                if isinstance(board, LocalBoard):
                    notes += await factory._replay_dependents(self.cfg, board)
                    if item is None:
                        return "; ".join(notes) or "replay: nothing moved"
                    top = board.item(item.key)
                    if top.status not in ("approved", "doing") or board.is_held(top):
                        return "; ".join([*notes, f"{top.key}: moved by a person, left alone"])
                    card = board.card_of(top)
                    taken = f" (taken from {taken_from or board.status_of(card)})"
                    line, _ = await self._story_line(card, "build", taken=taken)
                else:
                    assert isinstance(item, Card)
                    line, _ = await self._story_line(item, "run", taken="")
                return "; ".join([*notes, line])

        return Job("build", key, what, events, work)

    # ---- one story ----

    async def _story_line(
        self, card: Card, how: Literal["plan", "build", "run"], *, taken: str = ""
    ) -> tuple[str, StoryState | None]:
        """Plan, build or run the story of `card` and say what happened, as the tick's line has said it:
        `AIR-7 title: review (taken from approved)`, `AIR-7 title: blocked (reason)`. A story that cannot
        be placed or opened is set aside; one already in review is told so and moved there. The state is
        None for a story that was set aside."""
        cfg, board = self.cfg, self.board
        finished = (
            factory._recorded(cfg, card).get("stage") == "review"
        )  # then the run below has nothing to do
        st: StoryState | None = None
        try:
            where = factory.place(cfg, card)
            pipe = self.pipes.story(where.repo, where.github_repo)
            st = await getattr(pipe, how)(card)
        except StorySetupError as e:
            line = await factory._set_aside(cfg, board, card, e)
        else:
            if finished and st.stage == "review":
                line = await factory._already_in_review(board, card, st)
            else:
                outcome = (
                    f"blocked ({st.blocked})" if st.blocked else ("planned" if how == "plan" else st.stage)
                )
                line = f"{card.ref} {card.title}: {outcome}{taken}"
        # a title and a stop reason are issue and git text: their whitespace is folded, so it stays one line
        return " ".join(line.split()), st


__all__ = [
    "LANES",
    "Job",
    "Line",
    "Phase",
    "Pipelines",
    "busy_line",
    "decoding",
    "fanout",
    "paused",
    "read_phase",
    "set_paused",
    "wait_for_gap",
    "write_phase",
]
