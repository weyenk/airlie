"""Layer 5: web tools wired into role registries, doctor, and airlie.yaml.

AC1: a config granting research web tools loads, validates, and the built registry has them.
AC12: doctor validates the searxng config and the constructed web_search tool carries the base URL from
config, not from any agent-supplied argument.
Logging: a run through the wired tool appends a JSON line to the run dir with role, query, status.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from airlie.config import load_config
from airlie.doctor import run_checks
from airlie.topology.studio import StudioOrchestrator


def _write_config(tmp_path: Path, research_tools: list[str] | None, web: dict | None = None) -> Path:
    roles = {
        "coder": {"prompt": "prompts/roles/coder.md", "model": "team"},
        "summarizer": {"prompt": "prompts/roles/summarizer.md", "model": "team"},
        "judge": {"prompt": "prompts/roles/judge.md", "model": "team"},
        "aggregator": {"prompt": "prompts/roles/aggregator.md", "model": "team"},
        "research": {"prompt": "prompts/roles/research.md", "model": "team"},
    }
    if research_tools is not None:
        roles["research"]["tools"] = research_tools
    data = {
        "version": 1,
        "engines": {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]}},
        "models": {"team": {"repo": "mlx-community/Qwen3.8-27B-4bit", "revision": "abc123"}},
        "roles": roles,
    }
    if web is not None:
        data["web"] = web
    path = tmp_path / "airlie.yaml"
    path.write_text(yaml.safe_dump(data))
    (tmp_path / "prompts" / "roles").mkdir(parents=True)
    for name in roles:
        (tmp_path / "prompts" / "roles" / f"{name}.md").write_text(f"# {name}")
    return path


class _FakeEngine:
    def __init__(self) -> None:
        self.engine = None

    async def generate(self, req):  # never called in these tests
        raise AssertionError("engine must not be called")


class _RunDir:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)


def _orchestrator(tmp_path: Path, cfg) -> StudioOrchestrator:
    orch = StudioOrchestrator.__new__(StudioOrchestrator)
    orch.cfg = cfg
    orch.worktree = tmp_path
    orch.test_cmd = None
    orch.test_steps = ()
    orch.scope_base = ""
    from airlie.tools.scope import GateMemo

    orch.gate_memo = GateMemo()
    orch.run_dir = _RunDir(tmp_path / "run")
    return orch


class _FakeProbe:
    def sysctl_int(self, name: str):
        return None

    def total_ram_bytes(self) -> int:
        return 64 * 2**30

    def model_snapshot_dir(self, repo: str, revision: str):
        return None

    def tool_version(self, name: str):
        return "present"

    def model_config(self, repo: str, revision: str):
        return None

    def http_ok(self, url: str) -> bool:
        return False

    def resolve_binary(self, name: str):
        return None

    def file_contains(self, path: Path, needle: str):
        return None

    def model_file(self, repo: str, revision: str, file: str):
        return None


def test_research_web_tools_load_validate_and_build(tmp_path: Path) -> None:
    """AC1: research granted web tools loads, validate_references is clean, and the registry has them."""
    path = _write_config(tmp_path, ["web_search", "web_fetch"])
    cfg = load_config(path)
    assert cfg.validate_references() == []
    orch = _orchestrator(tmp_path, cfg)
    reg = orch._tools(cfg.roles["research"], None)
    names = reg.names()
    assert "web_search" in names
    assert "web_fetch" in names


def test_doctor_validates_searxng_and_tool_carries_config_url(tmp_path: Path) -> None:
    """AC12: doctor exercises the web section; the web_search tool takes its base URL from config."""
    path = _write_config(
        tmp_path,
        ["web_search", "web_fetch"],
        web={"search": {"kind": "searxng", "url": "http://127.0.0.1:8888"}},
    )
    cfg = load_config(path)
    results = run_checks(cfg, _FakeProbe())
    assert any(r.name == "web:search" for r in results)

    orch = _orchestrator(tmp_path, cfg)
    reg = orch._tools(cfg.roles["research"], None)
    tool = reg._tools["web_search"]
    assert tool._provider._base_url == "http://127.0.0.1:8888"


def test_wired_web_search_logs_json_line_to_run_dir(tmp_path: Path) -> None:
    """A run through the wired tool appends a JSON line with role, query, status to the run dir."""
    path = _write_config(tmp_path, ["web_search"])
    cfg = load_config(path)
    orch = _orchestrator(tmp_path, cfg)
    reg = orch._tools(cfg.roles["research"], None, "research")
    tool = reg._tools["web_search"]
    ctx = reg.ctx
    assert ctx.run_dir is not None and ctx.role == "research"

    async def _run() -> None:
        await tool.run({"query": "qwen3 benchmarks"}, ctx)

    asyncio.run(_run())
    lines = (tmp_path / "run" / "web_search.jsonl").read_text().splitlines()
    rec = json.loads(lines[-1])
    assert rec["role"] == "research"
    assert rec["query"] == "qwen3 benchmarks"
    assert rec["status"] in ("ok", "error")


def test_repo_airlie_yaml_grants_research_web_tools() -> None:
    """The committed airlie.yaml has a web section and grants research the web tools."""
    root = Path(__file__).resolve().parents[2]
    cfg = load_config(root / "airlie.yaml", local_path=root / "no-such-local.yaml")
    assert cfg.web.search.kind == "searxng"
    assert cfg.web.search.url == "http://127.0.0.1:8888"
    assert set(cfg.roles["research"].tools or []) >= {"web_search", "web_fetch"}
    assert cfg.validate_references() == []
