"""HTTPEngine against the real managed vllm-mlx engine (task up). Marked live; skipped when it is down."""

from __future__ import annotations

import json

import pytest

from airlie.config import Sampling
from airlie.engine import GenRequest, Message, TokenEvent, ToolSchema

pytestmark = pytest.mark.live


def req(role: str, content: str, **kw) -> GenRequest:
    return GenRequest(
        role=role,
        messages=[Message(role="user", content=content)],
        sampling=Sampling(temperature=0.0),
        max_tokens=kw.pop("max_tokens", 64),
        stop=[],
        **kw,
    )


async def test_health_lists_models(engine, cfg) -> None:
    h = await engine.health()
    assert h.ok
    assert set(cfg.engines["main"].models) <= set(h.models)


async def test_plain_generation_streams(engine) -> None:
    seen: list[TokenEvent] = []
    res = await engine.generate(
        req("coder", "Reply with the single word: pong", thinking=False, max_tokens=8), on_token=seen.append
    )
    assert "pong" in res.text.lower()
    assert res.finish_reason in ("stop", "length")
    assert res.prompt_tokens > 0 and res.completion_tokens > 0
    assert seen and res.ttft_ms > 0 and res.decode_tps >= 0


async def test_reasoning_is_separated(engine) -> None:
    res = await engine.generate(
        req(
            "coder",
            "What is 12*12? Think briefly, then answer with just the number.",
            thinking=True,
            max_tokens=512,
        )
    )
    assert res.reasoning.strip() != ""
    assert "144" in res.text


async def test_native_tool_call(engine) -> None:
    tool = ToolSchema(
        "read_file",
        "Read a file from the repository",
        {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )
    res = await engine.generate(
        req(
            "coder", "Use the read_file tool to read README.md.", thinking=False, max_tokens=128, tools=[tool]
        )
    )
    assert res.finish_reason == "tool_calls"
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0].name == "read_file"
    assert json.loads(res.tool_calls[0].arguments)["path"].endswith("README.md")


async def test_yes_no_on_judge(engine) -> None:
    yes = await engine.yes_no("judge", [Message(role="user", content="Is 2 + 2 equal to 4?")])
    no = await engine.yes_no("judge", [Message(role="user", content="Is 2 + 2 equal to 5?")])
    assert yes == (1.0, 0.0)
    assert no == (0.0, 1.0)


async def test_second_turn_after_tool_result_is_faster_than_first(engine) -> None:
    """Prefix cache: turn 2 resends the whole history; its TTFT must not pay the full prefill again."""
    long_system = "You are a careful engineer. " * 400  # ~2.4k tokens of shared prefix
    first = GenRequest(
        role="coder",
        messages=[Message(role="system", content=long_system), Message(role="user", content="Say OK.")],
        sampling=Sampling(temperature=0.0),
        max_tokens=4,
        stop=[],
        thinking=False,
    )
    r1 = await engine.generate(first)
    second = GenRequest(
        role="coder",
        messages=[
            *first.messages,
            Message(role="assistant", content=r1.text),
            Message(role="user", content="Say OK again."),
        ],
        sampling=Sampling(temperature=0.0),
        max_tokens=4,
        stop=[],
        thinking=False,
    )
    r2 = await engine.generate(second)
    assert r2.prompt_tokens > r1.prompt_tokens
    assert r2.ttft_ms < r1.ttft_ms * 0.8, f"turn 2 TTFT {r2.ttft_ms:.0f} ms vs turn 1 {r1.ttft_ms:.0f} ms"
