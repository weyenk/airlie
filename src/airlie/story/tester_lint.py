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

import ast
import io
import re
import shlex
import tokenize
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airlie.prompts import text as hub_text
from airlie.story.layer_checks import Finding
from airlie.teams.worktree import changed_files, differs_from, find_file, read_blob, tree_paths
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
        # --unsafe-fixes, as the coder's autofix (tools/scope.py): F841's removal of an unused local is
        # "unsafe" (AIR-47 layer 02, 2026-11-02); the layer's tests and the gate still run after it
        fix = "check --fix --unsafe-fixes" if self.sub == "check" else "format"
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


AMBIGUOUS = ("l", "O", "I")
"""The names ruff's E741 refuses, which no ruff fix renames (AIR-47 layer 02, 2026-11-02:
`for l in layers`)."""


def _scopes(tree: ast.Module) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    """The outermost functions of a module: its own and its top-level classes' methods."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node
        elif isinstance(node, ast.ClassDef):
            yield from (n for n in node.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef))


def rename_ambiguous(source: str) -> str:
    """`source` with each ambiguous name (AMBIGUOUS) a function binds renamed inside that function, at its
    own references only (an attribute `x.l` or a keyword `f(l=1)` keeps its name) to a fresh `l_`, `o_`
    or `i_`. A function that takes one as a parameter or declares it global is left as it is, as is the
    module's top level: a caller may use the name. Source that does not parse comes back unchanged."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    edits: list[tuple[int, int, int, str]] = []  # (line, start, end) in UTF-8 bytes, the new name
    for fn in _scopes(tree):
        nodes = list(ast.walk(fn))
        names = [n for n in nodes if isinstance(n, ast.Name)]
        kept = {
            a.arg
            for n in nodes
            if isinstance(n, ast.arguments)
            for a in (
                *n.posonlyargs,
                *n.args,
                *n.kwonlyargs,
                *(x for x in (n.vararg, n.kwarg) if x is not None),
            )
        }
        kept |= {d for n in nodes if isinstance(n, ast.Global | ast.Nonlocal) for d in n.names}
        taken = {n.id for n in names} | kept
        for old in AMBIGUOUS:
            if old in kept or not any(n.id == old and isinstance(n.ctx, ast.Store) for n in names):
                continue
            new, k = f"{old.lower()}_", 2
            while new in taken:
                new, k = f"{old.lower()}_{k}", k + 1
            taken.add(new)
            edits += [(n.lineno, n.col_offset, n.end_col_offset or 0, new) for n in names if n.id == old]
    if not edits:
        return source
    lines = source.splitlines(keepends=True)
    for line, start, end, new in sorted(edits, reverse=True):
        raw = lines[line - 1].encode()
        lines[line - 1] = (raw[:start] + new.encode() + raw[end:]).decode()
    return "".join(lines)


STDLIB_IMPORTS: dict[str, str] = {
    **dict.fromkeys(
        (
            "Mapping",
            "MutableMapping",
            "Sequence",
            "MutableSequence",
            "Callable",
            "Iterable",
            "Iterator",
            "Awaitable",
            "AsyncIterator",
            "AsyncIterable",
            "Coroutine",
            "Generator",
        ),
        "from collections.abc import {}",
    ),
    **dict.fromkeys(
        ("Any", "cast", "Literal", "Protocol", "TypeVar", "ClassVar", "Final", "overload", "TYPE_CHECKING"),
        "from typing import {}",
    ),
    "Path": "from pathlib import {}",
    "dataclass": "from dataclasses import {}",
    "field": "from dataclasses import {}",
    "SimpleNamespace": "from types import {}",
    **dict.fromkeys(
        ("json", "os", "re", "sys", "asyncio", "subprocess", "shutil", "tempfile", "time", "textwrap"),
        "import {}",
    ),
}
"""The import an undefined name (ruff's F821, which no ruff fix mends) gets when the standard library is where
it comes from by convention (AIR-47 layer 02, 2026-11-02: `Mapping` in an added test)."""
_UNDEFINED = re.compile(r"Undefined name `(?P<name>[A-Za-z_][A-Za-z0-9_]*)`")


