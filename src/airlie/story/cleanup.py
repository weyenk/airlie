"""What a story leaves in a clone once its run is set aside: its local branches and gh stack's record of its
stack. The story bench clears a sandbox with these after every run (`airlie.bench.story_bench`), and a story
started over clears its own (`airlie.story.restart`)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path


def local_branches(clone: Path) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(clone), "branch", "--format=%(refname:short)"], capture_output=True, text=True
    )
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def prune_stale_stacks(clone: Path) -> int:
    """Drop gh stack's records of stacks whose branches are gone from the clone (.git/gh-stack): deleting a
    run's branches leaves its stack on record, and a later run whose branch has the same name is refused at
    publish as "already part of a stack" (A/B run 3, 2026-10-29). How many stacks were dropped; nothing is
    done when the file is missing or unreadable."""
    path = clone / ".git" / "gh-stack"
    try:
        data = json.loads(path.read_text())
        stacks = data["stacks"]
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    local = local_branches(clone)
    kept = [s for s in stacks if all(b.get("branch") in local for b in s.get("branches") or [])]
    if len(kept) != len(stacks):
        path.write_text(json.dumps({**data, "stacks": kept}, indent=2) + "\n")
    return len(stacks) - len(kept)


def draft_ref(story: str, nn: str) -> str:
    """The ref that keeps story `story`'s drafted snapshot of layer `nn` from git's garbage collection (the
    tool-written packages design, 2026-11-06): the commit a later layer's draft was made on, which its
    rebase reads."""
    return f"refs/airlie/{story}/draft-{nn}"


def drop_draft_refs(clone: Path, story: str) -> int:
    """Delete story `story`'s draft refs (`draft_ref`) from the clone: once it publishes, or is set aside or
    started over. How many were deleted."""
    return len(
        _drop_refs(clone, f"refs/airlie/{story}/", lambda ref: ref.startswith(f"refs/airlie/{story}/draft-"))
    )


def drop_every_draft_ref(clone: Path) -> list[str]:
    """Delete every story's draft refs from the clone, and return them: the story bench clears its sandbox
    with this after every run, whatever story the run was (review of PR #251, 2026-11-06)."""
    return _drop_refs(clone, "refs/airlie/", lambda ref: ref.rpartition("/")[2].startswith("draft-"))


def _drop_refs(clone: Path, prefix: str, wanted: Callable[[str], bool]) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(clone), "for-each-ref", "--format=%(refname)", prefix],
        capture_output=True,
        text=True,
    )
    refs = [r for r in out.stdout.splitlines() if wanted(r)]
    for ref in refs:
        subprocess.run(["git", "-C", str(clone), "update-ref", "-d", ref], capture_output=True)
    return refs
