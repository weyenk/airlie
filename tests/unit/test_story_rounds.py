"""Planning in rounds: a plan after every few steps, a stop when a round leaves it unchanged."""

from __future__ import annotations

from typing import Any

import pytest

from airlie.story.model import Layer, StoryError
from airlie.story.rounds import FIX_NUDGE, ROUND_NUDGE, plan_in_rounds, same_layers


def layer(slug: str, scope: str = "a.py", ac: tuple[str, ...] = ("AC1",), title: str = "T") -> Layer:
    return Layer(slug, title, scope, "do it", ac)


def parse(text: str) -> tuple[Layer, ...]:
    if text == "garbage":
        raise StoryError("no JSON")
    return tuple(layer(s) for s in text.split(","))


class Script:
    """first() and again() answering from a list, in order; again() records each nudge."""

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.nudges: list[str] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def first(self) -> str:
        return self.texts.pop(0)

    async def again(self, nudge: str) -> str:
        self.nudges.append(nudge)
        return self.texts.pop(0)

    def emit(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))


async def run(script: Script, rounds: int = 4) -> str:
    return await plan_in_rounds(
        script.first, script.again, parse, same_layers, rounds=rounds, emit=script.emit
    )


async def test_a_round_that_changes_nothing_ends_the_rounds_on_the_plan_it_repeated() -> None:
    s = Script("a,b", "a,b,c", "a,b,c")
    assert await run(s) == "a,b,c"
    assert s.nudges == [ROUND_NUDGE, ROUND_NUDGE]  # round 4 was never asked
    assert [k for k, _ in s.events] == ["plan_round", "plan_round", "plan_round", "plan_done"]
    assert s.events[-1][1] == {"reason": "converged", "rounds": 3}
    assert s.events[0][1] == {"round": 1, "layers": ["a", "b"]}


async def test_the_first_plan_stands_when_round_two_repeats_it() -> None:
    s = Script("a", "a")
    assert await run(s) == "a"
    assert s.events[-1][1] == {"reason": "converged", "rounds": 2}


async def test_rounds_that_keep_changing_the_plan_stop_at_the_cap_with_the_last_plan() -> None:
    s = Script("a", "a,b", "a,b,c")
    assert await run(s, rounds=3) == "a,b,c"
    assert s.events[-1] == ("plan_done", {"reason": "cap", "rounds": 3})


async def test_one_round_is_planning_in_one_go() -> None:
    s = Script("a,b")
    assert await run(s, rounds=1) == "a,b"
    assert s.nudges == [] and s.events[-1][1] == {"reason": "cap", "rounds": 1}


async def test_an_unusable_later_plan_ends_the_rounds_on_the_plan_before_it() -> None:
    s = Script("a,b", "garbage")
    assert await run(s) == "a,b"
    kinds = [k for k, _ in s.events]
    assert kinds == ["plan_round", "plan_round_unusable", "plan_done"]
    assert s.events[-1][1] == {"reason": "malformed", "rounds": 1}


async def test_an_unusable_first_plan_goes_back_with_its_reason_in_the_same_conversation() -> None:
    s = Script("garbage", "a,b", "a,b")
    assert await run(s) == "a,b"
    assert s.nudges[0] == FIX_NUDGE.format(error="no JSON") and s.nudges[1] == ROUND_NUDGE
    assert [k for k, _ in s.events][0] == "plan_fix" and s.events[0][1]["attempt"] == 1


async def test_a_first_plan_that_stays_unusable_is_the_callers_story_error_after_the_fixes() -> None:
    s = Script("garbage", "garbage", "garbage")
    with pytest.raises(StoryError, match="no JSON"):
        await run(s)
    assert len(s.nudges) == 2 and [k for k, _ in s.events] == ["plan_fix", "plan_fix"]


def test_two_plans_are_the_same_when_their_layers_scopes_and_criteria_are() -> None:
    a = (layer("x", "s.py", ("AC1",), "Title one"), layer("y", "t.py", ("AC2",)))
    reworded = (
        Layer("x", "A new title", "s.py", "other words", ("AC1",)),
        Layer("y", "T", "t.py", "x", ("AC2",)),
    )
    assert same_layers(a, reworded)
    assert not same_layers(a, (a[0],))
    assert not same_layers(a, (a[0], layer("y", "t.py", ("AC3",))))
    assert not same_layers(a, (a[0], layer("y", "u.py", ("AC2",))))
    assert not same_layers(a, (a[1], a[0]))
