"""The generic node runners: one loop per node type, each run from the node's own fields
(docs/superpowers/specs/2026-11-08-nodes-are-config-design.md, "The node, by field", "Outputs", D14, D20).

A model node's brief is its `prompt` (the text, or a `prompt:<id>` file) rendered from its inputs, the
card and config (`render`); its structured output (the first with `fields`) is parsed from the reply
(`parse_output`), and a reply that does not parse goes back with the reason as `{feedback}`. Nothing is
appended to a prompt: a prompt carries its own format text. A node's `events` name the legacy event kinds
its outcomes emit (D14). A check or gate calls the catalog operation its `run` names.

The runners depend on the story package's `Asker`, `Recorder`, `Say`, `StoryBlocked` and `NotActionable`
types, but hold no story-specific logic: a workflow binds the asker, the record it writes on, how a YAML role
names a configured seat (`functools.partial`) and, for a meeting, its protocol."""

from __future__ import annotations

import contextlib
import inspect
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from airlie.flow.composer import FAIL_KEY, EndRun, RunContext, resolve_count
from airlie.flow.outputs import OutputSpec, parse_output
from airlie.flow.render import prompt_source, render
from airlie.flow.spec import NodeSpec
from airlie.story.asking import Asker, Recorder, Say, StoryBlocked, Writes
from airlie.story.model import NotActionable, StoryError

if TYPE_CHECKING:
    from airlie.config import AirlieConfig
    from airlie.story.amigos import Agreement, MeetingFactory, Turn
    from airlie.story.model import Layer, Story
    from airlie.tools import ToolRegistry

OnTurn = Callable[["Turn", Mapping[str, Any]], None]
Seat = Callable[[str], str]


# ---- what every model node shares: its brief, its structured output, its failure, its events ----


def brief(node: NodeSpec, ctx: RunContext, feedback: str | None = None, *, text: str | None = None) -> str:
    """The node's `prompt` (or `text`: its `turn`) rendered for this run: `{feedback}` is `feedback` (why the
    last reply did not parse) then the loop's carried feedback, `{item}` the bundle item."""
    source = text if text is not None else node.prompt
    assert source is not None  # spec.py: a model node needs a prompt
    return render(prompt_source(source, ctx.texts), ctx.lookup, feedback=feedback, item=ctx.item)


def structured(node: NodeSpec) -> tuple[str, OutputSpec] | None:
    """The node's first output with `fields`, which its replies are parsed by; None when it has none."""
    return next(((name, spec) for name, spec in node.outputs.items() if spec.fields), None)


def _role(node: NodeSpec, seat: Seat) -> str:
    assert isinstance(node.role, str)  # spec.py: an ask, a conversation and a write name one role
    return seat(node.role)


def _steps(node: NodeSpec, ctx: RunContext) -> int | None:
    return resolve_count(ctx.cfg, node.steps) if node.steps is not None else None


def _done(node: NodeSpec, st: Recorder, reply: str) -> None:
    """The node's legacy `done` event, with the fields the driver's has: the reply's length (D14)."""
    if (kind := node.events.get("done")) is not None:
        st.emit(kind, chars=len(reply))


def _failed(node: NodeSpec, ctx: RunContext, st: Recorder, error: Exception) -> Mapping[str, Any]:
    """A model node's failure: on `on_fail: continue` its `failed` event and the node fails without outputs;
    else it propagates, a seat that says the work cannot be done as written (`NotActionable`) as a block."""
    if node.on_fail != "continue":
        if isinstance(error, NotActionable):
            raise StoryBlocked(f"{node.role} says this cannot be built as written: {error}") from error
        raise error
    if (kind := node.events.get("failed")) is not None:
        from airlie.story.pipeline import _cap, _reason  # the pipeline imports this module

        st.emit(kind, reason=_cap(_reason(error)))  # the driver's fields and cap (`story.survey_failed`)
    return {FAIL_KEY: f"{type(error).__name__}: {error}"}


def _value(value: Any) -> Any:
    return value.to_dict() if hasattr(value, "to_dict") else value


# ---- ask ----


