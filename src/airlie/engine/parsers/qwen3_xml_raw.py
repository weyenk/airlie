"""Qwen3 XML tool calls (Qwen3-Coder, Qwen3.8) with parameter bodies kept as raw text.

Replaces vllm-mlx 0.5.0's `qwen3_coder`, an expat XML parser, for the reasons measured in
docs/bench/2026-10-14-qwen3-coder-tool-calls.md: a value is code, so `<<`, `<`, `&` and tool markup inside it
are data, and on the stream a bare `<function=` arrives split over three tokens.

Format, from the chat template: [prose] [<tool_call>] <function=NAME>
(<parameter=K>\\nVALUE\\n</parameter>)* </function> [</tool_call>], one newline on each side of VALUE being
format. A `</parameter>` ends the value only when the next markup is `<parameter=`, `</function>`, another
call or the end of the output. Hermes JSON (`<tool_call>{"name": …, "arguments": …}</tool_call>`) is read
too. Streaming emits each call whole once it is complete; a call cut off by max_tokens keeps unterminated
arguments, so the agent reports a runaway instead of writing half a file. Registered as `qwen3_xml_raw`;
delete it when a vllm-mlx release parses this format."""

from __future__ import annotations

import ast
import json
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from vllm_mlx.tool_parsers.abstract_tool_parser import ExtractedToolCallInformation, ToolParser

_CALL = "<tool_call>"
_CALL_END = "</tool_call>"
_FN = "<function="
_FN_END = "</function>"
_PARAM = "<parameter="
_PARAM_END = "</parameter>"
_AFTER_VALUE = (_PARAM, _FN_END, _CALL_END, _CALL, _FN)
_START = re.compile(r"<tool_call>|<function=")
_VALUE_END = re.compile(re.escape(_PARAM_END))
_NAME = re.compile(r"([^>\s]+)>")
_NAME_SO_FAR = re.compile(r"[^>\s]*")
_NON_WS = re.compile(r"\S")
_STRING = {"string", "str", "text", "varchar", "char", "enum"}
_JSON = json.JSONDecoder()


@dataclass
class _Call:
    name: str
    end: int = 0
    params: list[tuple[str, str]] = field(default_factory=list)
    json_args: str | None = None
    cut: bool = False  # the output ended inside a value or a tag


# A scan finds a _Call, False when the marker was prose, or None while only more output can tell.


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)  # pragma: no mutate  (ensure_ascii=None is falsy too)


def _skip_ws(text: str, j: int) -> int:
    m = _NON_WS.search(text, j)
    return m.start() if m else len(text)


def _could_become(s: str, *words: str) -> bool:
    return bool(s) and any(w.startswith(s) for w in words)


def _value_end(text: str, start: int, final: bool) -> int | None:
    """Where the value at start ends: a `</parameter>` followed by markup, or by nothing at the end of the
    output. None while no such `</parameter>` has arrived."""
    for m in _VALUE_END.finditer(text, start):
        after = _skip_ws(text, m.end())
        if text.startswith(_AFTER_VALUE, after) or (final and after == len(text)):
            return m.start()
    return None


def _after_call(text: str, j: int, final: bool) -> int | None:
    """End of a finished call, past an optional `</tool_call>`; None while one may still arrive."""
    k = _skip_ws(text, j)
    if text.startswith(_CALL_END, k):
        return k + len(_CALL_END)
    if not final and (k == len(text) or _could_become(text[k:], _CALL_END)):
        return None
    return j


def _scan_json(text: str, j: int, final: bool) -> _Call | Literal[False] | None:
    try:
        obj, end = _JSON.raw_decode(text, j)
    except ValueError:
        return False if final else None
    if not isinstance(obj, dict) or not isinstance(obj.get("name"), str):
        return False
    stop = _after_call(text, end, final)
    if stop is None:
        return None
    args = obj.get("arguments", {})
    json_args = args if isinstance(args, str) else _dumps(args)
    return _Call(obj["name"], stop, json_args=json_args)


def _scan(text: str, i: int, final: bool) -> _Call | Literal[False] | None:
    """The call that starts at i (`<tool_call>` or `<function=`)."""
    j = i
    if text.startswith(_CALL, j):
        j = _skip_ws(text, j + len(_CALL))
        if j == len(text):
            return False if final else None
        if text[j] == "{":
            return _scan_json(text, j, final)
        if not text.startswith(_FN, j):
            return None if not final and _could_become(text[j:], _FN) else False
    name = _NAME.match(text, j + len(_FN))
    if name is None:
        return None if not final and _NAME_SO_FAR.fullmatch(text, j + len(_FN)) else False
    call = _Call(name.group(1), len(text))
    j = _skip_ws(text, name.end())
    if not text.startswith((_PARAM, _FN_END), j):  # `<function=x>` followed by prose is not a call
        if j < len(text):
            return None if not final and _could_become(text[j:], _PARAM, _FN_END) else False
        return call if final else None  # the output ended right after the name
    while text.startswith(_PARAM, j):
        key = _NAME.match(text, j + len(_PARAM))
        k = _value_end(text, key.end(), final) if key else None
        if key is None or k is None:
            if not final:
                return None
            if key is not None:
                call.params.append((key.group(1), text[key.end() :].removeprefix("\n")))
            call.cut = True
            return call
        call.params.append((key.group(1), text[key.end() : k].removeprefix("\n").removesuffix("\n")))
        j = _skip_ws(text, k + len(_PARAM_END))
    if text.startswith(_FN_END, j):  # otherwise the model closed or moved on without `</function>`
        j += len(_FN_END)
    stop = _after_call(text, j, final)
    if stop is None:
        return None
    call.end = stop
    return call


