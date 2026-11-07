"""The read-only studio asks, lifted out of `StoryPipeline` (P3 of the planning line, 2026-10-24) so the epic
pipeline can ask the architect the way a story does: one specialist turn through the studio on a detached
worktree at a commit, no synthesis, in one go or in rounds, retried with the parser's reason until the reply
parses. The asker records what it did on whatever `Recorder` it is given (a story's state, an epic's): the
studio run on record before it starts, an event per ask, a retry event per refusal.

Behaviour is the pipeline's from before the move; nothing here is new."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from airlie.config import AirlieConfig
from airlie.story.fixes import line_event
from airlie.story.model import REPLY_TAIL, StoryError, unusable_why
from airlie.story.phase import fanout, wait_for_gap
from airlie.story.rounds import plan_in_rounds
from airlie.story.state import Lane
from airlie.teams.protocol import cut_off_text
from airlie.teams.worktree import detach_at, remove_worktree
from airlie.topology.studio import StudioOrchestrator
from airlie.topology.studio_plan import Plan, Subtask

StudioFactory = Callable[[], StudioOrchestrator]

ASK_LAYER: ContextVar[str | None] = ContextVar("ask_layer", default=None)
"""The layer (`"02"`) whose package the asks being made are for: they name it on their `run` event, so the
workflow screen puts each studio run under its layer. None for an ask outside a layer's package."""


class Say(Protocol):
    """One side of a conversation with a specialist: the first call (any argument) runs the opening brief,
    each later call sends its text as a follow-up in the same conversation, and each returns what the
    specialist said. `at` moves the conversation's read-only checkout to another commit before a later turn
    (a story's later layer's base, P9)."""

    def __call__(
        self, nudge: str | None = None, /, *, at: str | None = None, steps: int | None = None
    ) -> Awaitable[str]: ...


@dataclass(frozen=True)
class Writes:
    """What a conversation may write (the tool-written packages design, 2026-11-06): the builtin `tools` its
    subtask gets beside its role's, the file tools' `scope` and size per call (`max_chars`), and the hooks
    on its worktree: `prepare` before the first turn (`execute_task`'s), `collect` after each turn with the
    turn's result (a `TeamResult`, or None), before what it said is judged."""

    tools: tuple[str, ...] = ()
    scope: Literal["any", "tests"] = "any"
    max_chars: int | None = None
    prepare: Callable[[Path], Awaitable[None]] | None = None
    collect: Callable[[Path, Any], None] | None = None


@dataclass(frozen=True)
class MapUse:
    """The repository map an ask starts with: the config's (the default), none (`on=False`), or one focused on
    `focus` and capped at `chars` (a story's survey and layer planning, 2026-11-02)."""

    on: bool = True
    focus: tuple[str, ...] = ()
    chars: int | None = None

    def kwargs(self) -> dict[str, Any]:
        """`execute_task`'s map arguments; none for the default, so the studio's own defaults hold."""
        if self == CONFIG_MAP:
            return {}
        return {"repo_map": self.on, "map_focus": self.focus, "map_chars": self.chars}


CONFIG_MAP = MapUse()
"""The config's map, unfocused: what an ask that names no other gets."""


class StoryBlocked(Exception):
    """A stage cannot finish; the issue goes to blocked with this message. The diagnosis (why, by rules and by
    Muse's reading) is made on the way into the block, from what is on disk (planning-line P5a)."""


class Recorder(Protocol):
    """What an ask records on: a story's state or an epic's. `runs` names the studio runs it made, `card` is
    the item's card as a dict (its title names the ask), `base` the commit it reads, `save` writes the state
    and `emit` appends an event."""

    runs: list[str]
    card: dict[str, Any]
    base: dict[str, str] | None

    def save(self) -> None: ...

    def emit(self, kind: str, **fields: Any) -> None: ...


_RUNAWAY = re.compile(r"(.{1,8}?)\1{60,}$", re.DOTALL)


def runaway_repeat(text: str) -> str | None:
    """The unit a cut-off reply was repeating when it stopped (`"I"`: run 9 on airlie-sandbox, 2026-10-26,
    product's refine wrote `from_roman("IIII…` to max_tokens); None when its end is no runaway repeat."""
    found = _RUNAWAY.search(text.rstrip()[-2000:])
    return found.group(1) if found else None


