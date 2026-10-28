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
from pathlib import Path
from typing import Any, Protocol

from airlie.config import AirlieConfig
from airlie.story.fixes import line_event
from airlie.story.model import StoryError, unusable_why
from airlie.story.phase import fanout, wait_for_gap
from airlie.story.rounds import plan_in_rounds
from airlie.story.state import Lane
from airlie.teams.protocol import cut_off_text
from airlie.teams.worktree import remove_worktree
from airlie.topology.studio import StudioOrchestrator
from airlie.topology.studio_plan import Plan, Subtask

StudioFactory = Callable[[], StudioOrchestrator]
Say = Callable[[str | None], Awaitable[str]]
"""One side of a conversation with a specialist: the first call (any argument) runs the opening brief,
each later call sends its text as a follow-up in the same conversation, and each returns what the
specialist said."""


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
        st.emit(
            "run", stage=stage, studio_run=orch.run_dir.run_id, base=base.get("ref"), base_sha=base.get("sha")
        )
        return orch, base

    def answer(self, team: Any, role: str, orch: Any) -> str:
        """What a specialist's turn said, or the stop it ended in (a cut-off one stops the story: another try
        with the same config meets the same cap, so it is no StoryError, which is retried with its text in
        the model's prompt)."""
        if team is not None and team.stop_reason == "cut_off":
            unit = runaway_repeat(str(team.final_text or ""))
            if unit is not None:  # a loop, not a reply too long for its room: another try is worth it
                raise StoryError(
                    f"your reply repeated {unit!r} over and over until it was cut off: write it again, "
                    "complete, with no run of repeats"
                )
            raise StoryBlocked(f"the {role} did not finish: {cut_off_text(role, orch.cfg.roles[role])}")
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
    ) -> str:
        """One read-only specialist turn through the studio: a pinned one-subtask plan, a detached worktree at
        the record's base, or at commit `at` (removed after, even when the studio raises), no synthesis.
        `steps` limits its steps (the role's own limit when None); `images` are shown to a role that can see
        (P4: a picture the person attached to an initiative). `long` says it is a long turn, which the
        planning lane starts in the build lane's gap; consultations, surveys, reads and renders are short."""
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
            )
        finally:
            remove_worktree(self.repo, orch.run_dir.path / "studio")
        return self.answer(report.teams[0] if report.teams else None, role, orch)

    @asynccontextmanager
    async def conversation(
        self,
        st: Recorder,
        stage: str,
        role: str,
        brief: str,
        *,
        steps: int,
        at: str | None = None,
        images: Sequence[Path] = (),
        long: bool = True,
    ) -> AsyncIterator[Say]:
        """A read-only conversation with `role` (P4, 2026-10-25): the first `say(...)` runs `brief` as the
        subtask (detached worktree at the record's base, or `at`), each later `say(text)` sends `text` as a
        follow-up in the same conversation (`follow_up`, `steps` steps), and the worktree is removed when the
        block ends, even when it raises. Product's imagining holds one open across consultations."""
        plan = Plan((), (Subtask(stage, brief, role),))
        orch, base = self.start_ask(st, stage, at)
        started = False

        async def say(nudge: str | None = None) -> str:
            nonlocal started
            await self.wait(st, stage, long)  # every turn of the conversation is a long one
            if not started:
                started = True
                report = await orch.execute_task(
                    f"{stage}: {st.card['title']}",
                    repo=self.repo,
                    plan=plan,
                    base=base.get("sha"),
                    detach=True,
                    synthesise=False,
                    max_steps=steps,
                    images=images,
                )
                return self.answer(report.teams[0] if report.teams else None, role, orch)
            await orch.follow_up(stage, nudge or "", max_steps=steps)
            return self.answer(orch.runs[stage].result, role, orch)

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
    ) -> str:
        """`ask`, in rounds (story.rounds): `round_steps` steps and a plan, then again in the same
        conversation until a round leaves the plan unchanged or `rounds` are used."""
        plan = Plan((), (Subtask(stage, brief, role),))
        orch, base = self.start_ask(st, stage, at)

        async def first() -> str:
            await self.wait(st, stage, True)
            report = await orch.execute_task(
                f"{stage}: {st.card['title']}",
                repo=self.repo,
                plan=plan,
                base=base.get("sha"),
                detach=True,
                synthesise=False,
                max_steps=round_steps,
            )
            return self.answer(report.teams[0] if report.teams else None, role, orch)

        async def again(nudge: str) -> str:
            await self.wait(st, stage, True)
            await orch.follow_up(stage, nudge)
            return self.answer(orch.runs[stage].result, role, orch)

        try:
            return await plan_in_rounds(
                first,
                again,
                parse,
                same,
                rounds=rounds,
                emit=lambda kind, **f: st.emit(kind, stage=stage, **f),
                role=role,
            )
        finally:
            remove_worktree(self.repo, orch.run_dir.path / "studio")

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
    ) -> T:
        """Ask `role` until `parse` accepts the reply: an unusable one (StoryError) goes back with the reason,
        story.stage_attempts tries in all (or `attempts`), then StoryBlocked with the last reason. A reply
        cut off at max_tokens is not asked again (`ask`): it blocks at once. `at` is the commit the role
        reads (default the record's base). `ask` replaces `Asker.ask` (planning in rounds)."""
        feedback: str | None = None
        for _ in range(attempts or self.s.stage_attempts):
            reply = ""
            try:
                reply = await (ask or self.ask)(st, stage, role, brief(feedback), at)
                return parse(reply)
            except StoryError as e:
                feedback = unusable_why(e, reply)
                st.emit("retry", stage=stage, error=feedback)
        raise StoryBlocked(f"{role} did not produce a usable {what}: {feedback}")
