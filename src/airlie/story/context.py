"""What the architect is handed, read from the story's base commit, so it spends its steps on judgement and
not on finding the README: the repository's AGENTS.md and the files the story names."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from airlie.config import AirlieConfig
from airlie.outline import outline
from airlie.teams.worktree import find_file, read_blob

GUIDE_CHARS = 6_000
GUIDE_SHARE = 0.2
"""The share of a role's context the files read for it may take."""
CHARS_PER_TOKEN = 3
FILE_HEAD_CHARS = 2_000
"""How much of a big non-Python file the outline form keeps."""
PATH_EXTENSIONS = "py|md|yaml|yml|toml|json|js|ts|tsx|css|html|sql|sh"
"""The extensions that make a word in prose a repository path (a regex alternation)."""
_PATH = re.compile(rf"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.(?:{PATH_EXTENSIONS}))\b")
"""A repository path in prose: at least one directory, a known extension."""


def named_paths(text: str) -> list[str]:
    """The paths `text` names, in order of first mention, none twice."""
    seen: list[str] = []
    for m in _PATH.finditer(text):
        path = m.group(1).removeprefix("./")
        if path not in seen:
            seen.append(path)
    return seen


def _blob(repo: Path, commit: str, path: str) -> str | None:
    kind, sha = find_file(repo, commit, path)
    return read_blob(repo, sha) if kind == "blob" else None


def guide_budget(cfg: AirlieConfig, roles: Sequence[str]) -> int:
    """Characters of "read for you" the smallest context among `roles` can carry (a share of it)."""
    return int(min(cfg.roles[r].max_context for r in roles) * GUIDE_SHARE * CHARS_PER_TOKEN)


_ALSO = "### Also named (not shown)"


_BACKTICKS = re.compile(r"`+")


def fenced(body: str) -> str:
    """`body` in a code fence longer than any backtick run in it, so a Markdown file's own ``` fences
    cannot close it early and spill the rest of the file into the brief as markup."""
    fence = "`" * max(3, 1 + max((len(m.group()) for m in _BACKTICKS.finditer(body)), default=0))
    return f"{fence}\n{body}\n{fence}"


def _assemble(parts: list[str], listed: list[str]) -> str:
    sections = list(parts)
    if listed:
        sections.append(_ALSO + "\n" + "\n".join(listed))
    return "\n\n".join(sections)


_WHOLE_HEADING = re.compile(r"^### (.+) \(whole, \d+ lines\)$", re.MULTILINE)


def whole_paths(guide: str) -> frozenset[str]:
    """The paths a `repo_guide` shows whole (its `### <path> (whole, N lines)` headings), so a brief that
    carries the guide does not carry them a second time (the dossier)."""
    return frozenset(_WHOLE_HEADING.findall(guide))


def repo_guide(repo: Path, commit: str, text: str, *, budget_chars: int) -> str:
    """AGENTS.md (if the commit has one) and the files `text` names that it has, in order of mention, within
    `budget_chars` for the whole string: each file whole if it fits, else as an outline with line numbers,
    else just listed with its line count. Once even an outline does not fit, the rest are listed.
    Markdown ready to put in a brief; "" when there is nothing.

    2026-10-22: 3 in 4 named files were longer than the old 5000-char clip, and roles read them again from
    line 1 (about 45% of the tester's reads before its first write)."""
    parts: list[str] = []
    guide = _blob(repo, commit, "AGENTS.md")
    if guide is not None:
        head, tail = "### AGENTS.md\n", "\n[clipped]"
        limit = min(GUIDE_CHARS, budget_chars - len(head) - len(tail))
        if limit > 0:  # when even a clipped AGENTS.md cannot fit, it is dropped, never over budget
            parts.append(head + _clip(guide, limit))
    files = [(p, b) for p in named_paths(text) if (b := _blob(repo, commit, p)) is not None]
    lines = [f"- {p} ({len(b.splitlines())} lines)" for p, b in files]
    shown = 0
    for k, (path, body) in enumerate(files):
        n = len(body.splitlines())
        rest = lines[k + 1 :]
        whole = f"### {path} (whole, {n} lines)\n{fenced(body)}"
        if len(_assemble([*parts, whole], rest)) <= budget_chars:
            parts.append(whole)
            shown = k + 1
            continue
        heading = f"### {path} (outline: read a span with read_file offset and limit)"
        card = f"{heading}\n{fenced(outline(path, body, head_chars=FILE_HEAD_CHARS))}"
        if len(_assemble([*parts, card], rest)) <= budget_chars:
            parts.append(card)
            shown = k + 1
            continue
        break
    listed = lines[shown:]
    while listed and len(_assemble(parts, listed)) > budget_chars:
        listed.pop()  # never over budget: a listed line that does not fit is dropped
    return _assemble(parts, listed)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n[clipped]"
