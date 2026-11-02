"""The gate's linters on the architect's tests, before they are placed for the coder: each ruff step of the
gate is run first as its fixer (`ruff check --fix`, `ruff format`) and then as itself, on the Python files the
layer changed and nothing else. What the fixers cannot fix is a `lint_fails` finding, which goes back to the
architect with the rest of the package check's: the coder cannot edit the locked tests, so a lint error left
in them would fail the layer's gate with no one able to fix it (AIR-16 layer 01 loosened pyproject.toml to get
past one; AIR-22 layer 03 lost 2.5 hours to E741). The module keeps its name from the tester seat it was
written for (retired by P1, 2026-10-24).

Only a gate step that is a plain ruff command is read (no ;, &&, | or substitution): its own spelling of ruff
(`uv run ruff`, a path) is kept, its own targets are not. Type checkers are not run here: before the code,
the tests' imports of the names the layer adds are meant to fail them."""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airlie.prompts import text as hub_text
from airlie.story.layer_checks import Finding
from airlie.teams.worktree import changed_files, differs_from, find_file, read_blob
from airlie.tools.protocol import ToolContext
from airlie.tools.shell import run_cmd

LINT_FAILS = "the gate's lint step fails on the package's tests after airlie ran its fixer: fix these by hand"
_RUFF = re.compile(r"^(?P<ruff>[^;&|<>`$()]*?\bruff)\s+(?P<sub>check|format)\b(?P<rest>[^;&|<>`$()]*)$")
_PYTHON = (".py", ".pyi")
_OUTPUT_CHARS = 8_000
"""How much of a lint step's output a finding keeps: ruff names each error with its line."""


@dataclass(frozen=True)
class RuffStep:
    """A gate step that runs ruff: the command as the gate spells it, and the ruff before its subcommand."""

    step: str
    ruff: str
    sub: str

    def fixer(self, files: str) -> str:
        fix = "check --fix" if self.sub == "check" else "format"
        return f"{self.ruff} {fix} --no-cache --force-exclude -- {files}"

    def check(self, files: str) -> str:
        sub = "check" if self.sub == "check" else "format --check"
        return f"{self.ruff} {sub} --no-cache --force-exclude -- {files}"


def ruff_steps(commands: Sequence[str]) -> tuple[RuffStep, ...]:
    """The gate's ruff steps: `ruff check` (not one that already fixes) and `ruff format --check`."""
    steps: list[RuffStep] = []
    for c in commands:
        m = _RUFF.match(c.strip())
        if m is None:
            continue
        rest = m["rest"].split()
        if (m["sub"] == "check" and "--fix" not in rest) or (m["sub"] == "format" and "--check" in rest):
            steps.append(RuffStep(c.strip(), m["ruff"].strip(), m["sub"]))
    return tuple(steps)


RUFF_LINE_LENGTH = 88
"""Ruff's line length when no configuration names one."""
_RUFF_CONFIGS = ((".ruff.toml", ()), ("ruff.toml", ()), ("pyproject.toml", ("tool", "ruff")))
"""Where ruff reads its configuration, in its own order: the first of these that configures ruff wins."""


def ruff_line_length(repo: Path, commit: str) -> int:
    """The line length ruff applies at `commit`, read from its configuration files at the repository's root
    as ruff reads them (RUFF_LINE_LENGTH when none sets one). A file that does not parse is passed over."""
    for name, table in _RUFF_CONFIGS:
        kind, sha = find_file(repo, commit, name)
        if kind != "blob":
            continue
        try:
            config: Any = tomllib.loads(read_blob(repo, sha))
        except tomllib.TOMLDecodeError:
            continue
        for key in table:
            config = config.get(key) if isinstance(config, dict) else None
        if isinstance(config, dict):
            length = config.get("line-length")
            return length if isinstance(length, int) else RUFF_LINE_LENGTH
    return RUFF_LINE_LENGTH


def lint_note(repo: Path, commit: str, commands: Sequence[str]) -> str:
    """What the architect is told about the gate's lint before it writes a package's tests: the line length
    and the rules ruff's fixer cannot mend (`brief-package-lint`); "" when the gate runs no ruff step."""
    if not ruff_steps(commands):
        return ""
    return hub_text("brief-package-lint", line_length=ruff_line_length(repo, commit))


class LayerLint:
    """The gate's ruff steps, run on the files a worktree's HEAD changed since `base`."""

    def __init__(
        self, steps: Sequence[RuffStep], *, timeout_s: float, confinement: dict[str, Any] | None = None
    ) -> None:
        self.steps = tuple(steps)
        self.timeout_s = timeout_s
        self.confinement = confinement or {}

    def _files(self, worktree: Path, base: str) -> list[str]:
        return [p for p in changed_files(worktree, base) if p.endswith(_PYTHON) and (worktree / p).is_file()]

    async def fix(self, worktree: Path, base: str) -> list[str]:
        """Run each step's fixer on the changed files; return the files it changed (left uncommitted)."""
        files = self._files(worktree, base)
        if not files:
            return []
        quoted = " ".join(shlex.quote(f) for f in files)
        ctx = ToolContext(worktree, self.timeout_s, _OUTPUT_CHARS, **self.confinement)
        for s in self.steps:
            await run_cmd(s.fixer(quoted), ctx, shell=True)  # what it could not fix, check() reports
        return differs_from(worktree, "HEAD", files)

    async def check(self, worktree: Path, base: str) -> list[Finding]:
        """A `lint_fails` finding for each step that still fails on the changed files, with its output."""
        files = self._files(worktree, base)
        if not files:
            return []
        quoted = " ".join(shlex.quote(f) for f in files)
        ctx = ToolContext(worktree, self.timeout_s, _OUTPUT_CHARS, **self.confinement)
        found: list[Finding] = []
        for s in self.steps:
            result = await run_cmd(s.check(quoted), ctx, shell=True)
            if not result.ok:
                found.append(Finding("lint_fails", s.step, f"{LINT_FAILS}\n{result.output.strip()}", ""))
        return found


def lint_for(
    commands: Sequence[str], *, timeout_s: float, confinement: dict[str, Any] | None = None
) -> LayerLint | None:
    """The tester's lint for a gate of `commands`, or None when the gate runs no ruff."""
    steps = ruff_steps(commands)
    return LayerLint(steps, timeout_s=timeout_s, confinement=confinement) if steps else None
