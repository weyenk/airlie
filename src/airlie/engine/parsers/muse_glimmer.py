"""Muse Glimmer's channel format, parsed with vllm-mlx 0.5.0's parser base classes.

Generation begins ` to=self<|message|>…<|eom|>` (reasoning) and continues `to=user<|message|>…<|eot|>`
(answer) or `to=<tool><|message|><atem:function_calls>…` (a call); later turns may open with a bare
`to=user`. Regexes follow vllm-mlx PR #812 (unmerged on 2026-10-13); delete this module when a vllm-mlx
release registers `muse_glimmer` itself."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from vllm_mlx.reasoning.base import DeltaMessage, ReasoningParser
from vllm_mlx.tool_parsers.abstract_tool_parser import ExtractedToolCallInformation, ToolParser

# A header opens the text or follows an end token; with the special tokens stripped (seen 2026-10-18: a
# reviewer reply came as `to=selfWe need…to=user{…}`) it may also follow sentence punctuation, and `self` and
# `user` are read as whole recipients before any longer name, so `to=selfWe` is self and then "We".
_HEADER = re.compile(
    r"(?:^|<\|eom\|>|<\|eot\|>|(?<=[.!?)\]}\n]))\s*(?:<\|start\|>)?\s*(?:assistant\s+)?"
    r"to=(?P<r>self|user|[\w.\-]+)\s*(?:<\|message\|>)?"
)
_END = re.compile(r"<\|eom\|>|<\|eot\|>|<\|start\|>|<\|message\|>")
_INVOKE = re.compile(r'<atem:invoke\b[^>]*?\bname="(?P<name>[^"]+)">(?P<body>.*?)</atem:invoke>', re.DOTALL)
_PARAM = re.compile(
    r'<atem:parameter\b[^>]*?\bname="(?P<name>[^"]+)"[^>]*?>(?P<value>.*?)</atem:parameter>', re.DOTALL
)


def split_channels(text: str) -> list[tuple[str, str]]:
    """[(recipient, body)] in order; text with no header is one `user` block."""
    heads = list(_HEADER.finditer(text))
    if not heads:
        return [("user", _END.sub("", text).strip())]
    out: list[tuple[str, str]] = []
    lead = _END.sub("", text[: heads[0].start()]).strip()
    if lead:
        out.append(("user", lead))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((h.group("r"), _END.sub("", text[h.end() : end]).strip()))
    return out


def _coerce(value: str) -> Any:
    v = value.strip()
    try:
        return json.loads(v)
    except ValueError:
        return v


class MuseGlimmerReasoningParser(ReasoningParser):  # type: ignore[misc]  # vllm_mlx ships no stubs
    def extract_reasoning(self, model_output: str) -> tuple[str | None, str | None]:
        parts = split_channels(model_output)
        reasoning = "\n".join(b for r, b in parts if r == "self" and b)
        rest: list[str] = []
        for r, b in parts:
            if r == "self":
                continue
            rest.append(b if r == "user" else f"to={r}\n{b}")  # keep tool channels for the tool parser
        content = "\n".join(x for x in rest if x)
        return (reasoning or None, content or None)

    def extract_reasoning_streaming(
        self, previous_text: str, current_text: str, delta_text: str
    ) -> DeltaMessage | None:
        return DeltaMessage(content=delta_text)  # airlie parses complete outputs; stream raw


class MuseGlimmerToolParser(ToolParser):  # type: ignore[misc]  # vllm_mlx ships no stubs
    def extract_tool_calls(
        self, model_output: str, request: dict[str, Any] | None = None
    ) -> ExtractedToolCallInformation:
        calls: list[dict[str, Any]] = []
        user: list[str] = []
        for recipient, body in split_channels(model_output):
            if recipient == "self":
                continue
            if recipient == "user":
                if body:
                    user.append(body)
                continue
            for inv in _INVOKE.finditer(body):
                args = {
                    m.group("name"): _coerce(m.group("value")) for m in _PARAM.finditer(inv.group("body"))
                }
                calls.append(
                    {
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": inv.group("name"),
                        "arguments": json.dumps(args),
                    }
                )
        return ExtractedToolCallInformation(
            tools_called=bool(calls), tool_calls=calls, content=("\n".join(user) or None)
        )
