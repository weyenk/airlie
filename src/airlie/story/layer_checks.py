"""Checks on the tests a layer added or changed, run by the pipeline after the coder's build and before
the gate, with no model. Red check: with every other change of the layer put back to its base, those tests
run, and one that passes there does not test the change (`vacuous_test`). No-assertion check: one with
nothing in it that can fail it but a crash (`assertion_free_test`). Python and pytest only, behind
`checks_for`: any other gate gets none, and the pipeline logs that they did not run.

The test files are model output, read as data: parsed with `ast`, never imported here. The tests themselves
run as the gate runs them (tools.shell.run_cmd: the agent environment, killed on a timeout)."""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from airlie.teams.worktree import changed_files, find_file, put_back, read_blob, reset_to_head
from airlie.tools.protocol import ToolContext
from airlie.tools.scope import is_test_module, pytest_runner
from airlie.tools.shell import run_cmd

Kind = Literal["vacuous_test", "assertion_free_test", "unparseable_test"]
VACUOUS = (
    "it passes on the layer's base with the layer's other changes undone, so it does not test this change"
)
ASSERTION_FREE = "it has no assert, pytest.raises, pytest.warns or other check, so only a crash can fail it"
CHECK_CALLS = frozenset({"raises", "warns", "deprecated_call", "fail", "expect"})
"""Calls that are a check: pytest's, and expect(...) (playwright's). Any call whose name starts with "assert"
is one too: unittest's self.assertEqual, a mock's assert_called_once_with, a helper's assert_valid."""
_PASSED = re.compile(r"^PASSED (\S+)", re.MULTILINE)
_PARAMS = re.compile(r"\[.*\]$")
_SOURCE_CHARS = 4_000
"""How much of a test's source a finding keeps (for the events, as a negative example)."""
_OUTPUT_CHARS = 2_000_000
"""pytest's output is read for its PASSED lines, not shown: it is kept whole up to this."""


@dataclass(frozen=True)
class ChangedTest:
    """A test function the layer added or changed: its file, its name in it ("test_x" or "TestY::test_x"),
    its source, and whether it holds a check (`asserts`)."""

    path: str
    name: str
    source: str
    asserts: bool

    @property
    def id(self) -> str:
        """Its pytest node id, from the repository's root."""
        return f"{self.path}::{self.name}"


@dataclass(frozen=True)
class Finding:
    kind: Kind
    test: str
    why: str
    source: str


def _test_functions(tree: ast.Module) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """What pytest collects by default: test* functions, and test* methods of Test* classes."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test"):
            yield node.name, node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and sub.name.startswith("test"):
                    yield f"{node.name}::{sub.name}", sub


def _parse(text: str | None) -> ast.Module | None:
    if text is None:
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):  # unparseable_tests reports the layer's own; a base's is no baseline
        return None


def _layer_test_modules(worktree: Path, base: str) -> list[str]:
    """The test modules the worktree's HEAD changed since `base` that are still there."""
    return [p for p in changed_files(worktree, base) if is_test_module(p) and (worktree / p).is_file()]


def unparseable_tests(worktree: Path, base: str) -> list[Finding]:
    """A finding for each test module the layer changed that does not parse, with the error and its line: the
    coder gets a retry to fix it (HDS-5, 2026-10-17: a missing quote left the checks with nothing to check,
    and the gate blocked the layer with no retry)."""
    found = []
    for path in _layer_test_modules(worktree, base):
        text = (worktree / path).read_text(encoding="utf-8", errors="replace")
        try:
            ast.parse(text)
        except SyntaxError as e:
            found.append(
                Finding("unparseable_test", path, f"it does not parse: {e.msg} at line {e.lineno}", "")
            )
        except ValueError as e:
            found.append(Finding("unparseable_test", path, f"it does not parse: {e}", ""))
    return found


