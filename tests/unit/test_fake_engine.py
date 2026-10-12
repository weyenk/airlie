"""FakeEngine is the scripted test double every later package uses; it must honour the Engine contract."""

import pytest

from airlie.config import Sampling
from airlie.engine import (
    EngineError,
    FakeEngine,
    FakeReply,
    GenRequest,
    Message,
    TokenEvent,
    ToolCall,
)


def req(role: str = "coder", text: str = "hi") -> GenRequest:
    return GenRequest(
        role=role, messages=[Message(role="user", content=text)], sampling=Sampling(), max_tokens=64, stop=[]
    )


async def test_scripted_replies_are_returned_in_order_per_role() -> None:
    eng = FakeEngine(
        {"coder": [FakeReply(text="one"), FakeReply(text="two")], "judge": [FakeReply(text="YES")]}
    )
    await eng.start()
    assert (await eng.generate(req("coder"))).text == "one"
    assert (await eng.generate(req("judge"))).text == "YES"
    assert (await eng.generate(req("coder"))).text == "two"
    assert eng.stats().requests == 3


async def test_streams_tokens_per_word_and_reports_usage() -> None:
    eng = FakeEngine({"coder": [FakeReply(text="alpha beta gamma", cached_tokens=7)]})
    seen: list[TokenEvent] = []
    res = await eng.generate(req(), on_token=seen.append)
    assert [t.text for t in seen] == ["alpha ", "beta ", "gamma"]
    assert all(t.reasoning is False for t in seen)
    assert res.finish_reason == "stop"
    assert res.completion_tokens == 3
    assert res.cached_tokens == 7
    assert res.prompt_tokens > 0


async def test_tool_call_reply_sets_finish_reason() -> None:
    call = ToolCall(id="c1", name="read_file", arguments='{"path": "a.py"}')
    eng = FakeEngine({"coder": [FakeReply(tool_calls=[call])]})
    res = await eng.generate(req())
    assert res.tool_calls == [call]
    assert res.finish_reason == "tool_calls"


async def test_reasoning_is_streamed_before_text() -> None:
    eng = FakeEngine({"coder": [FakeReply(text="done", reasoning="think hard")]})
    seen: list[TokenEvent] = []
    res = await eng.generate(req(), on_token=seen.append)
    assert seen[0].reasoning is True
    assert seen[-1].text == "done"
    assert res.reasoning == "think hard"


async def test_yes_no_uses_scripted_probability() -> None:
    eng = FakeEngine({"judge": [FakeReply(p_yes=0.8)]})
    assert await eng.yes_no("judge", [Message(role="user", content="admit?")]) == (0.8, pytest.approx(0.2))


async def test_unknown_role_or_exhausted_script_raises() -> None:
    eng = FakeEngine({"coder": [FakeReply(text="x")]})
    with pytest.raises(EngineError, match="no script for role 'ghost'"):
        await eng.generate(req("ghost"))
    await eng.generate(req("coder"))
    with pytest.raises(EngineError, match="script exhausted for role 'coder'"):
        await eng.generate(req("coder"))


async def test_requests_are_recorded_for_assertions() -> None:
    eng = FakeEngine({"coder": [FakeReply(text="x")]})
    r = req("coder", "what is 2+2")
    await eng.generate(r)
    assert eng.requests == [r]


async def test_health_lists_scripted_roles_as_ready() -> None:
    eng = FakeEngine({"coder": []})
    h = await eng.health()
    assert h.ok is True
    assert h.models == {"coder": "ready"}
