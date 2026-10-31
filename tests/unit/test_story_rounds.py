"""Planning in rounds: a plan after every few steps, a stop when a round leaves it unchanged."""

from __future__ import annotations

from typing import Any

import pytest

from airlie.prompts import text as hub_text
from airlie.story.model import Layer, StoryError
from airlie.story.rounds import plan_in_rounds, same_layers


def layer(slug: str, scope: str = "a.py", ac: tuple[str, ...] = ("AC1",), title: str = "T") -> Layer:
    return Layer(slug, title, scope, "do it", ac)


def parse(text: str) -> tuple[Layer, ...]:
    if text == "garbage":
        raise StoryError("no JSON")
    return tuple(layer(s) for s in text.split("|")[0].split(","))


def signal(text: str) -> Any:
    """The verdict after the plan's "|": "stop", "more" or "junk" (a verdict that is not an object)."""
    word = text.partition("|")[2]
    if word == "stop":
        return {"worth_another_round": False, "reason": "settled", "check": "nothing"}
    if word == "more":
        return {"worth_another_round": True, "reason": "unsure", "check": "schema.py"}
    return "junk" if word == "junk" else None


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
        script.first, script.again, parse, same_layers, rounds=rounds, emit=script.emit, signal=signal
    )


async def test_a_round_that_changes_nothing_ends_the_rounds_on_the_plan_it_repeated() -> None:
    s = Script("a,b", "a,b,c", "a,b,c")
    assert await run(s) == "a,b,c"
    assert s.nudges == [hub_text("plan-round"), hub_text("plan-round")]  # round 4 was never asked
    kinds = [k for k, _ in s.events]
    assert kinds == ["plan_round", "steer", "plan_round", "steer", "plan_round", "plan_done"]
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
    assert kinds == ["plan_round", "steer", "plan_round_unusable", "plan_done"]
    assert s.events[-1][1] == {"reason": "malformed", "rounds": 1}


async def test_an_unusable_first_plan_goes_back_with_its_reason_in_the_same_conversation() -> None:
    s = Script("garbage", "a,b", "a,b")
    assert await run(s) == "a,b"
    assert s.nudges[0] == hub_text("plan-fix", error="no JSON") and s.nudges[1] == hub_text("plan-round")
    assert [k for k, _ in s.events][0] == "plan_fix" and s.events[0][1]["attempt"] == 1


async def test_a_first_plan_that_stays_unusable_is_the_callers_story_error_after_the_fixes() -> None:
    s = Script("garbage", "garbage", "garbage")
    with pytest.raises(StoryError, match="no JSON"):
        await run(s)
    assert len(s.nudges) == 2 and [k for k, _ in s.events] == ["plan_fix", "steer", "plan_fix", "steer"]


async def test_a_plan_that_says_another_round_is_not_worth_it_ends_the_rounds_at_once() -> None:
    s = Script("a,b|stop")
    assert await run(s) == "a,b|stop"
    assert s.nudges == []  # no second round was asked
    assert [k for k, _ in s.events] == ["plan_round", "plan_check", "plan_done"]
    assert s.events[1][1] == {
        "round": 1,
        "worth_another_round": False,
        "reason": "settled",
        "check": "nothing",
    }
    assert s.events[-1][1] == {"reason": "architect_done", "rounds": 1}


async def test_a_later_plan_that_says_stop_is_the_plan_the_rounds_end_on() -> None:
    s = Script("a|more", "a,b|stop", "a,b,c")
    assert await run(s) == "a,b|stop"
    assert len(s.nudges) == 1
    assert s.events[-1][1] == {"reason": "architect_done", "rounds": 2}


async def test_a_plan_that_wants_more_goes_on_until_the_cap() -> None:
    s = Script("a|more", "a,b|more", "a,b,c|more")
    assert await run(s, rounds=3) == "a,b,c|more"
    assert s.events[-1][1] == {"reason": "cap", "rounds": 3}
    assert [e[1]["check"] for e in s.events if e[0] == "plan_check"] == ["schema.py"] * 3


async def test_an_unchanged_plan_ends_the_rounds_as_converged_whatever_its_verdict_says() -> None:
    s = Script("a,b|more", "a,b|stop")
    assert await run(s) == "a,b|more"
    assert s.events[-1][1] == {"reason": "converged", "rounds": 2}


async def test_a_verdict_that_is_not_an_object_is_no_verdict() -> None:
    s = Script("a|junk", "a,b|junk", "a,b")
    assert await run(s) == "a,b|junk"  # round 3 repeated round 2's layers: converged, on round 2's text
    assert not [e for e in s.events if e[0] == "plan_check"]
    assert s.events[-1][1] == {"reason": "converged", "rounds": 3}


class Unfinished(Script):
    """A Script whose "unfinished" entries are a round that stopped with no answer (a StoryError)."""

    async def first(self) -> str:
        return self._answer(await super().first())

    async def again(self, nudge: str) -> str:
        return self._answer(await super().again(nudge))

    @staticmethod
    def _answer(text: str) -> str:
        if text == "unfinished":
            raise StoryError("the architect did not finish (max_steps)")
        return text


async def test_a_first_round_with_no_answer_is_asked_for_its_plan_in_the_same_conversation() -> None:
    s = Unfinished("unfinished", "a,b|stop")
    assert await run(s) == "a,b|stop"
    assert s.nudges == [hub_text("plan-unfinished")]
    fix = [f for k, f in s.events if k == "plan_fix"]
    assert fix == [{"attempt": 1, "error": "the architect did not finish (max_steps)"}]


