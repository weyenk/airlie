"""Test web section configuration for SearXNG provider seam."""

from pathlib import Path

import yaml

from airlie.config import load_config


def test_web_section_with_searxng_backend_parses(tmp_path: Path) -> None:
    """Test that a config with web section and searxng backend parses correctly."""
    config_data = {
        "version": 1,
        "engines": {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]}},
        "models": {"team": {"repo": "mlx-community/Qwen3.8-27B-4bit", "revision": "abc123"}},
        "roles": {
            "coder": {"prompt": "prompts/roles/coder.md", "model": "team"},
            "summarizer": {"prompt": "prompts/roles/summarizer.md", "model": "team"},
            "judge": {"prompt": "prompts/roles/judge.md", "model": "team"},
            "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
            "research": {"prompt": "prompts/roles/research.md", "model": "team"},
        },
        "web": {"search": {"kind": "searxng", "url": "http://127.0.0.1:8888"}, "fetch_timeout_s": 45.0},
    }

    # Create the config file
    config_file = tmp_path / "airlie.yaml"
    config_file.write_text(yaml.safe_dump(config_data))

    # Create prompt files
    (tmp_path / "prompts/roles").mkdir(parents=True)
    (tmp_path / "prompts/roles/coder.md").write_text("# Coder Prompt")
    (tmp_path / "prompts/roles/summarizer.md").write_text("# Summarizer Prompt")
    (tmp_path / "prompts/roles/judge.md").write_text("# Judge Prompt")
    (tmp_path / "prompts/roles/aggregator.md").write_text("# Aggregator Prompt")
    (tmp_path / "prompts/roles/research.md").write_text("# Research Prompt")

    # This should not raise an exception
    cfg = load_config(config_file)

    # Verify the web configuration is parsed correctly
    assert cfg.web.search.kind == "searxng"
    assert cfg.web.search.url == "http://127.0.0.1:8888"
    assert cfg.web.fetch_timeout_s == 45.0


def test_web_section_defaults_when_not_present(tmp_path: Path) -> None:
    """Test that web section uses defaults when not present."""
    config_data = {
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

    # Create the config file
    config_file = tmp_path / "airlie.yaml"
    config_file.write_text(yaml.safe_dump(config_data))

    # Create prompt files
    (tmp_path / "prompts/roles").mkdir(parents=True)
    (tmp_path / "prompts/roles/coder.md").write_text("# Coder Prompt")
    (tmp_path / "prompts/roles/summarizer.md").write_text("# Summarizer Prompt")
    (tmp_path / "prompts/roles/judge.md").write_text("# Judge Prompt")
    (tmp_path / "prompts/roles/aggregator.md").write_text("# Aggregator Prompt")

    # This should not raise an exception
    cfg = load_config(config_file)

    # Verify the web configuration defaults
    assert cfg.web.search.kind == "searxng"
    assert cfg.web.search.url == "http://127.0.0.1:8888"
    assert cfg.web.fetch_timeout_s == 30.0


def test_web_search_and_fetch_tools_in_known_tools(tmp_path: Path) -> None:
    """Test that web_search and web_fetch are in KNOWN_TOOLS."""
    # This test verifies the tools are recognized as valid
    config_data = {
        "version": 1,
        "engines": {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]}},
        "models": {"team": {"repo": "mlx-community/Qwen3.8-27B-4bit", "revision": "abc123"}},
        "roles": {
            "coder": {"prompt": "prompts/roles/coder.md", "model": "team"},
            "summarizer": {"prompt": "prompts/roles/summarizer.md", "model": "team"},
            "judge": {"prompt": "prompts/roles/judge.md", "model": "team"},
            "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
            "research": {
                "prompt": "prompts/roles/research.md",
                "model": "team",
                "tools": ["web_search", "web_fetch"],
            },
        },
    }

    # Create the config file
    config_file = tmp_path / "airlie.yaml"
    config_file.write_text(yaml.safe_dump(config_data))

    # Create prompt files
    (tmp_path / "prompts/roles").mkdir(parents=True)
    (tmp_path / "prompts/roles/coder.md").write_text("# Coder Prompt")
    (tmp_path / "prompts/roles/summarizer.md").write_text("# Summarizer Prompt")
    (tmp_path / "prompts/roles/judge.md").write_text("# Judge Prompt")
    (tmp_path / "prompts/roles/aggregator.md").write_text("# Aggregator Prompt")
    (tmp_path / "prompts/roles/research.md").write_text("# Research Prompt")

    # This should not raise an exception
    cfg = load_config(config_file)

    # Verify the tools are recognized
    assert "web_search" in cfg.roles["research"].tools
    assert "web_fetch" in cfg.roles["research"].tools
