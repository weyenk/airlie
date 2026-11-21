"""OpenAI Chat Completions <-> Anthropic Messages: the client keeps speaking Chat Completions while an engine
with ``format: anthropic`` is called in Messages shape. Pure functions over dicts; no I/O."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any

DEFAULT_MAX_TOKENS = 4096
"""Anthropic requires max_tokens; used when the Chat Completions body names none."""

ANTHROPIC_VERSION = "2023-06-01"
MIN_THINKING_BUDGET = 1024
"""Anthropic's floor for ``thinking.budget_tokens``."""


def messages_url(base_url: str) -> str:
    """``{root}/v1/messages`` for an engine's ``base_url`` (with or without its ``/v1``)."""
    return base_url.rstrip("/").removesuffix("/v1") + "/v1/messages"


def models_url(base_url: str) -> str:
    return base_url.rstrip("/").removesuffix("/v1") + "/v1/models"


def anthropic_headers(api_key: str | None) -> dict[str, str]:
    """The headers a Messages call carries: the version, and the key as ``x-api-key`` when there is one."""
    headers = {"anthropic-version": ANTHROPIC_VERSION}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}


def _finish_reason(stop_reason: object) -> str | None:
    if not isinstance(stop_reason, str):
        return None
    return _FINISH_REASONS.get(stop_reason, "stop")


# ---- request ----


def _text_of(content: object) -> str:
    """The plain text of a Chat Completions content value (a string or a list of parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content)


def _image_block(part: dict[str, Any]) -> dict[str, Any] | None:
    image = part.get("image_url")
    url = image.get("url") if isinstance(image, dict) else image
    if not isinstance(url, str):
        return None
    if url.startswith("data:") and ";base64," in url:
        header, data = url.split(";base64,", 1)
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": header[len("data:") :], "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _blocks(content: object) -> list[dict[str, Any]]:
    """Chat Completions content as Anthropic content blocks (text and images; empty text is dropped)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out: list[dict[str, Any]] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                if part:
                    out.append({"type": "text", "text": part})
            elif isinstance(part, dict):
                kind = part.get("type")
                if kind in ("text", "input_text", "output_text"):
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        out.append({"type": "text", "text": text})
                elif kind == "image_url":
                    block = _image_block(part)
                    if block is not None:
                        out.append(block)
    return out


