"""JevClient / JevRouter wire details and RouteDecision fields (pinned for mutation testing)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from airlie.router import JevClient, JevRouter
from airlie.router.protocol import ROLE_QUESTION, RouteDecision

CRITERIA = {"coder": "write code", "qa": "test it"}


def answering(payload: dict[str, Any], status: int = 200, text: str | None = None):  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler), seen


def ok_payload(question: str = "role", **ans: Any) -> dict[str, Any]:
    return {"answers": {question: {"choice": "coder", "probabilities": {"coder": 0.9, "qa": 0.1}, **ans}}}


async def test_defaults_model_timeout_and_trailing_slashes_are_normalised() -> None:
    transport, seen = answering(ok_payload(confidence=0.9))
    client = JevClient("http://k/v1/", transport=transport)
    assert client._client.timeout == httpx.Timeout(60.0)
    await client.ask("s", "role", "pick", CRITERIA)
    assert seen[0].url == "http://k/v1/systemone"
    assert json.loads(seen[0].content)["model"] == "jev"
    assert client.base_url == "http://k/v1"
    assert JevClient("http://k/v1///", transport=transport).base_url == "http://k/v1"


async def test_custom_model_and_timeout_are_used() -> None:
    transport, seen = answering(ok_payload(confidence=0.5))
    client = JevClient("http://k/v1", model="kev", timeout_s=5.0, transport=transport)
    assert client._client.timeout == httpx.Timeout(5.0)
    await client.ask("s", "role", "pick", CRITERIA)
    assert json.loads(seen[0].content)["model"] == "kev"


async def test_api_key_becomes_a_bearer_header_and_absent_key_sends_none() -> None:
    transport, seen = answering(ok_payload(confidence=0.5))
    await JevClient("http://k/v1", api_key="sekret", transport=transport).ask("s", "role", "i", CRITERIA)
    assert seen[0].headers["authorization"] == "Bearer sekret"
    await JevClient("http://k/v1", transport=transport).ask("s", "role", "i", CRITERIA)
    assert "authorization" not in seen[1].headers
    await JevClient("http://k/v1", api_key="", transport=transport).ask("s", "role", "i", CRITERIA)
    assert "authorization" not in seen[2].headers


async def test_non_200_raises_http_status_error_with_truncated_body() -> None:
    body = "x" * 250
    transport, _ = answering({}, status=503, text=body)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await JevClient("http://k/v1", transport=transport).ask("s", "role", "i", CRITERIA)
    assert str(ei.value) == f"systemone returned 503: {'x' * 200}"
    assert ei.value.response.status_code == 503
    assert ei.value.request.url.path == "/v1/systemone"


async def test_answer_missing_the_question_raises_with_truncated_payload() -> None:
    payload = {"answers": {}, "pad": "y" * 400}
    transport, _ = answering(payload)
    with pytest.raises(ValueError) as ei:
        await JevClient("http://k/v1", transport=transport).ask("s", "role", "i", CRITERIA)
    assert str(ei.value) == f"systemone answered without 'role': {str(payload)[:200]}"
    transport, _ = answering({"answers": {"role": {"probabilities": {}}}})
    with pytest.raises(ValueError, match="answered without 'role'"):
        await JevClient("http://k/v1", transport=transport).ask("s", "role", "i", CRITERIA)


async def test_missing_confidence_and_usage_default_to_zero() -> None:
    transport, _ = answering(ok_payload())
    ans = await JevClient("http://k/v1", transport=transport).ask("s", "role", "i", CRITERIA)
    assert ans.confidence == 0.0 and ans.input_tokens == 0
    transport, _ = answering({**ok_payload(confidence=0.4), "usage": {}})
    ans = await JevClient("http://k/v1", transport=transport).ask("s", "role", "i", CRITERIA)
    assert ans.confidence == 0.4 and ans.input_tokens == 0


async def test_answer_without_probabilities_is_empty() -> None:
    transport, _ = answering({"answers": {"role": {"choice": "coder"}}})
    ans = await JevClient("http://k/v1", transport=transport).ask("s", "role", "i", CRITERIA)
    assert ans.probabilities == {} and ans.choice == "coder"


def router(transport: httpx.MockTransport) -> JevRouter:
    return JevRouter(JevClient("http://k/v1", transport=transport), ambiguous_gap=0.15, min_confidence=0.5)


async def test_route_defaults_question_role_and_role_instructions() -> None:
    transport, seen = answering(ok_payload(confidence=0.9, **{}), status=200)
    await router(transport).route("do the thing", CRITERIA)
    body = json.loads(seen[0].content)
    assert body["state"] == "do the thing"
    assert body["questions"] == {
        "role": {"type": "choice", "instructions": ROLE_QUESTION, "criteria": CRITERIA}
    }


async def test_route_custom_question_and_instructions_are_forwarded() -> None:
    transport, seen = answering(ok_payload("sev", confidence=0.9))
    await router(transport).route("s", CRITERIA, question="sev", instructions="how bad")
    q = json.loads(seen[0].content)["questions"]
    assert q == {"sev": {"type": "choice", "instructions": "how bad", "criteria": CRITERIA}}


async def test_route_error_with_hint_returns_a_fully_populated_error_decision() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kev down")

    d = await router(httpx.MockTransport(boom)).route("s", CRITERIA, "qa")
    assert d == RouteDecision("role", None, "qa", "error", {}, 0.0, "qa", 0, "kev down")
    d = await router(httpx.MockTransport(boom)).route("s", CRITERIA, "qa", question="sev")
    assert d.question == "sev"


async def test_route_error_without_hint_raises_with_the_cause() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kev down")

    with pytest.raises(ValueError) as ei:
        await router(httpx.MockTransport(boom)).route("s", CRITERIA)
    assert str(ei.value) == "routing failed and no hint to fall back on: kev down"


async def test_off_set_choice_uses_hint_then_top_and_reports_every_field() -> None:
    payload = {
        "answers": {
            "role": {
                "choice": "zzz",
                "probabilities": {"zzz": 0.5, "qa": 0.3, "coder": 0.2},
                "confidence": 0.8,
            }
        },
        "usage": {"input_tokens": 42},
    }
    transport, _ = answering(payload)
    probs = {"zzz": 0.5, "qa": 0.3, "coder": 0.2}
    d = await router(transport).route("s", CRITERIA, "coder")
    assert d == RouteDecision("role", "zzz", "coder", "invalid", probs, 0.8, "coder", 42)
    d = await router(transport).route("s", CRITERIA)
    assert d == RouteDecision("role", "zzz", "qa", "invalid", probs, 0.8, None, 42)
    d = await router(transport).route("s", CRITERIA, "not-a-role")
    assert d.chosen == "qa" and d.hint == "not-a-role"


async def test_off_set_choice_with_nothing_in_set_and_no_hint_raises() -> None:
    payload = {"answers": {"role": {"choice": "zzz", "probabilities": {"zzz": 1.0}, "confidence": 0.8}}}
    transport, _ = answering(payload)
    with pytest.raises(ValueError) as ei:
        await router(transport).route("s", CRITERIA)
    assert str(ei.value) == "systemone chose 'zzz', outside the closed set, and no fallback"


async def test_clear_decision_reports_every_field() -> None:
    payload = {**ok_payload(confidence=0.95), "usage": {"input_tokens": 42}}
    transport, _ = answering(payload)
    d = await router(transport).route("s", CRITERIA, "qa")
    assert d == RouteDecision("role", "coder", "coder", "clear", {"coder": 0.9, "qa": 0.1}, 0.95, "qa", 42)


def test_only_slashes_are_stripped_from_the_base_url() -> None:
    assert JevClient("http://k/v1X").base_url == "http://k/v1X"
    assert JevClient("http://k/v1X//").base_url == "http://k/v1X"
