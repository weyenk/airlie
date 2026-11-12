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
from functools import partial
from typing import Annotated, Any

import anyio
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from airlie.auth.principal import Principal
from airlie.boxes.protocol import BoxUnavailable
from airlie.chat.confirm import Answered, Approval, Confirmations, answered_note, before_tool, guard
from airlie.chat.hooks import Hooks, TurnContext, TurnRefused
from airlie.chat.kinds import ChatKind, KindUnavailable, registry_for
from airlie.chat.repo_session import RepoSessions, session_branch, valid_repo
from airlie.chat.repo_tools import RepoChat
from airlie.chat.stream import DONE, STREAM_HEADERS, sse
from airlie.chat.superpowers import SuperpowersLibrary
from airlie.chat.threads import (
    StoredMessage,
    Thread,
    ThreadNotFound,
    ThreadStore,
    ThreadSummary,
    mark_answered,
)
from airlie.chat.tools import TodoList
from airlie.chat.turn import TurnHandle, TurnRequest, answered_calls, run_turn
from airlie.config import AirlieConfig
from airlie.engine.protocol import Engine
from airlie.engine.types import ContentPart, EngineError
from airlie.manager.client import InProcessHolder
from airlie.manager.core import Manager
from airlie.serve.bridge.clock import Clock, RealClock
from airlie.serve.uploads import TooManyAttachments
from airlie.skills.library import Skill
from airlie.tools.protocol import Tool, ToolContext

log = logging.getLogger("airlie.chat.api")
MISCONFIGURED = "chat is misconfigured; see the server log or run airlie doctor"
"""What a client is told when chat cannot serve: the reason names server paths, so it goes to the log only."""

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


def _owner_name(repo: str) -> str:
    if not valid_repo(repo):
        raise ValueError("repo must be owner/name")
    return repo


def _a_ref(ref: str) -> str:
    if not ref or ref.startswith("-"):
        raise ValueError("base_ref must be a branch name or a commit")
    return ref


RepoName = Annotated[str, Field(max_length=200), AfterValidator(_owner_name)]
BaseRef = Annotated[str, Field(max_length=200), AfterValidator(_a_ref)]


class TurnBody(BaseModel):
    """The AI SDK's request body plus `kind`, `skill` and, for a repository chat, `repo` (owner/name, named
    when the chat is first opened), `base_ref` (a branch or commit; default main) and `approvals` (the
    person's answers to the permission requests the stream carried); unknown fields (a `tools` list among
    them) are ignored."""

    model_config = ConfigDict(extra="ignore")
    id: str = Field(min_length=1, max_length=128)
    kind: ChatKind
    messages: list[UIMessage]
    skill: str | None = None
    repo: RepoName | None = None
    base_ref: BaseRef | None = None
    approvals: list[Approval] = Field(default_factory=list, max_length=50)

    @property
    def resumes(self) -> bool:
        """The turn carries the person's answers and no new message of theirs (the AI SDK resends the
        thread's last message, the assistant's, or nothing): it resumes on the answers alone."""
        return bool(self.approvals) and (not self.messages or self.messages[-1].role != "user")

    @model_validator(mode="after")
    def _a_user_message(self) -> TurnBody:
        if self.resumes:
            return self
        newest = last_user(self.messages)
        if newest is None:
            raise ValueError("messages needs a user message with text or an attachment, or approvals")
        for p in newest.parts:
            if p.get("type") == "file" and file_part_upload(p) is None:
                raise ValueError("a file part must be one of airlie's uploads (/v1/airlie/uploads/<id>)")
        return self


class NewThread(BaseModel):
    """`POST /v1/airlie/chat/threads`: a chat made before its first message, so the panel lists it at once. A
    repository chat names its repository and, optionally, the branch or commit its session starts from."""

    model_config = ConfigDict(extra="forbid")
    kind: ChatKind
    repo: RepoName | None = None
    base_ref: BaseRef | None = None

    @model_validator(mode="after")
    def _repo_for_its_kind(self) -> NewThread:
        if self.kind == "repository" and self.repo is None:
            raise ValueError("a repository chat names its repository: repo is owner/name")
        if self.kind == "plain" and (self.repo is not None or self.base_ref is not None):
            raise ValueError("a plain chat has no repository")
        return self


