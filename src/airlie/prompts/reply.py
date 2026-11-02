"""Reply templates: the shape a node's answer takes, one YAML file each in `airlie/prompts/replies/`.

A template holds the answer's JSON Schema (`schema`), the shape as the model is shown it (`shape`, readable
pseudo-JSON with `|` between choices) and the rules that go with it (`rules`). From the one file come the
contract a brief ends with (`contract`, the answer and nothing else; `part`, a block posted inside a meeting
turn) and the structural check of a parsed answer (`problem`): the parser after it keeps the checks a schema
cannot state (an id the story has, a criterion under one layer).

Why (2026-11-02, the oversharing audit): the contracts said "end your reply with one ```json block", and the
architect wrote prose before it; its layer plans were 6-19k characters, most of them outside the block, every
character decoded. And a reply whose JSON was wrong said only "unusable": the problem now names the path."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match

WHOLE = (
    "Reply with one ```json block holding exactly one object, and nothing else: no prose before or after it "
    "(think before you answer, not in the reply). The object:"
)
"""How a contract opens when the object is the role's whole answer."""
PART = "In your turn, post it as one ```json block holding exactly one object:"
"""How it opens when the object is one part of a meeting turn, which ends with a verdict."""
_KEYS = frozenset({"why", "schema", "shape", "rules"})


class ReplyError(ValueError):
    """A reply template that is missing or wrong; the message names it."""


@dataclass(frozen=True)
class Reply:
    name: str
    schema: Mapping[str, Any]
    shape: str
    rules: str

    def _text(self, opening: str) -> str:
        return "\n".join(x for x in (opening, self.shape.strip(), self.rules.strip()) if x)

    @property
    def contract(self) -> str:
        """The contract for an answer that is this object and nothing else."""
        return self._text(WHOLE)

    @property
    def part(self) -> str:
        """The contract for this object posted as one part of a meeting turn."""
        return self._text(PART)

    def problem(self, obj: Any) -> str | None:
        """What is structurally wrong with `obj` against the schema, in one line naming where (None: nothing).
        The error that best explains the failure is the one named."""
        error = best_match(Draft202012Validator(dict(self.schema)).iter_errors(obj))
        return None if error is None else _say(error)


def _say(error: ValidationError) -> str:
    where = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in error.absolute_path).lstrip(".")
    return f"{where or 'the object'}: {error.message}"


@cache
def reply(name: str) -> Reply:
    """The template `airlie/prompts/replies/<name>.yaml`, checked, loaded once. ReplyError when there is no
    such template, a key is unknown or missing, or its schema is not a valid JSON Schema."""
    path = files("airlie.prompts") / "replies" / f"{name}.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError:
        raise ReplyError(f"reply {name}: no such template") from None
    if not isinstance(data, dict) or set(data) - _KEYS or not {"schema", "shape"} <= set(data):
        raise ReplyError(f"reply {name}: needs schema and shape, and only {sorted(_KEYS)}")
    try:
        Draft202012Validator.check_schema(data["schema"])
    except Exception as e:  # noqa: BLE001 - jsonschema's SchemaError, named with the template
        raise ReplyError(f"reply {name}: its schema is not valid: {e}") from None
    return Reply(name, data["schema"], str(data["shape"]), str(data.get("rules") or ""))
