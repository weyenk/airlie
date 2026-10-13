"""Engine data types. The orchestrator speaks only these; engines translate to their wire format."""

from __future__ import annotations

import base64
import mimetypes
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from airlie.config import Sampling

FinishReason = Literal["stop", "length", "tool_calls", "cancelled"]
ModelState = Literal["ready", "loading", "lazy", "missing"]
"""lazy = registered in the vllm-mlx registry, loads on first request (preload: false)."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str
    """Raw JSON string exactly as the model produced it; tools parse it."""


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ContentPart:
    type: Literal["text", "image_url"]
    text: str = ""
    url: str = ""


def image_part(path: Path) -> ContentPart:
    """An image_url part carrying the file as a data URL (mime from the suffix; png when unknown)."""
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return ContentPart("image_url", url=f"data:{mime};base64,{data}")


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_calls: tuple[ToolCall, ...] | None = None
    tool_call_id: str | None = None
    parts: tuple[ContentPart, ...] | None = None
    """When set, the wire content is this list (text + image_url parts); `content` stays the text."""


@dataclass
class GenRequest:
    role: str
    messages: list[Message]
    sampling: Sampling
    max_tokens: int
    stop: list[str] = field(default_factory=list)
    tools: list[ToolSchema] | None = None
    thinking: bool | None = None
    draft_budget: int | None = None
    """HAL draft budget L_i for this request (spec 04 §1); sent only when the engine has per_request_draft."""
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class TokenEvent:
    text: str
    reasoning: bool = False


@dataclass
class GenResult:
    text: str
    reasoning: str
    tool_calls: list[ToolCall]
    finish_reason: FinishReason
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None = None
    accepted_draft_tokens: int | None = None
    draft_tokens: int | None = None
    ttft_ms: float = 0.0
    decode_tps: float = 0.0
    raw_finish: str = ""


@dataclass(frozen=True)
class EngineCapabilities:
    batching: bool
    prefix_cache: bool
    logprobs: bool
    native_tools: bool
    per_request_draft: bool = False


@dataclass(frozen=True)
class EngineHealth:
    ok: bool
    models: dict[str, ModelState]
    detail: str = ""


@dataclass
class EngineStats:
    requests: int = 0
    tokens_out: int = 0
    errors: int = 0


class EngineError(RuntimeError):
    """The engine could not serve a request; message says why and carries any partial text."""

    def __init__(self, message: str, partial_text: str = "") -> None:
        super().__init__(message)
        self.partial_text = partial_text
