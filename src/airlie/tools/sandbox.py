"""Path and output discipline shared by builtin tools."""

from __future__ import annotations

from pathlib import Path

from airlie.tools.protocol import ToolContext

SECRET_PREFIXES = (".env", "id_rsa", "id_ed25519", "id_ecdsa")
SECRET_SUFFIXES = (".pem", ".key", ".p12")


def secret_like(name: str) -> bool:
    n = name.lower()
    return n.startswith(SECRET_PREFIXES) or n.endswith(SECRET_SUFFIXES)


def resolve(ctx: ToolContext, path: str) -> Path:
    """Map a tool-supplied path to a file inside the worktree, or raise PermissionError."""
    if secret_like(Path(path).name):
        raise PermissionError(f"refusing to touch a likely secret file: {path}")
    root = ctx.worktree.resolve()
    # `root / "/abs"` is "/abs", and resolve() follows every symlink, so one containment check covers
    # absolute paths, `..` and symlink escapes alike
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        raise PermissionError(f"path escapes the worktree: {path}")
    return candidate


def cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"
