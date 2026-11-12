"""Server-side confirmations: a tool on a chat kind's `ask_first` list runs only after a person approved that
exact call. A model cannot approve for itself: an approval is made only by `approve`, which the turn route
calls with the `approvals` of the request body, authenticated as the person who owns the thread. It matches
one tool, one set of arguments (their canonical JSON's sha256) and one session, and is spent by the call it
allows. Two places hold the line: the `before_tool` hook asks the person (and the stream carries the request),
and `Confirmed` wraps each listed tool so that no path to its `run` skips the check."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from airlie.chat.hooks import TurnContext, Verdict
from airlie.chat.kinds import ChatKind, needs_yes
from airlie.config.schema import AirlieConfig
from airlie.engine.types import EngineError, ToolCall
from airlie.tools.jsonargs import parse_tool_args
from airlie.tools.protocol import Tool, ToolContext, ToolResult
from airlie.tools.registry import ToolRegistry

Answer = Literal["allowed", "pending", "denied"]
MAX_PENDING_PER_SESSION = 50
WAITING = "waiting for your approval: the person has been asked; do not retry until they have answered"


class Approval(BaseModel):
    """One answer from the client: the id of a request the server made, and the person's yes or no."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    approved: bool


@dataclass
class _Item:
    id: str
    session: str
    tool: str
    digest: str
    state: Literal["pending", "approved", "denied"] = "pending"


