"""M6: HAL fed from engine accepted-draft counters; draft budget sent only when the engine supports it."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml

from airlie.config import RoleSpec, Sampling, load_config
from airlie.engine import EngineCapabilities, FakeEngine, FakeReply, GenRequest, Message, ToolCall
from airlie.engine.http_engine import HTTPEngine
from airlie.orchestrator import ParallelAgentTeamOrchestrator

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "vllm_mlx"


def role_spec(tmp_path: Path) -> RoleSpec:
    (tmp_path / "p.md").write_text("x")
    return RoleSpec(prompt=tmp_path / "p.md", model="base", thinking=False, max_tokens=8)


def test_http_engine_reads_mtp_counters_from_generation_metadata(tmp_path: Path) -> None:
    import asyncio

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        body = (
            b'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2},'
            b'"generation_metadata":{"mtp_drafts":2,"mtp_accepted":1}}\n\ndata: [DONE]\n\n'
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    eng = HTTPEngine(
        "http://e/v1", lambda r: "base", lambda r: role_spec(tmp_path), transport=httpx.MockTransport(handler)
    )
    req = GenRequest(
        role="coder",
        messages=[Message(role="user", content="hi")],
        sampling=Sampling(),
        max_tokens=8,
        stop=[],
        draft_budget=5,
    )
    res = asyncio.run(eng.generate(req))
    assert res.accepted_draft_tokens == 1 and res.draft_tokens == 2
    assert "num_draft_tokens" not in seen[0]  # vllm-mlx has no per-request draft control


def test_http_engine_sends_draft_budget_when_capable(tmp_path: Path) -> None:
    import asyncio

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, content=(FIX / "plain.sse").read_bytes(), headers={"content-type": "text/event-stream"}
        )

    eng = HTTPEngine(
        "http://e/v1",
        lambda r: "base",
        lambda r: role_spec(tmp_path),
        transport=httpx.MockTransport(handler),
        per_request_draft=True,
    )
    assert eng.capabilities.per_request_draft is True
    req = GenRequest(
        role="coder",
        messages=[Message(role="user", content="hi")],
        sampling=Sampling(),
        max_tokens=8,
        stop=[],
        draft_budget=5,
    )
    asyncio.run(eng.generate(req))
    assert seen[0]["num_draft_tokens"] == 5


async def test_orchestrator_updates_hal_from_steps_and_emits_budgets(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (cfg_dir / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base", "thinking": False}
            for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "teams": {"k": 1, "orchestrator": "native", "max_steps": 5, "stop_on_first_pass": False},
        "tools": {"builtin": ["write_file"]},
        "lts": {"enabled": False},
        "aggregator": {"kind": "test_score"},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (cfg_dir / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(cfg_dir / "airlie.yaml")
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(
                    tool_calls=[ToolCall("a", "write_file", json.dumps({"path": "a", "content": "1"}))],
                    accepted_draft_tokens=12,
                    draft_tokens=16,
                ),
                FakeReply(text="done"),
            ]
        }
    )
    orch = ParallelAgentTeamOrchestrator(cfg, engine, run_id="r12")
    await orch.execute_task("goal", repo=repo, test_cmd="true")
    assert orch.hal.hal_history["team-1"] > 4.0  # EMA moved up from the prior 4.0 after accepting 12
    events = [
        json.loads(x) for x in (tmp_path / "state" / "runs" / "r12" / "events.jsonl").read_text().splitlines()
    ]
    hal = [e for e in events if e["kind"] == "hal"]
    assert (
        hal
        and hal[0]["team_id"] == "team-1"
        and hal[0]["accepted"] == 12
        and hal[0]["budgets"]["team-1"] >= 1
    )
    assert (
        EngineCapabilities(
            batching=True, prefix_cache=True, logprobs=False, native_tools=True
        ).per_request_draft
        is False
    )
