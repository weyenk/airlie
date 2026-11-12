"""One chat turn as a stream: the server-side tool loop on the `chat` role, written out as AI SDK UI message
chunks while it runs. The loop is `tools/loop.py`'s shape (one tool call per reply, a bounded number of
calls, then one last reply with no tools offered) but it streams and reports each step, which that loop
cannot.

The turn runs in a task of its own and the generator only relays what it puts on a queue. A client that goes
away (the generator is closed) does not stop the turn: it finishes, is saved, and `on_finish` hears
`disconnected`. A turn that is cancelled is stopped: generation cancelled, the partial reply saved
`interrupted`. Either way the engine hold is released and the user's message is saved."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, suppress
from dataclasses import asdict, dataclass, field, replace
from typing import Any

import anyio

from airlie.chat.hooks import Hooks, ToolEvent, TurnContext, TurnSummary
from airlie.chat.stream import StreamWriter
from airlie.chat.threads import StoredMessage, Thread, ThreadStore
from airlie.chat.tools import TodoList
from airlie.config.schema import RoleSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import ContentPart, EngineError, GenRequest, GenResult, Message, TokenEvent, ToolCall
from airlie.manager.client import Holder
from airlie.manager.core import Status
from airlie.prompts import text
from airlie.serve.bridge.clock import Clock
from airlie.tools.jsonargs import history_arguments, parse_tool_args
from airlie.tools.registry import ToolRegistry

log = logging.getLogger("airlie.chat.turn")

STATUS_EVERY_S = 1.0
INTERRUPTED_RESULT = "error: the turn ended before this call ran"
STOPPED_RESULT = "error: the turn was stopped before this call finished"
TOOL_PREFIX = "tool-"


class TurnHandle:
    """The way to Stop a turn: a client going away does not stop it, only this does. Made before the turn
    starts; `stop()` may come at any time, also before the stream's first chunk."""

    def __init__(self) -> None:
        self.stopped = False
        self._driver: asyncio.Task[None] | None = None
        self._settled = False

    def stop(self) -> None:
        """A no-op once the turn's loop has returned: what remains (the save, the hold's release, `on_finish`)
        is cleanup that a cancel would cut short."""
        if self._settled:
            return
        self.stopped = True
        if self._driver is not None:
            self._driver.cancel()

    def settle(self) -> None:
        self._settled = True


@dataclass
class TurnRequest:
    thread: Thread
    user_message: StoredMessage
    ctx: TurnContext
    user_parts: Mapping[str, tuple[ContentPart, ...]] = field(default_factory=dict)
    """Per user message id: what the model gets for a message that carries attachments (text, pictures and
    video frames, as `serve.uploads.resolve` made them). A message with no entry goes as its text."""


def _user_message(message: StoredMessage, user_parts: Mapping[str, tuple[ContentPart, ...]]) -> Message:
    parts = user_parts.get(message.id)
    if parts:
        return Message(role="user", content="\n".join(p.text for p in parts if p.type == "text"), parts=parts)
    return Message(role="user", content=_user_text(message))


def _user_text(message: StoredMessage) -> str:
    return "\n".join(str(p.get("text") or "") for p in message.parts if p.get("type") == "text")


def _tool_part_call(part: dict[str, Any]) -> ToolCall:
    raw = part.get("input") if "input" in part else part.get("rawInput")
    arguments = raw if isinstance(raw, str) else json.dumps(raw if raw is not None else {})
    return ToolCall(
        str(part["toolCallId"]), str(part["type"])[len(TOOL_PREFIX) :], history_arguments(arguments)
    )


