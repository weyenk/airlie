"""Placing a layer's package in a worktree and the mechanical readiness checks on it (P1 of the planning
line, 2026-10-24). The architect writes the layer's tests as code; before anyone codes against them the
pipeline checks them with no model, in a scratch worktree detached at the layer's base, so a rejected package
costs no branch: the files the package says it creates or modifies, the gate's ruff steps (fixer first, and
what it fixes is carried into the package, so the lock commit holds the text that was checked), that the test
modules parse, that each test a criterion is covered by is a new or changed test, that every import of this
repository's modules in the tests resolves, and that every new test is red on the base with every old test
still green (`layer_checks.PytestChecks`). Python and pytest only, as the pre-code check was: a gate without
pytest gets no red check and the caller logs that, as before.

The test text is model output: written only inside the worktree (a path that leaves it is refused), parsed
with `ast` and never imported here; the tests run as the gate runs them, confined. Findings go back to the
architect with `package_feedback_said`, whose last text is the hub's `package-fix`."""

from __future__ import annotations

import ast
import re
import tempfile
import unittest
from collections.abc import Collection, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from airlie.story.js_tests import NO_EXPECT, changed_js_tests, unparseable_js_tests, vitest_red
from airlie.story.layer_checks import (
    ASSERTION_FREE,
    Finding,
    Said,
    changed_tests,
    checks_for,
    listed_findings,
    test_functions,
    unparseable_tests,
)
from airlie.story.package import Package, PackageFile
from airlie.story.tester_lint import lint_for
from airlie.teams.worktree import add_worktree, commit_wip, find_file, remove_worktree
from airlie.tools.scope import is_js_test

ROOTS = ("", "src/")
"""Where this repository's modules are imported from: the worktree's root and its src/ (the layouts the
repositories airlie builds in use)."""
_PARAMS = re.compile(r"\[.*\]$")
PROOF_MISMATCH = (
    "the agreed proof names it, but your package does not cover it: cover it, or, if the agreed test is "
    "wrong or cannot run (too large a sweep, a false bound), reply with a proof_change naming it instead"
)
NOT_NEW = "it is not a new or changed test of that file, so it was there before this layer"
NO_MODULE = "the module is not in the worktree and the package creates no such file"
NO_NAME = "the name is defined neither in the file nor in the package's interfaces for it"
SWEEP_LIMIT = 10_000_000
"""The most cases one itertools.product sweep in a package test may go over: 7**8 is about 5.8 million."""
TOO_LARGE = (
    "it sweeps itertools.product over {size:,} cases ({how}), which cannot finish in the gate's time: bound "
    "the sweep (the rules' exhaustive check stops at four characters) and prove the rest another way"
)
NO_METHOD = (
    "it calls self.{name}(), which neither its class nor unittest.TestCase defines (a helper written at the "
    "module's top level is not a method)"
)
FILE_EXISTS = "the package says to create it, but it already exists on the layer's base"
FILE_MISSING = "the package says to modify it, but it does not exist on the layer's base"


def place_package(worktree: Path, package: Package) -> list[str]:
    """Write every test file of `package` into `worktree` (parents made) and return their paths, sorted.
    Nothing else is written. A path that is absolute or leaves the worktree is a ValueError: the text
    reaches the disk only inside it."""
    root = worktree.resolve()
    targets: dict[str, Path] = {}
    for path in package.tests:
        target = (root / path).resolve()
        if Path(path).is_absolute() or not target.is_relative_to(root):
            raise ValueError(f"test path {path!r} must be inside the worktree")
        targets[path] = target
    for path in sorted(targets):
        targets[path].parent.mkdir(parents=True, exist_ok=True)
        targets[path].write_text(package.tests[path], encoding="utf-8")
    return sorted(targets)


def _candidates(module: str) -> list[str]:
    stem = module.replace(".", "/")
    return [f"{stem}.py", f"{stem}/__init__.py"]