def _segments(text: str, start: int, final: bool) -> Iterator[tuple[int, str | _Call]]:
    """(end, prose or call) from start on, stopping before anything only more output can settle."""
    pos = start
    while (m := _START.search(text, pos)) is not None:
        if m.start() > pos:
            yield m.start(), text[pos : m.start()]
        found = _scan(text, m.start(), final)
        if found is None:
            return
        if found is False:
            pos = m.end()
            yield pos, m.group()
            continue
        pos = _skip_ws(text, found.end)  # whitespace between calls is format, not prose
        yield pos, found
    stop = len(text) if final else len(text) - _held(text)
    if stop > pos:
        yield stop, text[pos:stop]


def _held(text: str) -> int:
    """Length of a trailing fragment that may still grow into `<tool_call>` or `<function=`."""
    i = text.rfind("<")
    return len(text) - i if i >= 0 and _could_become(text[i:], _CALL, _FN) else 0


def _properties(request: dict[str, Any] | None, name: str) -> dict[str, Any]:
    for tool in (request or {}).get("tools") or []:
        fn = tool.get("function") or {}
        if fn.get("name") == name:
            props = (fn.get("parameters") or {}).get("properties")
            return props if isinstance(props, dict) else {}
    return {}


def _typed(value: str, prop: Any) -> Any:
    """String-typed and untyped parameters stay text; others are JSON or Python literals, else text."""
    kind = prop.get("type") if isinstance(prop, dict) else None
    if kind is None or set(kind if isinstance(kind, list) else [kind]) & _STRING:
        return value
    v = value.strip()
    try:
        return json.loads(v)
    except ValueError:
        pass
    try:
        lit = ast.literal_eval(v)
        json.dumps(lit)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return value
    return lit


def _arguments(call: _Call, request: dict[str, Any] | None) -> str:
    if call.json_args is not None:
        return call.json_args
    props = _properties(request, call.name)
    last = len(call.params) - 1
    args = {
        k: v if call.cut and i == last else _typed(v, props.get(k)) for i, (k, v) in enumerate(call.params)
    }
    out = _dumps(args)
    return out[:-2] if call.cut and call.params else out  # cut: no closing `"}`, so a runaway, not a call


def _call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"  # pragma: no mutate  (any unique id works)


class Qwen3XmlRawToolParser(ToolParser):  # type: ignore[misc]  # vllm_mlx ships no stubs
    SUPPORTS_NATIVE_TOOL_FORMAT = True  # earlier calls replay through the model's own template
    REQUIRES_EAGER_STREAMING = True  # vllm-mlx then passes every delta: a split `<function=` is seen

    def __init__(self, tokenizer: Any = None) -> None:
        super().__init__()  # the format needs no tokenizer
        self._request: dict[str, Any] | None = None  # pragma: no mutate  ("" is just as empty)
        self.reset()

    def reset(self) -> None:
        super().reset()
        self._pos = 0
        self._count = 0

    def extract_tool_calls(
        self, model_output: str, request: dict[str, Any] | None = None
    ) -> ExtractedToolCallInformation:
        text = self.strip_think_tags(model_output) if "</think>" in model_output else model_output
        calls: list[dict[str, Any]] = []
        prose: list[str] = []
        for _, seg in _segments(text, 0, final=True):
            if isinstance(seg, str):
                prose.append(seg)
            else:
                calls.append({"id": _call_id(), "name": seg.name, "arguments": _arguments(seg, request)})
        if not calls:
            return ExtractedToolCallInformation(tools_called=False, tool_calls=[], content=model_output)
        return ExtractedToolCallInformation(
            tools_called=True, tool_calls=calls, content="".join(prose).strip() or None
        )

    def extract_tool_calls_streaming(
        self,
        previous_text: str,
        current_text: str,
        delta_text: str,
        previous_token_ids: Any = None,
        current_token_ids: Any = None,
        delta_token_ids: Any = None,
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not previous_text:
            self.reset()
        if request is not None:
            self._request = request
        return self._advance(current_text, final=False)  # pragma: no mutate  (None is as falsy)

    def finalize_streaming(self, current_text: str) -> dict[str, Any] | None:
        return self._advance(current_text, final=True)

    def _advance(self, text: str, final: bool) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        for end, seg in _segments(text, self._pos, final):
            if isinstance(seg, str):
                out["content"] = out.get("content", "") + seg
            else:
                fn = {"name": seg.name, "arguments": _arguments(seg, self._request)}
                call = {"index": self._count, "id": _call_id(), "type": "function", "function": fn}
                out.setdefault("tool_calls", []).append(call)
                self._count += 1
            self._pos = end
        return out or None
