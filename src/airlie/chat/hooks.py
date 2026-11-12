"""The chat's four hooks (session_start, before_turn, after_tool, on_finish) and the session system prompt.
A hook that raises is logged and never stops the turn; only a `before_turn` guardrail may, with
`TurnRefused`. Persistence is the runner's own step, not a hook, so a failing hook cannot lose a turn."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from airlie.auth.principal import Principal
from airlie.chat.kinds import ChatKind
from airlie.chat.superpowers import SuperpowersLibrary
from airlie.config.schema import AirlieConfig
from airlie.engine.types import ToolCall
from airlie.skills.library import Skill, prompt_text

if TYPE_CHECKING:
    from airlie.notify.notifier import Notifier

log = logging.getLogger("airlie.chat.hooks")
audit_log = logging.getLogger("airlie.chat.audit")

HookName = Literal["session_start", "before_turn", "before_tool", "after_tool", "on_finish"]
REPLY_BODY_CHARS = 120
PLAIN_NOTE = (
    "This is a plain chat on the host: there is no repository, file or shell; "
    "skills that need files: do the work inline."
)


class TurnRefused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class UnknownSkill(KeyError):
    def __init__(self, skill: str, known: str) -> None:
        super().__init__(skill)
        self.skill = skill
        self.known = known

    def __str__(self) -> str:
        return f"unknown skill '{self.skill}'; skills: {self.known}"


@dataclass
class TurnContext:
    thread_id: str
    owner: str
    kind: ChatKind
    session: Principal
    system_prompt: str = ""
    prompt_error: str | None = None  # why session_start could not build the system prompt
    attachments: int = 0
    skill: str | None = None


@dataclass(frozen=True)
class Verdict:
    """A `before_tool` hook's refusal: the call does not run and `message` is its result. `approval_id` is set
    when a person's yes would let it through (the stream then carries a request for it)."""

    kind: Literal["pending", "denied"]
    message: str
    approval_id: str | None = None
    arguments: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ToolEvent:
    call_id: str
    name: str
    arguments: str
    output: str
    ok: bool


@dataclass(frozen=True)
class TurnSummary:
    thread_id: str
    owner: str
    kind: ChatKind
    title: str
    text: str
    usage: Mapping[str, Any]
    interrupted: bool
    failed: bool
    disconnected: bool = False  # the client was gone before the turn finished (set by the runner)


Hook = Callable[..., Awaitable[None]]


@dataclass
class Hooks:
    session_start: list[Hook] = field(default_factory=list)
    before_turn: list[Hook] = field(default_factory=list)
    before_tool: list[Hook] = field(default_factory=list)
    after_tool: list[Hook] = field(default_factory=list)
    on_finish: list[Hook] = field(default_factory=list)

    def on(self, name: HookName, fn: Hook) -> None:
        getattr(self, name).append(fn)

    async def run(self, name: HookName, *args: Any) -> None:
        for fn in list(getattr(self, name)):
            try:
                await fn(*args)
            except TurnRefused:
                if name == "before_turn":
                    raise
                log.exception("chat hook %s raised TurnRefused outside before_turn", name)
            except Exception:
                log.exception("chat hook %s failed", name)

    async def gate(self, ctx: TurnContext, call: ToolCall) -> Verdict | None:
        """The first refusal a `before_tool` hook gives `call`, or None to let it run. A hook that raises
        refuses it: a gate that fails must not let the call through."""
        for fn in list(self.before_tool):
            try:
                verdict: Verdict | None = await cast(Any, fn)(ctx, call)
            except Exception:
                log.exception("chat hook before_tool failed: refusing the call %s", call.name)
                return Verdict(
                    "denied", f"error: {call.name} was refused because its permission check failed"
                )
            if verdict is not None:
                return verdict
        return None


def slash_section(chat_skills: Mapping[str, Skill], skill: str | None) -> str:
    """The slash-command list, then the body of the one run for this request; empty when there is neither.
    Raises UnknownSkill for a `skill` that is not one of `chat_skills`."""
    out = ""
    if chat_skills:
        listing = "\n".join(f"- /{s.name}: {s.summary}" for s in chat_skills.values())
        out = f"## Slash commands\n\n{listing}\n"
    if skill is None:
        return out
    found = chat_skills.get(str(skill))
    if found is None:
        raise UnknownSkill(str(skill), ", ".join(chat_skills) or "none")
    return f"{out.rstrip()}\n\n## Skill: /{found.name}\n\n{found.text.strip()}\n".lstrip()


def build_system_prompt(
    cfg: AirlieConfig,
    kind: ChatKind,
    library: SuperpowersLibrary | None,
    chat_skills: Mapping[str, Skill],
    *,
    subagent: bool = False,
    skill: str | None = None,
) -> str:
    """The chat role's prompt, the superpowers bootstrap and skills listing, the slash commands, and the
    slash skill being run. Rebuilt every turn and never stored; the same inputs give the same bytes."""
    parts = [prompt_text(cfg.roles["chat"].prompt).rstrip()]
    if library is not None and cfg.chat.superpowers.enabled and not subagent:
        parts.append(library.bootstrap().rstrip())
        parts.append(f"## Skills\n\n{library.listing()}")
    known = skill if skill in chat_skills else None
    slash = slash_section(chat_skills, known)
    if slash:
        parts.append(slash.rstrip())
    if kind == "plain":
        parts.append(PLAIN_NOTE)
    return "\n\n".join(parts) + "\n"


def default_hooks(
    cfg: AirlieConfig,
    library: SuperpowersLibrary | None,
    *,
    chat_skills: Mapping[str, Skill],
    uploads_limit: int,
    notifier: Notifier | None = None,
) -> Hooks:
    hooks = Hooks()

    async def session_start(ctx: TurnContext) -> None:
        try:
            ctx.system_prompt = build_system_prompt(cfg, ctx.kind, library, chat_skills, skill=ctx.skill)
        except Exception as e:
            ctx.prompt_error = f"{type(e).__name__}: {e}"
            raise  # the hook runner logs it; the route refuses a turn that has no prompt

    async def guard(ctx: TurnContext) -> None:
        if ctx.attachments > uploads_limit:
            raise TurnRefused(400, f"a message may carry up to {uploads_limit} attachments")
        if ctx.skill is not None and ctx.skill not in chat_skills:
            known = ", ".join(chat_skills) or "none"
            raise TurnRefused(404, f"unknown skill '{ctx.skill}'; skills: {known}")

    async def audit(ctx: TurnContext, ev: ToolEvent) -> None:
        audit_log.info(
            "thread=%s call=%s tool=%s ok=%s args_bytes=%d output_bytes=%d",
            ctx.thread_id,
            ev.call_id,
            ev.name,
            ev.ok,
            len(ev.arguments.encode()),
            len(ev.output.encode()),
        )

    hooks.on("session_start", session_start)
    hooks.on("before_turn", guard)
    hooks.on("after_tool", audit)
    if notifier is not None:
        hooks.on("on_finish", notify_reply(notifier))
    return hooks


def notify_reply(notifier: Notifier) -> Callable[[TurnSummary], Awaitable[None]]:
    """A push when the reply finished with nobody watching: a short body, never more of the chat."""

    async def hook(summary: TurnSummary) -> None:
        if not summary.disconnected or summary.failed or summary.interrupted:
            return
        await notifier.notify(
            summary.owner, "chat_reply", "airlie replied", "/", summary.text[:REPLY_BODY_CHARS]
        )

    return hook
