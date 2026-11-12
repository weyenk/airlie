"""The streaming chat turn: tool loop, engine hold, persistence and hooks, on a scripted engine."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import pytest

from airlie.auth.principal import Principal
from airlie.chat.hooks import Hooks, ToolEvent, TurnContext, TurnSummary
from airlie.chat.stream import DONE
from airlie.chat.threads import StoredMessage, ThreadStore
from airlie.chat.tools import TodoList, TodoTool
from airlie.chat.turn import TurnHandle, TurnRequest, history_messages, run_turn
from airlie.config import AirlieConfig, load_config
from airlie.config.schema import RoleSpec
from airlie.engine.types import (
    ContentPart,
    EngineCapabilities,
    EngineError,
    GenRequest,
    GenResult,
    Message,
    TokenEvent,
    ToolCall,
)
from airlie.manager.core import Status
from airlie.prompts import text
from airlie.serve.bridge.clock import FakeClock
from airlie.tools.protocol import ToolContext, ToolResult
from airlie.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[2]
USER = Principal("u1", "Ann", "member", "session")


@pytest.fixture(scope="module")
def role() -> RoleSpec:
    cfg: AirlieConfig = load_config(ROOT / "airlie.yaml", ROOT / "no-such-local.yaml")
    return cfg.roles["chat"]


def reply(
    body: str = "",
    *,
    calls: list[ToolCall] | None = None,
    finish: Any = None,
    prompt: int = 100,
    completion: int = 7,
    cached: int | None = 40,
) -> GenResult:
    tool_calls = calls or []
    return GenResult(
        text=body,
        reasoning="",
        tool_calls=tool_calls,
        finish_reason=finish or ("tool_calls" if tool_calls else "stop"),
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
    )


@dataclass
class Step:
    result: GenResult | None = None
    tokens: list[TokenEvent] = field(default_factory=list)
    gate: asyncio.Event | None = None  # the generation waits here after its tokens
    error: Exception | None = None


def words(body: str) -> list[TokenEvent]:
    return [TokenEvent(w) for w in body.split("|")]


class ScriptedEngine:
    capabilities = EngineCapabilities(batching=True, prefix_cache=True, logprobs=False, native_tools=True)

    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.requests: list[GenRequest] = []
        self.cancelled: list[str] = []

    async def generate(
        self, req: GenRequest, on_token: Callable[[TokenEvent], None] | None = None
    ) -> GenResult:
        self.requests.append(req)
        step = self.steps.pop(0)
        for ev in step.tokens:
            if on_token:
                on_token(ev)
        if step.gate is not None:
            await step.gate.wait()
        if step.error is not None:
            raise step.error
        assert step.result is not None
        return step.result

    async def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)


class FakeHolder:
    def __init__(self, ready_after: int = 0) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.ready_after = ready_after
        self.polls = 0

    @asynccontextmanager
    async def hold(self, engine: str, owner: str) -> AsyncIterator[None]:
        while self.polls < self.ready_after:
            await asyncio.sleep(0)
        self.events.append(("hold", engine, owner))
        try:
            yield
        finally:
            self.events.append(("release", engine, owner))


class Echo:
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]}

    def __init__(self) -> None:
        self.ran: list[dict[str, Any]] = []
        self.block: asyncio.Event | None = None  # when set, a run waits on it (never released: a hung tool)
        self.started = asyncio.Event()

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.started.set()
        if self.block is not None:
            await self.block.wait()
        self.ran.append(args)
        return ToolResult(True, f"echoed {args['word']}")


def call(name: str, arguments: dict[str, Any] | str, call_id: str = "c1") -> ToolCall:
    return ToolCall(call_id, name, arguments if isinstance(arguments, str) else json.dumps(arguments))


class Rig:
    def __init__(self, tmp_path: Path, role: RoleSpec, steps: list[Step], *, max_steps: int = 4) -> None:
        self.engine = ScriptedEngine(steps)
        self.holder = FakeHolder()
        self.echo = Echo()
        self.todos = TodoList()
        self.registry = ToolRegistry([self.echo, TodoTool(self.todos)], ToolContext(worktree=tmp_path))
        self.store = ThreadStore(tmp_path / "threads.db")
        self.thread = self.store.open("t1", "u1", "plain")
        self.hooks = Hooks()
        self.finished: list[TurnSummary] = []
        self.tool_events: list[ToolEvent] = []
        self.statuses: list[list[Status]] = []
        self.clock = FakeClock()
        self.role = role
        self.max_steps = max_steps

        async def on_finish(summary: TurnSummary) -> None:
            self.finished.append(summary)

        async def after_tool(ctx: TurnContext, ev: ToolEvent) -> None:
            self.tool_events.append(ev)

        self.hooks.on("on_finish", on_finish)
        self.hooks.on("after_tool", after_tool)

    def status(self) -> list[Status]:
        self.holder.polls += 1
        if not self.statuses:
            return []
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def turn(self, user_text: str = "hello", handle: TurnHandle | None = None) -> AsyncIterator[str]:
        user = StoredMessage("u-1", "user", [{"type": "text", "text": user_text}], 1.0)
        ctx = TurnContext("t1", "u1", "plain", USER, system_prompt="SYSTEM")
        return run_turn(
            TurnRequest(self.thread, user, ctx),
            engine=self.engine,
            engine_name="chat-engine",
            holder=self.holder,
            registry=self.registry,
            todos=self.todos,
            store=self.store,
            hooks=self.hooks,
            role=self.role,
            max_steps=self.max_steps,
            clock=self.clock,
            status=self.status,
            handle=handle,
        )

    def saved(self) -> list[StoredMessage]:
        return self.store.get("t1", "u1").messages


def parse(events: list[str]) -> list[dict[str, Any]]:
    return [json.loads(e[6:]) for e in events if e != DONE]


async def collect(stream: AsyncIterator[str]) -> list[str]:
    return [e async for e in stream]


def types(events: list[str]) -> list[str]:
    return [c["type"] for c in parse(events)]


async def test_a_plain_answer_streams_text_deltas_then_finish_and_done(
    tmp_path: Path, role: RoleSpec
) -> None:
    rig = Rig(tmp_path, role, [Step(reply("Hello there"), words("Hello |there"))])

    events = await collect(rig.turn())

    chunks = parse(events)
    assert [c["type"] for c in chunks] == [
        "start",
        "start-step",
        "text-start",
        "text-delta",
        "text-delta",
        "text-end",
        "finish-step",
        "finish",
    ]
    assert [c["delta"] for c in chunks if c["type"] == "text-delta"] == ["Hello ", "there"]
    assert events[-1] == DONE
    assert chunks[0]["messageId"].startswith("msg-")


async def test_reasoning_tokens_stream_as_reasoning_parts(tmp_path: Path, role: RoleSpec) -> None:
    steps = [Step(reply("Done"), [TokenEvent("hmm", reasoning=True), TokenEvent("Done")])]
    rig = Rig(tmp_path, role, steps)

    events = await collect(rig.turn())

    assert types(events)[2:] == [
        "reasoning-start",
        "reasoning-delta",
        "reasoning-end",
        "text-start",
        "text-delta",
        "text-end",
        "finish-step",
        "finish",
    ]
    assert [p["type"] for p in rig.saved()[1].parts] == ["reasoning", "text"]


async def test_a_tool_call_streams_input_then_output_and_the_result_goes_back_to_the_model(
    tmp_path: Path, role: RoleSpec
) -> None:
    steps = [
        Step(reply("Let me check.", calls=[call("echo", {"word": "hi"})]), [TokenEvent("Let me check.")]),
        Step(reply("It said hi."), [TokenEvent("It said hi.")]),
    ]
    rig = Rig(tmp_path, role, steps)

    chunks = parse(await collect(rig.turn()))

    assert [c["type"] for c in chunks] == [
        "start",
        "start-step",
        "text-start",
        "text-delta",
        "text-end",
        "tool-input-start",
        "tool-input-available",
        "tool-output-available",
        "finish-step",
        "start-step",
        "text-start",
        "text-delta",
        "text-end",
        "finish-step",
        "finish",
    ]
    available = next(c for c in chunks if c["type"] == "tool-input-available")
    assert (available["toolCallId"], available["toolName"], available["input"]) == (
        "c1",
        "echo",
        {"word": "hi"},
    )
    assert next(c for c in chunks if c["type"] == "tool-output-available")["output"] == "echoed hi"
    second = rig.engine.requests[1].messages
    assert [m.role for m in second] == ["system", "user", "assistant", "tool"]
    assert second[2].tool_calls == (ToolCall("c1", "echo", '{"word": "hi"}'),)
    assert second[2].content == "Let me check."
    assert (second[3].content, second[3].tool_call_id, second[3].name) == ("echoed hi", "c1", "echo")
    parts = rig.saved()[1].parts
    assert [p["type"] for p in parts] == ["text", "tool-echo", "step-start", "text"]
    assert parts[1] == {
        "type": "tool-echo",
        "toolCallId": "c1",
        "input": {"word": "hi"},
        "state": "output-available",
        "output": "echoed hi",
    }


async def test_the_engine_is_held_as_chat_thread_id_for_the_whole_turn(
    tmp_path: Path, role: RoleSpec
) -> None:
    steps = [Step(reply(calls=[call("echo", {"word": "a"})])), Step(reply("done"), words("done"))]
    rig = Rig(tmp_path, role, steps)
    seen_during: list[int] = []
    real = rig.engine.generate

    async def spy(req: GenRequest, on_token: Any = None) -> GenResult:
        seen_during.append(len(rig.holder.events))
        return await real(req, on_token)

    rig.engine.generate = spy  # type: ignore[method-assign]

    events = await collect(rig.turn())

    assert seen_during == [1, 1]  # held before each step, not released between them
    assert rig.holder.events == [("hold", "chat-engine", "chat:t1"), ("release", "chat-engine", "chat:t1")]
    assert events[-1] == DONE


async def test_a_waiting_engine_streams_status_data_parts_until_ready(tmp_path: Path, role: RoleSpec) -> None:
    rig = Rig(tmp_path, role, [Step(reply("ok"), words("ok"))])
    rig.holder.ready_after = 4
    rig.statuses = [
        [Status("chat-engine", "engine", "waiting", "gpu busy")],
        [Status("chat-engine", "engine", "waiting", "gpu busy")],
        [Status("chat-engine", "engine", "starting", "")],
        [Status("chat-engine", "engine", "ready", "")],
    ]

    chunks = parse(await collect(rig.turn()))

    engine = [c for c in chunks if c["type"] == "data-engine"]
    assert [(c["data"]["state"], c["data"]["detail"]) for c in engine] == [
        ("waiting", "gpu busy"),
        ("starting", ""),
        ("ready", ""),
    ]
    assert all(c["transient"] is True and c["data"]["engine"] == "chat-engine" for c in engine)
    assert engine[0]["data"]["progress"] is None
    assert rig.clock.sleeps == [1.0] * len(rig.clock.sleeps) and rig.clock.sleeps
    assert chunks[-1]["type"] == "finish"


async def test_an_engine_that_is_up_streams_no_status(tmp_path: Path, role: RoleSpec) -> None:
    rig = Rig(tmp_path, role, [Step(reply("ok"), words("ok"))])
    rig.statuses = [[Status("chat-engine", "engine", "ready")]]

    assert "data-engine" not in types(await collect(rig.turn()))


async def test_a_tool_outside_the_kind_is_an_error_result_not_a_run(tmp_path: Path, role: RoleSpec) -> None:
    steps = [Step(reply(calls=[call("shell", {"cmd": "ls"})])), Step(reply("sorry"), words("sorry"))]
    rig = Rig(tmp_path, role, steps)

    chunks = parse(await collect(rig.turn()))

    err = next(c for c in chunks if c["type"] == "tool-output-error")
    assert err["toolCallId"] == "c1" and "unknown tool 'shell'" in err["errorText"]
    assert chunks[-1]["type"] == "finish" and "text-delta" in [c["type"] for c in chunks]
    assert rig.engine.requests[1].messages[-1].content.startswith("error: unknown tool 'shell'")
    assert rig.echo.ran == []


async def test_a_call_with_unparseable_arguments_is_an_input_error_and_the_model_sees_it(
    tmp_path: Path, role: RoleSpec
) -> None:
    bad = '{"word": "unterminated and long enough to be taken for a runaway generation'
    steps = [Step(reply(calls=[call("echo", bad)], finish="length")), Step(reply("ok"), words("ok"))]
    rig = Rig(tmp_path, role, steps)

    chunks = parse(await collect(rig.turn()))

    assert [c["type"] for c in chunks][2:7] == [
        "tool-input-start",
        "tool-input-error",
        "finish-step",
        "start-step",
        "text-start",
    ]
    err = next(c for c in chunks if c["type"] == "tool-input-error")
    assert err["input"] == bad and err["toolName"] == "echo" and "cut off" in err["errorText"]
    assert "tool-input-available" not in [c["type"] for c in chunks]
    assert rig.echo.ran == []
    assert "cut off" in rig.engine.requests[1].messages[-1].content
    assert rig.engine.requests[1].messages[-2].tool_calls == (ToolCall("c1", "echo", "{}"),)
    part = rig.saved()[1].parts[0]
    assert part["state"] == "output-error" and part["rawInput"] == bad and "input" not in part
    assert [(e.name, e.ok) for e in rig.tool_events] == [("echo", False)]


async def test_steps_are_bounded_then_one_last_call_without_tools(tmp_path: Path, role: RoleSpec) -> None:
    steps = [
        Step(reply(calls=[call("echo", {"word": "a"}, "c1")])),
        Step(reply(calls=[call("echo", {"word": "b"}, "c2")])),
        Step(reply("wrapped up", calls=[call("echo", {"word": "c"}, "c3")]), words("wrapped up")),
    ]
    rig = Rig(tmp_path, role, steps, max_steps=2)

    chunks = parse(await collect(rig.turn()))

    requests = rig.engine.requests
    assert [r.tools is None for r in requests] == [False, False, True]
    assert requests[2].messages[-1] == Message(role="user", content=text("no-more-tools"))
    assert rig.echo.ran == [{"word": "a"}, {"word": "b"}]  # the third call is not run
    assert chunks[-1]["type"] == "finish"
    assert [m.role for m in rig.saved()] == ["user", "assistant"]
    assert rig.saved()[1].parts[-1] == {"type": "text", "text": "wrapped up"}


async def test_only_the_first_tool_call_of_a_reply_runs(tmp_path: Path, role: RoleSpec) -> None:
    both = [call("echo", {"word": "one"}, "c1"), call("echo", {"word": "two"}, "c2")]
    rig = Rig(tmp_path, role, [Step(reply(calls=both)), Step(reply("ok"), words("ok"))])

    chunks = parse(await collect(rig.turn()))

    assert rig.echo.ran == [{"word": "one"}]
    assert [c["toolCallId"] for c in chunks if c["type"] == "tool-input-available"] == ["c1"]
    assert [m.role for m in rig.engine.requests[1].messages] == ["system", "user", "assistant", "tool"]


async def test_a_todo_call_emits_a_todos_data_part_and_saves_the_list(tmp_path: Path, role: RoleSpec) -> None:
    items = [{"id": "1", "text": "plan", "status": "in_progress"}]
    steps = [Step(reply(calls=[call("todo", {"items": items})])), Step(reply("ok"), words("ok"))]
    rig = Rig(tmp_path, role, steps)

    chunks = parse(await collect(rig.turn()))

    todos = [c for c in chunks if c["type"] == "data-todos"]
    assert todos == [{"type": "data-todos", "data": items}]
    assert [c["type"] for c in chunks].index("data-todos") > [c["type"] for c in chunks].index(
        "tool-output-available"
    )
    assert rig.store.get("t1", "u1").todos == items


async def test_a_refused_todo_call_saves_nothing(tmp_path: Path, role: RoleSpec) -> None:
    steps = [Step(reply(calls=[call("todo", {"items": "nope"})])), Step(reply("ok"), words("ok"))]
    rig = Rig(tmp_path, role, steps)

    chunks = parse(await collect(rig.turn()))

    assert "data-todos" not in [c["type"] for c in chunks]
    assert rig.store.get("t1", "u1").todos == []


async def test_after_tool_runs_after_each_tool_with_its_result(tmp_path: Path, role: RoleSpec) -> None:
    steps = [Step(reply(calls=[call("echo", {"word": "x"})])), Step(reply("ok"), words("ok"))]
    rig = Rig(tmp_path, role, steps)

    await collect(rig.turn())

    assert rig.tool_events == [ToolEvent("c1", "echo", '{"word": "x"}', "echoed x", True)]


async def test_finish_carries_usage_and_the_roles_max_context(tmp_path: Path, role: RoleSpec) -> None:
    steps = [
        Step(reply(calls=[call("echo", {"word": "x"})], prompt=100, completion=10, cached=30)),
        Step(reply("ok", prompt=150, completion=5, cached=120), words("ok")),
    ]
    rig = Rig(tmp_path, role, steps)

    chunks = parse(await collect(rig.turn()))

    usage = {
        "prompt_tokens": 150,
        "completion_tokens": 15,
        "cached_tokens": 120,
        "max_context": role.max_context,
    }
    assert chunks[-1] == {"type": "finish", "messageMetadata": {"usage": usage}}
    assert rig.saved()[1].usage == usage
    assert rig.finished[0].usage == usage


async def test_the_engine_error_becomes_an_error_chunk_then_finish_and_the_hold_is_released(
    tmp_path: Path, role: RoleSpec
) -> None:
    steps = [Step(tokens=words("part|ial"), error=EngineError("engine fell over"))]
    rig = Rig(tmp_path, role, steps)

    events = await collect(rig.turn())

    chunks = parse(events)
    assert [c["type"] for c in chunks][-4:] == ["error", "text-end", "finish-step", "finish"]
    assert next(c for c in chunks if c["type"] == "error")["errorText"] == "engine fell over"
    assert events[-1] == DONE
    assert rig.holder.events[-1] == ("release", "chat-engine", "chat:t1")
    assert rig.finished[0].failed and not rig.finished[0].interrupted
    assert rig.saved()[1].parts == [{"type": "text", "text": "partial"}]


async def test_a_hold_that_fails_is_an_engine_error_chunk(tmp_path: Path, role: RoleSpec) -> None:
    rig = Rig(tmp_path, role, [])

    @asynccontextmanager
    async def refuse(engine: str, owner: str) -> AsyncIterator[None]:
        raise EngineError("chat-engine: serve did not answer")
        yield

    rig.holder.hold = refuse  # type: ignore[method-assign]

    chunks = parse(await collect(rig.turn()))

    assert [c["type"] for c in chunks] == ["start", "start-step", "error", "finish-step", "finish"]
    assert [m.role for m in rig.saved()] == ["user"]
    assert rig.engine.requests == []


class Reader:
    """Drains a turn's stream in a task of its own and says when `n` chunks have come."""

    def __init__(self, stream: AsyncIterator[str], n: int) -> None:
        self.got: list[str] = []
        self.reached = asyncio.Event()
        self._n = n
        self._stream = stream

    async def run(self) -> None:
        async for e in self._stream:
            self.got.append(e)
            if len(self.got) >= self._n:
                self.reached.set()


