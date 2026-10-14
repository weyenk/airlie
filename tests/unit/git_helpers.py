"""git for tests: run it in a directory and return its stripped stdout (CalledProcessError when it fails)."""

import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()
