"""Prompt text with placeholders, rendered from a node's inputs, the card and config
(nodes-are-config design, "The node, by field", D3, D13).

A placeholder is `{path}`: `survey`, `story.title`, `config.story.max_layers`, plus the two names a node
always has, `{feedback}` (why the last reply did not parse, then the loop's carried feedback) and `{item}`
(the item a bundle runs for). `{?name}...{/name}` keeps its body only when `name` has a value. `{{` and `}}`
are literal braces. The caller owns where a path's value comes from: `render` asks the `lookup` callable only.
This module imports nothing from `airlie.flow.spec`, so the spec can check a node's prompt with it."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from airlie.flow.prompt_files import FEEDBACK, _feedback, prompt_text, render_value
from airlie.prompts.hub import PromptError, PromptText

ITEM = "item"
PREFIX = "prompt:"

_TOKEN = re.compile(r"\{\{|\}\}|\{\?([^{}\s]*)\}|\{/([^{}\s]*)\}|\{([^{}]*)\}|[{}]")
_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*")

# A parsed text: literal strings, ("var", path) and ("block", name, children).
_Part = str | tuple[str, str] | tuple[str, str, list[Any]]


def _check_path(path: str, text: str) -> str:
    if not _PATH.fullmatch(path):
        raise PromptError(
            f"prompt text: '{{{path}}}' is not a placeholder (a name or a dotted path): {text[:60]!r}"
        )
    return path


def _parse(text: str) -> list[_Part]:
    root: list[_Part] = []
    stack: list[tuple[str, list[_Part]]] = [("", root)]
    at = 0
    for m in _TOKEN.finditer(text):
        stack[-1][1].append(text[at : m.start()]) if m.start() > at else None
        at = m.end()
        token = m.group(0)
        if token == "{{":
            stack[-1][1].append("{")
        elif token == "}}":
            stack[-1][1].append("}")
        elif m.group(1) is not None:
            name = _check_path(m.group(1), text)
            children: list[_Part] = []
            stack[-1][1].append(("block", name, children))
            stack.append((name, children))
        elif m.group(2) is not None:
            name = m.group(2)
            if len(stack) == 1 or stack[-1][0] != name:
                raise PromptError(f"prompt text: '{{/{name}}}' closes a block that is not open")
            stack.pop()
        elif m.group(3) is not None:
            stack[-1][1].append(("var", _check_path(m.group(3).strip(), text)))
        else:
            raise PromptError(f"prompt text: a lone '{token}' (write '{token}{token}' for a literal brace)")
    if len(stack) > 1:
        raise PromptError(f"prompt text: the block '{{?{stack[-1][0]}}}' is never closed")
    if at < len(text):
        root.append(text[at:])
    return root


def _heads(parts: list[_Part], found: set[str]) -> None:
    for part in parts:
        if isinstance(part, str):
            continue
        found.add(part[1].split(".")[0])
        if part[0] == "block":
            _heads(part[2], found)  # type: ignore[misc]


def names(text: str) -> frozenset[str]:
    """The heads of the placeholders in `text` (`story` of `{story.title}`, `config` of `{config.x}`), and of
    the `{?name}...{/name}` blocks. `PromptError` when the text is malformed."""
    found: set[str] = set()
    _heads(_parse(text), found)
    return frozenset(found)


def prompt_source(node_prompt: str, texts: Mapping[str, PromptText]) -> str:
    """The text a node's `prompt` stands for: a `prompt:<id>` is the hub rule's or the draft's body, anything
    else is the text itself."""
    if node_prompt.startswith(PREFIX):
        return prompt_text(node_prompt.removeprefix(PREFIX), texts).body
    return node_prompt


def render(
    text: str, lookup: Callable[[str], Any], *, feedback: str | None = None, item: str | None = None
) -> str:
    """`text` with each placeholder filled. A path is `render_value(lookup(path))`; `lookup` raises `KeyError`
    for a name it does not have, which is a `PromptError` here (a block's name just counts as absent).
    `{feedback}` is `feedback` (the reason the last reply failed) then what `lookup("feedback")` carries, a
    blank line between, empty on a first try; `{item}` is `item` or empty."""

    def get(path: str) -> Any:
        return lookup(path)

    def carried() -> Any:
        try:
            return lookup(FEEDBACK)
        except KeyError:
            return None

    def present(path: str) -> bool:
        try:
            if path == FEEDBACK:
                return bool(_feedback(feedback, carried()))
            if path == ITEM:
                return bool(item)
            value = get(path)
        except KeyError:
            return False
        return value is not None and value != "" and value != [] and value != {}

    def fill(parts: list[_Part]) -> str:
        out: list[str] = []
        for part in parts:
            if isinstance(part, str):
                out.append(part)
            elif part[0] == "block":
                if present(part[1]):
                    out.append(fill(part[2]))  # type: ignore[misc]
            elif part[1] == FEEDBACK:
                out.append(_feedback(feedback, carried()))
            elif part[1] == ITEM:
                out.append(item or "")
            else:
                try:
                    out.append(render_value(get(part[1])))
                except KeyError:
                    raise PromptError(f"prompt names '{part[1]}', which has no value here") from None
        return "".join(out)

    return fill(_parse(text))
