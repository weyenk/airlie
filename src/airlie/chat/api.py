"""The chat routes: `POST /v1/airlie/chat/turn` (one turn as an AI SDK UI message stream) and the thread
routes. The server keeps the conversation (chat.threads), so a turn carries only the new user message: the
client's own message list is read for its last user message and the rest of it is ignored, as is any tool
list it sends. `app.py` builds what a turn needs and wires the router."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import Any

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from airlie.auth.principal import Principal
from airlie.chat.hooks import Hooks, TurnContext, TurnRefused
from airlie.chat.kinds import ChatKind, KindUnavailable, registry_for
from airlie.chat.stream import DONE, STREAM_HEADERS, sse
from airlie.chat.superpowers import SuperpowersLibrary
from airlie.chat.threads import StoredMessage, Thread, ThreadNotFound, ThreadStore
from airlie.chat.tools import TodoList
from airlie.chat.turn import TurnHandle, TurnRequest, run_turn
from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.engine.types import ContentPart
from airlie.manager.client import InProcessHolder
from airlie.manager.core import Manager
from airlie.serve.bridge.clock import Clock, RealClock
from airlie.serve.uploads import TooManyAttachments
from airlie.skills.library import Skill
from airlie.tools.protocol import Tool, ToolContext

log = logging.getLogger("airlie.chat.api")

STOP_WAIT_S = 30.0
"""The most a Stop waits for the cancelled turn to finish saving."""
STORED_PART_TYPES = ("text", "airlie_upload")
"""The parts of a user message the server keeps and the model can take; any other part is dropped."""
Attach = Callable[[Principal, list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]
"""Wire messages (`content` lists with `airlie_upload` parts) resolved for the chat model, in order."""


class UIMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    role: str
    parts: list[dict[str, Any]] = Field(default_factory=list)


UPLOAD_URL = re.compile(r"^(?:/v1/airlie/uploads/|https?://[^/?#]+/v1/airlie/uploads/)([0-9a-f]{32})$")
"""The url of an upload as a client gives it back: the AI SDK's `file` part carries the url it was handed."""


def file_part_upload(part: dict[str, Any]) -> dict[str, Any] | None:
    """An AI SDK `file` part naming one of airlie's uploads as the `airlie_upload` part it stands for; None
    for any other url (a remote address or a data URL is not something the server will fetch or trust)."""
    found = UPLOAD_URL.match(str(part.get("url") or ""))
    if found is None:
        return None
    out: dict[str, Any] = {"type": "airlie_upload", "upload": found.group(1)}
    if part.get("filename"):
        out["name"] = str(part["filename"])
    return out


def kept_parts(message: UIMessage) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in message.parts:
        if p.get("type") == "file":
            converted = file_part_upload(p)
            if converted is not None:
                out.append(converted)
        elif p.get("type") in STORED_PART_TYPES:
            out.append(p)
    return out


def last_user(messages: list[UIMessage]) -> UIMessage | None:
    """The newest user message, when it has text or an attachment to say."""
    for m in reversed(messages):
        if m.role == "user":
            says = any(p.get("type") == "airlie_upload" or p.get("text") for p in kept_parts(m))
            return m if says else None
    return None


class TurnBody(BaseModel):
    """The AI SDK's request body plus `kind` and `skill`; unknown fields (a `tools` list among them) are
    ignored."""

    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=128)
    kind: ChatKind
    messages: list[UIMessage]
    skill: str | None = None

    @field_validator("messages")
    @classmethod
    def _a_user_message(cls, messages: list[UIMessage]) -> list[UIMessage]:
        newest = last_user(messages)
        if newest is None:
            raise ValueError("messages needs a user message with text or an attachment")
        for p in newest.parts:
            if p.get("type") == "file" and file_part_upload(p) is None:
                raise ValueError("a file part must be one of airlie's uploads (/v1/airlie/uploads/<id>)")
        return messages


def content_parts(resolved: list[dict[str, Any]]) -> tuple[ContentPart, ...]:
    out: list[ContentPart] = []
    for p in resolved:
        if p.get("type") == "text":
            out.append(ContentPart("text", text=str(p.get("text") or "")))
        elif p.get("type") == "image_url":
            out.append(ContentPart("image_url", url=str((p.get("image_url") or {}).get("url") or "")))
    return tuple(out)


async def attachment_parts(
    attach: Attach, principal: Principal, thread: Thread, new: StoredMessage
) -> dict[str, tuple[ContentPart, ...]]:
    """What the model gets for each user message that carries attachments, the thread's and the new one,
    resolved together so the history caps (the newest pictures go as pixels) hold as in the old route."""
    users = [m for m in (*thread.messages, new) if m.role == "user"]
    if not any(p.get("type") == "airlie_upload" for m in users for p in m.parts):
        return {}
    wire = [{"role": "user", "content": list(m.parts)} for m in users]
    resolved = await attach(principal, wire)
    return {m.id: content_parts(r["content"]) for m, r in zip(users, resolved, strict=True)}


