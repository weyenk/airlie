"""Shared helpers extracted from tests/unit/test_config.py for tests.support.config."""

from __future__ import annotations

from pathlib import Path

import yaml

MINIMAL: dict = {
    "version": 1,
    "engines": {"main": {"kind": "mlx_vlm", "port": 8080, "models": ["team"]}},
    "models": {"team": {"repo": "mlx-community/Qwen3.8-27B-4bit", "revision": "abc123"}},
    "roles": {
        "coder": {"prompt": "prompts/roles/coder.md", "model": "team"},
        "summarizer": {"prompt": "prompts/roles/summarizer.md", "model": "team"},
        "judge": {"prompt": "prompts/roles/judge.md", "model": "team"},
        "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
    },
    "teams": {"role": "coder"},
    "chat": {"task": {"default_role": "coder", "roles": ["coder"]}},
}


def write(tmp_path: Path, data: dict, name: str = "airlie.yaml") -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data))
    for role in data.get("roles", {}).values():
        (tmp_path / role["prompt"]).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / role["prompt"]).write_text("# prompt\n")
    return p
