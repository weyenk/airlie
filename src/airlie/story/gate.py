"""A repository's test gate: the commands every change must pass, as the root AGENTS.md lists them under a
Test gate heading. It is read from a commit (a story's base: the trunk, which only a person merges into),
never from a worktree, so a model cannot weaken the gate it is held to. The commands run in order, each in a
group of its own, joined with && into the one command the studio's gate runs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from airlie.story.model import NoTestGate
from airlie.teams.worktree import find_file, read_blob

AGENTS_MD = "AGENTS.md"
EXAMPLE = ("uv sync --frozen", "uv run ruff check .", "uv run pytest -q")
"""The gate a note shows when a repository has none."""
NO_AGENTS_MD = "it has no AGENTS.md"
NO_SECTION = "its AGENTS.md has no Test gate section"
NO_COMMAND = (
    "the Test gate section of its AGENTS.md lists no command (no fenced block under the heading, or only "
    "blank and # lines in it)"
)
LEADS_NOWHERE = "its AGENTS.md is a symbolic link that leads to no file of the commit"
_QUOTED = 80
"""How many characters of the file's own text a message quotes."""
_HEADING = re.compile(r" {0,3}(#{1,6})(?:[ \t]+(.*))?")
"""An ATX heading: up to three spaces, one to six #, then nothing or a space and the text."""
_CLOSING = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
"""The #s that may close a heading's text."""
_UNDERLINE = re.compile(r" {0,3}(=+|-+)[ \t]*")
"""A Setext heading's underline: = for a level 1 heading, - for level 2, under the lines of a paragraph."""
_NO_PARAGRAPH = re.compile(
    r" {0,3}(?:[-+*](?:[ \t]|$)|\d{1,9}[.)](?:[ \t]|$)|>|(?:-[ \t]*){3,}$|(?:\*[ \t]*){3,}$|(?:_[ \t]*){3,}$)"
)
"""A list item, a block quote or a thematic break: no paragraph, so no Setext heading, is made of it."""
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")
_QUOTES = "'\"`"
_WORD_ENDS = " \t;&|()<>"
"""What ends a word in sh: after one of these, as at the start of a line, a # begins a comment."""


def gate_command(commands: Sequence[str]) -> str:
    """`commands` as the one command the studio's gate runs in sh: each in a brace group of its own, ended by
    a newline (so an inline `# comment` ends at its own line), joined with &&: the first that fails fails the
    gate and stops it, and a `cd` carries over to the commands after it."""
    return " && ".join(f"{{ {c}\n}}" for c in commands)


def gate_shown(commands: Sequence[str]) -> str:
    """`commands` as a note or a PR body shows them: as they read, joined with &&."""
    return " && ".join(commands)


@dataclass(frozen=True)
class Gate:
    """A test gate: its commands, in order, and the blob of the AGENTS.md they were read from."""

    commands: tuple[str, ...]
    blob: str

    @property
    def command(self) -> str:
        """What runs (gate_command)."""
        return gate_command(self.commands)

    @property
    def shown(self) -> str:
        """What a note or a PR body says (gate_shown)."""
        return gate_shown(self.commands)


def _quoted(text: str) -> str:
    """`text` as a message quotes it: its repr, cut to _QUOTED characters."""
    shown = repr(text)
    return shown if len(shown) <= _QUOTED else shown[: _QUOTED - 1] + "…"


def _heading(line: str) -> tuple[int, str] | None:
    """(level, text) of an ATX heading line, its closing #s dropped; None for any other line."""
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


def _paragraph_line(line: str, paragraph: list[str]) -> bool:
    """`line` is a line of a paragraph (one a Setext underline can turn into a heading): not blank, not a list
    item, a block quote or a thematic break, and not indented code (four spaces or a tab) unless it goes on a
    paragraph."""
    if not line.strip() or _NO_PARAGRAPH.match(line):
        return False
    return bool(paragraph) or not line.startswith(("    ", "\t"))


def _continues(command: str) -> bool:
    """`command` goes on onto the next line, as sh reads it: it ends in a backslash that is not escaped, it
    leaves a quote (' " or `) or a parenthesis open, or it ends in &&, || or |. A comment (a # that begins a
    word) is not read, and neither is what is quoted or escaped (a backslash escapes the character after it,
    but not inside single quotes)."""
    code: list[str] = []  # what sh reads as code, every quoted or escaped character as an x
    quote: str | None = None
    i = 0
    while i < len(command):
        c = command[i]
        if c == "\\" and quote != "'":
            if i + 1 == len(command):
                return True  # it escapes the newline: the next line goes on this one
            i += 1
            code.append("x")
        elif quote is not None:
            quote = None if c == quote else quote
            code.append("x")
        elif c in _QUOTES:
            quote = c
            code.append("x")
        elif c == "#" and (not code or code[-1] in _WORD_ENDS):
            break
        else:
            code.append(c)
        i += 1
    text = "".join(code).rstrip()
    return quote is not None or text.count("(") > text.count(")") or text.endswith(("&&", "|"))


def gate_commands(markdown: str) -> tuple[str, ...]:
    """The commands of the first heading, at any level, ATX (`## Test gate`) or Setext (`Test gate` over a
    line of = or -), whose text is "Test gate" (in any case): the lines of the first fenced block (``` or ~~~)
    after it and before the next heading of its level or a higher one, but blank lines and those that start
    with #, each trimmed. NoTestGate when there is no such heading, no command under it, or a command that
    goes on onto the next line (each is run on its own: gate_command). A heading inside a code block is code,
    and CRLF line ends are read as lines."""
    level: int | None = None  # the Test gate heading's, once it is found
    fence: tuple[str, int] | None = None  # the open code block's
    block: list[str] | None = None  # the lines of the gate's block, once it opened
    paragraph: list[str] = []  # the lines of the paragraph a Setext underline would make a heading of
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
            paragraph = []
            if level is not None:
                block = []
            continue
        heading = _heading(line)
        underline = _UNDERLINE.fullmatch(line) if heading is None and paragraph else None
        if underline is not None:
            heading = (1 if underline[1][0] == "=" else 2, " ".join(x.strip() for x in paragraph))
        if heading is None:
            paragraph = [*paragraph, line] if _paragraph_line(line, paragraph) else []
            continue
        paragraph = []
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
    going_on = next((c for c in commands if _continues(c)), None)
    if going_on is not None:
        raise NoTestGate(
            "the Test gate section of its AGENTS.md continues a command onto the next line "
            f"({_quoted(going_on)}): put each command on one line"
        )
    return commands


def read_gate(repo: Path, commit: str) -> Gate:
    """The test gate of the AGENTS.md at the root of `commit` in `repo`, read from git's objects (a link to
    another file of the commit followed). NoTestGate when there is no AGENTS.md there, it is a link out of the
    commit or to no file of it, or it holds no gate."""
    kind, found = find_file(repo, commit, AGENTS_MD)
    if kind == "symlink":
        raise NoTestGate(
            f"its AGENTS.md is a symbolic link out of the repository (to {_quoted(found)}), and airlie reads "
            "only the files of the commit"
        )
    if kind in ("dangling", "loop", "notdir"):
        raise NoTestGate(LEADS_NOWHERE)
    if kind != "blob":
        raise NoTestGate(NO_AGENTS_MD)
    return Gate(gate_commands(read_blob(repo, found)), found)


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
