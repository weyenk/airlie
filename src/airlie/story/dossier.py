"""The story dossier (lever P2 of the performance plan, 2026-10-28): the files a story's earlier steps read,
handed to each later step's brief so it does not read them again.

Every step of a story is a conversation of its own, and each started cold: the survey, the layer planning, the
meeting's three roles, the drafting, the critic and the coder each read the repository by tool, and mostly the
same few files (roman noise run 8: 28 reads, sandbox/__init__.py and tests/test_version.py six times each).
A read is a whole model call, thinking first, that also grows the conversation. The dossier is mechanical: the
`read_file` paths in the story's studio-run traces, most read first, their text taken from git at the commit
the next step reads (`story.dossier`)."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from airlie.teams.worktree import find_file, read_blob

DOSSIER_FILES = 12
"""The most files a dossier holds: the most read first."""


def read_paths(run_dirs: Iterable[Path]) -> list[str]:
    """The paths the `read_file` steps of the studio runs at `run_dirs` read, most read first (first read
    first among equals). A step trace that cannot be read is skipped."""
    count: Counter[str] = Counter()
    first: dict[str, int] = {}
    for run in run_dirs:
        for step in sorted((run / "trace").glob("**/step-*.json")):
            path = _read_path(step)
            if path:
                count[path] += 1
                first.setdefault(path, len(first))
    return sorted(count, key=lambda p: (-count[p], first[p]))


def _read_path(step: Path) -> str | None:
    try:
        call = json.loads(step.read_text())["u_t"]
        if call.get("name") != "read_file":
            return None
        args: Any = call.get("arguments")
        if isinstance(args, str):
            args = json.loads(args)
        path = str(args.get("path") or "").strip().removeprefix("./") if isinstance(args, dict) else ""
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return path or None


def dossier(repo: Path, commit: str, paths: Iterable[str], skip: Iterable[str] = ()) -> dict[str, str]:
    """The text at `commit` of up to DOSSIER_FILES of `paths`, in order: a path that is not a file there (one
    a layer created after it, a directory) or one in `skip` (a brief already holds it) is left out."""
    skipped = set(skip)
    out: dict[str, str] = {}
    for path in paths:
        if len(out) == DOSSIER_FILES:
            break
        if path in skipped or path in out:
            continue
        kind, sha = find_file(repo, commit, path)
        if kind == "blob":
            out[path] = read_blob(repo, sha)
    return out


def story_dossier(
    repo: Path, commit: str, run_dirs: Iterable[Path], skip: Iterable[str] = ()
) -> Mapping[str, str]:
    """The dossier for a step reading `commit`: the files the runs at `run_dirs` read (`read_paths`), at
    that commit (`dossier`)."""
    return dossier(repo, commit, read_paths(run_dirs), skip)