def _bound_names(body: Sequence[ast.stmt]) -> set[str]:
    """What a module's top level defines: functions, classes, assignment targets, imports; through `if`,
    `try` and `with` blocks, never into a definition."""
    names: set[str] = set()
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                names |= {n.id for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.AnnAssign | ast.AugAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            names |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names |= {a.asname or a.name for a in node.names}
        elif isinstance(node, ast.If | ast.With | ast.AsyncWith):
            names |= _bound_names(node.body) | (
                _bound_names(node.orelse) if isinstance(node, ast.If) else set()
            )
        elif isinstance(node, ast.Try):
            handlers = [s for h in node.handlers for s in h.body]
            names |= _bound_names([*node.body, *node.orelse, *node.finalbody, *handlers])
    return names


def _declares(signature: str, name: str) -> bool:
    n = re.escape(name)
    return bool(re.match(rf"\s*(?:(?:async\s+)?def\s+{n}\s*\(|class\s+{n}\b|{n}\s*[=:])", signature))


def _base_name(base: ast.expr) -> str:
    return base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else ""


def _class_names(
    cls: ast.ClassDef, classes: dict[str, ast.ClassDef], seen: frozenset[str] = frozenset()
) -> set[str] | None:
    """What `cls` has beyond unittest.TestCase: what its body defines or assigns (`self.x = ...` too) and
    what its bases in the same file have; None when a base comes from elsewhere (a mixin), unknown here."""
    names: set[str] = set()
    for base in cls.bases:
        name = _base_name(base)
        if name.endswith("TestCase"):
            continue
        local = classes.get(name)
        inherited = None if local is None or name in seen else _class_names(local, classes, seen | {cls.name})
        if inherited is None:
            return None
        names |= inherited
    names |= _bound_names(cls.body)
    for node in ast.walk(cls):
        for target in node.targets if isinstance(node, ast.Assign) else ():
            if isinstance(target, ast.Attribute) and getattr(target.value, "id", "") == "self":
                names.add(target.attr)
    return names


def _self_calls(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {
        call.func.attr
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and getattr(call.func.value, "id", "") == "self"
    }


def _sized(node: ast.expr, consts: dict[str, int], loops: dict[str, int]) -> int | None:
    """The length of a sweep's alphabet, or the value of its repeat count: a literal, a module constant, or
    (for a count) the largest value of an enclosing `for x in range(...)`; None when it cannot be read."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str | bytes | int):
        return node.value if isinstance(node.value, int) else len(node.value)
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return len(node.elts)
    if isinstance(node, ast.Name):
        return loops.get(node.id, consts.get(node.id))
    return None


def _sweep_size(call: ast.Call, consts: dict[str, int], loops: dict[str, int]) -> tuple[int, str] | None:
    """The number of cases an itertools.product call goes over, and how, when it can be read."""
    if not (isinstance(call.func, ast.Attribute | ast.Name) and _base_name(call.func) == "product"):
        return None
    if not call.args:
        return None
    sizes = [_sized(a, consts, {}) for a in call.args]
    repeat = next((_sized(k.value, consts, loops) for k in call.keywords if k.arg == "repeat"), 1)
    if None in sizes or repeat is None:
        return None
    total, how = 1, []
    for n in sizes:
        assert n is not None
        total *= n
        how.append(str(n))
    return total**repeat, "×".join(how) + (f" to the power {repeat}" if repeat != 1 else "")


def _range_top(node: ast.expr) -> int | None:
    if not (isinstance(node, ast.Call) and _base_name(node.func) == "range" and node.args):
        return None
    stop = node.args[1] if len(node.args) > 1 else node.args[0]
    return stop.value - 1 if isinstance(stop, ast.Constant) and isinstance(stop.value, int) else None


def sweeps_too_large(worktree: Path, package: Package) -> list[Finding]:
    """A finding for each test in the package's tests with an itertools.product sweep over more than
    SWEEP_LIMIT cases: `Finding("test_too_large", "<path>::[<Class>::]<test>", why, source)`. The alphabet and
    the repeat count are read from literals, module constants and enclosing `for x in range(...)` loops;
    a sweep whose size cannot be read is not one. 2026-10-29, bundle 3 (roman): AC5 named every string of
    sixteen or more characters, the architect swept itertools.product(letters, repeat=16), and the coder's
    test runs timed out at 600 s for an hour."""
    found: list[Finding] = []
    for path in sorted(p for p in package.tests if not is_js_test(p)):
        text = (worktree / path).read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):  # unparseable_tests reports it
            continue
        consts: dict[str, int] = {}
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ):
                size = _sized(node.value, {}, {})
                if size is not None:
                    consts[node.targets[0].id] = size
        for name, fn in test_functions(tree):
            loops = {
                n.target.id: top
                for n in ast.walk(fn)
                if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and (top := _range_top(n.iter))
            }
            # a sweep inside itertools.islice(..., stop) goes over at most `stop` cases (bundle 4 run 1: a
            # bounded islice(product(..., repeat=15), 1000) was sent back)
            sliced = {
                id(c.args[0])
                for c in ast.walk(fn)
                if isinstance(c, ast.Call) and _base_name(c.func) == "islice" and c.args
            }
            sizes = [
                got
                for c in ast.walk(fn)
                if isinstance(c, ast.Call) and id(c) not in sliced and (got := _sweep_size(c, consts, loops))
            ]
            big = [(n, how) for n, how in sizes if n > SWEEP_LIMIT]
            if big:
                size, how = max(big)
                source = (ast.get_source_segment(text, fn) or "")[:1500]
                found.append(
                    Finding("test_too_large", f"{path}::{name}", TOO_LARGE.format(size=size, how=how), source)
                )
    return found


def unknown_methods(worktree: Path, package: Package) -> list[Finding]:
    """A finding for each method of a unittest.TestCase class in the package's tests that calls a `self.`
    method its class does not have: `Finding("unknown_method", "<path>::<Class>::<method>", why, source)`.
    The class has what `_class_names` finds and what unittest.TestCase has; a class with a base from
    elsewhere is skipped. 2026-10-29, bundle 3 (roman): the architect wrote `_assert_malformed(s)` at the
    module's top level and the tests called `self._assert_malformed`; the coder, locked out of the test
    file, patched TestCase from the module under test."""
    found: list[Finding] = []
    for path in sorted(p for p in package.tests if not is_js_test(p)):
        text = (worktree / path).read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):  # unparseable_tests reports it
            continue
        classes = {c.name: c for c in tree.body if isinstance(c, ast.ClassDef)}
        for cls in classes.values():
            if not any(_base_name(b).endswith("TestCase") or _base_name(b) in classes for b in cls.bases):
                continue
            names = _class_names(cls, classes)
            if names is None:
                continue
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                source = ast.get_source_segment(text, fn) or ""
                found += [
                    Finding(
                        "unknown_method", f"{path}::{cls.name}::{fn.name}", NO_METHOD.format(name=m), source
                    )
                    for m in sorted(_self_calls(fn) - names)
                    if not hasattr(unittest.IsolatedAsyncioTestCase, m)
                ]
    return found


def imports_unresolved(worktree: Path, package: Package) -> list[Finding]:
    """A finding for each import in the package's tests of a module of this repository that is not there:
    `Finding("import_unresolved", "<test path>: <module>[.<name>]", why, "")`. Only modules whose root
    package lives in the worktree (under `ROOTS`) or is created by the package are resolved; pytest or httpx
    are not this repository's and are skipped. A name resolves when the module's file defines it at its top
    level, when it is a submodule, or when an interface of the package for that file declares it."""
    created = {f.path for f in package.files if f.action == "create"}

    def exists(rel: str) -> bool:
        return (worktree / rel).is_file() or rel in created

    def in_scope(top: str) -> bool:
        return any(
            (worktree / r / top).is_dir()
            or (worktree / f"{r}{top}.py").is_file()
            or any(c == f"{r}{top}.py" or c.startswith(f"{r}{top}/") for c in created)
            for r in ROOTS
        )

    def module_file(module: str) -> str | None:
        return next((f"{r}{c}" for r in ROOTS for c in _candidates(module) if exists(f"{r}{c}")), None)

    found: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def add(path: str, target: str, why: str) -> None:
        if (path, target) not in seen:
            seen.add((path, target))
            found.append(Finding("import_unresolved", f"{path}: {target}", why, ""))

    for path in sorted(package.tests):
        try:
            tree = ast.parse((worktree / path).read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):  # unparseable_tests reports it
            continue
        for node in ast.walk(tree):
            wanted: list[tuple[str, str | None]]
            if isinstance(node, ast.Import):
                wanted = [(a.name, None) for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                wanted = [(node.module, a.name) for a in node.names]
            else:
                continue
            for module, name in wanted:
                if not in_scope(module.split(".")[0]):
                    continue
                file = module_file(module)
                if file is None:
                    add(path, module, NO_MODULE)
                    continue
                if name is None or name == "*":
                    continue
                if module_file(f"{module}.{name}") is not None:
                    continue  # a submodule
                defined = False
                if (worktree / file).is_file():
                    try:
                        defined = name in _bound_names(
                            ast.parse((worktree / file).read_text("utf-8", "replace")).body
                        )
                    except (SyntaxError, ValueError):
                        defined = True  # a module that does not parse is its own finding elsewhere
                if not defined and not any(
                    i.file == file and _declares(i.signature, name) for i in package.interfaces
                ):
                    add(path, f"{module}.{name}", NO_NAME)
    return found


def _against_proof(package: Package, proof_ids: Collection[str]) -> tuple[set[str], set[str]]:
    """The package's covered test ids and the agreed ones as it names them. A parametrized test is its
    function's id; an agreed id without its class (as the meeting writes it) is the covered method it
    names, and one without its describe the covered Vitest test ("Wiki > lists topics")."""
    covered = {_PARAMS.sub("", t) for t in package.test_ids}

    def as_covered(agreed_id: str) -> str:
        where, _, name = agreed_id.partition("::")
        ends = (f"::{name}", f" > {name}")
        named = [t for t in covered if t.startswith(f"{where}::") and t.endswith(ends)]
        return named[0] if agreed_id not in covered and len(named) == 1 else agreed_id

    return covered, {as_covered(t) for t in proof_ids}


def proof_mismatches(package: Package, proof_ids: Collection[str]) -> list[Finding]:
    """A `proof_mismatch` for each agreed test the package does not cover (three-amigos D9, D12). A test the
    package adds beyond the proof is not one (`beyond_proof`): it is checked like any other, and sending a
    package back for it cost about 11 minutes a time without making the layer better (roman, 2026-10-27)."""
    covered, agreed = _against_proof(package, proof_ids)
    return [Finding("proof_mismatch", t, PROOF_MISMATCH, "") for t in sorted(agreed - covered)]


def beyond_proof(package: Package, proof_ids: Collection[str]) -> list[str]:
    """The tests the package covers that the agreed proof does not name: kept, and reported."""
    covered, agreed = _against_proof(package, proof_ids)
    return sorted(covered - agreed)


class PackageChecker:
    """The readiness check for a gate of `commands`: the gate's ruff steps (`lint`) and its pytest step
    (`checks`), each None when the gate has none."""

    def __init__(
        self,
        commands: Sequence[str],
        *,
        timeout_s: float,
        confinement: dict[str, Any] | None = None,
        node_cache: Path | None = None,
    ) -> None:
        self.lint = lint_for(commands, timeout_s=timeout_s, confinement=confinement)
        self.checks = checks_for(commands, timeout_s=timeout_s, confinement=confinement)
        self.timeout_s = timeout_s
        self.node_cache = node_cache or Path(tempfile.gettempdir()) / "airlie-node-modules"
        """Where a Vitest red check's node_modules are installed once per lock file (js_tests)."""

    @property
    def runs_red(self) -> bool:
        """Whether the gate runs pytest, so a package's tests are run on the base."""
        return self.checks is not None

    async def check(
        self,
        package: Package,
        repo: Path,
        base: str,
        scratch: Path,
        proof_ids: Collection[str] | None = None,
    ) -> tuple[Package, list[Finding]]:
        """The package with ruff's fixes applied to its test text, and the findings. The package is placed
        and committed in a detached worktree of `repo` at `base` under `scratch`, removed after.
        `proof_ids` are the tests the story meeting agreed for the layer (None: no agreed proof): the
        package must cover each of them (`proof_mismatch`), and may add more (`beyond_proof`). A file the
        package's interfaces name that its files leave out, and that is not on the base, is one it creates; so
        is a test file it writes in full and calls a change when the base lacks it."""
        # a test file the package writes in full and calls a change is a new one when its base lacks it
        # (2026-10-30, roman: a re-plan after a red build named the thrown-away build's test file "modify")
        mended = tuple(
            replace(f, action="create")
            if f.action == "modify"
            and f.path in package.tests
            and find_file(repo, base, f.path)[0] == "missing"
            else f
            for f in package.files
        )
        if mended != package.files:
            package = replace(package, files=mended)
        listed = {f.path for f in package.files}
        unlisted = sorted({i.file for i in package.interfaces} - listed)
        if made := [p for p in unlisted if find_file(repo, base, p)[0] == "missing"]:
            files = (
                *package.files,
                *(PackageFile(p, "create", "the package's interfaces are in it") for p in made),
            )
            package = replace(package, files=files)
        found = proof_mismatches(package, proof_ids) if proof_ids is not None else []
        found += [
            Finding("file_exists", f.path, FILE_EXISTS, "")
            for f in package.files
            if f.action == "create" and find_file(repo, base, f.path)[0] != "missing"
        ]
        found += [
            Finding("file_missing", f.path, FILE_MISSING, "")
            for f in package.files
            if f.action == "modify" and find_file(repo, base, f.path)[0] == "missing"
        ]
        remove_worktree(repo, scratch)  # one a killed run left behind
        add_worktree(repo, scratch, branch=None, start_point=base)
        try:
            place_package(scratch, package)
            commit_wip(scratch, "package check")
            if self.lint is not None:
                fixed = [p for p in await self.lint.fix(scratch, base) if p in package.tests]
                if fixed:
                    package = package.with_tests(
                        {**package.tests, **{p: (scratch / p).read_text(encoding="utf-8") for p in fixed}}
                    )
                    commit_wip(scratch, "package check: ruff's fixes")
                found += await self.lint.check(scratch, base)
            broken = unparseable_tests(scratch, base)
            if broken:
                return package, [*found, *broken]
            tests = changed_tests(scratch, base)
            found += [
                Finding("assertion_free_test", t.id, ASSERTION_FREE, t.source, t.before)
                for t in tests
                if not t.asserts
            ]
            # a Vue layer's Vitest tests (the web-tests design, 2026-10-27): read, checked, and run red below
            web = changed_js_tests(scratch, base)
            found += unparseable_js_tests(scratch, base)
            found += [
                Finding("assertion_free_test", t.id, NO_EXPECT, t.source, t.before)
                for t in web
                if not t.asserts
            ]
            changed = {t.id for t in tests} | {t.id for t in web}
            for tid in package.test_ids:
                path, _, name = tid.partition("::")
                key = tid if is_js_test(path) else f"{path}::{_PARAMS.sub('', name)}"
                if key not in changed:
                    found.append(Finding("covers_missing", tid, NOT_NEW, ""))
            found += imports_unresolved(scratch, package)
            found += unknown_methods(scratch, package)
            found += sweeps_too_large(scratch, package)
            if self.checks is not None:
                found += [
                    f
                    for f in await self.checks.run(scratch, base)
                    if f.kind not in ("assertion_free_test", "unparseable_test")
                    and not (f.kind == "no_new_test" and web)  # its new tests are Vitest's
                ]
            if web:
                found += await vitest_red(scratch, web, self.node_cache, timeout_s=self.timeout_s)
            return package, found
        finally:
            remove_worktree(repo, scratch)


def package_feedback_said(findings: Sequence[Finding]) -> tuple[str, tuple[str, ...]]:
    """What the architect is told when its package comes back, and the hub ids in it, in order: the caller
    that sends it emits a `steer` event for each. `checks-found`, a line per finding (with
    `passes-before-code-choice` and `loose-match` where they apply), then `package-fix`."""
    said = Said()
    lines = listed_findings(findings, said)
    return "\n".join([*lines, said("package-fix")]), tuple(said.ids)