def branch_of(thread_id: str, kind: ChatKind) -> str | None:
    return session_branch(thread_id) if kind == "repository" else None


def summary_json(t: ThreadSummary, running: bool) -> dict[str, Any]:
    """A thread in the list; `running` says a turn is in flight (a client that lost its stream waits for the
    chat hint at the turn's end, then reads the saved reply)."""
    return {**asdict(t), "branch": branch_of(t.id, t.kind), "running": running}


def content_parts(resolved: list[dict[str, Any]]) -> tuple[ContentPart, ...]:
    out: list[ContentPart] = []
    for p in resolved:
        if p.get("type") == "text":
            out.append(ContentPart("text", text=str(p.get("text") or "")))
        elif p.get("type") == "image_url":
            out.append(ContentPart("image_url", url=str((p.get("image_url") or {}).get("url") or "")))
    return tuple(out)


async def attachment_parts(
    attach: Attach, principal: Principal, thread: Thread, new: StoredMessage | None
) -> dict[str, tuple[ContentPart, ...]]:
    """What the model gets for each user message that carries attachments, the thread's and the new one,
    resolved together so the history caps (the newest pictures go as pixels) hold as in the old route."""
    added = (new,) if new is not None else ()
    users = [m for m in (*thread.messages, *added) if m.role == "user"]
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
        self._closing = False

    def take(self, thread_id: str) -> Running | None:
        if thread_id in self._turns:
            return None
        self._turns[thread_id] = running = Running()
        if self._closing:  # accepted while the server goes down: it ends as stopped, having saved its message
            running.handle.stop()
        return running

    def __contains__(self, thread_id: str) -> bool:
        return thread_id in self._turns

    def get(self, thread_id: str) -> Running | None:
        return self._turns.get(thread_id)

    def stop_all(self) -> None:
        """Stop every turn in flight (the server is going down): each saves its partial reply as interrupted
        and frees its thread, so a hung engine cannot hold the shutdown. A turn taken after this is stopped
        as it is taken."""
        self._closing = True
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
    repo_sessions: RepoSessions | None = None,
    confirmations: Confirmations | None = None,
    publish: Callable[[str, str | None], None] = lambda topic, key: None,
    pinnable: Callable[[], Awaitable[list[str]]] | None = None,
    background: set[asyncio.Task[None]] | None = None,
) -> APIRouter:
    """`publish` is the live feed's: each change to a thread (made, a turn's end, an answer, deleted) sends
    the `chat` hint with no key (the feed reaches every signed-in client: it names no one's chat; each
    refetches its own). `pinnable` lists the repositories a new chat's VM may be started for before its first
    message; `background` keeps those starts (serve's shutdown and tests wait on them)."""
    router = APIRouter()
    background = background if background is not None else set()
    prewarmed: dict[str, list[str]] = {}  # per person: the chats whose VM was started before a message
    busy = busy if busy is not None else Busy()
    clock = clock or RealClock()
    confirmations = confirmations if confirmations is not None else Confirmations(cfg)
    hooks.on("before_tool", before_tool(confirmations))

    async def drop_empty(thread: Thread, principal: Principal) -> None:
        """A thread this request opened and could not start leaves no empty row (nor VM) behind."""
        if thread.messages:
            return
        try:
            await threads.remove(thread.id, principal.user_id)
        except Exception:
            log.exception("removing the empty thread %s failed", thread.id)

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
            return error(MISCONFIGURED, 503)  # the reason (it names a server path) is in the startup log
        try:
            engine_name = cfg.engine_for_role("chat")
        except Exception as e:  # no engine serves the chat role's model
            log.error("the chat role has no engine: %s", e)
            return error("the chat role has no engine; see the server log or run airlie doctor", 503)
        if body.resumes and not threads.exists(body.id):  # an answer is for a chat that asked
            return error("no such thread", 404)
        if body.kind == "repository":
            if repo_sessions is None:
                return error("repository chats are not configured on this server", 503)
            if body.repo is None and not threads.exists(body.id):
                return error("a repository chat names its repository: repo is owner/name", 422)
        fresh = not threads.exists(body.id)  # a thread this request makes leaves no empty row if refused
        try:
            thread = threads.open(
                body.id, principal.user_id, body.kind, repo=body.repo, base_ref=body.base_ref
            )
        except ThreadNotFound:
            return error("no such thread", 404)
        turn = busy.take(thread.id)
        if turn is None:
            return error("this thread has a turn running; wait for it to finish", 409)
        answered = confirmations.approve(thread.id, body.approvals)  # the person's own, never the model's
        engines: list[Engine] = []
        released = False
        started = [False]
        used = [False]  # the turn reached its answers (it holds the VM and the engine)

        async def cleanup() -> None:
            """Free the thread and the engine client, once: the body or the response, whichever ends first."""
            nonlocal released
            if released:
                return
            released = True
            if not used[0]:  # the turn ended before it used its answers: they still wait for it
                confirmations.reopen(thread.id, answered)
            confirmations.end_turn(thread.id)  # a yes lasts as long as the turn it came with
            try:
                for engine in engines:
                    await engine.stop()
            finally:
                busy.release(thread.id)
                publish("chat", None)

        if body.resumes and not answered:  # answered already (another device, a second tap), or not ours
            await cleanup()
            return error("no permission request of this chat waits for that answer", 409)
        try:
            response = await start_turn(
                body, principal, thread, engine_name, engines, cleanup, turn, answered, started, used
            )
        except BaseException:
            if fresh:
                await drop_empty(thread, principal)  # a failed start leaves no empty row behind either
            await cleanup()
            raise
        if not isinstance(response, TurnStream):  # refused: nothing streams
            if fresh:
                await drop_empty(thread, principal)  # a thread this request opened leaves no empty row behind
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
        answered: list[Answered],
        started: list[bool],
        used: list[bool],
    ) -> Response:
        stored: StoredMessage | None = None
        retries: str | None = None
        parts: list[dict[str, Any]] = []
        message = None if body.resumes else last_user(body.messages)
        if message is not None:
            parts = kept_parts(message)
            newest = next((m for m in reversed(thread.messages) if m.role == "user"), None)
            if message.id and newest is not None and message.id == newest.id:  # Retry: answer it again
                retries = newest.id
                cut = thread.messages.index(newest) + 1
                thread.messages = thread.messages[:cut]  # the model does not see the reply it replaces
            else:
                taken = {m.id for m in thread.messages}  # keep ids unique
                mid = message.id if message.id and message.id not in taken else f"msg-{uuid.uuid4().hex}"
                stored = StoredMessage(mid, "user", parts, clock.wall())
        last = thread.messages[-1] if thread.messages else None
        resumes = last.id if body.resumes and last is not None and last.role == "assistant" else None
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
            log.error("chat is misconfigured, this turn answers 503: %s", reason)
            return error(MISCONFIGURED, 503)
        todos = TodoList.load(thread.todos)
        chat: RepoChat | None = None
        if thread.kind == "repository":
            assert repo_sessions is not None  # checked at the top of the route
            try:
                chat = await repo_sessions.open(thread)
            except BoxUnavailable as e:
                return error(str(e), 409)
            except EngineError as e:  # the repository or its recipe cannot be read
                return error(str(e), 422)
            tool_ctx = repo_sessions.tool_context(chat)
        else:
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
                repo=chat,
                task_engine_for=lambda role: engine,  # the turn's engine, routed by role, holds each call
            )
        except KindUnavailable as e:
            return error(str(e), 400)
        guard(registry, confirmations, thread.id, thread.kind)  # a listed tool cannot run without a yes
        around: Callable[[], Any] | None = None
        if chat is not None and repo_sessions is not None:  # the VM is held for the turn, inside the stream
            around = partial(repo_sessions.turn, chat, f"chat:{thread.id}")  # (a boot failure is a chunk)
        try:
            user_parts = await attachment_parts(attach, principal, thread, stored)
        except TooManyAttachments as e:
            return error(str(e), 400)
        from airlie.cli import _http_engine  # imported here: the cli imports serve

        engine = _http_engine(cfg, engine_name, transport=transport, manager=manager)
        engines.append(engine)
        await engine.start()
        answers = {a.id: a.approved for a in answered}
        calls = answered_calls(thread, answers) if body.resumes else {}
        noted = [a for a in answered if a.id not in calls]  # no call to run: the model is told instead

        def use_answers() -> None:
            """The turn holds the VM and the engine and is about to use its answers: they are spent from
            here, and a reload or another device shows them answered."""
            used[0] = True
            if answers:
                threads.answer(thread.id, answers)
                for m in thread.messages:
                    mark_answered(m.parts, answers)
                publish("chat", None)

        async def stream() -> AsyncIterator[str]:
            started[0] = True
            try:
                async for chunk in run_turn(
                    TurnRequest(
                        thread,
                        stored,
                        ctx,
                        user_parts,
                        resumes=resumes,
                        retries=retries,
                        note=answered_note(noted) if body.resumes and noted else None,
                        answered_calls=calls,
                        answers=answers,
                        on_answers=use_answers,
                    ),
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
                    around=around,
                    around_status=chat.vm_name if chat is not None else None,
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

    async def may_prewarm(owner: str, thread_id: str, repo: str) -> bool:
        """A repository the person may pin, and fewer than `boxes.prewarm_max_per_person` of their chats
        whose VM was started early and that have had no message yet. The slot is taken before the await
        (the check and the take have no await between them), and given back when the repository is not one
        airlie knows: concurrent requests cannot all pass the count."""
        if repo_sessions is None or pinnable is None:
            return False
        unused = [
            i
            for i in prewarmed.get(owner, [])
            if threads.exists(i) and not threads.get(i, owner).messages and i not in busy
        ]
        if len(unused) >= cfg.boxes.prewarm_max_per_person:
            prewarmed[owner] = unused
            return False
        prewarmed[owner] = [*unused, thread_id]
        try:
            known = {r.lower() for r in await pinnable()}
        except Exception:
            log.warning("could not list the repositories that may be pre-warmed", exc_info=True)
            known = set()
        if repo.lower() in known:
            return True
        prewarmed[owner] = [i for i in prewarmed.get(owner, []) if i != thread_id]
        return False

    async def prewarm(thread: Thread) -> None:
        """Open the session and start its VM (the code goes in too), then let go: the first turn finds it
        ready, or waits for it. A failure is only logged: the first turn meets it again and says why."""
        assert repo_sessions is not None
        try:
            chat = await repo_sessions.open(thread)
            async with repo_sessions.turn(chat, f"prewarm:{thread.id}"):
                pass
        except Exception:
            log.warning("pre-warming the VM of chat %s failed", thread.id, exc_info=True)

    @router.get("/v1/airlie/chat/threads")
    async def list_threads(request: Request, kind: ChatKind | None = None) -> Any:
        principal = principal_of(request)
        if principal is None:
            return error("not signed in", 401)
        return [summary_json(t, t.id in busy) for t in threads.list(principal.user_id, kind)]

    @router.post("/v1/airlie/chat/threads", status_code=201)
    async def create_thread(body: NewThread, request: Request) -> Any:
        """A new chat, empty, with an id the server chose; its first turn names it. A repository chat's VM
        starts here in the background when `may_prewarm` allows it; otherwise its first turn starts it, and
        the stream shows the VM's status while it does."""
        principal = principal_of(request)
        if principal is None:
            return error("not signed in", 401)
        if body.kind == "repository" and repo_sessions is None:
            return error("repository chats are not configured on this server", 503)
        t = threads.create(principal.user_id, body.kind, repo=body.repo, base_ref=body.base_ref)
        publish("chat", None)
        if t.repo is not None and await may_prewarm(principal.user_id, t.id, t.repo):
            task = asyncio.create_task(prewarm(t))
            background.add(task)
            task.add_done_callback(background.discard)
        made = ThreadSummary(t.id, t.title, t.kind, t.updated, t.created, t.repo, t.base_ref)
        return JSONResponse(summary_json(made, False), status_code=201)

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
            "repo": t.repo,
            "base_ref": t.base_ref,
            "branch": branch_of(t.id, t.kind),
            "running": t.id in busy,
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
        if busy.take(thread_id) is None:  # the slot is held while it goes: no turn starts on it meanwhile
            return error("this thread has a turn running; wait for it to finish", 409)
        try:
            removed = await threads.remove(thread_id, principal.user_id)
        except EngineError as e:  # its VM could not be deleted (in use): the chat stays
            return error(str(e), 409)
        finally:
            busy.release(thread_id)
        if not removed:
            return error("no such thread", 404)
        publish("chat", None)
        return Response(status_code=204)

    return router
