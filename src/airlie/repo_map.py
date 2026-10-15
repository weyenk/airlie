"""Deterministic repository map, injected before the goal so no branch re-discovers the layout (design §4).

Tracked files (git ls-files) to a depth, sizes, the test command, and for Python files the top-level def/class
signatures via ast. No model call. Cached per (path, mtime, size) of the tracked files."""

from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

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


def _signatures(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (SyntaxError, ValueError):
        return []
    sigs: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            sigs.append(f"class {node.name}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            sigs.append(f"def {node.name}({ast.unparse(node.args)}){ret}")
    return sigs


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def _render(repo: Path, files: list[Path], *, test_cmd: str | None, depth: int) -> str:
    lines = [HEADER]
    if test_cmd:
        lines.append(f"test command: {test_cmd}")
    shown_dirs: set[Path] = set()
    for rel in files:
        parts = rel.parts
        if len(parts) > depth:
            d = Path(*parts[:depth])
            if d not in shown_dirs:
                shown_dirs.add(d)
                lines.append(f"{d}/ …")
            continue
        full = repo / rel
        entry = f"{rel} ({_size(full.stat().st_size)})"
        if rel.suffix == ".py":
            sigs = _signatures(full)
            if sigs:
                entry += ": " + "; ".join(sigs)
        lines.append(entry)
    return "\n".join(lines)


def build_repo_map(
    repo: Path, *, test_cmd: str | None, max_chars: int, depth: int, cache_dir: Path | None
) -> tuple[str, bool]:
    """Returns (map text, served_from_cache)."""
    files = _tracked(repo)
    fingerprint = hashlib.sha256()
    for rel in files:
        st = (repo / rel).stat()
        fingerprint.update(f"{rel}\0{st.st_mtime_ns}\0{st.st_size}\n".encode())
    fingerprint.update(f"{test_cmd}\0{max_chars}\0{depth}".encode())
    cache_file = cache_dir / f"{fingerprint.hexdigest()}.md" if cache_dir else None
    if cache_file is not None and cache_file.is_file():
        return cache_file.read_text(), True
    text = _render(repo, files, test_cmd=test_cmd, depth=depth)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + TRUNCATED
    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(text)
    return text, False
