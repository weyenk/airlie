"""The red check on a layer's tests, run by the pipeline with no model, after the architect's package has been
placed in a scratch worktree and before the coder builds anything. The whole suite runs on that tree: each new
test must fail there, since the code it tests is not written yet (`passes_before_code`), and every test the
layer did not touch must still pass (`existing_test_fails`). A test with nothing in it that can fail it but a
crash is refused without running (`assertion_free_test`), as is a test module that does not parse. Python and
pytest only, behind `checks_for`: any other gate gets none, and the pipeline logs that they did not run.

The test files are model output, read as data: parsed with `ast`, never imported here. The tests themselves
run as the gate runs them (tools.shell.run_cmd: the agent environment, killed on a timeout).

What the tests' author is told when its package comes back is steering in the prompt hub (`checks-found`,
`passes-before-code-choice`, `loose-match`, then `package-fix`); `listed_findings` renders the first three
with their ids and `package_checks.package_feedback_said` adds the last, and the pipeline, which sends it,
emits a `steer` event per id. The labels of the findings (PASSES_BEFORE_CODE and the others) stay here,
beside the checks that find them."""

from __future__ import annotations

import ast
import re
import shlex
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from airlie.prompts import text as hub_text
from airlie.teams.worktree import add_worktree, changed_files, find_file, read_blob, remove_worktree
from airlie.tools.protocol import ToolContext
from airlie.tools.scope import is_test_module, pytest_runner
from airlie.tools.shell import run_cmd

