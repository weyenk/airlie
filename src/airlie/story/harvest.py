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
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    tests, deleted = harvest_files(worktree, since)
    if deleted:
        raise StoryError(DELETED_TESTS.format(ids=", ".join(deleted)))
    return tests


def harvest_files(worktree: Path, since: str) -> tuple[dict[str, str], list[str]]:
    """`harvest`'s two halves, with nothing refused: the test files, and the ids of the tests deleted since
    `since`. A try that ends unusable keeps its files for the next one, deletions and all."""
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
    return tests, deleted


def place_tests(worktree: Path, tests: Mapping[str, str]) -> list[str]:
    """Write `tests` (path -> whole text) into `worktree` and return their paths, sorted: the last try's test
    files, put back for the next try to edit (the tool-written packages design, 2026-11-06). A path that is
    absolute or leaves the worktree is a ValueError."""
    root = worktree.resolve()
    for path in sorted(tests):
        target = (root / path).resolve()
        if Path(path).is_absolute() or not target.is_relative_to(root):
            raise ValueError(f"test path {path!r} must be inside the worktree")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(tests[path], encoding="utf-8")
    return sorted(tests)


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


@dataclass(frozen=True)
class EditedTest:
    """A test that was on the layer's base and that the package changes in place (Noah, 2026-11-06: allowed,
    and shown to the critic and in the PR): its id and its source before and after."""

    id: str
    before: str
    after: str


def _sources(path: str, text: str) -> dict[str, str] | None:
    """Each test of the file `path` (its `text`) by its name in a test id, with its source; None when it does
    not parse."""
    if is_js_test(path):
        try:
            return {t.name: t.source for t in js_tests(text)}
        except JsParseError:
            return None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.splitlines()
    classes = {c.name: c for c in tree.body if isinstance(c, ast.ClassDef)}

    def whole(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
        """`node`'s source with its decorators: dropping a parametrize case changes only them (review of
        PR #251, 2026-11-06)."""
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        return "\n".join(lines[start - 1 : node.end_lineno])

    out: dict[str, str] = {}
    for name, fn in test_functions(tree):
        cls = classes.get(name.partition("::")[0]) if "::" in name else None
        # a method's class decorators (a mark, a parametrize) are part of what the test checks
        context = "".join(f"@{ast.get_source_segment(text, d)}\n" for d in cls.decorator_list) if cls else ""
        out[name] = context + whole(fn)
    return out


def edited_tests(tests: Mapping[str, str], base: Callable[[str], str | None]) -> list[EditedTest]:
    """The tests of `tests` (path -> whole text) that `base` (path -> text on the layer's base, None when
    absent) already has, with a different source, in path and file order. A file that does not parse, then
    or now, gives none."""
    found: list[EditedTest] = []
    for path in sorted(tests):
        old_text = base(path)
        old = _sources(path, old_text) if old_text is not None else None
        new = _sources(path, tests[path])
        if not old or new is None:
            continue
        found += [
            EditedTest(f"{path}::{name}", old[name], src)
            for name, src in new.items()
            if name in old and old[name] != src
        ]
    return found


def merged(ancestor: str, ours: str, theirs: str) -> str | None:
    """`theirs` (a draft's file) with what changed from `ancestor` (the file it was drafted on) to `ours`
    (the file on the layer's real base) merged in, by `git merge-file`; None when the two changes conflict.
    When `ours` is `ancestor`, `theirs` as it is."""
    if ours == ancestor:
        return theirs
    with tempfile.TemporaryDirectory(prefix="airlie-merge-") as d:
        paths = [Path(d) / n for n in ("ours", "ancestor", "theirs")]
        for p, text in zip(paths, (ours, ancestor, theirs), strict=True):
            p.write_text(text, encoding="utf-8")
        done = subprocess.run(
            ["git", "merge-file", "-p", *map(str, paths)], capture_output=True, text=True, check=False
        )
    return done.stdout if done.returncode == 0 else None


def rebase_draft(
    tests: Mapping[str, str], drafted: Callable[[str], str | None], base: Callable[[str], str | None]
) -> tuple[dict[str, str], list[str]]:
    """A tool-written draft's test files rebased onto its layer's real base (the tool-written packages
    design, 2026-11-06): each file merged three ways (`merged`), the ancestor the file as it was where the
    draft's edits were made (`drafted`, path -> text at the draft's `at`), ours the file on the real base
    (`base`). The rebased files, and the paths whose changes conflict (left out of the files)."""
    out: dict[str, str] = {}
    conflicts: list[str] = []
    for path in sorted(tests):
        got = merged(drafted(path) or "", base(path) or "", tests[path])
        if got is None:
            conflicts.append(path)
        else:
            out[path] = got
    return out, conflicts