async def test_stop_cancels_generation_releases_the_hold_and_saves_the_partial(
    tmp_path: Path, role: RoleSpec
) -> None:
    rig = Rig(tmp_path, role, [Step(tokens=words("so|far"), gate=asyncio.Event())])  # hangs after its tokens
    handle = TurnHandle()
    reader = Reader(rig.turn(handle=handle), 5)
    task = asyncio.create_task(reader.run())
    await reader.reached.wait()

    handle.stop()
    await task

    assert rig.engine.cancelled == [rig.engine.requests[0].request_id]
    assert rig.holder.events == [("hold", "chat-engine", "chat:t1"), ("release", "chat-engine", "chat:t1")]
    user, assistant = rig.saved()
    assert user.parts == [{"type": "text", "text": "hello"}]
    assert assistant.parts == [{"type": "text", "text": "sofar"}]
    assert assistant.interrupted
    assert [c["type"] for c in parse(reader.got)][-3:] == ["text-end", "finish-step", "finish"]
    assert reader.got[-1] == DONE


async def test_on_finish_gets_the_final_text_and_an_interrupted_flag_on_stop(
    tmp_path: Path, role: RoleSpec
) -> None:
    rig = Rig(tmp_path, role, [Step(tokens=words("so|far"), gate=asyncio.Event())])
    handle = TurnHandle()
    reader = Reader(rig.turn(handle=handle), 5)
    task = asyncio.create_task(reader.run())
    await reader.reached.wait()

    handle.stop()
    await task

    [summary] = rig.finished
    assert (summary.text, summary.interrupted, summary.failed, summary.disconnected) == (
        "sofar",
        True,
        False,
        False,
    )
    assert (summary.thread_id, summary.owner, summary.kind, summary.title) == ("t1", "u1", "plain", "hello")


