"""The generic node runners: one loop per node type, with a workflow's own pieces (brief builders, parsers,
after-hooks) looked up in the registry by id (docs/superpowers/specs/2026-11-06-generic-nodes-design.md).

Nothing here knows a story: a workflow binds the asker, the record it writes on and how a YAML role names a
configured seat (`functools.partial`), and registers the pieces its nodes name."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from airlie.flow.composer import FAIL_KEY, RunContext, resolve_count
from airlie.flow.spec import NodeSpec
from airlie.story.asking import Asker, Recorder, StoryBlocked
from airlie.story.model import NotActionable


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


async def _maybe(result: Any) -> Any:
    """`result`, awaited when a hook returned an awaitable."""
    return await result if inspect.isawaitable(result) else result