def error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@dataclass
class Running:
    """One turn in flight: the handle that stops it and an event set once the turn's cleanup has run."""

    handle: TurnHandle = field(default_factory=TurnHandle)
    done: asyncio.Event = field(default_factory=asyncio.Event)


class Busy:
    """The threads with a turn in flight, in memory (a restart starts empty). The check and the add have no
    await between them, so on the event loop they are one step."""

    def __init__(self) -> None:
        self._turns: dict[str, Running] = {}

    def take(self, thread_id: str) -> Running | None:
        if thread_id in self._turns:
            return None
        self._turns[thread_id] = running = Running()
        return running

    def __contains__(self, thread_id: str) -> bool:
        return thread_id in self._turns

    def get(self, thread_id: str) -> Running | None:
        return self._turns.get(thread_id)

    def stop_all(self) -> None:
        """Stop every turn in flight (the server is going down): each saves its partial reply as interrupted
        and frees its thread, so a hung engine cannot hold the shutdown."""
        for turn in list(self._turns.values()):
            turn.handle.stop()

    def release(self, thread_id: str) -> None:
        turn = self._turns.pop(thread_id, None)
        if turn is not None:
            turn.done.set()


class TurnStream(StreamingResponse):
    """The turn's response. `cleanup` runs however the response ends, also when the body never started (a
    client that dropped as the headers went out); a body that started runs it itself once the turn is over."""

    def __init__(
        self,
        content: AsyncIterator[str],
        cleanup: Callable[[], Awaitable[None]],
        started: list[bool],
        **kw: Any,
    ) -> None:
        super().__init__(content, **kw)
        self._cleanup = cleanup
        self._started = started

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if not self._started[0]:
                await self._cleanup()


