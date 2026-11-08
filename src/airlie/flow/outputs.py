"""What a node declares it produces: a name, a format and, for a structured answer, a short field list.

`Strict` and `Format` live here so `airlie.flow.spec` can import `OutputSpec` without a cycle; the spec
module re-exports them. The field-kind grammar (`parse_kind`) is the whole vocabulary a field may use:

    text | number | bool | list of <kind> | a | b | c  (optionally "one of a | b | c")

and any kind may end in `?` for an optional field. It is deliberately small: a short field list, not a JSON
schema (nodes-are-config design, Outputs)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


Format = Literal[
    "json", "markdown", "text", "bool", "list", "ref:branch", "ref:worktree", "ref:pr", "file", "session"
]

Scalar = Literal["text", "number", "bool"]
_SCALARS: tuple[Scalar, ...] = ("text", "number", "bool")


@dataclass(frozen=True)
class Kind:
    """One parsed field kind. `name` is text, number, bool, list or one_of; a list has an `item` kind and a
    one_of has its `options`."""

    name: Literal["text", "number", "bool", "list", "one_of"]
    optional: bool = False
    item: Kind | None = None
    options: tuple[str, ...] = ()


def parse_kind(text: str) -> Kind:
    """The `Kind` a field's text names; `ValueError` naming the text when it is not in the grammar."""
    body = text.strip()
    optional = body.endswith("?")
    if optional:
        body = body[:-1].strip()
    for scalar in _SCALARS:
        if body == scalar:
            return Kind(scalar, optional)
    if body.startswith("list of "):
        try:
            return Kind("list", optional, item=parse_kind(body.removeprefix("list of ")))
        except ValueError:
            raise ValueError(f"invalid field kind '{text}': a list needs a kind of item") from None
    choosing = "|" in body or body.startswith("one of ")
    names = tuple(o.strip() for o in body.removeprefix("one of ").split("|")) if choosing else ()
    if names and all(names):
        return Kind("one_of", optional, options=names)
    raise ValueError(
        f"invalid field kind '{text}': use text, number, bool, list of <kind>, or choices like 'a | b'"
    )


class OutputSpec(Strict):
    format: Format
    fields: dict[str, str] | None = None  # field name -> kind text, for a structured (json) answer

    @model_validator(mode="before")
    @classmethod
    def _bare_format(cls, data: Any) -> Any:
        return {"format": data} if isinstance(data, str) else data

    @model_validator(mode="after")
    def _kinds(self) -> OutputSpec:
        for name, kind in (self.fields or {}).items():
            try:
                parse_kind(kind)
            except ValueError as e:
                raise ValueError(f"field '{name}': {e}") from None
        return self


def _kind_text(kind: Kind) -> str:
    if kind.name == "list":
        assert kind.item is not None
        return f"list of {_kind_text(kind.item)}"
    return "one of " + " | ".join(kind.options) if kind.name == "one_of" else kind.name


def _fits(value: Any, kind: Kind) -> bool:
    if kind.name == "text":
        return isinstance(value, str)
    if kind.name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind.name == "bool":
        return isinstance(value, bool)
    if kind.name == "one_of":
        return isinstance(value, str) and value in kind.options
    assert kind.item is not None
    return isinstance(value, list) and all(_fits(v, kind.item) for v in value)


_BOOLS = {"yes": True, "true": True, "no": False, "false": False}


def parse_output(text: str, spec: OutputSpec) -> Any:
    """The value a reply `text` stands for under `spec`; `StoryError` naming what is wrong when it does not
    fit. `json`: the last ```json block, every non-optional field present and of its kind; `markdown` and
    `text`: the reply stripped; `bool`: yes/no/true/false; `list`: a JSON list. Other formats are refs the
    engine writes itself, not replies."""
    from airlie.story.model import StoryError
    from airlie.topology.studio_plan import extract_json_block, json_block_problem

    if spec.format in ("markdown", "text"):
        return text.strip()
    if spec.format == "bool":
        word = text.strip().strip(".!").lower()
        if word not in _BOOLS:
            raise StoryError("the reply must be yes or no (or true or false)")
        return _BOOLS[word]
    if spec.format == "list":
        try:
            value = json.loads(text.strip())
        except ValueError:
            value = None
        if not isinstance(value, list):
            raise StoryError("the reply must be a JSON list")
        return value
    if spec.format != "json":
        raise StoryError(f"a reply cannot be parsed as {spec.format}")
    block = extract_json_block(text)
    if block is None:
        problem = json_block_problem(text)
        raise StoryError(problem or "the reply has no JSON object (one ```json block)")
    for name, kind_text in (spec.fields or {}).items():
        kind = parse_kind(kind_text)
        if name not in block or block[name] is None:
            if kind.optional:
                continue
            raise StoryError(f"field '{name}' is missing; it must be {_kind_text(kind)}")
        if not _fits(block[name], kind):
            raise StoryError(f"field '{name}' must be {_kind_text(kind)}")
    return block


def describe(spec: OutputSpec) -> str:
    """The sentence a generic ask adds to say how to reply: the hub rule `output-format` filled with the
    fields. For the builder and the team node; never appended to a prompt automatically."""
    from airlie.prompts import hub

    if spec.format != "json" or not spec.fields:
        return "Reply with one ```json block." if spec.format == "json" else f"Reply with {spec.format}."
    listing = ", ".join(
        f"{name} ({_kind_text(kind)}{', optional' if kind.optional else ''})"
        for name, kind in ((n, parse_kind(k)) for n, k in spec.fields.items())
    )
    return hub.text("output-format", fields=listing)