async def ask_runner(
    node: NodeSpec,
    ctx: RunContext,
    *,
    asker: Asker,
    st: Recorder,
    seat: Seat = str,
    ask: Callable[..., Awaitable[str]] | None = None,
) -> Mapping[str, Any]:
    """One seat, one brief, one reply. With a fielded output the seat is asked until the reply parses by
    its spec (`Asker.usable`: `steps` tries, each failed try's reason as `{feedback}` in the next brief) and
    the parsed fields are that output; without, it is asked once (at most `steps` tool steps, a short ask)
    and the whole reply is its first output. The stage the asker records is the node's name, the role
    `seat(node.role)`, and `ask` replaces `Asker.ask` (a workflow's own map for the turn)."""
    role = _role(node, seat)
    steps = _steps(node, ctx)  # resolved before the ask: a wrong count is no failed ask to continue past
    asking = ask or asker.ask
    fielded = structured(node)
    replies: list[str] = []
    try:
        if fielded is not None:
            name, spec = fielded

            def parse(text: str) -> Any:
                replies.append(text)
                return parse_output(text, spec)

            value: Any = await asker.usable(
                st,
                ctx.node,
                role,
                name,
                lambda feedback: brief(node, ctx, feedback),
                parse,
                steps,
                ask=asking,
            )
        else:
            name = next(iter(node.outputs), ctx.node)
            value = await asking(st, ctx.node, role, brief(node, ctx), steps=steps, long=False)
            replies.append(value)
    except Exception as e:
        return _failed(node, ctx, st, e)
    _done(node, st, replies[-1])
    return {name: _value(value)} if name in node.outputs else {}


# ---- check, gate ----


