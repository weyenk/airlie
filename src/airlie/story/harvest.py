"""A tool-written package's tests, read from the architect's checkout (the tool-written packages design,
2026-11-06).

The architect writes a layer's tests with write_file and edit_file into its ask's worktree, and its reply is
the package JSON alone. After the turn `harvest` reads the test files changed since a commit (the layer's
base, or the snapshot the turn's edits were made on) and refuses a test that was there and is gone: an
existing test may be edited in place, never deleted (Noah, 2026-11-06). A reply that writes test blocks
anyway, in the old `### test file:` / `### test additions:` form, has them written into the checkout first
(`place_reply_tests`), so both forms reach `parse_package` the same way."""

from __future__ import annotations

import ast
from pathlib import Path

from airlie.story.js_tests import JsParseError, js_tests
from airlie.story.layer_checks import test_functions
from airlie.story.model import REPLY_TAIL, StoryError
from airlie.story.package import _normal, _test_blocks, joined_tests
from airlie.teams.worktree import changed_in_tree, find_file, read_blob
from airlie.tools.scope import is_js_test, is_test_file

DELETED_TESTS = (
    "your changes deleted {ids}, which the layer's base has: a test that is there stays. Put it back as it "
    "was; to change what it asserts, edit it in place with edit_file"
)


def _names(path: str, text: str) -> set[str] | None:
    """The tests the file `path` (its `text`) defines, by their names in a test id (pytest "Class::test_x",
    Vitest "describe > it"); None when it does not parse (the readiness check reports that)."""
    if is_js_test(path):
        try:
            return {t.name for t in js_tests(text)}
        except JsParseError:
            return None
    try:
        return {name for name, _ in test_functions(ast.parse(text))}
    except (SyntaxError, ValueError):
        return None


def harvest(worktree: Path, since: str) -> dict[str, str]:
    """The test files `worktree` added or changed since commit `since`, committed or not, path -> text, in
    path order. A test that a file had at `since` and has no more (the file deleted, the test removed or
    renamed) is a StoryError naming each, by its id; a file that does not parse, then or now, is not judged
    here."""
    tests: dict[str, str] = {}
    deleted: list[str] = []
    for path in sorted(p for p in changed_in_tree(worktree, since) if is_test_file(p)):
        f = worktree / path
        now = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None
        if now is not None:
            tests[path] = now
        kind, sha = find_file(worktree, since, path)
        before = _names(path, read_blob(worktree, sha)) if kind == "blob" else None
        after = _names(path, now) if now is not None else set()
        if before is not None and after is not None:
            deleted += [f"{path}::{name}" for name in sorted(before - after)]
    if deleted:
        raise StoryError(DELETED_TESTS.format(ids=", ".join(deleted)))
    return tests


def place_reply_tests(worktree: Path, text: str) -> list[str]:
    """Write the test blocks of reply `text` into `worktree` and return their paths, sorted: a
    `### test file:` block as the file's whole text, a `### test additions:` block joined to the file as it is
    there (`joined_tests`). Every block is checked before any is written: a path that is not a test file or
    leaves the repository, additions to a file that is not there, or a heading with no block is a StoryError
    that keeps the reply's end (`reply`), as `parse_package` refuses them."""
    try:
        return _place(worktree, text)
    except StoryError as e:
        e.reply = text[-REPLY_TAIL:]
        raise


def _place(worktree: Path, text: str) -> list[str]:
    root = worktree.resolve()
    out: dict[str, str] = {}
    for raw_path, (kind, body) in _test_blocks(text).items():
        path = _normal(raw_path, "a test file path")
        if not is_test_file(path):
            raise StoryError(f"{path} is not a test file (a pytest test_*.py, or a Vitest *.test.ts)")
        target = root / path
        if kind == "additions":
            if not target.is_file():
                raise StoryError(
                    f"test additions to {path}: the file is not in your checkout; write it whole with "
                    "write_file"
                )
            body = joined_tests(target.read_text(encoding="utf-8", errors="replace"), body)
        out[path] = body
    for path, body in out.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(body, encoding="utf-8")
    return sorted(out)
