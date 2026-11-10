"""Shared helpers extracted from tests/unit/test_serve_chat.py for tests.support.serve_chat."""

from __future__ import annotations

from pathlib import Path

import yaml

from airlie.config import load_config


def chat_cfg(tmp_path: Path, skills: dict[str, str] | None = None):
    skills = (
        {"story": "---\nsummary: Make a story\naction: story\n---\nSTORY BODY\n"}
        if skills is None
        else skills
    )
    (tmp_path / "skills").mkdir()
    for name, text in skills.items():
        (tmp_path / "skills" / f"{name}.md").write_text(text)
    for r, text in {
        "coder": "x",
        "summarizer": "x",
        "judge": "x",
        "aggregator": "x",
        "chief": "CHIEF PROMPT",
        "chat": "---\nsummary: chat\nroutable: false\n---\nCHAT PROMPT\n",
    }.items():
        (tmp_path / f"{r}.md").write_text(text)
    roles = {
        r: {"prompt": f"{r}.md", "model": "base"}
        for r in ("coder", "summarizer", "judge", "aggregator", "chief")
    }
    roles["chat"] = {"prompt": "chat.md", "model": "base", "skills": "skills"}
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://engine.test/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": roles,
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")