async def test_a_stop_before_the_turn_begins_saves_the_message_interrupted(
    tmp_path: Path, role: RoleSpec
) -> None:
    rig = Rig(tmp_path, role, [])
    handle = TurnHandle()
    handle.stop()

    events = await collect(rig.turn(handle=handle))

    assert events[-1] == DONE
    assert [m.role for m in rig.saved()] == ["user"]
    assert rig.finished[0].interrupted
    assert rig.engine.requests == []


async def test_a_stop_during_a_tool_keeps_the_call_with_an_error_result(
    tmp_path: Path, role: RoleSpec
) -> None:
    rig = Rig(tmp_path, role, [Step(reply(calls=[call("echo", {"word": "slow"})]))])
    rig.echo.block = asyncio.Event()
    handle = TurnHandle()
    reader = Reader(
        rig.turn(handle=handle), 7
    )  # start, start-step, tool-input-start/available ... the tool runs
    task = asyncio.create_task(reader.run())
    await rig.echo.started.wait()

    handle.stop()
    await task

    assistant = rig.saved()[1]
    assert assistant.interrupted
    assert assistant.parts[0]["state"] == "output-error"
    assert assistant.parts[0]["input"] == {"word": "slow"}
    assert "stopped" in assistant.parts[0]["errorText"]
    assert [m.role for m in history_messages(rig.saved())] == ["user", "assistant", "tool"]


