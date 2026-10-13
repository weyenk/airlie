"""Behaviour every Engine must satisfy; imported by the FakeEngine and HTTPEngine tests."""

from __future__ import annotations

from airlie.config import Sampling
from airlie.engine import GenRequest, Message, TokenEvent


def plain_request(role: str = "coder") -> GenRequest:
    return GenRequest(
        role=role,
        messages=[
            Message(role="system", content="Reply in one short sentence."),
            Message(role="user", content="Name three primary colours."),
        ],
        sampling=Sampling(temperature=0.0),
        max_tokens=32,
        stop=[],
        thinking=False,
    )


async def check_plain_stream(engine, req: GenRequest, expected_text_contains: str) -> None:
    seen: list[TokenEvent] = []
    res = await engine.generate(req, on_token=seen.append)
    assert expected_text_contains in res.text
    assert "".join(t.text for t in seen if not t.reasoning) == res.text
    assert res.finish_reason == "stop"
    assert res.prompt_tokens > 0 and res.completion_tokens > 0
    assert res.ttft_ms >= 0
    assert engine.stats().requests >= 1
