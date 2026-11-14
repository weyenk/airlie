"""Shared helpers extracted from tests/unit/test_studio_orchestrator.py for tests.support.studio."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from airlie.config import load_config
from airlie.engine import FakeReply, ToolCall

ROLES: dict[str, tuple[str, list[str] | None]] = {
    "chief": ("---\nsummary: no\nroutable: false\n---\n# chief\nJSON only.", []),
    "architect": (
        "---\nsummary: Implement or change code\n---\n# architect",
        ["read_file", "write_file", "edit_file", "list_dir", "grep", "shell", "run_tests", "git", "compute"],
    ),
    "qa": ("---\nsummary: Tests and edge cases\n---\n# qa", ["read_file", "write_file", "run_tests"]),
    "docs": ("---\nsummary: README and guides\n---\n# docs", ["read_file", "list_dir"]),
    "reviewer": ("---\nsummary: no\nroutable: false\n---\n# reviewer", []),
    "summarizer": ("# summarizer", None),
    "judge": ("# judge", None),
    "aggregator": ("# aggregator", None),
}


def cfg_for(
    tmp_path: Path,
    *,
    roles: dict[str, tuple[str, list[str] | None]] | None = None,
    extra: dict[str, object] | None = None,
    **studio: object,
):  # type: ignore[no-untyped-def]
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    role_cfg: dict[str, dict[str, object]] = {}
    for name, (text, tools) in (roles or ROLES).items():
        (d / f"{name}.md").write_text(text)
        role_cfg[name] = {"prompt": f"{name}.md", "model": "base", "thinking": False}
        if tools is not None:
            role_cfg[name]["tools"] = tools
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": role_cfg,
        "teams": {"orchestrator": "native", "max_steps": 6},
        "tools": {"builtin": ["read_file", "write_file", "list_dir", "run_tests"]},
        "topology": {"kind": "studio"},
        "router": {"kind": "chief"},
        "studio": {"parallel": 2, "review": {"enabled": True, "roles": ["architect"], "rounds": 1}, **studio},
        "repo_map": {"enabled": False},
        "observability": {"state_dir": str(tmp_path / "state")},
        **(extra or {}),
    }
    (d / "airlie.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False)
    )  # node order in a workflow is meaningful
    return load_config(d / "airlie.yaml")


PLAN = FakeReply(
    text=json.dumps(
        {
            "type": "plan",
            "assumptions": ["a1"],
            "subtasks": [
                {"id": "fix", "role": "architect", "brief": "make the check pass"},
                {"id": "note", "role": "docs", "brief": "describe the repo", "depends_on": ["fix"]},
            ],
        }
    )
)

SYNTH = FakeReply(text=json.dumps({"type": "synthesis", "answer": "all done", "open_questions": []}))

WRITE_PASS = FakeReply(
    tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "PASS", "content": "ok\n"}))]
)

RUN = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])

DONE = FakeReply(text="done")

APPROVE = FakeReply(text='{"verdict": "approve", "findings": []}')

CHANGES = FakeReply(
    text='{"verdict": "request_changes", "findings": [{"severity": "blocking", "file": "PASS", "line": 1, '
    '"claim": "wrong content", "evidence": "x"}]}'
)

WRITE_PASS = FakeReply(
    tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "PASS", "content": "ok\n"}))]
)
