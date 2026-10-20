"""The pre-code check on a layer's tests, run by the pipeline with no model, after the tester has written
them and before the coder builds anything. The whole suite runs on the tester's tree: each new test must fail
there, since the code it tests is not written yet (`passes_before_code`), and every test the layer did not
touch must still pass (`existing_test_fails`). A test with nothing in it that can fail it but a crash is
refused without running (`assertion_free_test`), as is a test module that does not parse. Python and pytest
only, behind `checks_for`: any other gate gets none, and the pipeline logs that they did not run.

The test files are model output, read as data: parsed with `ast`, never imported here. The tests themselves
run as the gate runs them (tools.shell.run_cmd: the agent environment, killed on a timeout)."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from airlie.teams.worktree import changed_files, find_file, read_blob
from airlie.tools.protocol import ToolContext
from airlie.tools.scope import is_test_module, pytest_runner
from airlie.tools.shell import run_cmd

Kind = Literal[
    "passes_before_code",
    "existing_test_fails",
    "assertion_free_test",
    "unparseable_test",
    "no_new_test",
    "suite_did_not_run",
    "lint_fails",
]
PASSES_BEFORE_CODE = "it passes before the layer's code is written, so it does not test what the layer builds"
EXISTING_FAILS = (
    "it passed before this layer and fails with the new tests in place: a fixture, conftest.py or shared "
    "helper the tests changed broke it"
)
ASSERTION_FREE = "it has no assert, pytest.raises, pytest.warns or other check, so only a crash can fail it"
NO_NEW_TEST = "the layer has no new or changed test to build against"
CHECK_CALLS = frozenset({"raises", "warns", "deprecated_call", "fail", "expect"})
"""Calls that are a check: pytest's, and expect(...) (playwright's). Any call whose name starts with "assert"
is one too: unittest's self.assertEqual, a mock's assert_called_once_with, a helper's assert_valid."""
PYTEST_FLAGS = "-rA -p no:cacheprovider --continue-on-collection-errors"
"""What the check adds to the gate's pytest step: a line per test with its outcome, no cache written into the
worktree, and every module run when a new one cannot be imported yet (the name it tests is not there)."""
NO_BYTECODE = "export PYTHONDONTWRITEBYTECODE=1; "
"""Before the step: the run writes no __pycache__ into the worktree, where it would be the tester's work
(committed, and locked to the coder) in a repository that does not ignore it."""
_OUTCOME = re.compile(r"^(PASSED|FAILED|ERROR) (\S+)", re.MULTILINE)
_SUMMARY = re.compile(r"^=+ short test summary info =+$", re.MULTILINE)
"""The header of pytest's -rA summary. The suite's is the last one: a test that runs pytest on a fixture
repository prints that run's own, which -rA shows above it in the test's captured output."""
_EXIT = re.compile(r"\[exit (\d+)\]\s*$")
_PARAMS = re.compile(r"\[.*\]$")
_RAN = frozenset({0, 1})
"""pytest's exit codes for a run that ran the tests: all passed, or some failed."""
_SOURCE_CHARS = 4_000
"""How much of a test's source a finding keeps (for the events, as a negative example)."""
_OUTPUT_CHARS = 2_000_000
"""pytest's output is read for its outcome lines, not shown: it is kept whole up to this."""
_TAIL_CHARS = 2_000
"""How much of the end of a run that did not run the tests a finding keeps."""


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


def _outcomes(output: str) -> dict[str, str]:
    """Each test id pytest's -rA summary names, with PASSED, FAILED or ERROR (a parametrized test's cases are
    one id: any case that is not PASSED makes it so). A module that cannot be collected is an ERROR on the
    module's path. Only the suite's summary is read, never a test's captured output (_SUMMARY)."""
    out: dict[str, str] = {}
    for outcome, node in _OUTCOME.findall(_SUMMARY.split(output)[-1]):
        key = _PARAMS.sub("", node)
        if out.get(key) in (None, "PASSED"):
            out[key] = outcome
    return out


def _same(test_id: str, reported: str) -> bool:
    """`reported` (from pytest's rootdir, which can be below the repository's root) names `test_id`."""
    return test_id == reported or test_id.endswith("/" + reported)


