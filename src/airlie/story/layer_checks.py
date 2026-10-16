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
from typing import Literal

from airlie.teams.worktree import changed_files, find_file, put_back, read_blob, reset_to_head
from airlie.tools.protocol import ToolContext
from airlie.tools.shell import run_cmd

Kind = Literal["vacuous_test", "assertion_free_test"]
VACUOUS = (
    "it passes on the layer's base with the layer's other changes undone, so it does not test this change"
)
ASSERTION_FREE = "it has no assert, pytest.raises, pytest.warns or other check, so only a crash can fail it"
CHECK_CALLS = frozenset({"raises", "warns", "deprecated_call", "fail", "expect"})
"""Calls that are a check: pytest's, and expect(...) (playwright's). Any call whose name starts with "assert"
is one too: unittest's self.assertEqual, a mock's assert_called_once_with, a helper's assert_valid."""
_OPERATORS = frozenset({"&&", "||", ";", "|", "&"})
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


def is_test_module(path: str) -> bool:
    """pytest's default test module: test_*.py or *_test.py."""
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


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
    except (SyntaxError, ValueError):  # a test file that does not parse: the gate says so, not this check
        return None


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
    for path in changed_files(worktree, base):
        file = worktree / path
        if not is_test_module(path) or not file.is_file():
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
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


def pytest_runner(commands: Sequence[str]) -> str | None:
    """How the gate runs pytest: the first command that does, up to and including `pytest` (or `-m pytest`),
    from after the last shell operator before it. None when no command runs pytest."""
    for command in commands:
        try:
            words = shlex.split(command)
        except ValueError:
            continue
        for i, word in enumerate(words):
            end = i + 1 if PurePosixPath(word).name == "pytest" else None
            if word == "-m" and words[i + 1 : i + 2] == ["pytest"]:
                end = i + 2
            if end is not None:
                cut = max((j + 1 for j, w in enumerate(words[:i]) if w in _OPERATORS), default=0)
                return shlex.join(words[cut:end])
    return None


class PytestChecks:
    """Both checks for a Python repository whose gate runs pytest with `runner` (`pytest_runner`)."""

    def __init__(self, runner: str, *, timeout_s: float) -> None:
        self.runner = runner
        self.timeout_s = timeout_s

    async def run(self, worktree: Path, base: str) -> list[Finding]:
        """The findings on the tests the worktree's HEAD added or changed since `base`. The worktree must hold
        no uncommitted change: the red check puts files back, and leaves it as its HEAD holds it."""
        tests = changed_tests(worktree, base)
        if not tests:
            return []
        green = await self._pass_on_base(worktree, base, tests)
        findings = []
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
        ctx = ToolContext(worktree, self.timeout_s, _OUTPUT_CHARS)
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


def checks_for(commands: Sequence[str], *, timeout_s: float) -> PytestChecks | None:
    """The checks for a gate of `commands`: pytest's when one of them runs pytest, else None (none run)."""
    runner = pytest_runner(commands)
    return PytestChecks(runner, timeout_s=timeout_s) if runner else None


def feedback(findings: Sequence[Finding]) -> str:
    """What the coder is told on its retry: each test and why, and what to do."""
    lines = [
        "airlie checked the tests you added or changed, and these do not test your change:",
        *(f"- {f.test}: {f.why}" for f in findings),
        "Fix each one: assert the behaviour this layer adds, so the test fails without your change and "
        "passes with it. Keep the tests for the acceptance criteria. Run run_tests until the whole suite "
        "passes, then stop.",
    ]
    return "\n".join(lines)
