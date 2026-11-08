"""What a node declares it produces: a name, a format and, for a structured answer, a short field list.

`Strict` and `Format` live here so `airlie.flow.spec` can import `OutputSpec` without a cycle; the spec
module re-exports them. The field-kind grammar (`parse_kind`) is the whole vocabulary a field may use:

    text | number | bool | list of <kind> | a | b | c  (optionally "one of a | b | c")

and any kind may end in `?` for an optional field. It is deliberately small: a short field list, not a JSON
schema (nodes-are-config design, Outputs)."""

from __future__ import annotations

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
