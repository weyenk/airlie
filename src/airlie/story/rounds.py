"""Planning in rounds: a plan after every few steps, and a stop when a round leaves it unchanged.

The architect used all 20 of its steps reading code, at ever slower steps (the prompt grows to 40-60k
tokens), and then wrote its plan. Reading rarely changes a plan after the first few files. So it plans early
(round 1: a few steps, then the plan), each later round reads a few more steps and writes the plan again, and
the pipeline compares the two: the same plan ends it. The step cap still holds (rounds x steps)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from airlie.story.model import Layer, StoryError

ROUND_NUDGE = (
    "That is your plan so far. Read only what could change it, for a few more steps; then reply with the "
    "whole plan again in the same JSON. If nothing you read changes it, reply with the same plan."
)
"""What the architect is told to start each later round."""

FIX_NUDGE = (
    "Your plan could not be used: {error}. Reply with the whole plan again, fixed, in the same JSON. You do "
    "not need to read anything more."
)
"""What the architect is told when its first plan is refused: sent back in the same conversation, which has
already read the code, instead of asked again from the start."""

Emit = Callable[..., None]


def same_layers(a: tuple[Layer, ...], b: tuple[Layer, ...]) -> bool:
    """The same plan: the same layers in order, with the same scope and the same criteria each. Titles and
    briefs are prose a model rewords for nothing: they are not compared."""
    return [(x.slug, x.scope, x.acceptance) for x in a] == [(x.slug, x.scope, x.acceptance) for x in b]


async def plan_in_rounds[T](
    first: Callable[[], Awaitable[str]],
    again: Callable[[str], Awaitable[str]],
    parse: Callable[[str], T],
    same: Callable[[T, T], bool],
    *,
    rounds: int,
    emit: Emit,
    fixes: int = 2,
) -> str:
    """The text of the plan the rounds end on. `first()` runs round 1 and `again(nudge)` each later one, up
    to `rounds` in all. A round-1 reply `parse` refuses goes back in the same conversation with the reason,
    up to `fixes` times (`plan_fix` events), and is then a StoryError (the caller asks again from the start);
    a later round's reply it refuses ends the rounds on the plan before it. A round whose plan is `same` as
    the one before ends them on it. Each round's plan and the reason the rounds ended are events."""
    text = await first()
    for attempt in range(fixes + 1):
        try:
            plan = parse(text)
            break
        except StoryError as e:
            if attempt == fixes:
                raise
            emit("plan_fix", attempt=attempt + 1, error=str(e)[:300])
            text = await again(FIX_NUDGE.format(error=e))
    emit("plan_round", round=1, **_shape(plan))
    for n in range(2, rounds + 1):
        later = await again(ROUND_NUDGE)
        try:
            nxt = parse(later)
        except StoryError as e:
            emit("plan_round_unusable", round=n, error=str(e)[:300])
            return _done(text, emit, "malformed", n - 1)
        emit("plan_round", round=n, **_shape(nxt))
        if same(plan, nxt):
            return _done(text, emit, "converged", n)
        text, plan = later, nxt
    return _done(text, emit, "cap", rounds)


def _done(text: str, emit: Emit, reason: str, rounds: int) -> str:
    emit("plan_done", reason=reason, rounds=rounds)
    return text


def _shape(plan: Any) -> dict[str, Any]:
    layers = plan if isinstance(plan, tuple) else ()
    return {"layers": [getattr(x, "slug", str(x)) for x in layers]}
