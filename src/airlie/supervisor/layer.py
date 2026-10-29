"""One supervisor per story layer: it gathers what Muse reads, asks it, records what it said (or why it said
nothing) in the ledger, and keeps each intervention open until the pipeline, the studio or the team says what
came of it.

It never stops a run. A Muse failure, evidence that cannot be built, a layer past its cap, even a ledger that
cannot be written: each is a fallback, announced as `supervisor_fallback`, and the caller sends today's text
as it stands. A fallback row stays open like any other, so the ledger shows how retries went without Muse
too, which is what the trial compares Muse against."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast, get_args

from airlie.config.schema import SupervisorSpec
from airlie.engine.types import Message
from airlie.story.diagnosis import Classified, Pack, render_pack
from airlie.supervisor.answer import Answer
from airlie.supervisor.evidence import TriageInput, layer_events, trace_tail
from airlie.supervisor.ledger import Intervention, Ledger, Outcome, Trigger
from airlie.supervisor.triage import Triage, TriageFailed
from airlie.teams.worktree import diff_between

DIFF_CAP = 20_000
"""How much of the layer's diff Muse is shown (`airlie supervisor try` shows the same)."""
_STUCK_MESSAGES = 8
_STUCK_MESSAGE_CAP = 800


def _trigger(value: str) -> Trigger:
    if value not in get_args(Trigger):
        raise ValueError(f"unknown supervisor trigger {value!r}")
    return cast(Trigger, value)


def _outcome(value: str) -> Outcome:
    if value not in get_args(Outcome):
        raise ValueError(f"unknown supervisor outcome {value!r}")
    return cast(Outcome, value)


