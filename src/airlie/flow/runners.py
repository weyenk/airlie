"""The generic node runners: one loop per node type, with a workflow's own pieces (brief builders, parsers,
after-hooks) looked up in the registry by id (docs/superpowers/specs/2026-11-06-generic-nodes-design.md).

Nothing here knows a story: a workflow binds the asker, the record it writes on and how a YAML role names a
configured seat (`functools.partial`), and registers the pieces its nodes name."""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from airlie.flow.composer import FAIL_KEY, EndRun, RunContext, resolve_count
from airlie.flow.spec import NodeSpec
from airlie.story.asking import Asker, Recorder, Say, StoryBlocked
from airlie.story.model import NotActionable

if TYPE_CHECKING:
    from airlie.config import AirlieConfig
    from airlie.story.amigos import Agreement, MeetingFactory, Turn
    from airlie.story.model import Layer, Story
    from airlie.tools import ToolRegistry

OnTurn = Callable[["Turn", Mapping[str, Any]], None]


async def ask_runner(
    node: NodeSpec,
    ctx: RunContext,
    *,
    asker: Asker,
    st: Recorder,
    seat: Callable[[str], str] = str,
    ask: Callable[..., Awaitable[str]] | None = None,
) -> Mapping[str, Any]:
    """One seat, one brief, one reply. With `parse`, the seat is asked until the reply parses
    (`Asker.usable`: `steps` tries, each failed try's reason in the next brief); without it, once, as a
    short ask of at most `steps` tool steps. The stage the asker records is the node's name, the role
    `seat(node.role)`, and `ask` replaces `Asker.ask` (a workflow's own map for the turn).

    Then `after(ctx, value)` (it may be async) runs, and the value is the node's one output (a parsed value
    with a `to_dict` is stored as that). A seat that says the work cannot be done as written
    (`NotActionable`) blocks with its reason. On an `on_fail: continue` node a failed ask is no reason to
    stop: `args.failed(ctx, error)` runs when the node names it, and the node fails without outputs."""
    assert node.prompt is not None and isinstance(node.role, str)  # spec.py: an ask needs a role and prompt
    role = seat(node.role)
    brief = ctx.registry[node.prompt]  # the ids and counts resolve before the ask: a wrong one is no
    parse = ctx.registry[node.parse] if node.parse is not None else None  # failed ask to continue past
    failed = ctx.registry[node.args["failed"]] if "failed" in node.args else None
    after = ctx.registry[node.after] if node.after is not None else None
    steps = resolve_count(ctx.cfg, node.steps) if node.steps is not None else None
    asking = ask or asker.ask
    try:
        if parse is not None:
            value: Any = await asker.usable(
                st,
                ctx.node,
                role,
                next(iter(node.outputs), ctx.node),
                lambda feedback: brief(ctx, feedback),
                parse,
                steps,
                ask=asking,
            )
        else:
            value = await asking(st, ctx.node, role, brief(ctx, None), steps=steps, long=False)
    except Exception as e:
        if node.on_fail != "continue":
            if isinstance(e, NotActionable):
                raise StoryBlocked(f"{node.role} says this cannot be built as written: {e}") from e
            raise
        if failed is not None:
            await _maybe(failed(ctx, e))
        return {FAIL_KEY: f"{type(e).__name__}: {e}"}
    if after is not None:
        await _maybe(after(ctx, value))
    plain = value.to_dict() if hasattr(value, "to_dict") else value
    return {name: plain for name in list(node.outputs)[:1]}


async def check_runner(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
    """A deterministic step: the node's `run` id called as `fn(ctx, **node.args)` (it may be async), its
    inputs read from `ctx.store`, and the mapping it returns is the node's outputs (False or None: none).
    Whatever it raises propagates: the composer's caller says what a stop means (waiting, blocked)."""
    assert node.run is not None  # spec.py: a check needs run
    fn = ctx.registry[node.run]  # resolved before the call: a wrong id is a config error, not the step's
    out = await _maybe(fn(ctx, **node.args))
    return out or {}


@dataclass(frozen=True)
class Opening:
    """How a conversation starts: its opening `brief`, the tool `steps` a turn has (None: its role's own),
    the stage the asker records (None: the node's name), the commit it reads (`at`, None: the record's
    base), `Asker.conversation`'s other keywords (`options`: the map, what it may write), and the
    workflow's own `state` the session carries (the story's: the drafting checkout's turns)."""

    brief: str
    steps: int | None = None
    stage: str | None = None
    at: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)
    state: Any = None