class Asker:
    """Read-only specialist turns in `repo`, through the studio `studio` makes."""

    def __init__(
        self,
        cfg: AirlieConfig,
        studio: StudioFactory,
        repo: Path,
        *,
        lane: Lane | None = "plan",
    ) -> None:
        """`lane` is the lane whose asks these are (planning-line P5b): the planning lane's long turns wait
        for the build lane's gaps and its short asks fan out under the decode floor; the build lane's own
        asks (the package, the critic) and a standalone run (`None`) never wait."""
        self.cfg = cfg
        self.s = cfg.story
        self.studio = studio
        self.repo = repo
        self.lane = lane
        self.stories = cfg.observability.state_dir.expanduser() / "stories"
        self.cut_off: set[tuple[str, str, str]] = set()
        """(the item, the stage, the role) of each reply cut off at max_tokens once: the next one stops."""

    @staticmethod
    def cut_key(st: Recorder, stage: str, role: str) -> tuple[str, str, str]:
        """The key `answer` keeps a cut-off under: the item (its card's key or title), the stage, the role."""
        return (str(st.card.get("key") or st.card.get("title") or ""), stage, role)

    # ---- the planning lane's manners (P5b) ----

    async def wait(self, st: Recorder, stage: str, long: bool) -> None:
        """A long turn of the planning lane starts only in the build lane's gap (D2, N5): wait for it, with
        a `lane_wait` event on the line's log and on the record when it had to. A short ask, and every ask
        of any other lane, goes at once."""
        if not long or self.lane != "plan":
            return
        state_dir = self.cfg.observability.state_dir.expanduser()

        def emit(kind: str, **fields: Any) -> None:
            line_event(state_dir, kind, stage=stage, key=st.card.get("key"), **fields)
            st.emit(kind, stage=stage, **fields)

        await wait_for_gap(self.stories, emit=emit)

    def width(self) -> int:
        """How many short asks may run side by side now: `line.fanout`, or 1 while the build lane decodes
        under `line.decode_floor_tps`; every round of asks reads it again."""
        if self.lane != "plan":
            return 1
        line = self.cfg.line
        return fanout(self.stories, width=line.fanout, floor_tps=line.decode_floor_tps)

    async def gather[T](self, *aws: Awaitable[T]) -> list[T]:
        """`asyncio.gather` of short asks, `width()` at a time."""
        gate = asyncio.Semaphore(self.width())

        async def one(aw: Awaitable[T]) -> T:
            async with gate:
                return await aw

        return list(await asyncio.gather(*(one(aw) for aw in aws)))

    def start_ask(self, st: Recorder, stage: str, at: str | None) -> tuple[Any, dict[str, str]]:
        """The studio for one read-only turn, on record in the state and the events, and the commit it reads
        (the record's base, or `at`)."""
        orch = self.studio()
        base = dict(st.base or {})
        if at is not None:
            base = {"ref": at, "sha": at}
        st.runs.append(orch.run_dir.run_id)  # on record before it starts: a run that dies is still named
        st.save()
        layer = ASK_LAYER.get()
        st.emit(
            "run",
            stage=stage,
            studio_run=orch.run_dir.run_id,
            base=base.get("ref"),
            base_sha=base.get("sha"),
            **({"layer": layer} if layer else {}),
        )
        return orch, base

    def answer(self, team: Any, role: str, orch: Any, *, cut: tuple[str, str, str] | None = None) -> str:
        """What a specialist's turn said, or the stop it ended in. A reply cut off at max_tokens is a
        StoryError, asked again with a request to be shorter, unless `cut` (the story, the stage and the role)
        was cut off before: then it stops the story (2026-10-29 and 30, roman: two stories blocked on one
        long-winded reply each, a layer plan and a package, that the same role writes well within its room on
        other runs)."""
        if team is not None and team.stop_reason == "cut_off":
            unit = runaway_repeat(str(team.final_text or ""))
            if unit is not None:  # a loop, not a reply too long for its room: another try is worth it
                raise StoryError(
                    f"your reply repeated {unit!r} over and over until it was cut off: write it again, "
                    "complete, with no run of repeats"
                )
            if cut is None or cut in self.cut_off:
                raise StoryBlocked(f"the {role} did not finish: {cut_off_text(role, orch.cfg.roles[role])}")
            self.cut_off.add(cut)
            raise StoryError(
                "your reply ran out of room and was cut off before it ended: write it again, complete and "
                "shorter; give what the format asks for and nothing else (no restated brief, no long prose)"
            )
        if team is None or not team.ok:
            raise StoryError(f"the {role} did not finish ({team.stop_reason if team else 'no result'})")
        return str(team.final_text)

    async def ask(
        self,
        st: Recorder,
        stage: str,
        role: str,
        brief: str,
        at: str | None = None,
        steps: int | None = None,
        images: Sequence[Path] = (),
        long: bool = True,
        repo_map: MapUse = CONFIG_MAP,
    ) -> str:
        """One read-only specialist turn through the studio: a pinned one-subtask plan, a detached worktree at
        the record's base, or at commit `at` (removed after, even when the studio raises), no synthesis.
        `steps` limits its steps (the role's own limit when None); `images` are shown to a role that can see
        (P4: a picture the person attached to an initiative). `long` says it is a long turn, which the
        planning lane starts in the build lane's gap; consultations, surveys, reads and renders are short.
        `repo_map` is the map the turn starts with (`MapUse`)."""
        await self.wait(st, stage, long)
        plan = Plan((), (Subtask(stage, brief, role),))
        orch, base = self.start_ask(st, stage, at)
        try:
            report = await orch.execute_task(
                f"{stage}: {st.card['title']}",
                repo=self.repo,
                plan=plan,
                base=base.get("sha"),
                detach=True,
                synthesise=False,
                max_steps=steps,
                images=images,
                **repo_map.kwargs(),
            )
        finally:
            remove_worktree(self.repo, orch.run_dir.path / "studio")
        return self.answer(
            report.teams[0] if report.teams else None, role, orch, cut=self.cut_key(st, stage, role)
        )

    @asynccontextmanager
    async def conversation(
        self,
        st: Recorder,
        stage: str,
        role: str,
        brief: str,
        *,
        steps: int | None,
        at: str | None = None,
        images: Sequence[Path] = (),
        long: bool = True,
        repo_map: MapUse = CONFIG_MAP,
        writes: Writes | None = None,
        capped_follow_ups: bool = True,
    ) -> AsyncIterator[Say]:
        """A read-only conversation with `role` (P4, 2026-10-25): the first `say(...)` runs `brief` as the
        subtask (detached worktree at the record's base, or `at`), each later `say(text)` sends `text` as a
        follow-up in the same conversation (`follow_up`, `steps` steps, or a turn's own `steps`), and the
        worktree is removed when the block ends, even when it raises. Product's imagining holds one open
        across consultations. `steps` None is the role's own allowance; with `capped_follow_ups` False a
        later turn that names no `steps` of its own has the role's allowance too (the planning rounds,
        `ask_in_rounds`).

        With `writes` the conversation may write test files (the architect's package, the tool-written
        packages design, 2026-11-06): its subtask gets the write tools, confined and capped as `writes` says;
        `writes.prepare` runs on the worktree before the first turn, and `writes.collect` after every turn,
        before what the turn said is judged, so a turn that ends unusable has still been read."""
        grant = writes or Writes()
        plan = Plan(
            (),
            (
                Subtask(
                    stage,
                    brief,
                    role,
                    tools=grant.tools,
                    write_scope=grant.scope,
                    write_max_chars=grant.max_chars,
                    must_act=writes is None,  # with writes the reply is the deliverable, the files beside it
                ),
            ),
        )
        orch, base = self.start_ask(st, stage, at)
        started = False
        tree = orch.run_dir.path / "studio"
        turn_steps = steps  # `say`'s own `steps` names one turn's

        def judged(team: Any) -> str:
            if writes is not None and writes.collect is not None and tree.is_dir():
                writes.collect(tree, team)
            return self.answer(team, role, orch, cut=self.cut_key(st, stage, role))

        async def say(nudge: str | None = None, /, *, at: str | None = None, steps: int | None = None) -> str:
            nonlocal started
            await self.wait(st, stage, long)  # every turn of the conversation is a long one
            if at is not None and started:  # a later turn about another commit (a later layer's base)
                detach_at(tree, at)
            if not started:
                started = True
                report = await orch.execute_task(
                    f"{stage}: {st.card['title']}",
                    repo=self.repo,
                    plan=plan,
                    base=base.get("sha"),
                    detach=True,
                    synthesise=False,
                    max_steps=turn_steps,
                    images=images,
                    prepare=writes.prepare if writes is not None else None,
                    **repo_map.kwargs(),
                )
                return judged(report.teams[0] if report.teams else None)
            later = steps or (turn_steps if capped_follow_ups else None)
            await orch.follow_up(stage, nudge or "", max_steps=later)
            return judged(orch.runs[stage].result)

        try:
            yield say
        finally:
            remove_worktree(self.repo, orch.run_dir.path / "studio")

    async def ask_in_rounds[T](
        self,
        st: Recorder,
        stage: str,
        role: str,
        brief: str,
        at: str | None,
        *,
        parse: Callable[[str], T],
        same: Callable[[T, T], bool],
        round_steps: int,
        rounds: int,
        signal: Callable[[str], Any] | None = None,
        repo_map: MapUse = CONFIG_MAP,
    ) -> str:
        """`ask`, in rounds (story.rounds): `round_steps` steps and a plan, then again in the same
        conversation until a round leaves the plan unchanged or `rounds` are used (`in_rounds`)."""
        talk = self.conversation(
            st, stage, role, brief, steps=round_steps, at=at, repo_map=repo_map, capped_follow_ups=False
        )
        async with talk as say:
            return await self.in_rounds(
                st, stage, role, say, parse=parse, same=same, rounds=rounds, signal=signal
            )

    async def in_rounds[T](
        self,
        st: Recorder,
        stage: str,
        role: str,
        say: Say,
        *,
        parse: Callable[[str], T],
        same: Callable[[T, T], bool],
        rounds: int,
        signal: Callable[[str], Any] | None = None,
    ) -> str:
        """The planning rounds (`plan_in_rounds`) as the turns of the conversation `say` speaks in: its
        opening turn is round 1 and each later round a follow-up; the rounds' events are the record's, with
        the stage."""
        return await plan_in_rounds(
            lambda: say(),
            say,
            parse,
            same,
            rounds=rounds,
            emit=lambda kind, **f: st.emit(kind, stage=stage, **f),
            role=role,
            signal=signal,
        )

    async def usable[T](
        self,
        st: Recorder,
        stage: str,
        role: str,
        what: str,
        brief: Callable[[str | None], str],
        parse: Callable[[str], T],
        attempts: int | None = None,
        at: str | None = None,
        ask: Callable[..., Awaitable[str]] | None = None,
        check: Callable[[T], Awaitable[None]] | None = None,
    ) -> T:
        """Ask `role` until `parse` accepts the reply: an unusable one (StoryError) goes back with the reason,
        story.stage_attempts tries in all (or `attempts`), then StoryBlocked with the last reason. A reply
        cut off at max_tokens is not asked again (`ask`): it blocks at once. `at` is the commit the role
        reads (default the record's base). `ask` replaces `Asker.ask` (planning in rounds). `check` is a
        further judgement of the parsed reply that may ask another role (the layer plan's size review,
        2026-11-05); its StoryError goes back as the parser's does."""
        feedback: str | None = None
        for _ in range(attempts or self.s.stage_attempts):
            reply = ""
            try:
                reply = await (ask or self.ask)(st, stage, role, brief(feedback), at)
                value = parse(reply)
                if check is not None:
                    await check(value)
                return value
            except StoryError as e:
                feedback = unusable_why(e, reply)
                st.emit("retry", stage=stage, error=feedback, reply=reply[-REPLY_TAIL:])
        raise StoryBlocked(f"{role} did not produce a usable {what}: {feedback}")
