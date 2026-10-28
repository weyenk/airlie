"""Planning in rounds: a plan after every few steps, and a stop when a round leaves it unchanged.

The architect used all 20 of its steps reading code, at ever slower steps (the prompt grows to 40-60k
tokens), and then wrote its plan. Reading rarely changes a plan after the first few files. So it plans early
(round 1: a few steps, then the plan), each later round reads a few more steps and writes the plan again, and
the pipeline compares the two: the same plan ends it. The step cap still holds (rounds x steps).

What the architect is told between rounds is steering in the prompt hub (`plan-round`, `plan-fix`,
`plan-unfinished`, their `when` and `why` there); each one sent is a `steer` event. REFINE_FIELD, the plan's
own verdict, is an output format and stays here beside its reader."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from airlie.prompts import text as hub_text
from airlie.story.model import Layer, StoryError, unusable_why

REFINE_FIELD = (
    'With every plan, add a top-level "refine" object: {"worth_another_round": true or false, "reason": one '
    'sentence, "check": what you would read next and why}. Say false when more reading would not change the '
    "layers, which criterion each one owns, or what a layer's tests must show; say true only with something "
    "specific to check."
)
"""What the layers brief adds when the architect plans in rounds: the plan carries its own verdict."""

Emit = Callable[..., None]


def same_layers(a: tuple[Layer, ...], b: tuple[Layer, ...]) -> bool:
    """The same plan: as many layers, in order, each owning the same criteria. Names, scopes and briefs are
    prose a model rewords between rounds for nothing (AIR-5 renamed every layer): they are not compared."""
    return [x.acceptance for x in a] == [x.acceptance for x in b]


async def plan_in_rounds[T](
    first: Callable[[], Awaitable[str]],
    again: Callable[[str], Awaitable[str]],
    parse: Callable[[str], T],
    same: Callable[[T, T], bool],
    *,
    rounds: int,
    emit: Emit,
    fixes: int = 2,
    signal: Callable[[str], Any] | None = None,
    role: str = "architect",
) -> str:
    """The text of the plan the rounds end on. `first()` runs round 1 and `again(nudge)` each later one, up
    to `rounds` in all; either raises StoryError for a round that ended with no answer. A round 1 with no
    answer, or whose reply `parse` refuses, goes back in the same conversation (`plan-unfinished`, or
    `plan-fix` with the reason), up to `fixes` times in all (`plan_fix` events), and is then a StoryError
    (the caller asks again from the start); a later round with no answer, or one it refuses, ends the rounds
    on the plan before it. `signal(text)` reads the plan's own verdict (its `refine` object):
    `worth_another_round: false` ends the rounds on that plan, at once (a `plan_check` event says what it
    said). A round whose plan is `same` as the one before ends them on it, whatever the verdict. Each round's
    plan and the reason the rounds ended are events, and each nudge sent is a `steer` event naming `role`,
    the planning role, and the round it goes into."""
    nudge: tuple[str, dict[str, object]] | None = None
    for attempt in range(fixes + 1):
        try:
            text = await (first() if nudge is None else again(_steer(emit, role, 1, *nudge)))
        except StoryError as e:
            if attempt == fixes:
                raise
            emit("plan_fix", attempt=attempt + 1, error=str(e)[:300])
            nudge = ("plan-unfinished", {})
            continue
        try:
            plan = parse(text)
            break
        except StoryError as e:
            why = unusable_why(e, text)
            if attempt == fixes:
                raise StoryError(why) from e
            emit("plan_fix", attempt=attempt + 1, error=why[:300])
            nudge = ("plan-fix", {"error": why})
    emit("plan_round", round=1, **_shape(plan))
    if _finished(signal, text, 1, emit):
        return _done(text, emit, "architect_done", 1)
    for n in range(2, rounds + 1):
        try:
            later = await again(_steer(emit, role, n, "plan-round", {}))
            nxt = parse(later)
        except StoryError as e:
            emit("plan_round_unusable", round=n, error=str(e)[:300])
            return _done(text, emit, "malformed", n - 1)
        emit("plan_round", round=n, **_shape(nxt))
        finished = _finished(signal, later, n, emit)
        if same(plan, nxt):
            return _done(text, emit, "converged", n)
        text, plan = later, nxt
        if finished:
            return _done(text, emit, "architect_done", n)
    return _done(text, emit, "cap", rounds)


def _steer(emit: Emit, role: str, n: int, id: str, fields: dict[str, object]) -> str:
    """The hub's steering text `id` with `fields` filled in, and a `steer` event saying it was sent: its id,
    the role and the round `n` it goes into (spec 2026-10-22-prompt-hub, "Steering events")."""
    emit("steer", id=id, role=role, round=n)
    return hub_text(id, **fields)


def _finished(signal: Callable[[str], Any] | None, text: str, n: int, emit: Emit) -> bool:
    """Whether the plan's own `refine` verdict says another round is not worth it (an event says what it
    said; no verdict, or a malformed one, is no)."""
    verdict = signal(text) if signal is not None else None
    if not isinstance(verdict, dict):
        return False
    worth = verdict.get("worth_another_round")
    emit(
        "plan_check",
        round=n,
        worth_another_round=worth,
        reason=str(verdict.get("reason", ""))[:300],
        check=str(verdict.get("check", ""))[:300],
    )
    return worth is False


def _done(text: str, emit: Emit, reason: str, rounds: int) -> str:
    emit("plan_done", reason=reason, rounds=rounds)
    return text


def _shape(plan: Any) -> dict[str, Any]:
    stories = getattr(plan, "stories", None)  # an epic's story plan (P3), not a story's layers
    if stories is not None:
        return {"stories": [s.id for s in stories]}
    layers = plan if isinstance(plan, tuple) else ()
    return {"layers": [getattr(x, "slug", str(x)) for x in layers]}