async def test_an_anyio_cancel_scope_on_the_client_is_a_disconnect_not_a_stop(
    tmp_path: Path, role: RoleSpec
) -> None:
    """Starlette cancels the response task with an anyio scope when the client drops."""
    gate = asyncio.Event()
    rig = Rig(tmp_path, role, [Step(reply("the whole answer"), words("the |whole |answer"), gate=gate)])
    reader = Reader(rig.turn(), 4)

    async with anyio.create_task_group() as tg:
        tg.start_soon(reader.run)
        await reader.reached.wait()
        gate.set()
        tg.cancel_scope.cancel()

    [summary] = rig.finished  # the stream's task is only done once the turn is
    assert (summary.text, summary.interrupted, summary.failed, summary.disconnected) == (
        "the whole answer",
        False,
        False,
        True,
    )
    assert rig.engine.cancelled == []
    assert rig.saved()[1].parts == [{"type": "text", "text": "the whole answer"}]
    assert rig.holder.events[-1] == ("release", "chat-engine", "chat:t1")


async def test_a_failure_that_is_not_an_engine_error_is_an_error_chunk_and_finish(
    tmp_path: Path, role: RoleSpec
) -> None:
    items = [{"id": "1", "text": "plan", "status": "pending"}]
    steps = [Step(reply(calls=[call("todo", {"items": items})]))]
    rig = Rig(tmp_path, role, steps)

    def broken(thread_id: str, todos: list[dict[str, Any]]) -> None:
        raise RuntimeError("disk full")

    rig.store.set_todos = broken  # type: ignore[method-assign]

    events = await collect(rig.turn())

    chunks = parse(events)
    assert [c["type"] for c in chunks][-3:] == ["error", "finish-step", "finish"]
    assert next(c for c in chunks if c["type"] == "error")["errorText"] == "RuntimeError: disk full"
    assert events[-1] == DONE
    assert rig.holder.events[-1] == ("release", "chat-engine", "chat:t1")
    assert rig.finished[0].failed
    assert [m.role for m in rig.saved()] == ["user", "assistant"]