def chat_router(
    cfg: AirlieConfig,
    *,
    threads: ThreadStore,
    library: SuperpowersLibrary | None,
    hooks: Hooks,
    web: Mapping[str, Tool],
    chat_skills: Mapping[str, Skill] | None = None,
    library_error: str | None = None,
    manager: Manager,
    attach: Attach,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
    busy: Busy | None = None,
) -> APIRouter:
    router = APIRouter()
    busy = busy if busy is not None else Busy()
    clock = clock or RealClock()

    def principal_of(request: Request) -> Principal | None:
        return getattr(request.state, "principal", None)

    @router.post("/v1/airlie/chat/turn")
    async def chat_turn(body: TurnBody, request: Request) -> Any:
        principal = principal_of(request)
        if principal is None:
            return error("not signed in", 401)
        if "chat" not in cfg.roles:
            return error("the chat role is not configured", 503)
        if library_error is not None:
            return error(f"chat is misconfigured: {library_error}", 503)
        try:
            engine_name = cfg.engine_for_role("chat")
        except Exception as e:  # no engine serves the chat role's model
            return error(f"the chat role has no engine: {e}", 503)
        try:
            thread = threads.open(body.id, principal.user_id, body.kind)
        except ThreadNotFound:
            return error("no such thread", 404)
        turn = busy.take(thread.id)
        if turn is None:
            return error("this thread has a turn running; wait for it to finish", 409)
        engines: list[Engine] = []
        released = False

        async def cleanup() -> None:
            """Free the thread and the engine client, once: the body or the response, whichever ends first."""
            nonlocal released
            if released:
                return
            released = True
            try:
                for engine in engines:
                    await engine.stop()
            finally:
                busy.release(thread.id)

        try:
            response = await start_turn(body, principal, thread, engine_name, engines, cleanup, turn)
        except BaseException:
            await cleanup()
            raise
        if not isinstance(response, TurnStream):  # refused: nothing streams
            if not thread.messages:  # a thread this request opened leaves no empty row behind
                threads.delete(thread.id, principal.user_id)
            await cleanup()
        return response

    async def start_turn(
        body: TurnBody,
        principal: Principal,
        thread: Thread,
        engine_name: str,
        engines: list[Engine],
        cleanup: Callable[[], Awaitable[None]],
        turn: Running,
    ) -> Response:
        message = last_user(body.messages)
        assert message is not None  # the body's validator
        parts = kept_parts(message)
        taken = {m.id for m in thread.messages}  # a retry resends its message's id: keep ids unique
        mid = message.id if message.id and message.id not in taken else f"msg-{uuid.uuid4().hex}"
        stored = StoredMessage(mid, "user", parts, clock.wall())
        ctx = TurnContext(
            thread.id,
            thread.owner,
            thread.kind,
            principal,
            attachments=sum(p.get("type") == "airlie_upload" for p in parts),
            skill=body.skill,
        )
        try:
            await hooks.run("session_start", ctx)
            await hooks.run("before_turn", ctx)
        except TurnRefused as e:
            return error(e.message, e.status)
        if not ctx.system_prompt:  # never run a turn without its instructions
            reason = ctx.prompt_error or "the system prompt is empty"
            return error(f"chat is misconfigured: {reason}", 503)
        todos = TodoList.load(thread.todos)
        tool_ctx = ToolContext(
            cfg.observability.state_dir.expanduser(),
            cfg.tools.shell_timeout_s,
            cfg.tools.output_cap_chars,
            role="chat",
        )
        try:
            registry = registry_for(
                thread.kind,
                cfg,
                library=library,
                todos=todos,
                ctx=tool_ctx,
                web=web,
                skills_extra=chat_skills,
            )
        except KindUnavailable:
            return error("repository chats arrive with plan 4", 400)
        try:
            user_parts = await attachment_parts(attach, principal, thread, stored)
        except TooManyAttachments as e:
            return error(str(e), 400)
        from airlie.cli import _http_engine  # imported here: the cli imports serve

        engine = _http_engine(cfg, engine_name, transport=transport, manager=manager)
        engines.append(engine)
        await engine.start()
        started = [False]

        async def stream() -> AsyncIterator[str]:
            started[0] = True
            try:
                async for chunk in run_turn(
                    TurnRequest(thread, stored, ctx, user_parts),
                    engine=engine,
                    engine_name=engine_name,
                    holder=InProcessHolder(manager),
                    registry=registry,
                    todos=todos,
                    store=threads,
                    hooks=hooks,
                    role=cfg.roles["chat"],
                    max_steps=cfg.chat.max_steps,
                    clock=clock,
                    status=manager.status,
                    handle=turn.handle,
                ):
                    yield chunk
            except Exception as e:  # after the first byte a failure is a chunk, not a status
                log.exception("chat turn %s failed", thread.id)
                yield sse({"type": "error", "errorText": f"{type(e).__name__}: {e}"})
                yield DONE
            finally:
                with anyio.CancelScope(
                    shield=True
                ):  # also when the response was cancelled as the client left
                    await cleanup()

        return TurnStream(stream(), cleanup, started, media_type="text/event-stream", headers=STREAM_HEADERS)

    @router.get("/v1/airlie/chat/threads")
    async def list_threads(request: Request, kind: ChatKind | None = None) -> Any:
        principal = principal_of(request)
        if principal is None:
            return error("not signed in", 401)
        return [asdict(t) for t in threads.list(principal.user_id, kind)]

    @router.get("/v1/airlie/chat/threads/{thread_id}")
    async def get_thread(thread_id: str, request: Request) -> Any:
        principal = principal_of(request)
        if principal is None:
            return error("not signed in", 401)
        try:
            t = threads.get(thread_id, principal.user_id)
        except ThreadNotFound:
            return error("no such thread", 404)
        return {
            "id": t.id,
            "title": t.title,
            "kind": t.kind,
            "created": t.created,
            "updated": t.updated,
            "todos": t.todos,
            "messages": [asdict(m) for m in t.messages],
        }

    @router.post("/v1/airlie/chat/threads/{thread_id}/stop")
    async def stop_thread(thread_id: str, request: Request) -> Any:
        """Stop the thread's turn: generation is cancelled and the partial reply saved as interrupted. A
        client going away is not a Stop (the turn finishes without it), so this is its own call. 204 also
        when no turn is running, so a Stop that races the turn's end is not an error."""
        principal = principal_of(request)
        if principal is None:
            return error("not signed in", 401)
        try:
            threads.get(thread_id, principal.user_id)
        except ThreadNotFound:
            return error("no such thread", 404)
        turn = busy.get(thread_id)
        if turn is not None:
            turn.handle.stop()
            with suppress(TimeoutError):  # a turn whose body never began is freed by its response
                await asyncio.wait_for(turn.done.wait(), STOP_WAIT_S)
        return Response(status_code=204)

    @router.delete("/v1/airlie/chat/threads/{thread_id}")
    async def delete_thread(thread_id: str, request: Request) -> Any:
        principal = principal_of(request)
        if principal is None:
            return error("not signed in", 401)
        if thread_id in busy:
            return error("this thread has a turn running; wait for it to finish", 409)
        if not threads.delete(thread_id, principal.user_id):
            return error("no such thread", 404)
        return Response(status_code=204)

    return router
