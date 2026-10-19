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
TEST_GATE = "Test gate"
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
_WORD_ENDS = " \t;&|()<>"
"""What ends a word in sh: after one of these, as at the start of a line, a # begins a comment."""
_TWO = ("&&", "||", "|&", "&>", ">&", "<&")
"""The operators of two characters that hold an & or a |."""
_REFUSED = {
    "&": (
        "runs a command in the background with &",
        ", so its result would never reach the gate: run each command in the foreground",
    ),
    "&>": (
        "redirects with &>",
        ", which only bash reads that way (the gate runs in sh, where it can put the command in the "
        "background): write > file 2>&1, or >> file 2>&1 for &>>",
    ),
    "|&": ("pipes with |&", ", which only bash reads (the gate runs in sh): write 2>&1 | instead"),
}
"""The operators a gate line may not hold, as (what the line does, what to write instead): sh puts the command
before a control operator & in the background, and its result never reaches the gate; &> (and &>>) and |& are
bash's, and in dash `cmd &>file` is `cmd &` then `>file`."""
_CLOSER = {'"': '"', "`": "`", "${": "}"}


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


def _read_line(command: str) -> tuple[bool, str | None]:
    """How sh reads `command`: whether it goes on onto the next line (it ends in a backslash that is not
    escaped, or in &&, || or |, or it leaves a quote, a backtick, a parenthesis, $(, ${ or $(( open), and the
    first operator of _REFUSED in it (a control operator &, not one of &&, >& or <&; &>, which &>> begins
    with; |&), None when there is none. Nothing quoted, escaped, in a comment, in ${...} or in $((...)) is an
    operator. A # that begins a word begins a comment, and a backslash escapes the character after it, but
    not in single quotes."""
    opened: list[str] = []  # what is open, innermost last: a quote, `, $(, (, ${, $(( or a ( inside $((
    refused: str | None = None
    ends_in_operator = in_word = False
    i = 0
    while i < len(command):
        top, c = (opened[-1] if opened else ""), command[i]
        if top == "'":  # nothing but the closing quote is special in single quotes
            if c == "'":
                opened.pop()
            i += 1
            continue
        if c == "\\":
            if i + 1 == len(command):
                return True, refused  # it escapes the newline: the next line goes on this one
            i, in_word, ends_in_operator = i + 2, True, False
            continue
        if c == _CLOSER.get(top):
            opened.pop()
            i, in_word = i + 1, True
            continue
        start = next((o for o in ("$((", "$(", "${") if command.startswith(o, i)), None)
        if start is None and (c == "`" or c in "'\"" and top != '"'):
            start = c
        if start is not None:
            opened.append(start)
            i += len(start)
            in_word, ends_in_operator = start not in ("$(", "`"), False  # after $( or `, commands begin
            continue
        if top in ('"', "${"):  # the rest of a double-quoted string or a ${...} is text
            i += 1
            continue
        if top in ("$((", "a("):  # arithmetic: & and | are its operators, and ( ) nest
            if c == "(":
                opened.append("a(")
            elif c == ")" and top == "a(":
                opened.pop()
            elif command.startswith("))", i) and top == "$((":
                opened.pop()
                i += 1
            i += 1
            continue
        # commands: the line itself, ( ... ), $( ... ) or ` ... `
        if c == "#" and not in_word and top != "`":
            break  # a comment, to the end of the line
        token = next((t for t in _TWO if command.startswith(t, i)), c)
        if token == "(":
            opened.append("(")
        elif token == ")" and top in ("(", "$("):
            opened.pop()
        elif token in _REFUSED and refused is None:
            refused = token
        if not token.isspace():
            ends_in_operator = token in ("&&", "||", "|")
        # a $(...) is part of a word, a ( ... ) is not
        in_word = token[-1] not in _WORD_ENDS or (token == ")" and top == "$(")
        i += len(token)
    return bool(opened) or ends_in_operator, refused


def _section(markdown: str, title: str) -> list[str] | None:
    """The lines of the first fenced block (``` or ~~~) under the first heading, at any level, ATX or Setext,
    whose text is `title` (in any case), before the next heading of its level or a higher one: [] when the
    heading has no block, None when there is no such heading. A heading inside a code block is code, and CRLF
    line ends are read as lines."""
    level: int | None = None  # the section heading's, once it is found
    fence: tuple[str, int] | None = None  # the open code block's
    block: list[str] | None = None  # the lines of the section's block, once it opened
    paragraph: list[str] = []  # the lines of the paragraph a Setext underline would make a heading of
    for raw in markdown.removeprefix("\ufeff").split("\n"):
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
            if heading[1].casefold() == title.casefold():
                level = heading[0]
        elif heading[0] <= level:
            break
    return None if level is None else block or []


def _commands(block: list[str], title: str) -> tuple[str, ...]:
    """The commands of a section's block: its lines but blank ones and those that start with #, each
    trimmed. NoTestGate when there is none, or one that goes on onto the next line, that runs one in the
    background, or that holds bash's &>, &>> or |& (each is run on its own in sh, and must pass:
    gate_command). The messages name the section by `title`."""
    commands = tuple(c for c in (x.strip() for x in block) if c and not c.startswith("#"))
    if not commands:
        raise NoTestGate(NO_COMMAND.replace("Test gate", title))
    for command in commands:
        goes_on, refused = _read_line(command)
        if goes_on:
            raise NoTestGate(
                f"the {title} section of its AGENTS.md continues a command onto the next line "
                f"({_quoted(command)}): put each command on one line"
            )
        if refused is not None:
            does, instead = _REFUSED[refused]
            raise NoTestGate(f"the {title} section of its AGENTS.md {does} ({_quoted(command)}){instead}")
    return commands


def gate_commands(markdown: str) -> tuple[str, ...]:
    """The commands of the first heading whose text is "Test gate" (`## Test gate`, or `Test gate` over a line
    of = or -): the lines of the first fenced block after it (`_section`), as `_commands` reads them.
    NoTestGate when there is no such heading, or `_commands` finds no command it can run."""
    block = _section(markdown, TEST_GATE)
    if block is None:
        raise NoTestGate(NO_SECTION)
    return _commands(block, TEST_GATE)


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
