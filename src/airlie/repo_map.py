"""Deterministic repository map, injected before the goal so no branch re-discovers the layout (design §4).

Tracked files (git ls-files) to a depth, sizes, the test command, and for Python files the top-level def/class
signatures via ast. No model call. Cached per (path, mtime, size) of the tracked files."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from airlie.outline import outline, outline_entries
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
SCOPE = "## In scope"


def _scope_block(repo: Path, focus: list[Path]) -> list[str]:
    """The focus files with their outline and line numbers, so a role sees the files it will change."""
    blocks: list[str] = []
    for rel in focus:
        try:
            source = (repo / rel).read_text(errors="replace")
        except OSError:  # a file this user cannot read
            continue
        blocks.append(outline(rel.as_posix(), source, head_chars=0))
    return [SCOPE, *blocks] if blocks else []


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


def _file_line(repo: Path, rel: Path, size: int) -> str:
    entry = f"{rel} ({_size(size)})"
    if rel.suffix == ".py":
        sigs = _signatures(repo / rel)
        if sigs:
            entry += ": " + "; ".join(sigs)
    return entry


def _dir_block(repo: Path, files: list[Path], sizes: dict[Path, int], depth: int) -> list[str]:
    """One top-level directory's file lines; what lies deeper than `depth` is one `dir/ …` line."""
    lines: list[str] = []
    shown_dirs: set[Path] = set()
    for rel in files:
        if len(rel.parts) > depth:
            d = Path(*rel.parts[:depth])
            if d not in shown_dirs:
                shown_dirs.add(d)
                lines.append(f"{d}/ …")
            continue
        lines.append(_file_line(repo, rel, sizes[rel]))
    return lines


def _render(
    repo: Path, files: list[Path], *, test_cmd: str | None, depth: int, max_chars: int, focus: Sequence[str]
) -> tuple[str, bool]:
    """(map text, whether it ran out of budget). The budget goes to the header and test command, the focus
    files, a line for every top-level directory, and then the directories expanded in `_rank` order."""
    sizes = {
        rel: st.st_size for rel in files if (st := _stat(repo / rel)) is not None
    }  # left out, as in the fingerprint
    tracked = [rel for rel in files if rel in sizes]
    wanted = {Path(f.removeprefix("./")) for f in focus}
    in_scope = [rel for rel in tracked if rel in wanted]
    lines = [HEADER]
    if test_cmd:
        lines.append(f"test command: {test_cmd}")
    lines += _scope_block(repo, in_scope)
    groups: dict[str, list[Path]] = {}
    for rel in tracked:
        groups.setdefault(rel.parts[0] if len(rel.parts) > 1 else "", []).append(rel)
    lines += [f"{top}/ ({len(group)} files)" for top, group in sorted(groups.items()) if top]
    used = len("\n".join(lines))
    order = sorted(groups.items(), key=lambda g: (_rank(g[0], g[1], set(in_scope)), g[0]))
    for _, group in order:
        block = _dir_block(repo, group, sizes, depth)
        used += 1 + len("\n".join(block))
        if used > max_chars:
            return "\n".join(lines), True
        lines += block
    return "\n".join(lines), False


def build_repo_map(
    repo: Path,
    *,
    test_cmd: str | None,
    max_chars: int,
    depth: int,
    cache_dir: Path | None,
    focus: Sequence[str] = (),
) -> tuple[str, bool]:
    """Returns (map text, served_from_cache). `focus` is the paths the role will work on: their outline comes
    first, and the directories that hold them are expanded before the rest."""
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
    if cut or len(text) > max_chars:
        text = text[:max_chars].rstrip() + TRUNCATED
    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(text)
    return text, False