Kind = Literal[
    "passes_before_code",
    "existing_test_changed",
    "existing_test_fails",
    "assertion_free_test",
    "unparseable_test",
    "test_type_error",
    "no_new_test",
    "suite_did_not_run",
    "lint_fails",
    "file_exists",
    "file_missing",
    "covers_missing",
    "import_unresolved",
    "split_invalid",
    "over_size",
    "criterion_unproven",
    "layer_unproven",
    "proof_layer_mismatch",
    "stale_reference",
    "verdict_missing",
    "part_invalid",
    "proof_mismatch",
    "proof_sampled",
]
"""What a finding is about."""
PASSES_BEFORE_CODE = "it passes before the layer's code is written, so it does not test what the layer builds"
EXISTING_CHANGED = (
    "it was already there on the layer's base and still passes, so your edit changed it without testing "
    "anything new. An edit anchored inside a test splits it: the rest of its body ends up under your new "
    "test. Put it back as it was on the base (below), unless the brief asks you to change it, and add new "
    "tests after it ends"
)
"""Why an existing test the layer changed and that still passes comes back: it is no new red, and the usual
cause is a split (AIR-9 layer 05, 2026-10-22: reported as passing before the code, it read as a new test to
make red, and neither the tests' author nor the supervisor saw that it had been cut in two)."""
EXISTING_FAILS = (
    "it passes on the layer's base and fails with the new tests in place: a fixture, conftest.py or shared "
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
"""Before the step: the run writes no __pycache__ into the worktree, where it would be the package's work
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
    its source, whether it holds a check (`asserts`), and its source on the base ("" when it is new there)."""

    path: str
    name: str
    source: str
    asserts: bool
    before: str = ""

    @property
    def id(self) -> str:
        """Its pytest node id, from the repository's root."""
        return f"{self.path}::{self.name}"


@dataclass(frozen=True)
class Finding:
    """What the check found: its kind, the test (or step), why, the test's source, and its source on the base
    when the layer changed a test that was there ("" otherwise)."""

    kind: Kind
    test: str
    why: str
    source: str
    before: str = ""


def _is_test_case(node: ast.ClassDef) -> bool:
    """Whether the class derives from unittest's TestCase (or IsolatedAsyncioTestCase), by the name it is
    written under."""
    for base in node.bases:
        name = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else ""
        if name.endswith("TestCase"):
            return True
    return False


def test_functions(tree: ast.Module) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """What pytest collects by default: test* functions, and test* methods of Test* classes and of
    unittest.TestCase subclasses of any name (which unittest collects too)."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test"):
            yield node.name, node
        elif isinstance(node, ast.ClassDef) and (node.name.startswith("Test") or _is_test_case(node)):
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
        base_text = _base_text(worktree, base, path)
        before = _parse(base_text)
        old = {name: fn for name, fn in test_functions(before)} if before else {}
        helpers = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
        for name, fn in test_functions(tree):
            was = old.get(name)
            if was is not None and ast.dump(was) == ast.dump(fn):
                continue
            source = (ast.get_source_segment(text, fn) or "")[:_SOURCE_CHARS]
            was_source = (ast.get_source_segment(base_text or "", was) or "")[:_SOURCE_CHARS] if was else ""
            found.append(ChangedTest(path, name, source, _checks(fn, helpers, {fn.name}), was_source))
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
        self.failing_at_base: list[str] = []
        """The existing tests the last `run` found failing that fail on the base as well, so are not a
        finding: the layer's tests did not break them (AIR-9 layer 02, 2026-10-21)."""

    async def run(self, worktree: Path, base: str) -> list[Finding]:
        """The findings on the tests the worktree's HEAD added or changed since `base`, with the suite run on
        that HEAD. The worktree must hold no uncommitted change (the package check commits what it placed)."""
        findings = unparseable_tests(worktree, base)
        tests = changed_tests(worktree, base)
        if not tests:
            return findings or [Finding("no_new_test", "(none)", NO_NEW_TEST, "")]
        findings += [
            Finding("assertion_free_test", t.id, ASSERTION_FREE, t.source, t.before)
            for t in tests
            if not t.asserts
        ]
        outcomes, output = await self._suite(worktree)
        if outcomes is None:
            why = f"the suite did not run its tests: {output[-_TAIL_CHARS:]}"
            return [*findings, Finding("suite_did_not_run", self.step, why, "")]
        modules = {t.path for t in tests} | {f.test for f in findings if f.kind == "unparseable_test"}
        for t in tests:
            if any(o == "PASSED" and _same(t.id, k) for k, o in outcomes.items()):
                if t.before:  # it was there: a test the edit changed, not a new one that is not red
                    findings.append(
                        Finding("existing_test_changed", t.id, EXISTING_CHANGED, t.source, t.before)
                    )
                else:
                    findings.append(Finding("passes_before_code", t.id, PASSES_BEFORE_CODE, t.source))
        existing = [
            k
            for k, o in outcomes.items()
            if o != "PASSED" and not any(_same(m, k.split("::")[0]) for m in modules)
        ]
        self.failing_at_base = await self._failing_at(worktree, base, existing) if existing else []
        findings += [
            Finding("existing_test_fails", k, EXISTING_FAILS, "")
            for k in existing
            if k not in self.failing_at_base
        ]
        return findings

    async def failing(self, worktree: Path) -> list[str] | None:
        """The ids of the tests that do not pass on the worktree as it is (a module that cannot be collected
        is its path), with the whole outcome read from -rA, not from a capped gate output; None when the
        suite did not run its tests."""
        outcomes, _ = await self._suite(worktree)
        return None if outcomes is None else [k for k, o in outcomes.items() if o != "PASSED"]

    async def run_one(self, worktree: Path, test: str) -> str:
        """The gate's pytest step on the node id `test` alone, confined as the suite is, for a reader (Muse)
        that wants to see one test fail: the end of the output, where pytest says why."""
        _, output = await self._suite(worktree, [test])
        return output[-_TAIL_CHARS:]

    async def _failing_at(self, worktree: Path, base: str, tests: list[str]) -> list[str]:
        """Which of `tests` (ids from the worktree's suite) fail on `base` too: run alone, confined as the
        suite is, in a detached checkout of `base` beside the worktree that is removed after. A run that does
        not run them (a module the base lacks) counts none as failing there, so they stay findings."""
        tree = worktree.parent / f"{worktree.name}.base"
        remove_worktree(worktree, tree)  # one a killed run left behind
        add_worktree(worktree, tree, branch=None, start_point=base)
        try:
            outcomes, _ = await self._suite(tree, tests)
        finally:
            remove_worktree(worktree, tree)
        if outcomes is None:
            return []
        return [t for t in tests if any(o != "PASSED" and _same(t, k) for k, o in outcomes.items())]

    async def _suite(self, worktree: Path, tests: Sequence[str] = ()) -> tuple[dict[str, str] | None, str]:
        """The gate's pytest step run on the worktree with PYTEST_FLAGS (on `tests` alone when given): each
        test's outcome (None when the suite did not run its tests), and the output."""
        ctx = ToolContext(worktree, self.timeout_s, _OUTPUT_CHARS, **self.confinement)
        only = f" {shlex.join(tests)}" if tests else ""
        result = await run_cmd(f"{NO_BYTECODE}{self.step.rstrip()} {PYTEST_FLAGS}{only}", ctx, shell=True)
        code = _EXIT.search(result.output)
        if not result.ok and (code is None or int(code.group(1)) not in _RAN):
            return None, result.output
        return _outcomes(result.output), result.output


def checks_for(
    commands: Sequence[str], *, timeout_s: float, confinement: dict[str, Any] | None = None
) -> PytestChecks | None:
    """The check for a gate of `commands`: pytest's, on the first command that runs pytest, else None.
    `confinement` is the ToolContext's sandbox and writable, for the suite the check runs."""
    step = next((c for c in commands if pytest_runner([c]) is not None), None)
    return PytestChecks(step, timeout_s=timeout_s, confinement=confinement) if step else None


class Said:
    """Hub steering texts rendered for one message, with their ids recorded in the order they are rendered,
    so the ids are exactly the texts the message holds."""

    def __init__(self) -> None:
        self.ids: list[str] = []

    def __call__(self, id: str, /) -> str:
        self.ids.append(id)
        return hub_text(id)


def listed_findings(findings: Sequence[Finding], said: Said) -> list[str]:
    """The lines that list `findings` for a role that must fix them: `checks-found`, then each finding and
    why, a `passes-before-code-choice` line under a test that passes before the code, a `loose-match` hint
    under such a test that has a `match=` pattern, and an existing test the layer changed as it was on the
    base. The hub ids are recorded in `said`, in the order they are rendered."""
    listed: list[str] = [said("checks-found")]
    for f in findings:
        listed.append(f"- {f.test}: {f.why}")
        if f.kind == "passes_before_code":
            listed.append("  " + said("passes-before-code-choice"))
            if "match=" in f.source:
                listed.append("  " + said("loose-match"))
        if f.kind == "existing_test_changed":
            listed.append(f"  as it was on the base:\n```python\n{f.before}\n```")
    return listed


_EVIDENCE_SOURCE_CHARS = 1_500
"""How much of a finding's test source the supervisor is shown."""


def findings_text(found: Sequence[Finding]) -> str:
    """The findings as the supervisor reads them: one block per finding, with its kind, its test, why, and
    the start of the test's source (what the role is told lists the test and why, not the source)."""
    blocks = []
    for f in found:
        block = f"kind: {f.kind}\ntest: {f.test}\nwhy: {f.why}"
        if f.before:  # the test was there: both whole, so a test cut in two shows where it used to end
            block += f"\non the base:\n{f.before[:_EVIDENCE_SOURCE_CHARS]}"
            block += f"\nnow:\n{f.source[:_EVIDENCE_SOURCE_CHARS]}"
        elif f.source:
            block += f"\nsource:\n{f.source[:_EVIDENCE_SOURCE_CHARS]}"
        blocks.append(block)
    return "\n\n".join(blocks)
