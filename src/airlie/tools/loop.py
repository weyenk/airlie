"""A bounded tool loop for the roles that make one model call, not a team run: the chief (plans and
synthesises) and the reviewer (reviews a diff). With tools (web_search, web_fetch) they may look something up
before they answer; without, the call is the plain one it always was.

The loop is short on purpose: a few calls, then the model is asked for its answer with no tools offered, so a
role that keeps searching still ends in a reply."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from airlie.engine.protocol import Engine
from airlie.engine.types import GenRequest, GenResult, Message
from airlie.prompts import text
from airlie.tools.jsonargs import history_arguments
from airlie.tools.registry import ToolRegistry

DEFAULT_STEPS = 4
"""Tool calls a role may make before its answer."""


async def generate_with_tools(
    engine: Engine,
    req: GenRequest,
    tools: ToolRegistry | None,
    max_steps: int = DEFAULT_STEPS,
    *,
    on_steer: Callable[[str], None] | None = None,
) -> GenResult:
    """`engine.generate(req)`, with `tools` offered: each reply that asks for a tool has it run (one call per
    reply) and goes back with the result, up to `max_steps` calls; then one last request offers no tools and
    asks for the answer. No registry, or one with no tools: the plain call. `on_steer` is told the id of each
    hub steering text the loop sends (`no-more-tools`), so a caller with an event stream records it as a
    `steer` event (spec 2026-10-22-prompt-hub, "Steering events"); the loop itself has no stream."""
    if tools is None or not tools.names():
        return await engine.generate(req)
    messages = list(req.messages)
    schemas = tools.schemas()
    for _ in range(max_steps):
        res = await engine.generate(replace(req, messages=messages, tools=schemas))
        calls = res.tool_calls[:1]  # one action per reply, as the team loop does
        if not calls:
            return res
        call = calls[0]
        result = await tools.call(call)
        replay = replace(call, arguments=history_arguments(call.arguments))
        messages.append(Message(role="assistant", content=res.text, tool_calls=(replay,)))
        messages.append(Message(role="tool", content=result.output, tool_call_id=call.id, name=call.name))
    messages.append(Message(role="user", content=text("no-more-tools")))
    if on_steer is not None:
        on_steer("no-more-tools")
    return await engine.generate(replace(req, messages=messages, tools=None))
