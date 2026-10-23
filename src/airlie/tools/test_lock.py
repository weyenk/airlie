"""Which definitions in a locked test file are the tester's.

A story layer locks the tester's tests against the coder. Locking whole files stopped AIR-9 layer 05
(2026-10-22): the coder could not change the fakes `RecordingBoard`, `FlakyBoard` and `DownBoard` in a file
the tester had only added tests to. This locks just what the tester wrote: the top-level functions and classes
that are new or changed since the layer's base. Imports and constants are never locked, constants the tester
wrote included (CASES = [] still empties a parametrized test). A module-level statement that names a locked
test (a rebinding, a `del`, `__test__ = False`, a redefinition under `if`) or binds `pytestmark` is guarded
with it, since it can switch the test off without touching its definition. A move or a reformat is no
change, since `ast.dump` leaves out line numbers. No model call."""

from __future__ import annotations

import ast
from collections import Counter
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


def _mentions(node: ast.stmt, names: Collection[str]) -> frozenset[str]:
    """The locked `names` a module-level statement names anywhere in it, plus "pytestmark" when it binds that.

    A name counts as a `Name` (read, bound or deleted), a nested definition, an import's name or alias, or a
    string constant (`globals()["test_new"]`)."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            if sub.id in names:
                found.add(sub.id)
            if sub.id == "pytestmark" and not isinstance(sub.ctx, ast.Load):
                found.add("pytestmark")
        elif isinstance(sub, Definition) and sub.name in names:
            found.add(sub.name)
        elif isinstance(sub, ast.alias):
            found.update(bound for bound in (sub.name, sub.asname) if bound is not None and bound in names)
            if (sub.asname or sub.name) == "pytestmark":
                found.add("pytestmark")
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value in names:
            found.add(sub.value)
    return frozenset(found)


def _guarding(tree: ast.Module, names: Collection[str]) -> Counter[str]:
    """The dumped module-level statements, not definitions, that mention a locked name or bind pytestmark."""
    return Counter(
        ast.dump(node) for node in tree.body if not isinstance(node, Definition) and _mentions(node, names)
    )


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
    """Sorted `names` missing from `after` or changed from `before`. None if `after` does not parse.

    A module-level statement that mentions one of `names`, or binds pytestmark, and was added, removed or
    changed reports the names it mentions ("pytestmark" for that binding): it can switch a test off without
    touching its definition. Imports the tests merely use are not guarded, so an autofix merging import lines
    does not undo the coder's work."""
    try:
        after_tree = ast.parse(after)
    except (SyntaxError, ValueError):
        return None
    now = _definitions(after) or {}
    was = _definitions(before) or {}
    hit = {name for name in names if name not in now or now[name] != was.get(name)}
    try:
        before_tree: ast.Module | None = ast.parse(before)
    except (SyntaxError, ValueError):
        before_tree = None
    old = _guarding(before_tree, names) if before_tree is not None else Counter()
    new = _guarding(after_tree, names)
    if old != new:
        bodies = list(after_tree.body) + (list(before_tree.body) if before_tree is not None else [])
        differing = set((old - new) + (new - old))
        for node in bodies:
            if not isinstance(node, Definition) and ast.dump(node) in differing:
                hit |= _mentions(node, names)
    return sorted(hit)
