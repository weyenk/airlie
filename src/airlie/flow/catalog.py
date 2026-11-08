"""The catalog of checks: the deterministic operations a check or gate node names by `run`, each declared with
the artifacts it reads and the ones it returns (nodes-are-config design, "Checks", D2, D13, D18).

A check is code by nature: a Python function `(ctx, **args)` (it may be async) registered under a name with
its declared inputs and outputs. The builder shows the declaration as "what this check does", and
`check_workflows(..., catalog=)` holds a node to it: a `run` the catalog lacks, or a node whose outputs or
inputs disagree with the declaration, is a problem before anything runs. Item-scoped names (`built.{item}`)
are declared and matched literally.

`STORY_CATALOG` is the story's (filled by `airlie.story.flow` with the `check` decorator); this module
imports nothing from the story or the config, so `airlie.flow.spec` may name it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from airlie.flow.outputs import Format

__all__ = ["STORY_CATALOG", "Catalog", "Check", "CheckFn", "check"]

CheckFn = Callable[..., Awaitable[Mapping[str, Any]] | Mapping[str, Any]]
"""`fn(ctx, **args)`: the outputs it returns, by name (an empty mapping or None: none)."""

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class Check:
    """One declared operation: its `name` (the `run` id), what it reads (`inputs`) and returns (`outputs`),
    a one-line `title` for the builder, the function, and the format of each output it states one for."""

    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    title: str
    fn: CheckFn
    formats: Mapping[str, Format] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "formats": dict(self.formats),
        }


class Catalog:
    """Checks by name, in the order they were declared."""

    def __init__(self) -> None:
        self._checks: dict[str, Check] = {}

    def add(self, check: Check) -> None:
        if check.name in self._checks:
            raise ValueError(f"check '{check.name}' is declared twice")
        if stray := sorted(set(check.formats) - set(check.outputs)):
            raise ValueError(
                f"check '{check.name}': formats name {', '.join(stray)}, which it does not output"
            )
        self._checks[check.name] = check

    def __getitem__(self, name: str) -> Check:
        if name not in self._checks:
            raise KeyError(f"the catalog has no check '{name}'")
        return self._checks[name]

    def __contains__(self, name: object) -> bool:
        return name in self._checks

    def __iter__(self) -> Iterator[Check]:
        return iter(self._checks.values())

    def __len__(self) -> int:
        return len(self._checks)

    def names(self) -> list[str]:
        return list(self._checks)

    def describe(self) -> list[dict[str, Any]]:
        """Every check as the builder lists it: name, title, inputs, outputs (and their formats)."""
        return [c.describe() for c in self._checks.values()]

    def declare(
        self,
        name: str,
        *,
        inputs: Sequence[str] = (),
        outputs: Sequence[str] = (),
        formats: Mapping[str, Format] | None = None,
        title: str,
    ) -> Callable[[F], F]:
        """A decorator: the function, unchanged, declared here under `name`."""

        def register(fn: F) -> F:
            self.add(Check(name, tuple(inputs), tuple(outputs), title, fn, dict(formats or {})))
            return fn

        return register


STORY_CATALOG = Catalog()
"""The story and layer workflows' checks, declared in `airlie.story.flow`."""


def check(
    name: str,
    *,
    inputs: Sequence[str] = (),
    outputs: Sequence[str] = (),
    formats: Mapping[str, Format] | None = None,
    title: str,
) -> Callable[[F], F]:
    """Declare the decorated `(ctx, **args)` function on `STORY_CATALOG` (`Catalog.declare`)."""
    return STORY_CATALOG.declare(name, inputs=inputs, outputs=outputs, formats=formats, title=title)
