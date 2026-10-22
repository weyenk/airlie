"""Deterministic repository map, injected before the goal so no branch re-discovers the layout (design §4).

Tracked files (git ls-files) to a depth, sizes, the test command, and for Python files the top-level def/class
signatures via ast. No model call. Cached per (path, mtime, size) of the tracked files."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from airlie.outline import outline_entries
from airlie.subproc import NO_HOOKS_ARGS, agent_env

HEADER = "[Repository map]"
TRUNCATED = "\n[map truncated]"


def _tracked(repo: Path) -> list[Path]:
    # airlie's own git in a checkout a model may have worked in: reading the index can run an fsmonitor
    out = subprocess.run(
        ["git", *NO_HOOKS_ARGS, "ls-files", "-z"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env=agent_env(),
    ).stdout
    return sorted(Path(p) for p in out.split("\0") if p)


def _stat(path: Path) -> os.stat_result | None:
    """`path`'s stat, or None when there is none to give: a tracked symlink whose target is not in this
    checkout (an ignored file it points at is not in a fresh worktree), a file that went away."""
    try:
        return path.stat()
    except OSError:
        return None


def _signatures(path: Path) -> list[str]:
    try:
        entries = outline_entries(path.read_text(errors="replace"))
    except OSError:  # a file this user cannot read
        return []
    # the map has always written `def`, async or not
    return [e.text.removeprefix("async ") for e in entries or [] if e.depth == 0]


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


CODE_SUFFIXES = frozenset(
    [".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cpp"]
)
TEST_DIRS = frozenset({"tests", "test"})


def _rank(top: str, files: list[Path], focus: set[Path]) -> int:
    """Where a top-level directory is expanded: focus files first, then code, tests, root files, the rest.
    A directory named `tests` is tests even though it holds code, so `src/` comes before it."""
    if any(f in focus for f in files):
        return 0
    if top in TEST_DIRS:
        return 2
    if any(f.suffix in CODE_SUFFIXES for f in files):
        return 1
    return 3 if top == "" else 4


def _code_count(files: list[Path]) -> int:
    return sum(f.suffix in CODE_SUFFIXES for f in files)


def _file_line(repo: Path, rel: Path, size: int) -> str:
    entry = f"{rel} ({_size(size)})"
    if rel.suffix == ".py":
        sigs = _signatures(repo / rel)
        if sigs:
            entry += ": " + "; ".join(sigs)
    return entry


def _dir_block(
    repo: Path, files: list[Path], sizes: dict[Path, int], depth: int, focus: set[Path]
) -> list[str]:
    """One top-level directory's file lines; what lies deeper than `depth` is one `dir/ …` line, except a
    focus file, which is shown at any depth."""
    lines: list[str] = []
    shown_dirs: set[Path] = set()
    for rel in files:
        if len(rel.parts) > depth and rel not in focus:
            d = Path(*rel.parts[:depth])
            if d not in shown_dirs:
                shown_dirs.add(d)
                lines.append(f"{d}/ …")
            continue
        lines.append(_file_line(repo, rel, sizes[rel]))
    return lines


Choice = tuple[str, ...]
"""One entry of an expanded directory: the line to show, then shorter stand-ins if it does not fit."""


def _parts(
    repo: Path, top: Path, files: list[Path], sizes: dict[Path, int], focus: set[Path]
) -> tuple[list[Choice], list[Choice]]:
    """A directory too large to expand whole, as (toward the focus, the rest). Toward the focus: each
    subdirectory that holds a focus file as a count line and then descended into, and the focus files
    themselves. The rest, deepest directory first: its other files (with signatures, else name and size),
    then its other subdirectories as count lines in `_rank` order. A role's files stay in view however deep
    they are and however many files sit beside them."""
    base = top  # a directory that only holds one subdirectory (`src/pkg/`) is looked through
    while True:
        n = len(base.parts) + 1
        direct = [f for f in files if len(f.parts) == n]
        subs: dict[str, list[Path]] = {}
        for f in files:
            if len(f.parts) > n:
                subs.setdefault(f.parts[n - 1], []).append(f)
        if direct or len(subs) != 1:
            break
        base = base / next(iter(subs))
    ranked = sorted(subs.items(), key=lambda g: (_rank(g[0], g[1], focus), -_code_count(g[1]), g[0]))
    toward: list[Choice] = []
    rest: list[Choice] = []
    for sub, group in ranked:
        if any(f in focus for f in group):
            inner_toward, inner_rest = _parts(repo, base / sub, group, sizes, focus)
            toward += [(f"{base / sub}/ ({len(group)} files)",), *inner_toward]
            rest += inner_rest
    for f in direct:
        (toward if f in focus else rest).append((_file_line(repo, f, sizes[f]), f"{f} ({_size(sizes[f])})"))
    rest += [
        (f"{base / sub}/ ({len(group)} files)",)
        for sub, group in ranked
        if not any(f in focus for f in group)
    ]
    return toward, rest


def _render(
    repo: Path, files: list[Path], *, test_cmd: str | None, depth: int, max_chars: int, focus: Sequence[str]
) -> tuple[str, bool]:
    """(map text, whether something did not fit). The budget goes to the header and test command, a line for
    every top-level directory, and then the directories in `_rank` order, each whole if it fits, else as
    much of its `_parts` as fits. The directories that hold focus files go first, and when one is too large
    its way to the focus files is shown before any directory's rest, so `tests/` is not left out because
    `src/` had many files."""
    sizes = {
        rel: st.st_size for rel in files if (st := _stat(repo / rel)) is not None
    }  # left out, as in the fingerprint
    tracked = [rel for rel in files if rel in sizes]
    wanted = {Path(f.removeprefix("./")) for f in focus}
    scope = {rel for rel in tracked if rel in wanted}
    budget = max_chars - len(TRUNCATED)  # room for the marker, so a cut map is never over `max_chars`
    head = [HEADER, *([f"test command: {test_cmd}"] if test_cmd else [])]
    groups: dict[str, list[Path]] = {}
    for rel in tracked:
        groups.setdefault(rel.parts[0] if len(rel.parts) > 1 else "", []).append(rel)
    dirs = [f"{top}/ ({len(group)} files)" for top, group in sorted(groups.items()) if top]
    room = budget - len("\n".join([*head, *dirs]))  # always present
    if room < 0:
        return "\n".join([*head, *dirs]), True
    order = sorted(groups.items(), key=lambda g: (_rank(g[0], g[1], scope), -_code_count(g[1]), g[0]))
    shown: dict[str, list[str]] = {top: [] for top, _ in order}
    later: dict[str, list[Choice]] = {}
    cut = False

    def take(top: str, choices: list[Choice]) -> None:
        nonlocal room
        for choice in choices:  # the first stand-in that fits; an entry nothing of fits is passed over
            line = next((c for c in choice if len(c) + 1 <= room), None)
            if line is not None:
                room -= len(line) + 1
                shown[top].append(line)

    def place(top: str, group: list[Path]) -> None:
        nonlocal room, cut
        block = _dir_block(repo, group, sizes, depth, scope)
        if len("\n".join(block)) + 1 <= room:
            room -= len("\n".join(block)) + 1
            shown[top] = block
            return
        cut = True
        toward, later[top] = _parts(repo, Path(top), group, sizes, scope)
        take(top, toward)

    for top, group in order:
        if _rank(top, group, scope) == 0:
            place(top, group)
    for top, group in order:
        if top in later:
            take(top, later[top])
        elif not shown[top]:
            place(top, group)
            take(top, later.get(top, []))
    lines = [*head, *dirs, *(line for top, _ in order for line in shown[top])]
    return "\n".join(lines), cut


def build_repo_map(
    repo: Path,
    *,
    test_cmd: str | None,
    max_chars: int,
    depth: int,
    cache_dir: Path | None,
    focus: Sequence[str] = (),
) -> tuple[str, bool]:
    """Returns (map text, served_from_cache). `focus` is the paths the role will work on: the directories
    that hold them are expanded first, toward those files. Their outlines are the story guide's to give."""
    files = _tracked(repo)
    fingerprint = hashlib.sha256()
    for rel in files:
        st = _stat(repo / rel)
        if st is None:  # a path that cannot be stat'ed is left out of the map
            continue
        fingerprint.update(f"{rel}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    fingerprint.update(f"{test_cmd}\0{max_chars}\0{depth}\0{sorted(focus)}".encode())
    cache_file = cache_dir / f"{fingerprint.hexdigest()}.md" if cache_dir else None
    if cache_file is not None and cache_file.is_file():
        return cache_file.read_text(), True
    text, cut = _render(repo, files, test_cmd=test_cmd, depth=depth, max_chars=max_chars, focus=focus)
    if cut:
        text += TRUNCATED
    if len(text) > max_chars:  # a guard: only when the header and directory lines alone are over the cap
        text = text[: max_chars - len(TRUNCATED)].rstrip() + TRUNCATED
    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(text)
    return text, False
