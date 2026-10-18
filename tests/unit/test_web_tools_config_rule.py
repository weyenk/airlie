"""Config rule: web tools cannot be combined with mutating tools (story layer 4)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from airlie.config import ConfigError, load_config

MINIMAL: dict = {
    "version": 1,
    "engines": {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]}},
    "models": {"team": {"repo": "mlx-community/Qwen3.8-27B-4bit", "revision": "abc123"}},
    "roles": {
        "coder": {"prompt": "prompts/roles/coder.md", "model": "team"},
        "summarizer": {"prompt": "prompts/roles/summarizer.md", "model": "team"},
        "judge": {"prompt": "prompts/roles/judge.md", "model": "team"},
        "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
    },
}


def write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "airlie.yaml"
    p.write_text(yaml.safe_dump(data))
    for role in data.get("roles", {}).values():
        (tmp_path / role["prompt"]).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / role["prompt"]).write_text("# prompt\n")
    return p


def test_web_tools_with_a_mutating_tool_are_refused(tmp_path: Path) -> None:
    """AC2: a role with web tools plus a mutating tool fails load_config, naming the role."""
    data = {
        **MINIMAL,
        "roles": {
            **MINIMAL["roles"],
            "coder": {
                "prompt": "prompts/roles/coder.md",
                "model": "team",
                "tools": ["read_file", "write_file", "web_search", "web_fetch"],
            },
        },
    }
    with pytest.raises(ConfigError) as ei:
        load_config(write(tmp_path, data))
    msg = str(ei.value)
    assert "web tools cannot be combined with mutating tools" in msg
    assert "coder" in msg


def test_web_tools_inherited_from_builtin_are_refused(tmp_path: Path) -> None:
    """Counter-test: the rule looks at the role's *effective* tools, so a role inheriting web tools from
    tools.builtin alongside its mutating builtin tools is refused too."""
    data = {
        **MINIMAL,
        "tools": {
            "builtin": ["read_file", "write_file", "web_search", "web_fetch"],
        },
    }
    with pytest.raises(ConfigError) as ei:
        load_config(write(tmp_path, data))
    msg = str(ei.value)
    assert "web tools cannot be combined with mutating tools" in msg
    assert "coder" in msg