async def check_runner(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
    """A deterministic step: the catalog's check the node's `run` names (`Registry.check`: the registry's
    catalog, else its id) called as `fn(ctx, **node.args)` (it may be async), its inputs read from
    `ctx.store`, and the mapping it returns is the node's outputs (False or None: none). Whatever it raises
    propagates: the composer's caller says what a stop means (waiting, blocked)."""
    assert node.run is not None  # spec.py: a check needs run
    fn = ctx.registry.check(node.run)  # resolved before the call: a wrong id is a config error
    out = await _maybe(fn(ctx, **node.args))
    return out or {}


async def gate_runner(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
    """The run's end: the check the node's `run` names (`Registry.check`) called as `fn(ctx, **node.args)`
    (it may be async), then `EndRun`, whatever it returned. The composer finishes the node as done (not in
    the done set: a resume reaches the gate again) and its `run` returns the gate's name; whatever the check
    raises propagates as a check's does."""
    assert node.run is not None  # spec.py: a gate needs run
    fn = ctx.registry.check(node.run)  # resolved before the call: a wrong id is a config error
    await _maybe(fn(ctx, **node.args))
    raise EndRun()


# ---- held conversations: conversation, write ----


@dataclass(frozen=True)
class Opening:
    """How a conversation starts: its opening `brief`, the tool `steps` a turn has (None: its role's own),
    the stage the asker records (None: the node's name), the commit it reads (`at`, None: the record's
    base), `Asker.conversation`'s other keywords (`options`: the map, what it may write), and the
    workflow's own `state` the session carries (a write node's: its checkout, the story's drafting: the
    checkout's turns)."""

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


def session_output(node: NodeSpec) -> str | None:
    """The node's output of format `session`, if it has one."""
    return next((n for n, spec in node.outputs.items() if spec.format == "session"), None)


@contextlib.asynccontextmanager
async def held_session(
    node: NodeSpec, ctx: RunContext, asker: Asker, st: Recorder, role: str, opening: Callable[[], Opening]
) -> AsyncIterator[tuple[Session, bool]]:
    """The node's conversation and whether it continues one: the session the node's `session` names, when
    one is held (the node's first turn is then a nudge in it); else a new one from `opening()`, held on the
    run's stack when the node outputs a session (dropped from the sessions when the stack closes it), else
    closed when the block ends."""
    continued = ctx.sessions.get(node.session) if node.session is not None else None
    if continued is not None:
        yield continued, True
        return
    output = session_output(node)
    held = ctx.held if output is not None else None
    if output is not None:
        if held is None:
            raise RuntimeError(f"node '{ctx.node}' outputs a session, but the run holds nothing open")
        name = output if ctx.item is None else output.replace("{item}", ctx.item)
        held.callback(ctx.sessions.pop, name)  # after the conversation closes (the stack is LIFO)
    async with converse(asker, st, role, opening(), stage=ctx.node, held=held) as session:
        yield session, False


async def conversation_runner(
    node: NodeSpec,
    ctx: RunContext,
    *,
    asker: Asker,
    st: Recorder,
    seat: Seat = str,
) -> Mapping[str, Any]:
    """One seat with a held transcript. Turn 0 is the rendered prompt: the conversation's opening, or, for a
    node that continues a held `session`, one more turn of that conversation; each later turn sends the
    rendered `turn` text, its `{feedback}` the reason the last reply did not parse. Each reply is parsed by
    the fielded output's spec and the conversation ends on the first that parses, within `steps` turns
    (default 1); without a fielded output the first reply is the value. The value is the first non-session
    output; a session output is the conversation, held for the run (`Composer.held`). A conversation that
    never parses fails with the last reason (`on_fail`)."""
    role = _role(node, seat)
    count = _steps(node, ctx) or 1
    fielded = structured(node)
    first = next((n for n, spec in node.outputs.items() if spec.format != "session"), None)
    reply, value, reason = "", None, ""
    try:
        async with held_session(node, ctx, asker, st, role, lambda: Opening(brief(node, ctx))) as (s, cont):
            for index in range(count):
                if index == 0:
                    nudge = brief(node, ctx) if cont else None
                elif node.turn is not None:
                    nudge = brief(node, ctx, reason, text=node.turn)
                else:
                    break
                reply = await s.say(nudge)
                if fielded is None:
                    value = reply
                    break
                try:
                    value = parse_output(reply, fielded[1])
                    break
                except StoryError as e:
                    reason, value = str(e), None
            if fielded is not None and value is None:
                raise StoryBlocked(f"{role} did not produce a usable {fielded[0]}: {reason}")
            session = s if not cont else None  # a continued session is the node's input, not its output
    except Exception as e:
        return _failed(node, ctx, st, e)
    _done(node, st, reply)
    out: dict[str, Any] = {}
    name = fielded[0] if fielded is not None else first
    if name is not None:
        out[name] = _value(value)
    if (output := session_output(node)) is not None and session is not None:
        out[output] = session
    return out


@dataclass
class Checkout:
    """Where a write node's conversation writes (its session's `state`): the worktree, and the commit its
    first turn started from, as `Writes.prepare` found them."""

    tree: Path | None = None
    start: str | None = None

    async def prepare(self, tree: Path) -> None:
        self.tree = tree
        self.start = _git(tree, "rev-parse", "HEAD").strip()

    def diff(self) -> str:
        """Everything the conversation changed since its start, new files included. It is read through a
        copy of the checkout's index, so nothing is left staged in a checkout a held session goes on in."""
        if self.tree is None or self.start is None:
            return ""
        index = Path(_git(self.tree, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if index.is_file():
                shutil.copyfile(index, env["GIT_INDEX_FILE"])
            _git(self.tree, "add", "-A", env=env)
            return _git(self.tree, "diff", "--cached", self.start, env=env)


def _git(tree: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True, text=True, env=env)
    return done.stdout


def write_tools(node: NodeSpec, ctx: RunContext, role: str) -> tuple[str, ...]:
    """The write tools a write node's turn gets: the node's `tools`, else the role's own write tools."""
    from airlie.topology.studio import WRITE_TOOLS  # the studio imports the config, which imports flow.spec

    if node.tools is not None:
        return tuple(node.tools)
    spec = ctx.cfg.roles[role]
    listed = ctx.cfg.tools.builtin if spec.tools is None else spec.tools
    return tuple(t for t in listed if t in WRITE_TOOLS)


def _worktree_at(node: NodeSpec, ctx: RunContext) -> str | None:
    """The commit of the worktree a `ref:worktree` input names (its checked-out HEAD), else None."""
    for name in node.inputs:
        if ctx.formats.get(name) == "ref:worktree" and ctx.store.has(name):
            return _git(Path(str(ctx.store.get(name))), "rev-parse", "HEAD").strip()
    return None


async def write_runner(
    node: NodeSpec,
    ctx: RunContext,
    *,
    asker: Asker,
    st: Recorder,
    seat: Seat = str,
) -> Mapping[str, Any]:
    """One writing turn of a conversation: the rendered prompt, with the node's write tools (`write_tools`)
    on any file (`Writes.scope` "any"), on a checkout of its own detached at the commit of the worktree a
    `ref:worktree` input names (its HEAD: edits not committed there are not seen until Part 2), else at the
    record's base, at most `steps` tool steps. A node that continues a held `session` sends its prompt as
    the next turn of it, with the same `steps`. Its outputs: `diff` (or `diff.<x>`) is the change since the
    conversation's start (`git diff`, new files included), `account` (or `account.<x>`) the reply, a
    `session` output the held conversation; any other output is refused (`StoryError`), before anything is
    asked."""
    role = _role(node, seat)
    steps = _steps(node, ctx)
    routes = _write_routes(node, ctx)
    checkout = Checkout()

    def opening() -> Opening:
        grant = Writes(tools=write_tools(node, ctx, role), scope="any", prepare=checkout.prepare)
        at = _worktree_at(node, ctx)
        return Opening(brief(node, ctx), steps, at=at, options={"writes": grant}, state=checkout)

    try:
        async with held_session(node, ctx, asker, st, role, opening) as (s, cont):
            reply = await (s.say(brief(node, ctx), steps=steps) if cont else s.say())
            diff = s.state.diff() if isinstance(s.state, Checkout) else ""
            session = s if not cont else None
    except Exception as e:
        return _failed(node, ctx, st, e)
    _done(node, st, reply)
    values = {"diff": diff, "account": reply, "session": session}
    return {name: values[what] for name, what in routes.items() if values[what] is not None}


def _write_routes(node: NodeSpec, ctx: RunContext) -> dict[str, str]:
    """What each output of a write node holds: `diff`, `account` (by the name's head) or its `session`."""
    routes: dict[str, str] = {}
    for name, spec in node.outputs.items():
        head = name.partition(".")[0]
        if spec.format == "session":
            routes[name] = "session"
        elif head in ("diff", "account"):
            routes[name] = head
        else:
            raise StoryError(
                f"write node '{ctx.node}': output '{name}' is none of what a write produces "
                "(diff, account, a session)"
            )
    return routes


# ---- meeting ----


@dataclass(frozen=True)
class Hold:
    """How a meeting's protocol holds its meeting (`meeting_runner`): called with the checkout the seats'
    tools read and `Meets.run`'s arguments, it makes the meeting from the factory (`tools(checkout)`, a
    fresh `on_turn()`, which saves the team state every turn) and holds it from `state`, the team state a
    meeting cut short saved (None: afresh). `before_turn()` is what each seat's turn waits for first, when
    there is one and the meeting takes it (`Meeting.before_turn`); `turn` is the node's turn text, which
    replaces the hub's in every seat's system message (`Meeting.turn_text`; None: the hub's). The turn cap
    is read from the same config by the factory."""

    meeting: MeetingFactory
    cfg: AirlieConfig
    tools: Callable[[Path], Callable[[str], ToolRegistry]]
    on_turn: Callable[[], OnTurn]
    before_turn: Callable[[], Callable[[], Awaitable[Any]] | None] = lambda: None
    state: Mapping[str, Any] | None = None
    turn: str | None = None

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
        if self.turn is not None and hasattr(meeting, "turn_text"):
            meeting.turn_text = self.turn
        return await meeting.run(
            opening, story, layers, levels, guidelines, repo=repo, base=base, state=self.state
        )


Agree = Callable[[RunContext, Hold, str | None], Awaitable[Mapping[str, Any]]]
"""A meeting's protocol: holds the meeting through the `Hold` with the opening text (None: its own) and
returns what it agreed, by output name."""


async def meeting_runner(
    node: NodeSpec,
    ctx: RunContext,
    *,
    agree: Agree,
    meeting: MeetingFactory,
    tools: Callable[[Path], Callable[[str], ToolRegistry]],
    on_turn: Callable[[], OnTurn],
    before_turn: Callable[[], Callable[[], Awaitable[Any]] | None] = lambda: None,
    roles: Collection[str] | None = None,
) -> Mapping[str, Any]:
    """Several seats on one shared transcript, by the workflow's protocol (`agree`, D20): it is handed a
    `Hold` over the factory, the tools, the turn callback, the node's rendered `turn` text and the team state
    the store saved as `meeting` (so a killed meeting goes on from its next speaker), and the node's rendered
    `prompt` as its opening text. The node's declared outputs are taken from what it returns. `roles` are
    the seats the protocol holds: a node that names others is a config error."""
    assert isinstance(node.role, list)  # spec.py: a meeting names its seats
    if roles is not None and list(node.role) != list(roles):
        raise ValueError(f"a meeting node seats {', '.join(roles)}, in that order; it names {node.role}")
    opening = brief(node, ctx) if node.prompt is not None else None
    turn = brief(node, ctx, text=node.turn) if node.turn is not None else None
    state = ctx.store.get("meeting") if ctx.store.has("meeting") else None
    hold = Hold(meeting, ctx.cfg, tools, on_turn, before_turn, state, turn)
    out = await agree(ctx, hold, opening)
    return {name: out[name] for name in node.outputs if name in out}


async def _maybe(result: Any) -> Any:
    """`result`, awaited when a hook returned an awaitable."""
    return await result if inspect.isawaitable(result) else result