async def test_a_client_that_goes_away_does_not_stop_the_turn(tmp_path: Path, role: RoleSpec) -> None:
    gate = asyncio.Event()
    steps = [Step(reply("the whole answer"), words("the |whole |answer"), gate=gate)]
    rig = Rig(tmp_path, role, steps)
    stream = rig.turn()
    seen = [await anext(stream) for _ in range(4)]

    closer = asyncio.create_task(stream.aclose())  # type: ignore[attr-defined]
    await asyncio.sleep(0)
    gate.set()
    await closer

    assert len(seen) == 4
    [summary] = rig.finished
    assert (summary.text, summary.interrupted, summary.failed, summary.disconnected) == (
        "the whole answer",
        False,
        False,
        True,
    )
    assert rig.engine.cancelled == []
    assistant = rig.saved()[1]
    assert assistant.parts == [{"type": "text", "text": "the whole answer"}] and not assistant.interrupted
    assert rig.holder.events[-1] == ("release", "chat-engine", "chat:t1")


async def test_a_client_that_stays_is_not_disconnected(tmp_path: Path, role: RoleSpec) -> None:
    rig = Rig(tmp_path, role, [Step(reply("hi"), words("hi"))])

    await collect(rig.turn())

    assert rig.finished[0].disconnected is False and rig.finished[0].text == "hi"