def _definers(worktree: Path, names: set[str]) -> dict[str, set[str]]:
    """For each of `names`, the modules of the worktree's committed Python outside tests that define it at
    their top level (a class, a function or an assignment), as dotted names (a leading src/ dropped)."""
    found: dict[str, set[str]] = {n: set() for n in names}
    if not names:
        return found
    alternation = "|".join(sorted(names))
    top = re.compile(rf"^(?:class|def|async def)\s+({alternation})\b|^({alternation})\s*(?::[^=\n]*)?=", re.M)
    for rel in tree_paths(worktree, "HEAD"):
        parts = Path(rel).parts
        if not rel.endswith(".py") or "tests" in parts or parts[0].startswith("."):
            continue
        try:
            text = (worktree / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        module = ".".join(Path(*(parts[1:] if parts[0] == "src" else parts)).with_suffix("").parts)
        for m in top.finditer(text):
            found[m[1] or m[2]].add(module.removesuffix(".__init__"))
    return found


def import_for(name: str, definers: Mapping[str, set[str]]) -> str | None:
    """The import line an undefined `name` gets: the standard library's (STDLIB_IMPORTS), else the one module
    of the repository that defines it; None when it is not known or more than one module defines it."""
    if name in STDLIB_IMPORTS:
        return STDLIB_IMPORTS[name].format(name)
    modules = definers.get(name, set())
    return f"from {next(iter(modules))} import {name}" if len(modules) == 1 else None


def with_imports(source: str, lines: Sequence[str]) -> str:
    """`source` with each import line of `lines` it does not hold placed after its last top-level import
    (or after its docstring when it has none); ruff's fixer then sorts them. Source that does not parse comes
    back unchanged."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    new = [x for x in dict.fromkeys(lines) if x not in source.splitlines()]
    if not new:
        return source
    at = 0
    for n, node in enumerate(tree.body):
        is_doc = n == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        if isinstance(node, ast.Import | ast.ImportFrom) or is_doc:
            at = node.end_lineno or node.lineno
    body = source.splitlines(keepends=True)
    return "".join([*body[:at], *(x + "\n" for x in new), *body[at:]])


_DIRECTIVES = re.compile(
    r"^#\s*(?:noqa|type:|pragma|fmt:|ruff:|pyright:|mypy:|isort:|pylint:)|^#!|noqa", re.I
)
"""A comment a tool reads (`# noqa: E501`, `# type: ignore`, a shebang): never wrapped, as moving it would
change what it applies to."""


def _docstring_lines(tree: ast.Module, lines: Sequence[str]) -> set[int]:
    """The 0-based lines inside the module's string-literal statements (docstrings, a module's, a class's,
    a function's, an attribute's), each of which starts and ends a line of its own: no code beside it."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
        if lines[first][: node.col_offset].strip() or lines[last][node.end_col_offset or 0 :].strip():
            continue
        found |= set(range(first, last + 1))
    return found


def _greedy(words: Sequence[str], first: int, rest: int) -> list[str] | None:
    """`words` filled into lines of at most `first` characters, then `rest`; None when a word cannot fit."""
    out: list[str] = []
    for w in words:
        room = first if len(out) <= 1 else rest
        if out and len(out[-1]) + 1 + len(w) <= room:
            out[-1] += f" {w}"
        elif len(w) <= (first if not out else rest):
            out.append(w)
        else:
            return None
    return out


def _wrapped_docstring_line(line: str, width: int) -> list[str] | None:
    """A docstring's line re-broken at word boundaries within `width`, its indentation kept on each, or None
    when it cannot be (a word too long, a doctest, a backslash a break would turn into a continuation)."""
    indent = line[: len(line) - len(line.lstrip())]
    body = line.strip()
    if body.startswith((">>>", "...")) or "\\" in body:
        return None
    room = width - len(indent)
    chunks = _greedy(body.split(), room, room)
    return None if chunks is None else [indent + c for c in chunks]


def _wrapped_comment_line(line: str, col: int, width: int) -> list[str] | None:
    """A line whose `#` comment starts at `col`, the comment re-broken within `width`: a comment on a line of
    its own keeps its indentation on each line; a trailing one keeps as many words as fit after its code and
    continues on comment lines below at the code's indentation. None when it cannot be."""
    comment = line[col:]
    if _DIRECTIVES.search(comment):
        return None
    indent = line[: len(line) - len(line.lstrip())]
    words = comment.lstrip("#").split()
    if not words:
        return None
    rest = width - len(indent) - 2
    if not line[:col].strip():  # a comment on a line of its own
        chunks = _greedy(words, rest, rest)
        return None if chunks is None else [f"{indent}# {c}" for c in chunks]
    head = line[:col] + "# "
    chunks = _greedy(words, width - len(head), rest)
    if chunks is None or not chunks[0]:
        return None
    return [head + chunks[0], *(f"{indent}# {c}" for c in chunks[1:])]


