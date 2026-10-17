"""What the architect is handed, read from the story's base commit, so it spends its steps on judgement and
not on finding the README: the repository's AGENTS.md and the files the story names."""

from __future__ import annotations

import re
from pathlib import Path

from airlie.teams.worktree import find_file, read_blob

GUIDE_CHARS = 6_000
FILE_CHARS = 5_000
MAX_FILES = 5
_PATH = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.(?:py|md|yaml|yml|toml|json|js|ts|tsx|css|html|sql|sh))\b"
)
"""A repository path in prose: at least one directory, a known extension."""


def named_paths(text: str) -> list[str]:
    """The paths `text` names, in order of first mention, none twice."""
    seen: list[str] = []
    for m in _PATH.finditer(text):
        path = m.group(1).lstrip("./")
        if path not in seen:
            seen.append(path)
    return seen


def _blob(repo: Path, commit: str, path: str) -> str | None:
    kind, sha = find_file(repo, commit, path)
    return read_blob(repo, sha) if kind == "blob" else None


def repo_guide(repo: Path, commit: str, text: str) -> str:
    """AGENTS.md (if the commit has one) and up to MAX_FILES of the files `text` names that it has, each
    clipped: markdown ready to put in a brief; "" when there is nothing."""
    parts: list[str] = []
    guide = _blob(repo, commit, "AGENTS.md")
    if guide is not None:
        parts.append("### AGENTS.md\n" + _clip(guide, GUIDE_CHARS))
    shown = 0
    for path in named_paths(text):
        if shown == MAX_FILES:
            break
        body = _blob(repo, commit, path)
        if body is None:
            continue
        parts.append(f"### {path}\n```\n{_clip(body, FILE_CHARS)}\n```")
        shown += 1
    return "\n\n".join(parts)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n[clipped]"
