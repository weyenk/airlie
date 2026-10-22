"""Fork-point diversification (design §3.3): hypotheses from a small-model role, or a temperature spread."""

from __future__ import annotations

import copy
import re
from typing import Any

from airlie.config import RoleSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import EngineError, GenRequest, Message
from airlie.prompts import text
from airlie.skills import prompt_text

_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+(.+?)\s*$")
_FAILED = re.compile(r"(\d+) failed")
_PASSED = re.compile(r"(\d+) passed")


def parse_hypotheses(text: str, width: int) -> list[str] | None:
    found: list[str] = []
    for line in text.splitlines():
        m = _NUMBERED.match(line)
        if m and m.group(2) not in found:
            found.append(m.group(2))
    return found[:width] if len(found) >= width else None


async def hypotheses(
    engine: Engine, role_name: str, role: RoleSpec, *, goal: str, diff: str, test_tail: str, width: int
) -> list[str] | None:
    """One call; None when the output cannot be parsed (caller falls back to temperature)."""
    prompt = (
        f"Goal:\n{goal}\n\nThe branch's last change:\n```\n{diff or '(no change)'}\n```\n\n"
        f"The tests still fail:\n```\n{test_tail or '(no output)'}\n```\n\n"
        f"List {width} distinct root-cause hypotheses, each with a different fix approach, one per line, "
        f"numbered 1. to {width}. No other text."
    )
    req = GenRequest(
        role=role_name,
        messages=[
            Message(role="system", content=prompt_text(role.prompt)),
            Message(role="user", content=prompt),
        ],
        sampling=role.sampling,
        max_tokens=role.max_tokens,
        stop=list(role.stop),
        tools=[],
        thinking=role.thinking,
    )
    try:
        res = await engine.generate(req)
    except EngineError:
        return None
    return parse_hypotheses(res.text, width)


def nudge_text(
    label: str,
    width: int,
    hypothesis: str | None,
    test_tail: str,
    repeated: tuple[str, str, str] | None = None,
) -> str:
    """The nudge a fork branch starts with: the hub's branch sentences around the framing kept here (the
    stalled call's error cut to its last 600 characters, the last test output to 1500, and the hypothesis)."""
    lines = [text("branch-start", label=label, width=width)]
    if repeated:
        name, args, out = repeated
        lines.append(text("branch-stalled", name=name, args=args, out=out[-600:]))
    if test_tail:
        lines.append(f"Last test output:\n```\n{test_tail[-1500:]}\n```")
    if hypothesis:
        lines.append(f"Pursue this hypothesis: {hypothesis}")
    else:
        lines.append(text("branch-different"))
    lines.append(text("branch-finish"))
    return "\n\n".join(lines)


def role_at_temperature(role: RoleSpec, t: float) -> RoleSpec:
    return role.model_copy(update={"sampling": role.sampling.model_copy(update={"temperature": t})})


def parse_failing(test_output: str) -> int | None:
    m = _FAILED.search(test_output)
    if m:
        return int(m.group(1))
    return 0 if _PASSED.search(test_output) else None


def _is_action_pair(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """(assistant tool call, tool result) in either the native or the AutoGen state format."""
    native = a.get("role") == "assistant" and bool(a.get("tool_calls")) and b.get("role") == "tool"
    autogen = a.get("type") == "AssistantMessage" and b.get("type") == "FunctionExecutionResultMessage"
    return native or autogen


def trim_repeated_tail(state: dict[str, Any], repeat_limit: int) -> dict[str, Any]:
    """Drop up to repeat_limit trailing (action, result) pairs so a child does not inherit a stuck loop."""
    out = copy.deepcopy(state)
    msgs: list[dict[str, Any]] = out["llm_context"]["messages"] if "llm_context" in out else out["messages"]
    dropped = 0
    while dropped < repeat_limit and len(msgs) >= 2 and _is_action_pair(msgs[-2], msgs[-1]):
        del msgs[-2:]
        dropped += 1
    return out
