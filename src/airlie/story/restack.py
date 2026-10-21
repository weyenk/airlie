"""A stopped story's built layers, moved onto the trunk as it is now (`task story:restack`). A story builds on
the commit it started from for its whole life (StoryPipeline._base), so a fix that lands on the trunk while it
is stopped never reaches it: its next layer would be built, and held to the gate, on the old code (AIR-9 layer
02, 2026-10-21: an existing test fixed on the trunk still failed on the story's base, and the coder patched
production code around it). Each layer's one commit is replayed in order on a detached checkout of the new
trunk and the story's gate run on it, confined as the studio runs it; only when every layer passes is anything
moved (StoryPipeline.restack). The checkout is removed whatever happens."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from airlie.teams.worktree import add_worktree, cherry_pick, head_sha, remove_worktree
from airlie.tools.protocol import ToolContext
from airlie.tools.shell import run_cmd

_TAIL_CHARS = 1_500
"""How much of the end of a gate that failed the error keeps."""


class RestackError(Exception):
    """The story cannot be restacked; nothing was changed."""


async def replay(
    repo: Path,
    tree: Path,
    base: str,
    layers: Mapping[str, str],
    *,
    gate: str,
    shown: str,
    timeout_s: float,
    output_chars: int,
    confinement: dict[str, Any],
) -> dict[str, str]:
    """Each of `layers` ({"01": its commit, ...}, bottom first) replayed on `base` in a detached checkout at
    `tree`, and `gate` run there after each: the new commit of each layer. A conflict or a red gate is a
    RestackError that names the layer. `tree` is removed before (one a killed run left) and after."""
    remove_worktree(repo, tree)
    add_worktree(repo, tree, branch=None, start_point=base)
    moved: dict[str, str] = {}
    try:
        for nn, commit in layers.items():
            clashes = cherry_pick(tree, commit)
            if clashes is not None:
                where = ", ".join(clashes)
                raise RestackError(f"layer {nn} conflicts with the trunk in {where}; nothing was changed")
            moved[nn] = head_sha(tree)
            ctx = ToolContext(tree, timeout_s, output_chars, gate, **confinement)
            result = await run_cmd(gate, ctx, shell=True)
            if not result.ok:
                tail = result.output.strip()[-_TAIL_CHARS:]
                raise RestackError(f"layer {nn} fails `{shown}` on {base[:12]}: {tail}\nnothing was changed")
    finally:
        remove_worktree(repo, tree)
    return moved
