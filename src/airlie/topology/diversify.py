"""Fork-point diversification (design §3.3): hypotheses from a small-model role, or a temperature spread."""

from __future__ import annotations

import re

from airlie.config import RoleSpec
from airlie.engine.protocol import Engine
from airlie.engine.types import EngineError, GenRequest, Message

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
            Message(role="system", content=role.prompt.read_text()),
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


def nudge_text(label: str, width: int, hypothesis: str | None, test_tail: str) -> str:
    lines = [
        f"You are now branch {label} of {width}, continuing from the conversation above in your own copy "
        "of the repository. The previous approach did not make the tests pass; do not repeat it.",
    ]
    if test_tail:
        lines.append(f"Last test output:\n```\n{test_tail[-1500:]}\n```")
    if hypothesis:
        lines.append(f"Pursue this hypothesis: {hypothesis}")
    else:
        lines.append("Take a different approach from the one you tried.")
    lines.append("Make the change, run the tests, and stop with a short summary when they pass.")
    return "\n\n".join(lines)


def role_at_temperature(role: RoleSpec, t: float) -> RoleSpec:
    return role.model_copy(update={"sampling": role.sampling.model_copy(update={"temperature": t})})


def parse_failing(test_output: str) -> int | None:
    m = _FAILED.search(test_output)
    if m:
        return int(m.group(1))
    return 0 if _PASSED.search(test_output) else None
