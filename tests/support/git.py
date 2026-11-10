"""git for tests, and the git repos/configs the fork tests build on."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from airlie.config import load_config


def git(cwd: Path, *args: str) -> str:
    """Run git in a directory and return its stripped stdout (CalledProcessError when it fails)."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def repo_with_marker_tests(tmp_path: Path) -> Path:
    """Tests pass only once a file named PASS exists in the worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    (repo / "check.sh").write_text(
        "#!/bin/sh\nif [ -f PASS ]; then echo '1 passed'; exit 0; fi\necho '1 failed'; exit 1\n"
    )
    os.chmod(repo / "check.sh", 0o755)
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", *env, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *env, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def cfg_for(tmp_path: Path, **fork: object):  # type: ignore[no-untyped-def]
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (cfg_dir / f"{r}.md").write_text(f"you are {r}")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base", "thinking": False}
            for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "teams": {"orchestrator": "native", "max_steps": 10},
        "tools": {"builtin": ["write_file", "run_tests"]},
        "topology": {"kind": "fork"},
        "fork": {
            "width": 2,
            "after_failures": 2,
            "child_steps": 4,
            "max_rounds": 2,
            "diversify": "temperature",
            **fork,
        },
        "repo_map": {"enabled": False},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (cfg_dir / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(cfg_dir / "airlie.yaml")
