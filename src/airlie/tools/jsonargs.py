"""Lenient parsing of tool-call arguments.

vllm-mlx's `qwen3_coder` tool parser rebuilds JSON from the model's XML-style call; when a parameter value
contains braces it has been seen to append an extra `}` (`{"path": …, "content": "d = {}"}}`), and a cut-off
generation leaves the string unterminated. `repair_json_args` takes the first complete JSON object and drops
anything before or after it; a truncated object is returned unchanged so the caller reports it."""

from __future__ import annotations

import json
from typing import Any

_DECODER = json.JSONDecoder()


def repair_json_args(raw: str) -> str:
    text = raw.strip()
    start = text.find("{")
    if start < 0:
        return raw
    try:
        obj, end = _DECODER.raw_decode(text, start)
    except json.JSONDecodeError:
        return raw
    if not isinstance(obj, dict):
        return raw
    if start == 0 and end == len(text):
        return raw
    return json.dumps(obj, ensure_ascii=False)  # pragma: no mutate  (ensure_ascii=None is falsy too)


def parse_tool_args(raw: str) -> dict[str, Any]:
    """Parse (repairing when possible); raise ValueError with a readable message otherwise."""
    fixed = repair_json_args(raw or "{}")  # pragma: no mutate  (a junk-wrapped "{}" repairs to "{}" anyway)
    try:
        obj = json.loads(fixed)
    except json.JSONDecodeError as e:
        raise ValueError(f"could not parse tool arguments as JSON ({e.msg} at char {e.pos})") from e
    if not isinstance(obj, dict):
        raise ValueError("tool arguments must be a JSON object")
    return obj