def _call_name(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _checks(
    node: ast.AST, helpers: dict[str, ast.FunctionDef | ast.AsyncFunctionDef], seen: set[str]
) -> bool:
    """`node` holds a check: an assert, a raise, a check call, or a call to a module function holding one."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert | ast.Raise):
            return True
        if isinstance(sub, ast.Call):
            name = _call_name(sub)
            if name.startswith("assert") or name in CHECK_CALLS:
                return True
            if isinstance(sub.func, ast.Name) and name in helpers and name not in seen:
                seen.add(name)
                if _checks(helpers[name], helpers, seen):
                    return True
    return False


def _base_text(repo: Path, base: str, path: str) -> str | None:
    kind, sha = find_file(repo, base, path)
    return read_blob(repo, sha) if kind == "blob" else None


def changed_tests(worktree: Path, base: str) -> list[ChangedTest]:
    """The test functions of the test modules the worktree's HEAD changed since `base` that are new there or
    whose syntax tree differs from the base's (a reformat, a comment, is no change), in file order."""
    found: list[ChangedTest] = []
    for path in _layer_test_modules(worktree, base):
        text = (worktree / path).read_text(encoding="utf-8", errors="replace")
        tree = _parse(text)
        if tree is None:
            continue
        before = _parse(_base_text(worktree, base, path))
        old = {name: ast.dump(fn) for name, fn in _test_functions(before)} if before else {}
        helpers = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
        for name, fn in _test_functions(tree):
            if old.get(name) == ast.dump(fn):
                continue
            source = (ast.get_source_segment(text, fn) or "")[:_SOURCE_CHARS]
            found.append(ChangedTest(path, name, source, _checks(fn, helpers, {fn.name})))
    return found


def _passed(output: str, ids: Sequence[str]) -> set[str]:
    """Which of `ids` pytest's -rA summary reports PASSED (any of a parametrized test's cases counts). Its ids
    are from pytest's rootdir, which can be below the repository's root: a match on the end is a match."""
    passed = {_PARAMS.sub("", m) for m in _PASSED.findall(output)}
    return {i for i in ids if any(i == p or i.endswith("/" + p) for p in passed)}


class PytestChecks:
    """Both checks for a Python repository whose gate runs pytest with `runner` (`pytest_runner`)."""

    def __init__(self, runner: str, *, timeout_s: float, confinement: dict[str, Any] | None = None) -> None:
        self.runner = runner
        self.timeout_s = timeout_s
        self.confinement = confinement or {}
        """ToolContext's sandbox and writable for the test runs (tools.confinement); default: confined."""

    async def run(self, worktree: Path, base: str, *, red_base: str | None = None) -> list[Finding]:
        """The findings on the tests the worktree's HEAD added or changed since `base`. The red check puts the
        other changes since `red_base` back (default `base`): a layer that tests code an earlier layer wrote
        (the hardening layer) is checked against the story's base. The worktree must hold no uncommitted
        change: the red check puts files back, and leaves it as its HEAD holds it."""
        findings = unparseable_tests(worktree, base)
        tests = changed_tests(worktree, base)
        if not tests:
            return findings
        green = await self._pass_on_base(worktree, red_base or base, tests)
        for t in tests:
            if t.id in green:
                findings.append(Finding("vacuous_test", t.id, VACUOUS, t.source))
            if not t.asserts:
                findings.append(Finding("assertion_free_test", t.id, ASSERTION_FREE, t.source))
        return findings

    async def _pass_on_base(self, worktree: Path, base: str, tests: list[ChangedTest]) -> set[str]:
        """The ids of `tests` that pass with every file the layer changed put back to `base`, but its test
        modules and the files beside them in a tests directory (helpers, conftest.py)."""
        others = [p for p in changed_files(worktree, base) if not _test_side(p)]
        ids = sorted({t.id for t in tests})
        ctx = ToolContext(worktree, self.timeout_s, _OUTPUT_CHARS, **self.confinement)
        try:
            put_back(worktree, base, others)
            result = await run_cmd(
                f"{self.runner} -rA -p no:cacheprovider {shlex.join(ids)}", ctx, shell=True
            )
        finally:
            reset_to_head(worktree)
        return _passed(result.output, ids)


def _test_side(path: str) -> bool:
    """A file the red check leaves as the layer has it: a test module, or a file under a tests directory."""
    parts = PurePosixPath(path).parts
    return (
        is_test_module(path) or any(p in ("tests", "test") for p in parts[:-1]) or parts[-1] == "conftest.py"
    )


def checks_for(
    commands: Sequence[str], *, timeout_s: float, confinement: dict[str, Any] | None = None
) -> PytestChecks | None:
    """The checks for a gate of `commands`: pytest's when one of them runs pytest, else None (none run)."""
    runner = pytest_runner(commands)
    return PytestChecks(runner, timeout_s=timeout_s, confinement=confinement) if runner else None


LOOSE_MATCH = (
    "  its `match=` pattern is probably satisfied by an error that exists without your change (an unknown "
    "name is refused with a message that lists every valid name). Match a phrase only your new message "
    "contains, copied from the message your code raises."
)
"""Said under a vacuous test that matches an error's text: the usual cause of passing on the base."""


def feedback(findings: Sequence[Finding]) -> str:
    """What the coder is told on its retry: each test and why, and what to do."""
    listed: list[str] = []
    for f in findings:
        listed.append(f"- {f.test}: {f.why}")
        if f.kind == "vacuous_test" and "match=" in f.source:
            listed.append(LOOSE_MATCH)
    lines = [
        "airlie checked the tests you added or changed, and these need fixing:",
        *listed,
        "Fix each one: assert the behaviour this layer adds, so the test fails without your change and "
        "passes with it. A test that only checks that nothing happens (no event, no message, no change) "
        "cannot fail on the base, where nothing happens either: put that check in the same test that "
        "triggers the behaviour, before the trigger, or drop it. Keep the tests for the acceptance "
        "criteria. Run run_tests until the whole suite passes, then stop.",
    ]
    return "\n".join(lines)
