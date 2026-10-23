"""Which definitions in a locked test file are the tester's.

A story layer locks the tester's tests against the coder. Locking whole files stopped AIR-9 layer 05
(2026-10-22): the coder could not change the fakes `RecordingBoard`, `FlakyBoard` and `DownBoard` in a file
the tester had only added tests to. This locks just what the tester wrote: the top-level functions and classes
that are new or changed since the layer's base. Imports and constants are never locked. A move or a reformat
is no change, since `ast.dump` leaves out line numbers. No model call."""

from __future__ import annotations

import ast
from collections.abc import Collection

Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _definitions(source: str) -> dict[str, str] | None:
    """Top-level definition names mapped to their dumped syntax. None when `source` does not parse.

    A later definition of the same name replaces an earlier one, as in Python."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    return {node.name: ast.dump(node) for node in tree.body if isinstance(node, Definition)}


def written_by_tester(base: str | None, locked: str) -> frozenset[str] | None:
    """Names in `locked` that are absent from `base` or differ there. None means lock the whole file.

    `base=None`, or a `base` that does not parse, means the file is new: every definition is the tester's."""
    now = _definitions(locked)
    if now is None:
        return None
    was = _definitions(base) if base is not None else None
    if was is None:
        return frozenset(now)
    return frozenset(name for name, dump in now.items() if was.get(name) != dump)


def touched(names: Collection[str], before: str, after: str) -> list[str] | None:
    """Sorted `names` missing from `after` or changed from `before`. None if `after` does not parse."""
    now = _definitions(after)
    if now is None:
        return None
    was = _definitions(before) or {}
    return sorted(name for name in names if name not in now or now[name] != was.get(name))
