"""Shared helpers extracted from tests/unit/test_config.py for tests.support.config."""

from __future__ import annotations

from pathlib import Path

import yaml

MINIMAL: dict = {
    "version": 1,
    "engines": {"main": {"kind": "http", "base_url": "http://127.0.0.1:8080/v1", "models": ["team"]}},
    "models": {"team": {}},
    "roles": {
        "architect": {"prompt": "prompts/roles/architect.md", "model": "team"},
        "summarizer": {"prompt": "prompts/roles/summarizer.md", "model": "team"},
        "judge": {"prompt": "prompts/roles/judge.md", "model": "team"},
        "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
    },
    "teams": {"role": "architect"},
    "chat": {"task": {"default_role": "architect", "roles": ["architect"]}},
}


def with_architect(**architect: object) -> dict:
    """MINIMAL with patched ``architect`` fields (thinking-budget and similar tests)."""
    return {
        **MINIMAL,
        "roles": {**MINIMAL["roles"], "architect": {**MINIMAL["roles"]["architect"], **architect}},
    }


def write(tmp_path: Path, data: dict, name: str = "airlie.yaml") -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data))
    for role in data.get("roles", {}).values():
        (tmp_path / role["prompt"]).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / role["prompt"]).write_text("# prompt\n")
    return p