def _tool_input(arguments: object) -> object:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_message_content(blocks: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """A lone text block stays a plain string, as the client wrote it."""
    if len(blocks) == 1 and blocks[0]["type"] == "text":
        return str(blocks[0]["text"])
    return blocks


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    system: list[str] = []
    # (role, blocks); consecutive same-role turns merge since Anthropic requires alternating roles
    turns: list[tuple[str, list[dict[str, Any]]]] = []

    def add(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if turns and turns[-1][0] == role:
            turns[-1][1].extend(blocks)
        else:
            turns.append((role, list(blocks)))

    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            text = _text_of(message.get("content"))
            if text:
                system.append(text)
        elif role == "user":
            add("user", _blocks(message.get("content")))
        elif role == "assistant":
            blocks = _blocks(message.get("content"))
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": _tool_input(fn.get("arguments")),
                    }
                )
            add("assistant", blocks)
        elif role == "tool":
            add(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.get("tool_call_id", ""),
                        "content": _text_of(message.get("content")),
                    }
                ],
            )
    out: list[dict[str, Any]] = []
    for role, blocks in turns:
        if role == "user":  # tool results must lead the turn they answer
            blocks = [b for b in blocks if b["type"] == "tool_result"] + [
                b for b in blocks if b["type"] != "tool_result"
            ]
        out.append({"role": role, "content": _as_message_content(blocks)})
    return ("\n\n".join(system) if system else None), out


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools:
        fn = tool.get("function") if tool.get("type") == "function" else None
        if not isinstance(fn, dict):
            continue
        converted: dict[str, Any] = {
            "name": fn.get("name", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }
        if fn.get("description"):
            converted["description"] = fn["description"]
        out.append(converted)
    return out


def _convert_tool_choice(choice: object) -> dict[str, Any] | None:
    if choice == "auto":
        return {"type": "auto"}
    if choice == "required":
        return {"type": "any"}
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name")
        if name:
            return {"type": "tool", "name": name}
    return None


def to_anthropic_messages_body(openai_chat_body: dict[str, Any]) -> dict[str, Any]:
    """A Chat Completions request as an Anthropic Messages request. Fields Anthropic has no name for
    (penalties, ``min_p``, ``chat_template_kwargs``, ...) are dropped."""
    system, messages = _convert_messages(list(openai_chat_body.get("messages") or []))
    max_tokens = openai_chat_body.get("max_completion_tokens") or openai_chat_body.get("max_tokens")
    out: dict[str, Any] = {
        "model": openai_chat_body.get("model"),
        "messages": messages,
        "max_tokens": int(max_tokens) if max_tokens else DEFAULT_MAX_TOKENS,
    }
    if system:
        out["system"] = system
    for key in ("temperature", "top_p", "top_k"):
        if openai_chat_body.get(key) is not None:
            out[key] = openai_chat_body[key]
    stop = openai_chat_body.get("stop")
    if isinstance(stop, str) and stop:
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        out["stop_sequences"] = [str(s) for s in stop]
    if openai_chat_body.get("stream"):
        out["stream"] = True
    choice = openai_chat_body.get("tool_choice")
    tools = openai_chat_body.get("tools")
    if tools and choice != "none":
        converted = _convert_tools(tools)
        if converted:
            out["tools"] = converted
            tool_choice = _convert_tool_choice(choice)
            if tool_choice is not None:
                out["tool_choice"] = tool_choice
    return out


# ---- response ----


def _usage(usage: dict[str, Any] | None) -> dict[str, int]:
    u = usage or {}
    prompt = (
        int(u.get("input_tokens") or 0)
        + int(u.get("cache_creation_input_tokens") or 0)
        + int(u.get("cache_read_input_tokens") or 0)
    )
    completion = int(u.get("output_tokens") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def from_anthropic_message_response(anthropic_json: dict[str, Any]) -> dict[str, Any]:
    """An Anthropic Messages response as a Chat Completions response."""
    text: list[str] = []
    thinking: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in anthropic_json.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            text.append(str(block.get("text") or ""))
        elif kind == "thinking":
            thinking.append(str(block.get("thinking") or ""))
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
    if thinking:
        message["reasoning_content"] = "".join(thinking)
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not message["content"]:
            message["content"] = None
    return {
        "id": anthropic_json.get("id", ""),
        "object": "chat.completion",
        "model": anthropic_json.get("model", ""),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _finish_reason(anthropic_json.get("stop_reason")) or "stop",
            }
        ],
        "usage": _usage(anthropic_json.get("usage")),
    }


# ---- request tuning ----


def tune_anthropic_request(body: dict[str, Any], *, thinking: bool, budget: int | None) -> dict[str, Any]:
    """Make a translated Messages body one Anthropic accepts, in place: ``thinking`` becomes an extended
    thinking block (``budget`` tokens, default half of ``max_tokens``, held under ``max_tokens`` and above
    Anthropic's 1024 floor; no room for that and there is no thinking block), a thinking turn drops the
    sampling fields it may not set, and otherwise ``top_p`` yields to ``temperature`` (newer models refuse
    both together)."""
    if thinking:
        max_tokens = int(body["max_tokens"])
        wanted = budget if budget else max_tokens // 2
        granted = min(wanted, max_tokens - 1)
        if granted >= MIN_THINKING_BUDGET:
            body["thinking"] = {"type": "enabled", "budget_tokens": granted}
            for key in ("temperature", "top_p", "top_k"):
                body.pop(key, None)
            return body
    if body.get("temperature") is not None:
        body.pop("top_p", None)
    return body


# ---- stream ----