@dataclass(frozen=True)
class Session:
    """A held conversation (the `session` artifact format): what continues it (`say`), the studio run it
    is held in, and its opening's `state`. It lives in the process only, never in a saved record."""

    say: Say
    run: str
    state: Any = None


@contextlib.asynccontextmanager
async def converse(
    asker: Asker,
    st: Recorder,
    role: str,
    opening: Opening,
    *,
    stage: str,
    held: contextlib.AsyncExitStack | None = None,
) -> AsyncIterator[Session]:
    """The conversation `opening` starts with `role`, as a Session (its run is the one it opened). It is
    closed when the block ends; on `held` it is entered there instead, and closed when that stack is,
    after the block (a conversation later nodes or steps continue)."""
    talk = asker.conversation(
        st,
        opening.stage or stage,
        role,
        opening.brief,
        steps=opening.steps,
        at=opening.at,
        **opening.options,
    )
    if held is None:
        async with talk as say:
            yield Session(say, st.runs[-1], opening.state)
    else:
        say = await held.enter_async_context(talk)
        yield Session(say, st.runs[-1], opening.state)


class Talk:
    """What a conversation node's turns are had through (`conversation_runner`): `opening` builds the
    node's opening (its `prompt` piece), `open` starts the conversation, or continues the session the node
    names, and `count` is the node's turn count (`steps`, else 1)."""

    def __init__(self, node: NodeSpec, ctx: RunContext, *, asker: Asker, st: Recorder, role: str) -> None:
        self.node, self.ctx, self.asker, self.st, self.role = node, ctx, asker, st, role
        self.count = resolve_count(ctx.cfg, node.steps) if node.steps is not None else 1
        self.output = next((n for n, f in node.outputs.items() if f == "session"), None)
        named = ctx.sessions.get(node.session) if node.session is not None else None
        self.continued: Session | None = named
        self.session: Session | None = named

    async def opening(self, feedback: str | None = None) -> Opening:
        assert self.node.prompt is not None  # spec.py: a conversation needs a prompt
        got = await _maybe(self.ctx.registry[self.node.prompt](self.ctx, feedback))
        return got if isinstance(got, Opening) else Opening(str(got))

    @contextlib.asynccontextmanager
    async def open(self, opening: Opening) -> AsyncIterator[Session]:
        """The node's conversation from `opening`: the session it names, when one is held (`opening` is
        then its next turn's to send, not a new conversation's); else a new one, held on the run's stack
        when the node outputs a session (it is dropped from the sessions when the stack closes it), else
        closed when the block ends."""
        if self.continued is not None:
            yield self.continued
            return
        held = self.ctx.held if self.output is not None else None
        if self.output is not None:
            if held is None:
                raise RuntimeError(
                    f"node '{self.ctx.node}' outputs a session, but the run holds nothing open"
                )
            item = self.ctx.item
            name = self.output if item is None else self.output.replace("{item}", item)
            held.callback(self.ctx.sessions.pop, name)  # after the conversation closes (the stack is LIFO)
        async with converse(self.asker, self.st, self.role, opening, stage=self.ctx.node, held=held) as s:
            self.session = s
            yield s


async def conversation_runner(
    node: NodeSpec,
    ctx: RunContext,
    *,
    asker: Asker,
    st: Recorder,
    seat: Callable[[str], str] = str,
) -> Mapping[str, Any]:
    """One seat with a held transcript. With `args.turns` the node's turns are that registry piece's,
    `turns(ctx, talk)` (a workflow's own turn body: the story's drafting and planning rounds), which opens
    the conversation through `talk` and returns the node's value. Else the generic loop: the opening
    (`prompt(ctx, feedback)`) is turn 0, or, for a node that names a held `session`, its opening is one
    more turn of that conversation; each later turn sends `turn(ctx, index, feedback)`, up to `steps`
    turns in all; each reply is `parse`d when the node names a parser, and `done(ctx, value)` ends the
    turns early. Then `after(ctx, value)`. The value is the node's first non-session output; a session
    output is the conversation, held for the run (`Composer.held`)."""
    assert isinstance(node.role, str)  # spec.py: a conversation needs a role
    talk = Talk(node, ctx, asker=asker, st=st, role=seat(node.role))
    parse = ctx.registry[node.parse] if node.parse is not None else None
    after = ctx.registry[node.after] if node.after is not None else None
    if "turns" in node.args:
        value = await _maybe(ctx.registry[node.args["turns"]](ctx, talk))
    else:
        turn = ctx.registry[node.turn] if node.turn is not None else None
        done = ctx.registry[node.done] if node.done is not None else None
        opening = await talk.opening(ctx.feedback)
        value = None
        async with talk.open(opening) as session:
            for index in range(talk.count):
                if index == 0:
                    nudge = opening.brief if talk.continued is not None else None
                elif turn is not None:
                    nudge = await _maybe(turn(ctx, index, ctx.feedback))
                else:
                    break
                reply = await session.say(nudge)
                value = parse(reply) if parse is not None else reply
                if done is not None and done(ctx, value):
                    break
    if after is not None:
        await _maybe(after(ctx, value))
    out: dict[str, Any] = {}
    if (first := next((n for n, f in node.outputs.items() if f != "session"), None)) is not None:
        out[first] = value.to_dict() if hasattr(value, "to_dict") else value
    if talk.output is not None and talk.session is not None and talk.continued is None:
        out[talk.output] = talk.session  # a continued session is the node's input, not its output
    return out