async def test_a_first_round_that_never_answers_is_a_story_error_after_its_fixes() -> None:
    s = Unfinished("unfinished", "unfinished", "unfinished")
    with pytest.raises(StoryError, match="did not finish"):
        await run(s)
    assert s.nudges == [hub_text("plan-unfinished"), hub_text("plan-unfinished")]


async def test_an_unfinished_and_an_unusable_first_round_share_the_fixes() -> None:
    s = Unfinished("unfinished", "garbage", "a|stop")
    assert await run(s) == "a|stop"
    assert s.nudges == [hub_text("plan-unfinished"), hub_text("plan-fix", error="no JSON")]


async def test_a_refused_first_round_keeps_its_replys_end_in_the_plan_fix_event() -> None:
    """Roman, 2026-10-31: a plan with no "layers" list was refused and its reply was kept nowhere, so why
    could not be read after the run."""
    s = Script("garbage", "a|stop")
    assert await run(s) == "a|stop"
    fix = [f for k, f in s.events if k == "plan_fix"]
    assert fix == [{"attempt": 1, "error": "no JSON", "reply": "garbage"}]


async def test_a_later_round_with_no_answer_ends_the_rounds_on_the_plan_before_it() -> None:
    s = Unfinished("a|more", "unfinished")
    assert await run(s) == "a|more"
    unusable = {"round": 2, "error": "the architect did not finish (max_steps)"}
    assert ("plan_round_unusable", unusable) in s.events
    assert s.events[-1][1] == {"reason": "malformed", "rounds": 1}


def steers(s: Script) -> list[dict[str, Any]]:
    return [f for k, f in s.events if k == "steer"]


async def test_a_plan_fix_emits_a_steer_event() -> None:
    """Each nudge the rounds send is a hub steering text and a `steer` event with its id, the role and the
    round it goes back into (spec 2026-10-22-prompt-hub, "Steering events")."""
    s = Script("garbage", "a,b", "a,b")
    await run(s)
    assert s.nudges[0] == hub_text("plan-fix", error="no JSON")
    assert [k for k, _ in s.events][:3] == ["plan_fix", "steer", "plan_round"]
    assert s.events[1][1] == {"id": "plan-fix", "role": "architect", "round": 1}


async def test_each_later_round_is_a_plan_round_steer_event_with_its_round() -> None:
    s = Script("a", "a,b", "a,b,c")
    await run(s, rounds=3)
    assert steers(s) == [
        {"id": "plan-round", "role": "architect", "round": 2},
        {"id": "plan-round", "role": "architect", "round": 3},
    ]


async def test_a_first_round_with_no_answer_is_a_plan_unfinished_steer_event() -> None:
    s = Unfinished("unfinished", "garbage", "a|stop")
    await run(s)
    assert steers(s) == [
        {"id": "plan-unfinished", "role": "architect", "round": 1},
        {"id": "plan-fix", "role": "architect", "round": 1},
    ]


async def test_one_round_that_ends_on_its_own_verdict_sends_no_steering() -> None:
    s = Script("a,b|stop")
    await run(s)
    assert steers(s) == []


async def test_the_steer_events_name_the_role_that_plans() -> None:
    s = Script("a", "a")
    await plan_in_rounds(s.first, s.again, parse, same_layers, rounds=2, emit=s.emit, role="planner")
    assert steers(s) == [{"id": "plan-round", "role": "planner", "round": 2}]


def test_two_plans_are_the_same_when_they_have_the_same_layers_owning_the_same_criteria() -> None:
    a = (layer("x", "s.py", ("AC1",), "Title one"), layer("y", "t.py", ("AC2",)))
    renamed = (
        Layer("storage", "A new title", "other/store.py", "other words", ("AC1",)),
        Layer("show", "T", "elsewhere.py", "x", ("AC2",)),
    )
    assert same_layers(a, renamed)  # names, scopes and prose are reworded between rounds for nothing
    assert not same_layers(a, (a[0],))
    assert not same_layers(a, (a[0], layer("y", "t.py", ("AC3",))))
    assert not same_layers(a, (a[0], layer("y", "t.py", ("AC1", "AC2"))))
    assert not same_layers(a, (a[1], a[0]))


async def test_a_json_block_that_does_not_parse_goes_back_with_what_is_wrong_with_it() -> None:
    """Roman, 2026-10-27: the architect left its layers list unclosed before "refine", was told three times
    that there was "no JSON object", and the story blocked."""
    from airlie.story.model import NO_JSON

    broken = 'Plan:\n```json\n{"layers": [{"slug": "a"}, "refine": {"worth_another_round": false}}\n```'

    def strict(text: str) -> tuple[Layer, ...]:
        if text == broken:
            raise StoryError(NO_JSON)
        return parse(text)

    s = Script(broken, "a", "a")
    await plan_in_rounds(s.first, s.again, strict, same_layers, rounds=4, emit=s.emit, signal=signal)
    told = s.events[0][1]["error"]
    assert told.startswith("the ```json block is not valid JSON: ") and "line 1 column" in told
    assert s.nudges[0] == hub_text("plan-fix", error=told)
    s = Script(broken, broken, broken)
    with pytest.raises(StoryError, match="not valid JSON"):
        await plan_in_rounds(s.first, s.again, strict, same_layers, rounds=4, emit=s.emit, signal=signal)