def wrap_long_lines(source: str, width: int) -> str:
    """`source` with each line longer than `width` whose overflow is in a docstring or a `#` comment re-broken
    at word boundaries, keeping its indentation (AIR-47, AIR-48, AIR-59, AIR-61: an E501 on a docstring or
    comment line, which no ruff fix wraps, sent a package back a whole try, 17 times; AIR-59's was a
    docstring's `carries "type": "story" so ... the section write.`, 111 characters against 110). A docstring
    is a string-literal statement on lines of its own (`_docstring_lines`); a comment is what `tokenize` says
    is one, a trailing one wrapped only when its code fits. Code, any other string, a directive comment
    (`_DIRECTIVES`) and a doctest are never touched; a line that cannot be wrapped is left for the check to
    report. Source that does not parse, or that would not after, comes back unchanged."""
    try:
        tree = ast.parse(source)
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError, IndentationError):
        return source
    lines = source.splitlines(keepends=True)
    bare = [x.rstrip("\r\n") for x in lines]
    docs = _docstring_lines(tree, bare)
    comments = {t.start[0] - 1: t.start[1] for t in tokens if t.type == tokenize.COMMENT}
    out: list[str] = []
    changed = False
    for n, line in enumerate(bare):
        ending = lines[n][len(line) :]
        got = None
        if len(line) > width:
            if n in comments and comments[n] < width:
                got = _wrapped_comment_line(line, comments[n], width)
            elif n in docs:
                got = _wrapped_docstring_line(line, width)
        if got is None:
            out.append(lines[n])
        else:
            changed = True
            out += [g + (ending or "\n") for g in got[:-1]] + [got[-1] + ending]
    if not changed:
        return source
    wrapped = "".join(out)
    try:
        ast.parse(wrapped)
    except SyntaxError:
        return source
    return wrapped


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
        """Run each step's fixer on the changed files, with the renames, imports and wrapping of over-long
        docstring and comment lines at the line length of the ruff configuration on `base`
        (`wrap_long_lines`) that no ruff fix makes; return the files it changed (left uncommitted)."""
        files = self._files(worktree, base)
        if not files:
            return []
        for f in files:  # before ruff, which then formats what the rename lengthened
            path = worktree / f
            text = path.read_text(encoding="utf-8")
            if (renamed := rename_ambiguous(text)) != text:
                path.write_text(renamed, encoding="utf-8")
        quoted = " ".join(shlex.quote(f) for f in files)
        ctx = ToolContext(worktree, self.timeout_s, _OUTPUT_CHARS, **self.confinement)
        await self._import_undefined(worktree, files, quoted, ctx)
        width = ruff_line_length(worktree, base)
        self._wrap(worktree, files, width)
        for s in self.steps:
            await run_cmd(s.fixer(quoted), ctx, shell=True)  # what it could not fix, check() reports
        if self._wrap(worktree, files, width):  # ruff's indenting of a comment took it over again
            for s in self.steps:
                await run_cmd(s.fixer(quoted), ctx, shell=True)
        return differs_from(worktree, "HEAD", files)

    @staticmethod
    def _wrap(worktree: Path, files: Sequence[str], width: int) -> bool:
        """Wrap each file's over-long docstring and comment lines (`wrap_long_lines`); whether any changed."""
        changed = False
        for f in files:
            path = worktree / f
            text = path.read_text(encoding="utf-8")
            if (wrapped := wrap_long_lines(text, width)) != text:
                path.write_text(wrapped, encoding="utf-8")
                changed = True
        return changed

    async def _import_undefined(
        self, worktree: Path, files: list[str], quoted: str, ctx: ToolContext
    ) -> None:
        """Add the import of each name ruff's F821 says is undefined, where `import_for` knows the one place
        it comes from; any other F821 is left for check() to report."""
        check = next((s for s in self.steps if s.sub == "check"), None)
        if check is None:
            return
        undefined: dict[str, set[str]] = {}
        for f in files:  # one file a run, one line an error: no path to match, and no output cap to cut it
            flags = "--select F821 --output-format concise --no-cache --force-exclude"
            cmd = f"{check.ruff} check {flags} -- {shlex.quote(f)}"
            result = await run_cmd(cmd, ctx, shell=True)
            if not result.ok:
                undefined[f] = {m["name"] for m in _UNDEFINED.finditer(result.output)}
        definers = _definers(
            worktree, {n for names in undefined.values() for n in names} - set(STDLIB_IMPORTS)
        )
        for f, names in undefined.items():
            lines = [line for n in sorted(names) if (line := import_for(n, definers)) is not None]
            if lines:
                path = worktree / f
                path.write_text(with_imports(path.read_text(encoding="utf-8"), lines), encoding="utf-8")

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