def digest_of(args: Mapping[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class NeedsApproval(EngineError):
    """A step that is not a tool call (the image build) needs a yes first; the turn ends with a request."""

    def __init__(self, approval_id: str, tool: str, arguments: Mapping[str, Any], what: str) -> None:
        super().__init__(f"waiting for your approval: {what}")
        self.approval_id = approval_id
        self.tool = tool
        self.arguments = dict(arguments)


Subject = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
"""What a tool asks the person about in place of the model's arguments (`push_branch`: the commits it would
push, bound to their head); read fresh at every look, so a yes for one subject never lets another through.
A tool has one when it has `subject(args)` and `run_approved(args, subject, ctx)`."""


class Confirmations:
    def __init__(self, cfg: AirlieConfig) -> None:
        self._cfg = cfg
        self._items: dict[str, _Item] = {}
        self._subjects: dict[str, dict[str, Subject]] = {}

    def use_subjects(self, session_id: str, subjects: Mapping[str, Subject]) -> None:
        """The subjects of the session's tools that have one, for this turn's registry (guard sets them)."""
        if subjects:
            self._subjects[session_id] = dict(subjects)
        else:  # a plain chat's: nothing kept per session
            self._subjects.pop(session_id, None)

    async def subject(self, session_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """What the person is asked about for this call: the tool's subject when it has one, else the
        arguments. A subject that cannot be read raises its EngineError: the call is an error, not a
        request."""
        found = self._subjects.get(session_id, {}).get(tool)
        return args if found is None else await found(args)

    def required(self, tool: str, kind: ChatKind = "repository") -> bool:
        return tool in needs_yes(kind, self._cfg)  # the config cannot drop the always-asked ones

    def request(self, session_id: str, call_id: str, tool: str, args: Mapping[str, Any]) -> str:
        """Record that `tool` with `args` waits for a yes and return the request's id; the same call asked
        again before it is answered is the same request."""
        digest = digest_of(args)
        for item in self._items.values():
            if (item.session, item.tool, item.digest, item.state) == (session_id, tool, digest, "pending"):
                return item.id
        mine = [i for i in self._items.values() if i.session == session_id and i.state == "pending"]
        if len(mine) >= MAX_PENDING_PER_SESSION:
            del self._items[mine[0].id]  # the oldest unanswered request goes
        item = _Item(uuid.uuid4().hex, session_id, tool, digest)
        self._items[item.id] = item
        return item.id

    def approve(self, session_id: str, approvals: Sequence[Approval]) -> None:
        """Apply the person's answers. An id that is not a pending request of this session (another
        session's, an answered one, a made-up one) changes nothing."""
        for a in approvals:
            item = self._items.get(a.id)
            if item is not None and item.session == session_id and item.state == "pending":
                item.state = "approved" if a.approved else "denied"

    def status(
        self, session_id: str, tool: str, args: Mapping[str, Any], kind: ChatKind = "repository"
    ) -> Answer:
        """What `check` would answer, spending nothing."""
        return self._answer(session_id, tool, args, kind, spend=False)

    def check(
        self, session_id: str, tool: str, args: Mapping[str, Any], kind: ChatKind = "repository"
    ) -> Answer:
        """`allowed` for a tool that needs no yes, or that has one for exactly these arguments (spent now);
        `denied` once for a call the person refused (spent now); else `pending`."""
        return self._answer(session_id, tool, args, kind, spend=True)

    def _answer(
        self, session_id: str, tool: str, args: Mapping[str, Any], kind: ChatKind, *, spend: bool
    ) -> Answer:
        if not self.required(tool, kind):
            return "allowed"
        digest = digest_of(args)
        for item in self._items.values():
            if (item.session, item.tool, item.digest) != (
                session_id,
                tool,
                digest,
            ) or item.state == "pending":
                continue
            if spend:
                del self._items[item.id]
            return "allowed" if item.state == "approved" else "denied"
        return "pending"

    def end_turn(self, session_id: str) -> None:
        """An answer is for the turn whose request body carried it: at that turn's end (however it ended)
        every answered item the turn did not spend goes. Requests still waiting for an answer stay."""
        for item_id in [
            i.id for i in self._items.values() if i.session == session_id and i.state != "pending"
        ]:
            del self._items[item_id]

    def __len__(self) -> int:
        return len(self._items)

    def forget(self, session_id: str) -> None:
        self._subjects.pop(session_id, None)
        for item_id in [i.id for i in self._items.values() if i.session == session_id]:
            del self._items[item_id]


def before_tool(confirmations: Confirmations) -> Any:
    """The `before_tool` hook: a listed call with no yes ends as a request, a refused one as a refusal. It
    spends nothing: `Confirmed` does, when the call runs."""

    async def hook(ctx: TurnContext, call: ToolCall) -> Verdict | None:
        if not confirmations.required(call.name, ctx.kind):
            return None
        try:
            parsed = parse_tool_args(call.arguments)
        except ValueError:
            return None  # the call never runs: the registry answers it as malformed
        try:
            args = await confirmations.subject(ctx.thread_id, call.name, parsed)
        except EngineError as e:  # nothing to ask about (nothing to push): the call is an error
            return Verdict("denied", f"error: {e}")
        answer = confirmations.status(ctx.thread_id, call.name, args, ctx.kind)
        if answer == "allowed":
            return None
        if answer == "denied":
            confirmations.check(
                ctx.thread_id, call.name, args, ctx.kind
            )  # the no is reported once, then spent
            return Verdict("denied", f"refused: the person said no to this {call.name} call", None, args)
        approval_id = confirmations.request(ctx.thread_id, call.id, call.name, args)
        return Verdict("pending", f"error: {WAITING}", approval_id, args)

    return hook


def guard(
    registry: ToolRegistry, confirmations: Confirmations, session_id: str, kind: ChatKind
) -> ToolRegistry:
    """`registry` with every tool the kind lists in `ask_first` wrapped in `Confirmed`, and the subjects of
    those that have one given to `confirmations` for the hook."""
    subjects: dict[str, Subject] = {}
    for name in registry.names():
        tool = registry.get(name)
        if tool is not None and confirmations.required(name, kind):
            if _has_subject(tool):
                subjects[name] = cast(Any, tool).subject
            registry.add(Confirmed(tool, confirmations, session_id, kind))
    confirmations.use_subjects(session_id, subjects)
    return registry


def _has_subject(tool: Tool) -> bool:
    return callable(getattr(tool, "subject", None)) and callable(getattr(tool, "run_approved", None))


class Confirmed:
    """A tool that runs only with a yes: `run` spends it, so the call cannot get here any other way."""

    def __init__(self, inner: Tool, confirmations: Confirmations, session_id: str, kind: ChatKind) -> None:
        self.name = inner.name
        self.description = inner.description
        self.parameters = inner.parameters
        self.distinct_by_arguments = bool(getattr(inner, "distinct_by_arguments", False))
        self._inner = inner
        self._confirmations = confirmations
        self._session = session_id
        self._kind: ChatKind = kind

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """A tool with a subject is checked against it, read now, and runs with exactly that subject."""
        inner: Any = self._inner
        subject = args
        if _has_subject(self._inner):
            try:
                subject = await inner.subject(args)
            except EngineError as e:
                return ToolResult(False, f"error: {e}")
        if self._confirmations.check(self._session, self.name, subject, self._kind) != "allowed":
            return ToolResult(False, f"error: {self.name} needs the person's approval first")
        if _has_subject(self._inner):
            return cast(ToolResult, await inner.run_approved(args, subject, ctx))
        return await self._inner.run(args, ctx)
