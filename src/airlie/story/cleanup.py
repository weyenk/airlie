"""What a story leaves in a clone once its run is set aside: its local branches and gh stack's record of its
stack. The story bench clears a sandbox with these after every run (`airlie.bench.story_bench`), and a story
started over clears its own (`airlie.story.restart`)."""

from __future__ import annotations

import json
import subprocess
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