class PytestChecks:
    """The pre-code check for a Python repository whose gate runs pytest in `step`."""

    def __init__(self, step: str, *, timeout_s: float, confinement: dict[str, Any] | None = None) -> None:
        self.step = step
        self.timeout_s = timeout_s
        self.confinement = confinement or {}
        """ToolContext's sandbox and writable for the test runs (tools.confinement); default: confined."""

    async def run(self, worktree: Path, base: str) -> list[Finding]:
        """The findings on the tests the worktree's HEAD added or changed since `base`, with the suite run on
        that HEAD. The worktree must hold no uncommitted change (the pipeline commits the tester's work)."""
        findings = unparseable_tests(worktree, base)
        tests = changed_tests(worktree, base)
        if not tests:
            return findings or [Finding("no_new_test", "(none)", NO_NEW_TEST, "")]
        findings += [
            Finding("assertion_free_test", t.id, ASSERTION_FREE, t.source) for t in tests if not t.asserts
        ]
        ctx = ToolContext(worktree, self.timeout_s, _OUTPUT_CHARS, **self.confinement)
        result = await run_cmd(f"{NO_BYTECODE}{self.step.rstrip()} {PYTEST_FLAGS}", ctx, shell=True)
        code = _EXIT.search(result.output)
        if not result.ok and (code is None or int(code.group(1)) not in _RAN):
            why = f"the suite did not run its tests: {result.output[-_TAIL_CHARS:]}"
            return [*findings, Finding("suite_did_not_run", self.step, why, "")]
        outcomes = _outcomes(result.output)
        modules = {t.path for t in tests} | {f.test for f in findings if f.kind == "unparseable_test"}
        for t in tests:
            if any(o == "PASSED" and _same(t.id, k) for k, o in outcomes.items()):
                findings.append(Finding("passes_before_code", t.id, PASSES_BEFORE_CODE, t.source))
        for k, o in outcomes.items():
            if o != "PASSED" and not any(_same(m, k.split("::")[0]) for m in modules):
                findings.append(Finding("existing_test_fails", k, EXISTING_FAILS, ""))
        return findings


def static_findings(worktree: Path, base: str) -> list[Finding]:
    """The checks that read the tests and run none: a module that does not parse, a test with no check in
    it. For a layer with no tester, whose tests and code come in one subtask and cannot be run before it."""
    found = unparseable_tests(worktree, base)
    found += [
        Finding("assertion_free_test", t.id, ASSERTION_FREE, t.source)
        for t in changed_tests(worktree, base)
        if not t.asserts
    ]
    return found


def checks_for(
    commands: Sequence[str], *, timeout_s: float, confinement: dict[str, Any] | None = None
) -> PytestChecks | None:
    """The check for a gate of `commands`: pytest's, on the first command that runs pytest, else None.
    `confinement` is the ToolContext's sandbox and writable, for the suite the check runs."""
    step = next((c for c in commands if pytest_runner([c]) is not None), None)
    return PytestChecks(step, timeout_s=timeout_s, confinement=confinement) if step else None


LOOSE_MATCH = (
    "  its `match=` pattern is probably satisfied by an error that exists without the layer's code (an "
    "unknown name is refused with a message that lists every valid name). Match a phrase only the new "
    "message contains, as the brief gives it."
)
"""Said under a test that passes before the code and matches an error's text: the usual cause."""


def feedback(findings: Sequence[Finding], *, before_code: bool = True) -> str:
    """What the tests' author is told on its retry: each finding and why, and what to do. `before_code` is
    the tester's (the code is not written yet); else the coder's, which wrote tests and code together."""
    listed: list[str] = []
    for f in findings:
        listed.append(f"- {f.test}: {f.why}")
        if f.kind == "passes_before_code" and "match=" in f.source:
            listed.append(LOOSE_MATCH)
    if before_code:
        what = (
            "Fix each one. Every test you wrote must fail now, because the code it tests is not written yet, "
            "and pass once the brief is built. A test that only checks that nothing happens (no event, no "
            "message, no change) passes now, where nothing happens either: put that check in the same test "
            "that triggers the behaviour, before the trigger, or drop it. Every test that was there before "
            "must still pass. Keep a test for each acceptance criterion. Do not write the feature. Run "
            "run_tests and check that your new tests are the only red, then stop."
        )
    else:
        what = (
            "Fix each one: assert the behaviour this layer adds. Keep the tests for the acceptance criteria. "
            "Run run_tests until your tests pass, then stop."
        )
    return "\n".join(["airlie checked the tests, and these need fixing:", *listed, what])
