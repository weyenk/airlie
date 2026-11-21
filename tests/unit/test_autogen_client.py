"""The AutoGen client tells a role its tool-less reflection request offers no tools.

Wiki run B (2026-10-27): after its tool rounds AutoGen's reflection request offers no tools, and a role that
still wanted to read answered in prose, with no verdict."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from autogen_core import FunctionCall
from autogen_core.models import CreateResult, LLMMessage, RequestUsage, UserMessage
from autogen_ext.models.openai import OpenAIChatCompletionClient

from airlie.prompts import entry
from airlie.teams.autogen_client import RepairingOpenAIClient


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


def client() -> RepairingOpenAIClient:
    return RepairingOpenAIClient(
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


async def test_the_tool_less_request_after_a_tool_round_says_no_tools_are_left(served: Any) -> None:
    """Wiki run B (2026-10-27): after its tool rounds, AutoGen's reflection request offers no tools, and the
    role answered "Let me read the files I haven't seen yet" in prose, with no verdict, in 6 of 8 meeting
    turns. That request now ends by saying no tools are left, so the role answers instead."""
    from autogen_core import FunctionCall as Call
    from autogen_core.models import AssistantMessage, FunctionExecutionResult, FunctionExecutionResultMessage

    history = [
        UserMessage(content="go", source="u"),
        AssistantMessage(content=[Call(id="1", name="read_file", arguments="{}")], source="a"),
        FunctionExecutionResultMessage(
            content=[FunctionExecutionResult(call_id="1", content="x", name="read_file")]
        ),
    ]
    served.answers.append(reply("My verdict."))
    assert (await client().create(history, tools=[])).content == "My verdict."
    sent, _ = served[0]
    assert sent[-1].content == entry("last-turn-retry").body.strip()  # type: ignore[union-attr]
    served.answers.append(reply("fine"))
    await client().create([UserMessage(content="go", source="u")], tools=[])
    assert served[1][0][-1].content == "go"  # a tool-less request that follows no tool round is left as it is


async def test_the_reflection_request_carries_the_callers_note(served: Any) -> None:
    """2026-10-31 (roman): the architect's meeting turns that used compute ended in the reflection with the
    checks' results and no verdict, twice. The meeting sets its verdict contract as the note."""
    from autogen_core import FunctionCall as Call
    from autogen_core.models import AssistantMessage, FunctionExecutionResult, FunctionExecutionResultMessage

    history = [
        UserMessage(content="go", source="u"),
        AssistantMessage(content=[Call(id="1", name="compute", arguments="{}")], source="a"),
        FunctionExecutionResultMessage(
            content=[FunctionExecutionResult(call_id="1", content="15", name="compute")]
        ),
    ]
    served.answers.append(reply("fine"))
    c = client()
    c.reflection_note = "End with your verdict."
    await c.create(history, tools=[])
    sent, _ = served[0]
    assert sent[-1].content.startswith(entry("last-turn-retry").body.strip())  # type: ignore[union-attr]
    assert sent[-1].content.endswith("End with your verdict.")  # type: ignore[union-attr]
