"""The checkout this airlie runs from, as a commit: `build_commit` for the start/serve events that record
which code ran."""

from __future__ import annotations

import subprocess
from pathlib import Path


def build_commit() -> str | None:
    """The short commit of the checkout this airlie runs from: which code ran it.
    None when the code is not in a git checkout (an installed copy) or git does not answer."""
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "-C", str(here), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None