async def test_the_user_message_is_saved_even_when_the_turn_fails(tmp_path: Path, role: RoleSpec) -> None:
    rig = Rig(tmp_path, role, [Step(error=EngineError("boom"))])

    await collect(rig.turn("tell me a story"))

    saved = rig.saved()
    assert [m.role for m in saved] == ["user"]
    assert saved[0].parts == [{"type": "text", "text": "tell me a story"}]
    assert rig.store.get("t1", "u1").title == "tell me a story"


def test_history_is_replayed_with_tool_calls_and_without_reasoning() -> None:
    stored = [
        StoredMessage("u1", "user", [{"type": "text", "text": "hi"}], 1.0),
        StoredMessage(
            "a1",
            "assistant",
            [
                {"type": "reasoning", "text": "thinking"},
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool-echo",
                    "toolCallId": "c1",
                    "input": {"word": "x"},
                    "state": "output-available",
                    "output": "echoed x",
                },
                {"type": "step-start"},
                {
                    "type": "tool-echo",
                    "toolCallId": "c2",
                    "rawInput": "{bad",
                    "state": "output-error",
                    "errorText": "no",
                },
                {"type": "tool-echo", "toolCallId": "c3", "input": {"word": "y"}, "state": "input-available"},
                {"type": "data-todos", "data": []},
                {"type": "text", "text": "Done."},
            ],
            2.0,
        ),
        StoredMessage("a2", "assistant", [{"type": "reasoning", "text": "only thoughts"}], 3.0),
    ]

    got = history_messages(stored)

    assert got == [
        Message(role="user", content="hi"),
        Message(role="assistant", content="Checking.", tool_calls=(ToolCall("c1", "echo", '{"word": "x"}'),)),
        Message(role="tool", content="echoed x", tool_call_id="c1", name="echo"),
        Message(role="assistant", content="", tool_calls=(ToolCall("c2", "echo", "{}"),)),
        Message(role="tool", content="no", tool_call_id="c2", name="echo"),
        Message(role="assistant", content="", tool_calls=(ToolCall("c3", "echo", '{"word": "y"}'),)),
        Message(
            role="tool", content="error: the turn ended before this call ran", tool_call_id="c3", name="echo"
        ),
        Message(role="assistant", content="Done."),
    ]