@dataclass(frozen=True)
class Hold:
    """How a meeting node's `run` piece holds its meeting (`meeting_runner`): called with the checkout the
    seats' tools read and `Meets.run`'s arguments, it makes the meeting from the factory (`tools(checkout)`,
    a fresh `on_turn()`, which saves the team state every turn) and holds it from `state`, the team state a
    meeting cut short saved (None: afresh). `before_turn()` is what each seat's turn waits for first, when
    there is one and the meeting takes it (`Meeting.before_turn`). The turn cap is the node's `steps`, read
    from the same config by the factory."""

    meeting: MeetingFactory
    cfg: AirlieConfig
    tools: Callable[[Path], Callable[[str], ToolRegistry]]
    on_turn: Callable[[], OnTurn]
    before_turn: Callable[[], Callable[[], Awaitable[Any]] | None] = lambda: None
    state: Mapping[str, Any] | None = None

    async def __call__(
        self,
        checkout: Path,
        opening: str,
        story: Story,
        layers: tuple[Layer, ...],
        levels: Mapping[str, int],
        guidelines: Collection[str],
        *,
        repo: Path,
        base: str,
    ) -> Agreement:
        meeting = self.meeting(self.cfg, self.tools(checkout), self.on_turn())
        if (wait := self.before_turn()) is not None and hasattr(meeting, "before_turn"):
            meeting.before_turn = wait
        return await meeting.run(
            opening, story, layers, levels, guidelines, repo=repo, base=base, state=self.state
        )


async def meeting_runner(
    node: NodeSpec,
    ctx: RunContext,
    *,
    meeting: MeetingFactory,
    tools: Callable[[Path], Callable[[str], ToolRegistry]],
    on_turn: Callable[[], OnTurn],
    before_turn: Callable[[], Callable[[], Awaitable[Any]] | None] = lambda: None,
) -> Mapping[str, Any]:
    """Several seats on one shared transcript. The node's `run` piece is called as `run(ctx, hold,
    **node.args)` (it may be async): it builds the opening and holds the meeting through `hold` (a `Hold`
    over the factory, the tools, the turn callback and the team state the store saved as `meeting`, so a
    killed meeting goes on from its next speaker), and the mapping it returns is the node's outputs."""
    assert node.run is not None  # spec.py: a meeting needs run
    run = ctx.registry[node.run]  # resolved before the meeting: a wrong id is a config error
    state = ctx.store.get("meeting") if ctx.store.has("meeting") else None
    hold = Hold(meeting, ctx.cfg, tools, on_turn, before_turn, state)
    out = await _maybe(run(ctx, hold, **node.args))
    return out or {}


async def gate_runner(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
    """The run's end: the node's `run` id called as `fn(ctx, **node.args)` (it may be async), then `EndRun`,
    whatever it returned. The composer records the node done and its `run` returns the gate's name; whatever
    the piece raises propagates as a check's does."""
    assert node.run is not None  # spec.py: a gate needs run
    fn = ctx.registry[node.run]  # resolved before the call: a wrong id is a config error, not the gate's
    await _maybe(fn(ctx, **node.args))
    raise EndRun()


async def _maybe(result: Any) -> Any:
    """`result`, awaited when a hook returned an awaitable."""
    return await result if inspect.isawaitable(result) else result
