"""A repository's test gate: the commands every change must pass, as the root AGENTS.md lists them under a
Test gate heading. It is read from a commit (a story's base: the trunk, which only a person merges into),
never from a worktree, so a model cannot weaken the gate it is held to. The commands run in order, joined
with && into the one command the studio's gate runs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from airlie.story.model import NoTestGate
from airlie.teams.worktree import show_file

AGENTS_MD = "AGENTS.md"
EXAMPLE = ("uv sync --frozen", "uv run ruff check .", "uv run pytest -q")
"""The gate a note shows when a repository has none."""
NO_AGENTS_MD = "it has no AGENTS.md"
NO_SECTION = "its AGENTS.md has no Test gate section"
NO_COMMAND = (
    "the Test gate section of its AGENTS.md lists no command (no fenced block under the heading, or only "
    "blank and # lines in it)"
)
_HEADING = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*))?")
"""An ATX heading: up to three spaces, one to six #, then nothing or a space and the text."""
_CLOSING = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
"""The #s that may close a heading's text."""
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")


@dataclass(frozen=True)
class Gate:
    """A test gate: its commands, in order, and the blob of the AGENTS.md they were read from."""

    commands: tuple[str, ...]
    blob: str

    @property
    def command(self) -> str:
        """The commands as the one command the studio's gate runs: the first that fails fails it."""
        return " && ".join(self.commands)


def _heading(line: str) -> tuple[int, str] | None:
    """(level, text) of a heading line, its closing #s dropped; None for any other line."""
    m = _HEADING.fullmatch(line)
    if m is None:
        return None
    return len(m[1]), _CLOSING.sub("", m[2] or "").strip()


def _opens(line: str) -> tuple[str, int] | None:
    """The character and length of the fence `line` opens a code block with, else None (a backtick fence's
    info string holds no backtick)."""
    m = _FENCE.fullmatch(line)
    if m is None or (m[1][0] == "`" and "`" in m[2]):
        return None
    return m[1][0], len(m[1])


def _closes(line: str, fence: tuple[str, int]) -> bool:
    """`line` closes a block opened with `fence`: the same character, at least as many, nothing after."""
    m = _CLOSE.fullmatch(line)
    return m is not None and m[1][0] == fence[0] and len(m[1]) >= fence[1]


def gate_commands(markdown: str) -> tuple[str, ...]:
    """The commands of the first heading, at any level, whose text is "Test gate" (in any case): the lines of
    the first fenced block (``` or ~~~) after it and before the next heading of its level or a higher one, but
    blank lines and those that start with #, each trimmed. NoTestGate when there is no such heading, or no
    command under it. A heading inside a code block is code, and CRLF line ends are read as lines."""
    level: int | None = None  # the Test gate heading's, once it is found
    fence: tuple[str, int] | None = None  # the open code block's
    block: list[str] | None = None  # the lines of the gate's block, once it opened
    for raw in markdown.removeprefix("﻿").split("\n"):
        line = raw.removesuffix("\r")
        if fence is not None:
            if _closes(line, fence):
                if block is not None:
                    break
                fence = None
            elif block is not None:
                block.append(line)
            continue
        fence = _opens(line)
        if fence is not None:
            if level is not None:
                block = []
            continue
        heading = _heading(line)
        if heading is None:
            continue
        if level is None:
            if heading[1].casefold() == "test gate":
                level = heading[0]
        elif heading[0] <= level:
            break
    if level is None:
        raise NoTestGate(NO_SECTION)
    commands = tuple(c for c in (x.strip() for x in block or ()) if c and not c.startswith("#"))
    if not commands:
        raise NoTestGate(NO_COMMAND)
    return commands


def read_gate(repo: Path, commit: str) -> Gate:
    """The test gate of the AGENTS.md at the root of `commit` in `repo`, read from git's objects (a link to
    another file of the commit followed). NoTestGate when there is no AGENTS.md there, or no gate in it."""
    found = show_file(repo, commit, AGENTS_MD)
    if found is None:
        raise NoTestGate(NO_AGENTS_MD)
    blob, text = found
    return Gate(gate_commands(text), blob)


def missing_gate(repository: str, trunk: str, sha: str, problem: str) -> str:
    """What the issue is told when `repository` has no test gate on `trunk` (at `sha`): the `problem`, and
    what to add, with an example."""
    example = "\n".join(EXAMPLE)
    return (
        f"{repository} has no test gate on {trunk} ({sha[:12]}): {problem}. Add a `## Test gate` section to "
        "AGENTS.md at the repository root (make the file if there is none): one fenced block listing the "
        "commands every change must pass, one per line, run in order. For example:\n"
        f"```sh\n{example}\n```"
    )