def test_a_user_message_with_resolved_attachments_goes_to_the_model_as_its_parts() -> None:
    stored = [
        StoredMessage("u1", "user", [{"type": "text", "text": "see"}, {"type": "airlie_upload"}], 1.0),
        StoredMessage("u2", "user", [{"type": "text", "text": "plain"}], 2.0),
    ]
    parts = (ContentPart("text", text="see"), ContentPart("image_url", url="data:image/png;base64,AA"))

    got = history_messages(stored, {"u1": parts})

    assert got == [Message(role="user", content="see", parts=parts), Message(role="user", content="plain")]


async def test_a_second_turn_sends_the_stored_history_to_the_model(tmp_path: Path, role: RoleSpec) -> None:
    rig = Rig(tmp_path, role, [Step(reply("one"), words("one")), Step(reply("two"), words("two"))])
    await collect(rig.turn("first"))
    rig.thread = rig.store.get("t1", "u1")
    rig.statuses = []

    user = StoredMessage("u-2", "user", [{"type": "text", "text": "second"}], 2.0)
    ctx = TurnContext("t1", "u1", "plain", USER, system_prompt="SYSTEM")
    await collect(
        run_turn(
            TurnRequest(rig.thread, user, ctx),
            engine=rig.engine,
            engine_name="chat-engine",
            holder=rig.holder,
            registry=rig.registry,
            todos=rig.todos,
            store=rig.store,
            hooks=rig.hooks,
            role=role,
            max_steps=4,
            clock=rig.clock,
            status=rig.status,
        )
    )

    sent = rig.engine.requests[1]
    assert [(m.role, m.content) for m in sent.messages] == [
        ("system", "SYSTEM"),
        ("user", "first"),
        ("assistant", "one"),
        ("user", "second"),
    ]
    assert (sent.role, sent.max_tokens, sent.thinking) == ("chat", role.max_tokens, role.thinking)
    assert [t.name for t in sent.tools or []] == ["echo", "todo"]