class AnthropicStreamTranslator:
    """Anthropic Messages SSE lines as Chat Completions chunks, fed one line at a time: a role chunk,
    content / reasoning / tool-call deltas, one chunk carrying the finish reason, then (like
    ``stream_options.include_usage``) a last chunk with no choices and the usage. ``feed`` raises
    RuntimeError on an ``error`` event; ``stopped`` is true once ``message_stop`` has been seen."""

    def __init__(self) -> None:
        self.stopped = False
        self._chunk_id = ""
        self._model = ""
        self._input_tokens = 0
        self._cache_tokens = 0
        self._output_tokens = 0
        self._tool_index: dict[int, int] = {}  # content block index -> index among the tool calls
        self._finished = False
        self._usage_sent = False

    def _chunk(self, delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": self._chunk_id,
            "object": "chat.completion.chunk",
            "model": self._model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def _usage_chunk(self) -> dict[str, Any]:
        prompt = self._input_tokens + self._cache_tokens
        return {
            "id": self._chunk_id,
            "object": "chat.completion.chunk",
            "model": self._model,
            "choices": [],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": self._output_tokens,
                "total_tokens": prompt + self._output_tokens,
            },
        }

    def feed(self, raw: str | bytes) -> list[dict[str, Any]]:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line.startswith("data:"):
            return []
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            return []
        try:
            event = json.loads(payload)
        except ValueError:
            return []
        if not isinstance(event, dict):
            return []
        return self._event(event)

    def _event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            self._chunk_id = str(message.get("id", ""))
            self._model = str(message.get("model", ""))
            u = message.get("usage") or {}
            self._input_tokens = int(u.get("input_tokens") or 0)
            self._cache_tokens = int(u.get("cache_creation_input_tokens") or 0) + int(
                u.get("cache_read_input_tokens") or 0
            )
            self._output_tokens = int(u.get("output_tokens") or 0)
            return [self._chunk({"role": "assistant", "content": ""})]
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") != "tool_use":
                return []
            index = self._tool_index.setdefault(int(event.get("index", 0)), len(self._tool_index))
            call = {
                "index": index,
                "id": block.get("id", ""),
                "type": "function",
                "function": {"name": block.get("name", ""), "arguments": ""},
            }
            return [self._chunk({"tool_calls": [call]})]
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            dkind = delta.get("type")
            if dkind == "text_delta":
                return [self._chunk({"content": str(delta.get("text", ""))})]
            if dkind == "thinking_delta":
                return [self._chunk({"reasoning_content": str(delta.get("thinking", ""))})]
            if dkind == "input_json_delta":
                known = self._tool_index.get(int(event.get("index", 0)))
                if known is not None:
                    fragment = {"index": known, "function": {"arguments": str(delta.get("partial_json", ""))}}
                    return [self._chunk({"tool_calls": [fragment]})]
            return []
        if kind == "message_delta":
            u = event.get("usage") or {}
            if u.get("output_tokens") is not None:
                self._output_tokens = int(u["output_tokens"])
            if u.get("input_tokens") is not None:
                self._input_tokens = int(u["input_tokens"])
            reason = _finish_reason((event.get("delta") or {}).get("stop_reason"))
            if reason and not self._finished:
                self._finished = True
                return [self._chunk({}, reason)]
            return []
        if kind == "message_stop":
            self.stopped = True
            if not self._usage_sent:
                self._usage_sent = True
                return [self._usage_chunk()]
            return []
        if kind == "error":
            err = event.get("error") or {}
            raise RuntimeError(
                f"anthropic stream error: {err.get('type', 'error')}: {err.get('message', '')}"
            )
        return []

    def flush(self) -> list[dict[str, Any]]:
        """What the lines' end still owes: the usage chunk of a stream that finished but sent no stop."""
        if self._finished and not self._usage_sent:
            self._usage_sent = True
            return [self._usage_chunk()]
        return []


def iter_openai_chunks_from_anthropic_sse(lines: Iterable[str | bytes]) -> Iterator[dict[str, Any]]:
    """Anthropic Messages SSE lines as Chat Completions chunks (see ``AnthropicStreamTranslator``). An
    ``error`` event raises RuntimeError."""
    translator = AnthropicStreamTranslator()
    for raw in lines:
        yield from translator.feed(raw)
    yield from translator.flush()


def _sse_frame(obj: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


async def openai_sse_from_anthropic(byte_chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """An Anthropic Messages byte stream as a Chat Completions one: ``data: {chunk}`` frames and, when the
    stream ended with ``message_stop``, a closing ``data: [DONE]``. An ``error`` event, or a stream that
    stops without ``message_stop``, is left to the reader: the first becomes an ``{"error": ...}`` frame, the
    second simply has no ``[DONE]``."""
    translator = AnthropicStreamTranslator()
    buf = b""
    try:
        async for piece in byte_chunks:
            buf += piece
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                for obj in translator.feed(line):
                    yield _sse_frame(obj)
        if buf.strip():
            for obj in translator.feed(buf):
                yield _sse_frame(obj)
        for obj in translator.flush():
            yield _sse_frame(obj)
    except RuntimeError as e:
        yield _sse_frame({"error": {"message": str(e)}})
        return
    if translator.stopped:
        yield b"data: [DONE]\n\n"
