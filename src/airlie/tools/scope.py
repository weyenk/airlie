"""Scoped test runs for a story layer: its own tests, not the whole suite.

A story layer's tester and coder call run_tests after every change, and the repository's gate runs the whole
suite each time (minutes). Scoped, run_tests runs the gate with the layer's own tests limited to the files it
added or changed (seconds), and that is the subtask's measure of done. The whole gate is the studio's, once
the subtasks are done and reviewed; it remembers the tree that last passed it (`GateMemo`), so a gate asked
again of the same files does not run again.

A layer's tests may be pytest modules or JS/TS test files (the web-tests design, 2026-10-27): a pytest step
runs only the Python modules, and a JS/TS test file runs under vitest in its own package.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from airlie.subproc import NO_HOOKS_ARGS, agent_env

_OPERATORS = frozenset({"&&", "||", ";", "|", "&"})


@dataclass
class GateMemo:
    """The fingerprint of the last worktree tree that passed the whole gate (None: none has)."""

    passed: str | None = None


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


def is_test_module(path: str) -> bool:
    """pytest's default test module: test_*.py or *_test.py."""
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


_JS_TEST = re.compile(r"\.(test|spec)\.(ts|tsx|mts|js|jsx|mjs)$")


def is_js_test(path: str) -> bool:
    """A Vitest (or Jest) test file: *.test.ts, *.spec.ts and their .tsx/.mts/.js/.jsx/.mjs kin, never one
    under node_modules (the web-tests design, 2026-10-27)."""
    return bool(_JS_TEST.search(path)) and "node_modules" not in PurePosixPath(path).parts


def is_test_file(path: str) -> bool:
    """A test file of either kind: a pytest module or a JS/TS test file."""
    return is_test_module(path) or is_js_test(path)


def _git(worktree: Path, *args: str, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        ["git", *NO_HOOKS_ARGS, *args],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
        env={**agent_env(), **(env or {})},
    )
    return done.stdout if done.returncode == 0 else ""


def changed_test_files(worktree: Path, base: str) -> list[str]:
    """The test files the worktree has added or changed since `base`, committed or not, that still exist:
    pytest modules and JS/TS test files alike (the web-tests design, 2026-10-27)."""
    names = set(_git(worktree, "diff", "--name-only", "--diff-filter=d", base).splitlines())
    names |= set(_git(worktree, "ls-files", "--others", "--exclude-standard").splitlines())
    return sorted(n for n in names if is_test_file(n) and (worktree / n).is_file())


def tree_fingerprint(worktree: Path) -> str | None:
    """A hash of every file of the worktree as it is now (tracked or not, the ignored left out), made with
    a throwaway index: it changes with any edit and is the same for the same files. None when git cannot
    say."""
    with tempfile.TemporaryDirectory(prefix="airlie-fp-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(worktree, "read-tree", "HEAD", env=env)
        _git(worktree, "add", "-A", env=env)
        out = _git(worktree, "write-tree", env=env).strip()
    return out or None


def _js_package_dir(worktree: Path, path: str) -> str | None:
    """The nearest directory of `path` that holds a package.json, walking up to the worktree root. None when
    there is none: vitest has no package to run in."""
    current = worktree / path
    for directory in [current.parent, *current.parents]:
        if (directory / "package.json").is_file():
            return str(directory.relative_to(worktree)) or "."
    return None


def scope_step(step: str, files: Sequence[str], worktree: Path | None = None) -> str | None:
    """`step` limited to the layer's own tests in `files`; the step unchanged when it does not run pytest.

    A pytest step runs everything in `files` that is not a JS/TS test file — a JS/TS test file is not a
    pytest module and would make pytest collect nothing (exit 5, "no tests ran") — and when there is no
    module to run the step becomes a vitest run of the JS/TS tests in their package, so a layer whose only
    tests are JS/TS still runs them (the web-tests design, 2026-10-27)."""
    if pytest_runner([step]) is None:
        return step
    py_files = [f for f in files if not is_js_test(f)]
    js_files = [f for f in files if is_js_test(f)]
    if py_files:
        return f"{step.rstrip()} {shlex.join(py_files)}"
    if not js_files:
        return None
    # No pytest module to run, but the layer's tests are JS/TS: run them under vitest in their package.
    by_dir: dict[str, list[str]] = {}
    for f in js_files:
        d = _js_package_dir(worktree, f) if worktree is not None else None
        if d is None:
            continue  # no package.json up the tree: vitest has nowhere to run
        rel = f[len(d) + 1 :] if d != "." else f  # the file, relative to its package
        by_dir.setdefault(d, []).append(rel)
    if not by_dir:
        return None
    return " && ".join(
        f"cd {d} && npx vitest run --reporter=basic {shlex.join(rels)}" for d, rels in by_dir.items()
    )


def scoped_steps(steps: Sequence[str], files: Sequence[str], worktree: Path | None = None) -> list[str]:
    """`steps` with the layer's own tests in `files`; a pytest step with no test file to run is left out."""
    return [scoped for step in steps if (scoped := scope_step(step, files, worktree)) is not None]


def autofix_steps(steps: Sequence[str]) -> list[str]:
    """The commands that mend what the gate's ruff steps would only report: `ruff format` for a
    `ruff format --check` step and `ruff check --fix --unsafe-fixes` for a `ruff check` step. Style is
    mechanical: a coder told a line is too long, a file unformatted or an `if` that wants a ternary (SIM108,
    which ruff only fixes as "unsafe") spends steps on it that the layer's own change needs. An unsafe fix may
    change what code does: the layer's tests and the whole gate still run after it."""
    out: list[str] = []
    for step in steps:
        words = step.split()
        if "ruff" not in " ".join(words):
            continue
        if "format" in words and "--check" in words:
            out.append(" ".join(w for w in words if w != "--check"))
        elif "check" in words and "--fix" not in words:
            out.append(f"{step.rstrip()} --fix --unsafe-fixes")
    return out
