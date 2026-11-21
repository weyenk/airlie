"""OpenAI Chat Completions <-> Anthropic Messages: the client keeps speaking Chat Completions while an engine
with ``format: anthropic`` is called in Messages shape. Pure functions over dicts; no I/O."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

DEFAULT_MAX_TOKENS = 4096
"""Anthropic requires max_tokens; used when the Chat Completions body names none."""

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


# ---- stream ----


def iter_openai_chunks_from_anthropic_sse(lines: Iterable[str | bytes]) -> Iterator[dict[str, Any]]:
    """Anthropic Messages SSE lines as Chat Completions chunks: a role chunk, content / reasoning / tool-call
    deltas, one chunk carrying the finish reason, then (like ``stream_options.include_usage``) a last chunk
    with no choices and the usage. An ``error`` event raises RuntimeError."""
    chunk_id = ""
    model = ""
    input_tokens = 0
    cache_tokens = 0
    output_tokens = 0
    tool_index: dict[int, int] = {}  # content block index -> index among the tool calls
    finished = False
    usage_sent = False

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    def usage_chunk() -> dict[str, Any]:
        prompt = input_tokens + cache_tokens
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": output_tokens,
                "total_tokens": prompt + output_tokens,
            },
        }

    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            chunk_id = str(message.get("id", ""))
            model = str(message.get("model", ""))
            u = message.get("usage") or {}
            input_tokens = int(u.get("input_tokens") or 0)
            cache_tokens = int(u.get("cache_creation_input_tokens") or 0) + int(
                u.get("cache_read_input_tokens") or 0
            )
            output_tokens = int(u.get("output_tokens") or 0)
            yield chunk({"role": "assistant", "content": ""})
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                index = tool_index.setdefault(int(event.get("index", 0)), len(tool_index))
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {"name": block.get("name", ""), "arguments": ""},
                            }
                        ]
                    }
                )
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            dkind = delta.get("type")
            if dkind == "text_delta":
                yield chunk({"content": str(delta.get("text", ""))})
            elif dkind == "thinking_delta":
                yield chunk({"reasoning_content": str(delta.get("thinking", ""))})
            elif dkind == "input_json_delta":
                known = tool_index.get(int(event.get("index", 0)))
                if known is not None:
                    yield chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": known,
                                    "function": {"arguments": str(delta.get("partial_json", ""))},
                                }
                            ]
                        }
                    )
        elif kind == "message_delta":
            u = event.get("usage") or {}
            if u.get("output_tokens") is not None:
                output_tokens = int(u["output_tokens"])
            if u.get("input_tokens") is not None:
                input_tokens = int(u["input_tokens"])
            reason = _finish_reason((event.get("delta") or {}).get("stop_reason"))
            if reason and not finished:
                finished = True
                yield chunk({}, reason)
        elif kind == "message_stop":
            if not usage_sent:
                usage_sent = True
                yield usage_chunk()
        elif kind == "error":
            err = event.get("error") or {}
            raise RuntimeError(
                f"anthropic stream error: {err.get('type', 'error')}: {err.get('message', '')}"
            )
    if finished and not usage_sent:
        yield usage_chunk()
