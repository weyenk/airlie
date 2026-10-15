"""HTTPEngine: the Engine seam over any OpenAI-compatible server (vllm-mlx by default).

Written against the recorded vllm-mlx 0.5.0 responses in tests/fixtures/vllm_mlx (see the design doc's
"wire format" section). Also works with llama-server and mlx_lm.server, which follow the same shape."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from airlie.config import ModelSpec, RoleSpec
from airlie.engine.output_parse import parse_output
from airlie.engine.protocol import OnToken
from airlie.engine.sse import iter_sse
from airlie.engine.types import (
    EngineCapabilities,
    EngineError,
    EngineHealth,
    EngineStats,
    FinishReason,
    GenRequest,
    GenResult,
    Message,
    ModelState,
    TokenEvent,
    ToolCall,
)

YES_NO_SYSTEM = "Answer with exactly one word: YES or NO."
HTTP_POST = "POST"  # a module constant: httpx upper-cases the method, so "post" would be an equivalent mutant


class HTTPEngine:
    capabilities = EngineCapabilities(batching=True, prefix_cache=True, logprobs=False, native_tools=True)

    def __init__(
        self,
        base_url: str,
        served_name_for_role: Callable[[str], str],
        role_spec_for: Callable[[str], RoleSpec],
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 600.0,
        per_request_draft: bool = False,
        model_spec_for: Callable[[str], ModelSpec] | None = None,
    ) -> None:
        self.capabilities = EngineCapabilities(
            batching=True,
            prefix_cache=True,
            logprobs=False,
            native_tools=True,
            per_request_draft=per_request_draft,
        )
        self.base_url = base_url.rstrip("/")
        self._root = self.base_url.removesuffix("/v1")
        self._served = served_name_for_role
        self._role = role_spec_for
        self._model = model_spec_for
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._transport = transport
        self._timeout = timeout_s
        self._client: httpx.AsyncClient | None = None
        self._stats = EngineStats()
        self._cancelled: set[str] = set()
        self._live: dict[str, httpx.Response] = {}

    # ---- lifecycle ----
    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers=self._headers,
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                transport=self._transport,
            )
        return self._client

    async def start(self) -> None:
        self._c()

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health(self) -> EngineHealth:
        try:
            h = await self._c().get(f"{self._root}/health")
            m = await self._c().get(f"{self.base_url}/models")
        except httpx.HTTPError as e:
            return EngineHealth(ok=False, models={}, detail=f"{type(e).__name__}: {e}")
        ok = h.status_code == 200 and m.status_code == 200
        ids: list[str] = []
        if m.status_code == 200:
            ids = [str(x.get("id")) for x in m.json().get("data", [])]
        models: dict[str, ModelState] = dict.fromkeys(ids, "ready")
        detail = ""  # pragma: no mutate  (None is just as falsy for the fallback below)
        if h.status_code == 200:
            hj = h.json()
            detail = f"{hj.get('status', '?')} engine={hj.get('engine_type', '?')}"
        return EngineHealth(ok=ok, models=models, detail=detail or f"health {h.status_code}")

    # ---- generation ----
    def _body(self, req: GenRequest, *, stream: bool) -> dict[str, Any]:
        spec = self._role(req.role)
        thinking = spec.thinking if req.thinking is None else req.thinking
        body: dict[str, Any] = {
            "model": self._served(req.role),
            "messages": [_wire_message(m) for m in req.messages],
            "stream": stream,
            "max_tokens": req.max_tokens,
            "temperature": req.sampling.temperature,
            "top_p": req.sampling.top_p,
            "top_k": req.sampling.top_k,
            "min_p": req.sampling.min_p,
            "presence_penalty": req.sampling.presence_penalty,
            "chat_template_kwargs": {"enable_thinking": bool(thinking), **spec.chat_template_kwargs},
        }
        if req.sampling.repetition_penalty != 1.0:
            body["repetition_penalty"] = req.sampling.repetition_penalty
        if stream:
            body["stream_options"] = {"include_usage": True}
        if req.stop:
            body["stop"] = req.stop
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in req.tools
            ]
            body["tool_choice"] = "auto"
        if thinking and spec.thinking_budget:
            body["thinking_budget"] = spec.thinking_budget
        if req.draft_budget is not None and self.capabilities.per_request_draft:
            body["num_draft_tokens"] = req.draft_budget
        return body

    async def generate(self, req: GenRequest, on_token: OnToken | None = None) -> GenResult:
        if req.request_id in self._cancelled:
            self._cancelled.discard(req.request_id)
            return GenResult("", "", [], "cancelled", 0, 0, raw_finish="cancelled")
        self._stats.requests += 1
        text: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish = "stop"
        prompt_tokens = completion_tokens = 0
        cached: int | None = None
        accepted: int | None = None
        drafts: int | None = None
        t0 = time.monotonic()
        t_first: float | None = None  # pragma: no mutate  ("" is just as falsy)
        t_last = t0
        try:
            async with self._c().stream(
                HTTP_POST, f"{self.base_url}/chat/completions", json=self._body(req, stream=True)
            ) as r:
                self._live[req.request_id] = r
                if r.status_code != 200:
                    raise EngineError(f"engine returned {r.status_code}: {_error_text(await r.aread())}")
                async for frame in iter_sse(r.aiter_bytes()):
                    if req.request_id in self._cancelled:
                        finish = "cancelled"
                        break
                    meta = frame.get("generation_metadata")
                    if isinstance(meta, dict):
                        if meta.get("mtp_accepted") is not None:
                            accepted = int(meta["mtp_accepted"])
                        if meta.get("mtp_drafts") is not None:
                            drafts = int(meta["mtp_drafts"])
                    usage = frame.get("usage")
                    if isinstance(usage, dict):
                        prompt_tokens = int(usage.get("prompt_tokens", prompt_tokens))
                        completion_tokens = int(usage.get("completion_tokens", completion_tokens))
                        details = usage.get("prompt_tokens_details")
                        if isinstance(details, dict) and details.get("cached_tokens") is not None:
                            cached = int(details["cached_tokens"])
                    for choice in frame.get("choices") or []:
                        delta = choice.get("delta") or {}
                        now = time.monotonic()
                        if rc := delta.get("reasoning_content"):
                            t_first = t_first or now
                            t_last = now
                            reasoning.append(rc)
                            if on_token:
                                on_token(TokenEvent(text=rc, reasoning=True))
                        if c := delta.get("content"):
                            t_first = t_first or now
                            t_last = now
                            text.append(c)
                            if on_token:
                                on_token(TokenEvent(text=c))
                        for tc in delta.get("tool_calls") or []:
                            t_first = t_first or now
                            t_last = now
                            idx = int(tc.get("index", 0))
                            slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            fn = tc.get("function") or {}
                            if tc.get("id"):
                                if slot["id"] and str(tc["id"]) != slot["id"]:
                                    slot["arguments"] = ""  # vllm-mlx re-sends a finished call under a new id
                                slot["id"] = slot["id"] or str(tc["id"])
                            if fn.get("name"):
                                slot["name"] = str(fn["name"])
                            if fn.get("arguments"):  # fragments, in order (a value may well start with "{")
                                slot["arguments"] += str(fn["arguments"])
                        if choice.get("finish_reason"):
                            finish = str(choice["finish_reason"])
        except EngineError as e:
            self._stats.errors += 1
            raise EngineError(str(e), partial_text="".join(text)) from e
        except httpx.HTTPError as e:
            self._stats.errors += 1
            raise EngineError(f"{type(e).__name__}: {e}", partial_text="".join(text)) from e
        finally:
            self._live.pop(req.request_id, None)
            self._cancelled.discard(req.request_id)
        tool_calls = [
            ToolCall(id=v["id"], name=v["name"], arguments=v["arguments"]) for _, v in sorted(calls.items())
        ]
        if tool_calls and finish != "cancelled":
            finish = "tool_calls"
        self._stats.tokens_out += completion_tokens
        ttft_ms = ((t_first or t_last) - t0) * 1000.0
        span = max(t_last - (t_first or t_last), 1e-6)
        res = GenResult(
            text="".join(text),
            reasoning="".join(reasoning),
            tool_calls=tool_calls,
            finish_reason=_finish(finish),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached,
            accepted_draft_tokens=accepted,
            draft_tokens=drafts,
            ttft_ms=ttft_ms,
            decode_tps=(completion_tokens / span) if completion_tokens > 1 else 0.0,
            raw_finish=finish,
        )
        if self._model is not None:
            res = parse_output(self._model(req.role), res)
        return res

    async def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)
        r = self._live.get(request_id)
        if r is not None:
            await r.aclose()

    async def yes_no(self, role: str, messages: list[Message]) -> tuple[float, float]:
        msgs = [Message(role="system", content=YES_NO_SYSTEM), *[m for m in messages if m.role != "system"]]
        body = {
            "model": self._served(role),
            "messages": [_wire_message(m) for m in msgs],
            "stream": False,
            "max_tokens": 3,
            "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        self._stats.requests += 1
        r = await self._c().post(f"{self.base_url}/chat/completions", json=body)
        if r.status_code != 200:
            self._stats.errors += 1
            raise EngineError(f"engine returned {r.status_code}: {_error_text(r.content)}")
        content = str(r.json()["choices"][0]["message"].get("content") or "")
        word = content.strip().upper().lstrip("*\"'").split()
        if word and word[0].startswith("YES"):
            return 1.0, 0.0
        if word and word[0].startswith("NO"):
            return 0.0, 1.0
        raise EngineError(f"yes_no: role '{role}' answered {content!r}, not YES/NO")

    def stats(self) -> EngineStats:
        return self._stats


def _finish(raw: str) -> FinishReason:
    if raw in ("length", "tool_calls", "cancelled"):  # anything else, "stop" included, is "stop"
        return raw  # type: ignore[return-value]
    return "stop"


def _wire_message(m: Message) -> dict[str, Any]:
    content: Any = m.content
    if m.parts:
        content = [
            {"type": "text", "text": p.text}
            if p.type == "text"
            else {"type": "image_url", "image_url": {"url": p.url}}
            for p in m.parts
        ]
    d: dict[str, Any] = {"role": m.role, "content": content}
    if m.name:
        d["name"] = m.name
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    if m.tool_calls:
        d["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in m.tool_calls
        ]
    return d


def _error_text(raw: bytes) -> str:
    try:
        obj = json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")[:300]  # pragma: no mutate  (codec names are case-insensitive)
    if isinstance(obj, dict):
        if isinstance(obj.get("error"), dict):
            return str(obj["error"].get("message", obj["error"]))
        if "detail" in obj:
            return str(obj["detail"])
    return str(obj)[:300]
