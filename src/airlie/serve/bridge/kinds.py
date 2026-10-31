"""Kind registry: payload → serve call → hub result."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from airlie.config.schema import BridgeSpec
from airlie.serve.bridge.airlie_client import ServeClient, ServeJobNotFound
from airlie.serve.bridge.clock import Clock, RealClock
from airlie.serve.bridge.errors import BridgeError
from airlie.serve.bridge.payload import assert_data_payload
from airlie.serve.bridge.repos import resolve_repo

log = logging.getLogger(__name__)

HUB_RESULT_KEYS = frozenset({"ok", "final_answer", "branch", "tests"})


def map_run_result(serve_result: dict[str, Any]) -> dict[str, Any]:
    """Drop Mac-only fields (report) for the hub contract."""
    if serve_result.get("type") == "answer":
        return {"ok": not serve_result.get("failed", False), "final_answer": serve_result.get("answer", "")}
    out: dict[str, Any] = {}
    for key in HUB_RESULT_KEYS:
        if key in serve_result:
            out[key] = serve_result[key]
    if "ok" not in out and "failed" in serve_result:
        out["ok"] = not serve_result["failed"]
    return out


def default_test_cmd(spec: BridgeSpec) -> str | None:
    cmds = [c.strip() for c in spec.test_cmds if c.strip()]
    return cmds[0] if cmds else None


@dataclass
class KindOutcome:
    status: str
    result: dict[str, Any] | None = None
    error_message: str | None = None


class KindHandler(Protocol):
    async def run(self, payload: dict[str, Any]) -> KindOutcome: ...


def _unreachable(e: httpx.HTTPError) -> KindOutcome:
    """A transport error talking to serve (down, refused, timed out): the job fails, the bridge goes on."""
    return KindOutcome("failed", error_message=f"serve unreachable: {type(e).__name__}: {e}")


class StudioKind:
    """POST /v1/airlie/runs with plan omitted (None), poll to terminal, map result for the hub."""

    def __init__(
        self,
        bridge: BridgeSpec,
        serve: ServeClient,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._bridge = bridge
        self._serve = serve
        self._clock = clock or RealClock()

    async def run(self, payload: dict[str, Any]) -> KindOutcome:
        data = assert_data_payload(payload)
        goal = data.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return KindOutcome("failed", error_message="studio payload requires a non-empty goal")
        repo_key = data.get("repo_key")
        if repo_key is not None and not isinstance(repo_key, str):
            return KindOutcome("failed", error_message="repo_key must be a string")
        try:
            repo = resolve_repo(self._bridge, repo_key if isinstance(repo_key, str) else None)
        except BridgeError as e:
            return KindOutcome("failed", error_message=str(e))

        body: dict[str, Any] = {
            "goal": goal.strip(),
            "repo": str(repo),
            "plan": None,
        }
        test_cmd = default_test_cmd(self._bridge)
        if test_cmd is not None:
            body["test_cmd"] = test_cmd
        for key in ("images", "base", "synthesise"):
            if key in data:
                body[key] = data[key]

        try:
            job_id = await self._serve.start_run(body)
        except BridgeError as e:
            return KindOutcome("failed", error_message=str(e))
        except httpx.HTTPError as e:
            return _unreachable(e)

        log.info("studio serve job %s started", job_id)
        while True:
            try:
                snap = await self._serve.poll_job(job_id, wait=0.0)
            except ServeJobNotFound:
                return KindOutcome(
                    "failed",
                    error_message=f"serve lost job {job_id} (404 while polling)",
                )
            except BridgeError as e:
                return KindOutcome("failed", error_message=str(e))
            except httpx.HTTPError as e:
                return _unreachable(e)
            state = str(snap.get("state", ""))
            if state == "running":
                await self._clock.sleep(self._bridge.job_poll_s)
                continue
            if state == "done":
                raw = snap.get("result")
                if not isinstance(raw, dict):
                    return KindOutcome("failed", error_message="serve job finished without a result object")
                return KindOutcome("done", result=map_run_result(raw))
            if state in ("failed", "cancelled"):
                err = snap.get("error") or f"serve job {state}"
                return KindOutcome("failed", error_message=str(err))
            return KindOutcome("failed", error_message=f"unexpected serve state {state!r}")


LLM_CALLERS = frozenset({"lhr", "ecff"})
LLM_OUTPUT_CHAT = "chat.completions"


class LlmKind:
    """POST /v1/chat/completions with inline persona as system + prompt as user.

    Posts a raw catalog model, not the chat role: the persona is the only system text. Role defaults do not
    apply to a raw model, so the bridge sets max_tokens and thinking from ``bridge.llm``.
    """

    def __init__(
        self,
        bridge: BridgeSpec,
        serve: ServeClient,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._bridge = bridge
        self._serve = serve
        self._clock = clock or RealClock()

    async def run(self, payload: dict[str, Any]) -> KindOutcome:
        try:
            data = assert_data_payload(payload)
        except BridgeError as e:
            return KindOutcome("failed", error_message=str(e))

        if data.get("model") is not None:
            return KindOutcome("failed", error_message="llm payload field model is not supported yet")
        if data.get("thinking") is not None:
            return KindOutcome("failed", error_message="llm payload field thinking is not supported yet")

        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return KindOutcome("failed", error_message="llm payload requires a non-empty prompt")
        persona = data.get("persona")
        if not isinstance(persona, str) or not persona.strip():
            return KindOutcome("failed", error_message="llm payload requires a non-empty persona")
        output = data.get("output")
        if output != LLM_OUTPUT_CHAT:
            return KindOutcome(
                "failed",
                error_message=f"llm payload output must be {LLM_OUTPUT_CHAT!r}",
            )
        caller = data.get("caller")
        if not isinstance(caller, str) or caller not in LLM_CALLERS:
            return KindOutcome(
                "failed",
                error_message=f"llm payload caller must be one of {sorted(LLM_CALLERS)}",
            )

        spec = self._bridge.llm
        body: dict[str, Any] = {
            "model": spec.model,
            "messages": [
                {"role": "system", "content": persona.strip()},
                {"role": "user", "content": prompt.strip()},
            ],
            "max_tokens": spec.max_tokens,
            "chat_template_kwargs": {"enable_thinking": spec.thinking},
        }
        if spec.thinking and spec.thinking_budget:
            body["thinking_token_budget"] = spec.thinking_budget
        try:
            raw = await self._serve.chat_completions(body, timeout_s=spec.timeout_s)
        except BridgeError as e:
            return KindOutcome("failed", error_message=str(e))
        except httpx.HTTPError as e:  # serve down, or no reply within bridge.llm.timeout_s
            return _unreachable(e)

        content = _assistant_content(raw)
        if content is None:
            return KindOutcome("failed", error_message="serve chat completions returned no assistant content")
        return KindOutcome(
            "done",
            result={"output": LLM_OUTPUT_CHAT, "content": content},
        )


def _assistant_content(raw: dict[str, Any]) -> str | None:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def handler_for(
    kind: str,
    bridge: BridgeSpec,
    serve: ServeClient,
    *,
    clock: Clock | None = None,
) -> KindHandler:
    if kind not in bridge.kinds:
        raise BridgeError(f"kind {kind!r} is not in bridge.kinds")
    if kind == "studio":
        return StudioKind(bridge, serve, clock=clock)
    if kind == "llm":
        return LlmKind(bridge, serve, clock=clock)
    raise BridgeError(f"no handler for kind {kind!r}")
