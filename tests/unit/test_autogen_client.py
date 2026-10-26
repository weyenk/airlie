"""The AutoGen client parses a model's tool calls and reasoning when the server did not, as HTTPEngine does.

Found on the first real three-amigos run (2026-10-25): the server runs one tool parser for every model it
serves (qwen3_xml_raw), so Muse's `<atem:function_calls>` came back as text and its turns never reached a
verdict; and AutoGen's tool-less reflection call came back as a raw `<tool_call>` when the generalist still
wanted to read."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, LLMMessage, RequestUsage, UserMessage
from autogen_ext.models.openai import OpenAIChatCompletionClient

from airlie.config import ModelSpec
from airlie.prompts import entry
from airlie.teams.autogen_client import RepairingOpenAIClient

MUSE = ModelSpec(repo="r", revision="x", tool_call_parser="muse_glimmer", reasoning_parser="muse_glimmer")
QWEN = ModelSpec(repo="r", revision="x", tool_call_parser="qwen3_xml_raw", reasoning_parser="qwen3")
MUSE_CALL = (
    "to=selfWe need to understand the repository test tree. Let's list dir.<|eom|>assistant to=list_dir"
    '<atem:function_calls>\n<atem:invoke name="list_dir">\n<atem:parameter name="path">.</atem:parameter>\n'
    "</atem:invoke>\n</atem:function_calls>"
)
QWEN_CALL = (
    "<tool_call>\n<function=read_file>\n<parameter=path>\nAGENTS.md\n</parameter>\n</function>\n</tool_call>"
)
TOOL = {"name": "list_dir", "description": "List", "parameters": {"type": "object", "properties": {}}}


def reply(content: str | list[FunctionCall]) -> CreateResult:
    return CreateResult(
        finish_reason="stop",
        content=content,
        usage=RequestUsage(prompt_tokens=1, completion_tokens=1),
        cached=False,
    )


class Server(list[tuple[list[LLMMessage], int]]):
    """What the server is asked (messages, number of tools offered), and a queue of what it answers."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: list[CreateResult] = []


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Server:
    server = Server()

    async def create(
        self: Any, messages: Sequence[LLMMessage], *, tools: Sequence[Any] = (), **_: Any
    ) -> Any:
        server.append((list(messages), len(tools)))
        return server.answers.pop(0)

    monkeypatch.setattr(OpenAIChatCompletionClient, "create", create)
    return server


def client(model: ModelSpec | None) -> RepairingOpenAIClient:
    c = RepairingOpenAIClient(
        model="reviewer",
        base_url="http://127.0.0.1:1/v1",
        api_key="x",
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": False,
            "family": "unknown",
            "structured_output": False,
        },
    )
    c.model_spec = model
    return c


async def test_a_muse_tool_call_the_server_left_as_text_becomes_a_call(served: Any) -> None:
    served.answers.append(reply(MUSE_CALL))
    res = await client(MUSE).create([UserMessage(content="go", source="u")], tools=[TOOL])
    assert isinstance(res.content, list) and [(c.name, c.arguments) for c in res.content] == [
        ("list_dir", '{"path": "."}')
    ]
    assert res.finish_reason == "function_calls"
    assert res.thought is not None and "test tree" in res.thought


async def test_a_muse_answer_loses_its_channel_headers_and_keeps_the_verdict(served: Any) -> None:
    verdict = '```json\n{"type": "verdict", "requirements": "agree"}\n```'
    served.answers.append(reply("to=selfThe plan holds.<|eom|>to=user" + verdict))
    res = await client(MUSE).create([UserMessage(content="go", source="u")], tools=[TOOL])
    assert res.content == verdict and res.thought == "The plan holds."


async def test_a_call_on_the_tool_less_last_request_is_asked_again_once_for_the_answer(served: Any) -> None:
    served.answers += [reply(QWEN_CALL), reply("Here is my turn.")]
    res = await client(QWEN).create([UserMessage(content="go", source="u")], tools=[])
    assert res.content == "Here is my turn."
    (first, n0), (second, n1) = served
    assert n0 == n1 == 0 and len(second) == len(first) + 2
    assert second[-1].content == entry("last-turn-retry").body.strip()  # type: ignore[union-attr]


async def test_a_second_call_on_the_tool_less_request_ends_as_text_without_the_call(served: Any) -> None:
    served.answers += [reply(QWEN_CALL), reply("Still reading." + QWEN_CALL)]
    res = await client(QWEN).create([UserMessage(content="go", source="u")], tools=[])
    assert res.content == "Still reading."
    assert len(served) == 2


async def test_calls_the_server_parsed_and_a_client_without_a_model_are_left_alone(served: Any) -> None:
    call = [FunctionCall(id="1", name="list_dir", arguments="{}")]
    served.answers += [reply(call), reply(MUSE_CALL)]
    assert (await client(MUSE).create([UserMessage(content="go", source="u")], tools=[TOOL])).content == call
    assert (
        await client(None).create([UserMessage(content="go", source="u")], tools=[TOOL])
    ).content == MUSE_CALL


def test_the_meetings_clients_carry_their_models_parsers() -> None:
    from pathlib import Path

    from airlie.config.load import load_config
    from airlie.story.amigos import http_clients

    root = Path(__file__).resolve().parents[2]
    cfg = load_config(root / "airlie.yaml", root / "no-local-overrides.yaml")
    make = http_clients(cfg)
    tester = cfg.roles["tester"]
    built = make("tester", tester)
    assert isinstance(built, RepairingOpenAIClient)
    assert built.model_spec is not None and built.model_spec.tool_call_parser == "muse_glimmer"


async def test_a_reply_with_no_answer_in_it_is_asked_again_once(served: Any) -> None:
    """Run 6 (2026-10-26): Muse ended meeting turns with reasoning only, or a bare `to=user`."""
    verdict = '```json\n{"type": "verdict"}\n```'
    thinking_only = "to=selfI should check the plan first.<|eom|>to=user"
    served.answers += [reply(thinking_only), reply("to=user" + verdict)]
    res = await client(MUSE).create([UserMessage(content="go", source="u")], tools=[TOOL])
    assert res.content == verdict
    assert served[1][0][-1].content == entry("empty-reply").body.strip()  # type: ignore[union-attr]
    served.answers += [reply("to=user"), reply("to=user")]
    assert (await client(MUSE).create([UserMessage(content="go", source="u")], tools=[])).content == ""
    assert len(served) == 4  # once only
