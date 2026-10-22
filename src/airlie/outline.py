"""Code outline: the defs and classes of a file with their line numbers.

Shared by the repo map (signatures without lines) and the brief's "read for you" block (outline with
lines), so a story role sees a file's shape without spending a step to read it. A file that is not Python,
or does not parse, gets its line count and head instead. No model call."""

from __future__ import annotations

import ast
from dataclasses import dataclass

INDENT = "    "


@dataclass(frozen=True)
class Entry:
    line: int
    depth: int  # 0 for top level, 1 for a method of a top-level class
    text: str  # `class Name`, `def name(args) -> ret`, or `async def ...`


def _function_text(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{keyword} {node.name}({ast.unparse(node.args)}){ret}"


def _entry(node: ast.stmt, depth: int) -> Entry | None:
    if isinstance(node, ast.ClassDef):
        return Entry(node.lineno, depth, f"class {node.name}")
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return Entry(node.lineno, depth, _function_text(node))
    return None


def outline_entries(source: str) -> list[Entry] | None:
    """Top-level defs and classes, and the methods of top-level classes. None when `source` does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    entries: list[Entry] = []
    for node in tree.body:
        top = _entry(node, 0)
        if top is None:
            continue
        entries.append(top)
        if isinstance(node, ast.ClassDef):
            entries.extend(e for child in node.body if (e := _entry(child, 1)) is not None)
    return entries


def _head(source: str, head_chars: int) -> str:
    """The first lines of `source` up to `head_chars`, cut on a line boundary."""
    kept: list[str] = []
    used = 0
    for line in source.splitlines():
        used += len(line) + (1 if kept else 0)
        if used > head_chars:
            break
        kept.append(line)
    return "\n".join(kept)


def outline(path: str, source: str, *, head_chars: int) -> str:
    header = f"{path}: {len(source.splitlines())} lines"
    entries = outline_entries(source) if path.endswith(".py") else None
    if entries is None:
        head = _head(source, head_chars)
        return f"{header}\n{head}" if head else header
    return "\n".join([header, *(f"{e.line}: {INDENT * e.depth}{e.text}" for e in entries)])