class LayerSupervisor:
    def __init__(
        self,
        *,
        ledger: Ledger,
        triage: Triage,
        spec: SupervisorSpec,
        story: str,
        layer: str,
        studio_run: str,
        trace_dir: Path,
        worktree: Path,
        base_sha: str,
        brief: str,
        events: Path,
        emit: Callable[..., None],
    ) -> None:
        self.ledger = ledger
        self.triage = triage
        self.spec = spec
        self.story = story
        self.layer = layer
        self.studio_run = studio_run
        self.trace_dir = trace_dir
        self.worktree = worktree
        self.base_sha = base_sha
        self.brief = brief
        self.events = events
        self.emit = emit
        self.triage_calls = 0
        """Muse requests actually made on this layer: what the cap counts (a capped call makes none)."""
        self._open: dict[tuple[str, str], list[int]] = {}
        """Ledger ids with no outcome yet, by (trigger, subtask): subtasks of one layer can run side by side,
        and each retry's outcome must go to its own row."""

    # ---- asking --------------------------------------------------------------------------------------------

    last_category: str | None = None
    """The category of the latest advice (None after a fallback): the studio and the team loop act on a
    wrong-test diagnosis (WRONG_TEST) by sending the layer back to the architect."""

    async def advise(
        self,
        trigger: str,
        subtask: str,
        role: str,
        told: str,
        *,
        kinds: Sequence[str] = (),
        evidence: str = "",
    ) -> str | None:
        """Muse's message for the role, to add to `told`; None on any fallback. The row stays open for
        `settle`."""
        trig = _trigger(trigger)
        if trig == "block":
            raise ValueError("a block goes through at_block, which settles it at once")
        ledger_id, answer = await self._triage(trig, subtask, role, told, tuple(kinds), evidence)
        self.last_category = answer.category if answer else None
        if ledger_id is not None:
            self._open.setdefault((trig, subtask), []).append(ledger_id)
        return answer.message if answer else None

    async def at_block(
        self,
        subtask: str,
        role: str,
        reason: str,
        *,
        pack: Pack | None = None,
        classified: Classified | None = None,
        kinds: Sequence[str] = (),
        evidence: str = "",
    ) -> Answer | None:
        """Muse's reading of why the story blocked; None on a fallback. The story is stopping, so the outcome
        is known at once. With the block's evidence `pack` and the mechanical `classified` (the diagnosis,
        P5a), Muse reads the pack beside the class and says whether it agrees with it; the ledger row keeps
        the class and the signature."""
        if pack is not None:
            evidence = render_pack(pack)
            if classified is not None:
                evidence += (
                    f"\n\nMechanical class: {classified.class_} / {classified.kind} (rule {classified.rule}; "
                    f"signature {classified.signature}). Say whether you agree (class_agrees, class_reason)."
                )
                kinds = kinds or (classified.kind,)
        ledger_id, answer = await self._triage(
            "block", subtask, role, reason, tuple(kinds), evidence, classified=classified
        )
        if ledger_id is not None:
            self._write_outcome("block", ledger_id, "blocked", "")
        return answer

    # ---- the repeat guard (StuckAdvisor) -------------------------------------------------------------------

    async def stuck(self, subtask: str, role: str, told: str, messages: Sequence[Message]) -> str | None:
        """The repeat guard: Muse reads the team's own recent messages, which is where the loop shows."""
        shown = "\n".join(f"{m.role}: {m.content[:_STUCK_MESSAGE_CAP]}" for m in messages[-_STUCK_MESSAGES:])
        return await self.advise("repeat_guard", subtask, role, told, evidence=shown)

    def stuck_settled(self, subtask: str, helped: bool) -> None:
        self.settle("repeat_guard", "helped" if helped else "not_helped", subtask=subtask)

    async def red(self, subtask: str, role: str, told: str, messages: Sequence[Message]) -> str | None:
        """A red streak: the role's tests failed several runs in a row, each edit different, so the repeat
        guard never fired (roman, 2026-10-28: 35 minutes red on one test, the supervisor never asked). Muse
        reads the same recent messages, the last failure among them."""
        shown = "\n".join(f"{m.role}: {m.content[:_STUCK_MESSAGE_CAP]}" for m in messages[-_STUCK_MESSAGES:])
        return await self.advise("red_streak", subtask, role, told, evidence=shown)

    def red_settled(self, subtask: str, helped: bool) -> None:
        self.settle("red_streak", "helped" if helped else "not_helped", subtask=subtask)

    # ---- what came of it -----------------------------------------------------------------------------------

    def settle(self, trigger: str, outcome: str, detail: str = "", *, subtask: str | None = None) -> None:
        """The outcome of the latest open intervention for `trigger` (and `subtask`, when given), which is
        then closed; with none open, nothing happens (a second settle of the same retry is ignored)."""
        trig, out = _trigger(trigger), _outcome(outcome)
        keys = [k for k, ids in self._open.items() if ids and k[0] == trig and subtask in (None, k[1])]
        if not keys:
            return
        ids = self._open[max(keys, key=lambda k: max(self._open[k]))]
        latest = max(ids)
        ids.remove(latest)
        self._write_outcome(trig, latest, out, detail)

    def close(self) -> None:
        """The layer is over: whatever is still open is `unknown`, so no row is left without an outcome."""
        for (trig, _), ids in self._open.items():
            for ledger_id in ids:
                self._write_outcome(trig, ledger_id, "unknown", "")
        self._open.clear()

    # ---- inside --------------------------------------------------------------------------------------------

    async def _triage(
        self,
        trigger: Trigger,
        subtask: str,
        role: str,
        told: str,
        kinds: tuple[str, ...],
        evidence: str,
        *,
        classified: Classified | None = None,
    ) -> tuple[int | None, Answer | None]:
        answer: Answer | None = None
        fallback: str | None = None
        seconds, tokens = 0.0, 0
        if self.triage_calls >= self.spec.per_layer:
            fallback = "cap"
        else:
            try:
                inp = self._input(trigger, subtask, role, told, kinds, evidence)
            except Exception as e:  # noqa: BLE001  # evidence that cannot be built is a fallback, not a stop
                fallback = f"evidence: {type(e).__name__}"
            else:
                self.triage_calls += 1
                try:
                    triaged = await self.triage(
                        inp,
                        self.worktree,
                        on_steer=lambda id: self._emit(
                            "steer", id=id, role=self.triage.role_name, subtask=subtask, layer=self.layer
                        ),
                    )
                except TriageFailed as e:
                    fallback, seconds, tokens = e.reason, e.seconds, e.tokens  # what the failed call cost
                else:
                    answer, seconds, tokens = triaged.answer, triaged.seconds, triaged.tokens
        try:
            ledger_id = self.ledger.record(
                Intervention(
                    story=self.story,
                    layer=self.layer,
                    studio_run=self.studio_run,
                    subtask=subtask,
                    role=role,
                    trigger=trigger,
                    kinds=kinds,
                    answer=answer,
                    fallback=fallback,
                    model=self.triage.model,
                    seconds=seconds,
                    tokens=tokens,
                    class_=classified.class_ if classified else None,
                    signature=classified.signature if classified else None,
                )
            )
        except Exception as e:  # noqa: BLE001  # a ledger that cannot be written never stops the run
            self._fallback(trigger, f"ledger: {type(e).__name__}", None)
            return None, None
        if answer is None:
            self._fallback(trigger, fallback or "", ledger_id)
            return ledger_id, None
        self._emit(
            "supervisor_triage",
            trigger=trigger,
            category=answer.category,
            owner=answer.owner,
            ledger_id=ledger_id,
            layer=self.layer,
        )
        return ledger_id, answer

    def _input(
        self, trigger: Trigger, subtask: str, role: str, told: str, kinds: tuple[str, ...], evidence: str
    ) -> TriageInput:
        return TriageInput(
            trigger=trigger,
            role=role,
            subtask=subtask,
            told=told,
            evidence=evidence,
            kinds=kinds,
            brief=self.brief,
            trace=trace_tail(self.trace_dir, subtask, self.spec.trace_steps),
            diff=diff_between(self.worktree, self.base_sha, DIFF_CAP),
            events=layer_events(self.events, self.layer),
            earlier=tuple(self.ledger.for_layer(self.story, self.layer)),
            recall=tuple(self.ledger.recall(trigger, kinds, self.spec.recall)),
        )

    def _emit(self, kind: str, **fields: object) -> None:
        # an event that cannot be written never stops the run
        with contextlib.suppress(Exception):
            self.emit(kind, **fields)

    def _fallback(self, trigger: str, reason: str, ledger_id: int | None) -> None:
        self._emit(
            "supervisor_fallback", trigger=trigger, reason=reason, ledger_id=ledger_id, layer=self.layer
        )

    def _write_outcome(self, trigger: str, ledger_id: int, outcome: Outcome, detail: str) -> None:
        try:
            self.ledger.outcome(ledger_id, outcome, detail)
        except Exception as e:  # noqa: BLE001  # as for the record: a ledger failure never stops the run
            self._fallback(trigger, f"ledger: {type(e).__name__}", ledger_id)