async def test_a_cut_off_tool_call_is_reported_to_the_model_not_dropped(
    tmp_path: Path, role: RoleSpec
) -> None:
    runaway = '{"word": "' + "x" * 60
    steps = [
        Step(reply("", calls=[call("echo", runaway)], finish="length")),
        Step(reply("shorter"), words("shorter")),
    ]
    rig = Rig(tmp_path, role, steps)

    await collect(rig.turn())

    told = rig.engine.requests[1].messages[-1]
    assert told.role == "tool" and told.content.startswith("error: the arguments of echo were cut off")
    assert rig.echo.ran == []


async def test_the_save_checks_the_owner_through_the_store(tmp_path: Path, role: RoleSpec) -> None:
    rig = Rig(tmp_path, role, [Step(reply("hi"), words("hi"))])
    rig.thread.owner = "someone-else"

    await collect(rig.turn())

    assert rig.saved() == []  # ThreadNotFound: nothing written for a thread the owner does not hold
    assert rig.finished[0].failed


async def test_a_stop_once_the_loop_has_returned_is_a_no_op(tmp_path: Path, role: RoleSpec) -> None:
    """A Stop that lands while the turn saves and notifies must not cut the save or the hook short."""
    rig = Rig(tmp_path, role, [Step(reply("done"), words("done"))])
    handle = TurnHandle()
    reached: list[str] = []

    async def stop_then_work(summary: TurnSummary) -> None:
        handle.stop()
        await asyncio.sleep(0)  # a cancel would land here
        reached.append("finished")

    rig.hooks.on("on_finish", stop_then_work)

    events = await collect(rig.turn(handle=handle))

    assert reached == ["finished"]
    assert events[-1] == DONE
    assert not rig.saved()[1].interrupted
    assert rig.holder.events[-1][0] == "release"
