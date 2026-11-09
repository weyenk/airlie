"""The request fields per engine kind (airlie.engine.dialect), and that vllm-mlx bodies did not change."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import httpx

from airlie.config import RoleSpec, Sampling
from airlie.engine import GenRequest, Message, ToolSchema
from airlie.engine.dialect import dialect_for, thinking_fields
from airlie.engine.http_engine import HTTPEngine


def test_vllm_mlx_fields_are_what_the_client_sends_today() -> None:
    assert thinking_fields(dialect="vllm_mlx", thinking=True, budget=4096, chat_template_kwargs={"x": 1}) == {
        "enable_thinking": True,
        "chat_template_kwargs": {"enable_thinking": True, "x": 1},
        "thinking_token_budget": 4096,
    }


def test_mlx_vlm_fields_use_its_names_and_map_reasoning_strength() -> None:
    assert thinking_fields(
        dialect="mlx_vlm",
        thinking=True,
        budget=4096,
        chat_template_kwargs={"reasoning_strength": "low"},
        tool_parser="atem",
        tools=True,
    ) == {"enable_thinking": True, "thinking_budget": 4096, "reasoning_effort": "low", "tool_parser": "atem"}


def test_mlx_vlm_fields_omit_what_is_unset() -> None:
    assert thinking_fields(dialect="mlx_vlm", thinking=False, budget=4096, chat_template_kwargs={}) == {
        "enable_thinking": False,
    }  # no budget when not thinking; no tool_parser without tools or a parser name
    assert "tool_parser" not in thinking_fields(
        dialect="mlx_vlm", thinking=True, budget=None, chat_template_kwargs={}, tool_parser="atem"
    )


def test_the_dialect_follows_the_engine_kind() -> None:
    assert dialect_for("mlx_vlm") == "mlx_vlm"
    assert {dialect_for(k) for k in ("vllm_mlx", "llama_server", "openai_http")} == {"vllm_mlx"}


# What HTTPEngine sent before the dialect module, recorded from that code, byte for byte
THINKING_WITH_TOOLS = (
    '{"model":"small","messages":[{"role":"user","content":"hi"}],"stream":true,"max_tokens":8,'
    '"temperature":0.7,"top_p":0.8,"top_k":20,"min_p":0.0,"presence_penalty":0.0,'
    '"chat_template_kwargs":{"enable_thinking":true,"x":1},"enable_thinking":true,'
    '"stream_options":{"include_usage":true},"tools":[{"type":"function","function":{"name":"f",'
    '"description":"d","parameters":{"type":"object"}}}],"tool_choice":"auto","thinking_token_budget":4096}'
)
PLAIN = (
    '{"model":"small","messages":[{"role":"user","content":"hi"}],"stream":true,"max_tokens":8,'
    '"temperature":0.7,"top_p":0.8,"top_k":20,"min_p":0.0,"presence_penalty":0.0,'
    '"chat_template_kwargs":{"enable_thinking":false},"enable_thinking":false,'
    '"stream_options":{"include_usage":true}}'
)
YES_NO = (
    '{"model":"small","messages":[{"role":"system","content":"Answer with exactly one word: YES or NO."},'
    '{"role":"user","content":"hi"}],"stream":false,"max_tokens":3,"temperature":0.0,'
    '"chat_template_kwargs":{"enable_thinking":false},"enable_thinking":false}'
)


def _spec(**kw: object) -> RoleSpec:
    return RoleSpec(prompt=Path("/dev/null"), model="small", max_tokens=64, **kw)  # type: ignore[arg-type]


async def test_vllm_mlx_request_bodies_are_byte_for_byte_what_they_were() -> None:
    sent: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        sent.append(req.content.decode())
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "YES"}, "finish_reason": "stop"}], "usage": {}}
        )

    specs = {
        "t": _spec(thinking=True, thinking_budget=4096, chat_template_kwargs={"x": 1}),
        "n": _spec(thinking=False, thinking_budget=4096),
    }
    eng = HTTPEngine(
        "http://e/v1", lambda r: "small", lambda r: specs[r], transport=httpx.MockTransport(handle)
    )
    tool = ToolSchema(name="f", description="d", parameters={"type": "object"})
    for role, tools in (("t", [tool]), ("n", None)):
        req = GenRequest(
            role=role, messages=[Message(role="user", content="hi")], sampling=Sampling(), max_tokens=8
        )
        req.tools = tools
        with contextlib.suppress(Exception):  # the stub answers JSON to a stream; only the body matters
            await eng.generate(req)
    await eng.yes_no("t", [Message(role="user", content="hi")])
    assert sent == [THINKING_WITH_TOOLS, PLAIN, YES_NO]
    assert json.loads(sent[0])["thinking_token_budget"] == 4096