def history_messages(
    messages: list[StoredMessage], user_parts: Mapping[str, tuple[ContentPart, ...]] | None = None
) -> list[Message]:
    """Stored messages as the model's history: a user message's text; an assistant message's text and its
    tool calls (each with its result, or a note that it never ran). Reasoning and data parts stay behind."""
    out: list[Message] = []
    for m in messages:
        if m.role == "user":
            msg = _user_message(m, user_parts or {})
            if msg.content or msg.parts:
                out.append(msg)
            continue
        buffer = ""
        for part in m.parts:
            kind = str(part.get("type") or "")
            if kind == "text":
                buffer += str(part.get("text") or "")
            elif kind.startswith(TOOL_PREFIX) and "toolCallId" in part:
                call = _tool_part_call(part)
                result = (
                    part.get("output") if part.get("state") == "output-available" else part.get("errorText")
                )
                out.append(Message(role="assistant", content=buffer, tool_calls=(call,)))
                out.append(
                    Message(
                        role="tool",
                        content=INTERRUPTED_RESULT if result is None else str(result),
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                buffer = ""
        if buffer.strip():
            out.append(Message(role="assistant", content=buffer))
    return out


@dataclass
class _Run:
    """What one turn works with and has so far."""

    req: TurnRequest
    engine: Engine
    engine_name: str
    holder: Holder
    registry: ToolRegistry
    todos: TodoList
    store: ThreadStore
    hooks: Hooks
    role: RoleSpec
    max_steps: int
    clock: Clock
    status: Callable[[], list[Status]]
    out: asyncio.Queue[str | None]
    message_id: str = field(default_factory=lambda: f"msg-{uuid.uuid4().hex}")
    gone: bool = False  # the client closed the stream; keep going, stop writing
    parts: list[dict[str, Any]] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)  # what the model sees, grown by each step
    step_text: str = ""
    usage: dict[str, Any] | None = None
    completion_tokens: int = 0
    handle: TurnHandle | None = None
    w: StreamWriter = field(init=False)

    def __post_init__(self) -> None:
        self.w = StreamWriter(self.message_id)

    def emit(self, chunks: list[str]) -> None:
        """Queue chunks for the client. The queue is unbounded on purpose: the turn is bounded (max_steps
        replies of at most max_tokens) and the chunks are small, so a slow reader costs a little memory and
        never holds the engine; once the client is gone nothing is queued."""
        if not self.gone:
            for c in chunks:
                self.out.put_nowait(c)

    def add_delta(self, kind: str, delta: str) -> None:
        if self.parts and self.parts[-1].get("type") == kind and self.parts[-1].get("open"):
            self.parts[-1]["text"] += delta
        else:
            self.close_part()
            self.parts.append({"type": kind, "text": delta, "open": True})

    def close_part(self) -> None:
        if self.parts and self.parts[-1].get("open"):
            del self.parts[-1]["open"]

    def stored_parts(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in p.items() if k != "open"} for p in self.parts]

    def finish_usage(self) -> dict[str, Any]:
        last = self.usage or {}
        return {
            "prompt_tokens": last.get("prompt_tokens", 0),
            "completion_tokens": self.completion_tokens,
            "cached_tokens": last.get("cached_tokens", 0),
            "max_context": self.role.max_context,
        }

    def messages_for_model(self) -> list[Message]:
        thread, user = self.req.thread, self.req.user_message
        return [
            Message(role="system", content=self.req.ctx.system_prompt),
            *history_messages(thread.messages, self.req.user_parts),
            _user_message(user, self.req.user_parts),
        ]

    # --- the hold -------------------------------------------------------------------------------------------

    async def enter_hold(self, stack: AsyncExitStack) -> None:
        """Take the engine as `chat:{thread}`; while it is not ready, relay the manager's status."""
        owner = f"chat:{self.req.thread.id}"
        enter = asyncio.create_task(stack.enter_async_context(self.holder.hold(self.engine_name, owner)))
        seen: tuple[str, str] | None = None
        try:
            await asyncio.sleep(0)  # an engine already up is held at once: no status to show
            while not enter.done():
                mine = [s for s in self.status() if s.name == self.engine_name]
                if mine and (mine[0].state, mine[0].detail) != seen:
                    s = mine[0]
                    seen = (s.state, s.detail)
                    progress = None if s.progress is None else asdict(s.progress)
                    payload = {"engine": s.name, "state": s.state, "detail": s.detail, "progress": progress}
                    self.emit(self.w.data("engine", payload, transient=True))
                sleeper = asyncio.create_task(self.clock.sleep(STATUS_EVERY_S))
                try:
                    await asyncio.wait({enter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    sleeper.cancel()
            await enter  # a failed hold raises here
        finally:
            if not enter.done():
                enter.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await enter

    # --- one generation -------------------------------------------------------------------------------------

    async def generate(self, gen_req: GenRequest) -> GenResult:
        """Run one generation, relaying its tokens as they arrive."""
        tokens: asyncio.Queue[TokenEvent | None] = asyncio.Queue()
        self.step_text = ""
        streamed = {"text": False, "reasoning": False}
        task = asyncio.create_task(self.engine.generate(gen_req, tokens.put_nowait))
        task.add_done_callback(lambda _t: tokens.put_nowait(None))
        try:
            while (ev := await tokens.get()) is not None:
                self.relay(ev)
                streamed["reasoning" if ev.reasoning else "text"] = True
            res = task.result()
            if res.reasoning and not streamed["reasoning"]:
                self.relay(TokenEvent(res.reasoning, reasoning=True))
            if res.text and not streamed["text"]:
                self.relay(TokenEvent(res.text))
            return res
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
                try:
                    await self.engine.cancel(gen_req.request_id)
                except Exception:
                    log.exception("cancelling generation %s failed", gen_req.request_id)

    def relay(self, ev: TokenEvent) -> None:
        if ev.reasoning:
            self.add_delta("reasoning", ev.text)
            self.emit(self.w.reasoning(ev.text))
        else:
            self.add_delta("text", ev.text)
            self.step_text += ev.text
            self.emit(self.w.text(ev.text))

    def note_result(self, res: GenResult) -> None:
        self.completion_tokens += res.completion_tokens
        self.usage = {"prompt_tokens": res.prompt_tokens, "cached_tokens": res.cached_tokens or 0}

    # --- one tool call --------------------------------------------------------------------------------------

    async def run_tool(self, call: ToolCall) -> None:
        owner = self.req.thread.owner
        try:
            parse_tool_args(call.arguments)
            parsed = True
        except ValueError:
            parsed = False
        self.close_part()
        part: dict[str, Any] = {"type": f"{TOOL_PREFIX}{call.name}", "toolCallId": call.id}
        if parsed:
            part["input"] = json.loads(history_arguments(call.arguments))
            self.emit(self.w.tool_call(call.id, call.name, history_arguments(call.arguments)))
        else:
            part["rawInput"] = call.arguments
        try:
            result = await self.registry.call(call)  # never runs a call whose arguments do not parse
        except asyncio.CancelledError:  # stopped mid-call: history keeps the call the client saw
            part.update(state="output-error", errorText=STOPPED_RESULT)
            self.parts.append(part)
            raise
        if parsed:
            self.emit(self.w.tool_result(call.id, result.output, result.ok))
        else:
            self.emit(self.w.tool_input_error(call.id, call.name, call.arguments, result.output))
        if result.ok:
            part.update(state="output-available", output=result.output)
        else:
            part.update(state="output-error", errorText=result.output)
        self.parts.append(part)
        await self.hooks.run(
            "after_tool",
            self.req.ctx,
            ToolEvent(call.id, call.name, call.arguments, result.output, result.ok),
        )
        if call.name == "todo" and result.ok:
            items = [asdict(i) for i in self.todos.items()]
            self.emit(self.w.data("todos", items))
            self.store.get(self.req.thread.id, owner)
            self.store.set_todos(self.req.thread.id, items)
        self.messages.append(
            Message(role="tool", content=result.output, tool_call_id=call.id, name=call.name)
        )

    # --- the loop -------------------------------------------------------------------------------------------

    async def loop(self) -> None:
        self.messages = self.messages_for_model()
        schemas = self.registry.schemas() or None
        role = self.role
        for _ in range(self.max_steps):
            gen_req = GenRequest(
                role="chat",
                messages=list(self.messages),
                sampling=role.sampling,
                max_tokens=role.max_tokens,
                thinking=role.thinking,
                tools=schemas,
            )
            res = await self.generate(gen_req)
            self.note_result(res)
            if not res.tool_calls or schemas is None:
                return
            call = res.tool_calls[0]  # one action per reply, as the team loop does
            replay = replace(call, arguments=history_arguments(call.arguments))
            self.messages.append(Message(role="assistant", content=res.text, tool_calls=(replay,)))
            await self.run_tool(call)
            self.emit(self.w.next_step())
            self.parts.append({"type": "step-start"})
        self.messages.append(Message(role="user", content=text("no-more-tools")))
        last = GenRequest(
            role="chat",
            messages=list(self.messages),
            sampling=role.sampling,
            max_tokens=role.max_tokens,
            thinking=role.thinking,
            tools=None,
        )
        self.note_result(await self.generate(last))

    # --- the turn -------------------------------------------------------------------------------------------

    def settle(self) -> None:
        if self.handle is not None:
            self.handle.settle()

    async def drive(self) -> None:
        interrupted = failed = False
        stack = AsyncExitStack()
        try:
            if self.handle is not None and self.handle.stopped:  # a Stop that came before the turn began
                raise asyncio.CancelledError
            self.emit(self.w.start())
            await self.enter_hold(stack)
            await self.loop()
            self.settle()
            self.emit(self.w.finish(self.finish_usage()))
        except EngineError as e:
            failed = True
            self.emit(self.w.error(str(e)))
            self.emit(self.w.finish(self.finish_usage()))
        except asyncio.CancelledError:  # Stop (or the loop shutting down): the client still gets an ending
            interrupted = True
            self.emit(self.w.finish(self.finish_usage()))
        except Exception as e:  # a bug or a failed save is a chunk, not a stream that just stops
            log.exception("chat turn %s failed", self.req.thread.id)
            failed = True
            self.emit(self.w.error(f"{type(e).__name__}: {e}"))
            self.emit(self.w.finish(self.finish_usage()))
        finally:
            self.settle()  # a Stop from here on would only cut the save or the hold's release short
            try:
                await self.persist(interrupted=interrupted, failed=failed)
            finally:
                await stack.aclose()
                self.out.put_nowait(None)

    async def persist(self, *, interrupted: bool, failed: bool) -> None:
        req = self.req
        thread, owner = req.thread, req.thread.owner
        self.close_part()
        parts = self.stored_parts()
        usage = None if self.usage is None else self.finish_usage()
        title = thread.title
        try:
            self.store.get(thread.id, owner)
            self.store.append(thread.id, req.user_message)
            if any(p.get("type") != "step-start" for p in parts):
                created = self.clock.wall()
                assistant = StoredMessage(self.message_id, "assistant", parts, created, usage, interrupted)
                self.store.append(thread.id, assistant)
            title = self.store.get(thread.id, owner).title
        except Exception:
            log.exception("saving chat thread %s failed", thread.id)
            failed = True
        summary = TurnSummary(
            thread_id=thread.id,
            owner=owner,
            kind=thread.kind,
            title=title,
            text=self.step_text,
            usage=self.finish_usage(),
            interrupted=interrupted,
            failed=failed,
            disconnected=self.gone,
        )
        await self.hooks.run("on_finish", summary)


async def run_turn(
    req: TurnRequest,
    *,
    engine: Engine,
    engine_name: str,
    holder: Holder,
    registry: ToolRegistry,
    todos: TodoList,
    store: ThreadStore,
    hooks: Hooks,
    role: RoleSpec,
    max_steps: int,
    clock: Clock,
    status: Callable[[], list[Status]],
    handle: TurnHandle | None = None,
) -> AsyncIterator[str]:
    """The turn's SSE strings. `req.ctx` is already prepared (the route ran `session_start` and `before_turn`
    first, so a refusal is an HTTP error before the first byte)."""
    out: asyncio.Queue[str | None] = asyncio.Queue()
    run = _Run(
        req, engine, engine_name, holder, registry, todos, store, hooks, role, max_steps, clock, status, out
    )
    run.handle = handle
    driver = asyncio.create_task(run.drive())
    if handle is not None:
        handle._driver = driver
    try:
        while (chunk := await out.get()) is not None:
            yield chunk
        await driver
    except (GeneratorExit, asyncio.CancelledError):
        # The client is gone (a closed generator, or the server cancelling the response as the connection
        # drops). The turn is not: it finishes and is saved, and `on_finish` hears `disconnected`. Only
        # `handle.stop()` cancels it. The wait is shielded so that the caller's cleanup (releasing the
        # thread, stopping the engine client) comes after the turn, not under it.
        run.gone = True
        with anyio.CancelScope(shield=True):
            try:
                await driver
            except (asyncio.CancelledError, Exception):
                log.exception("chat turn %s ended badly after the client went away", req.thread.id)
        raise
