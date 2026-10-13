"""decide: a calibrated closed-set choice from Kev, available to any role as a tool (design §14)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from airlie.engine import ToolCall
from airlie.router import JevClient
from airlie.tools import ToolContext, ToolRegistry
from airlie.tools.decide import Decide
from tests.unit.test_router import systemone


def decide(handler) -> Decide:  # type: ignore[no-untyped-def]
    client = JevClient("http://k/v1", transport=httpx.MockTransport(handler))
    return Decide(client, ambiguous_gap=0.15, min_confidence=0.5)


async def test_decide_returns_choice_probabilities_and_policy(tmp_path: Path) -> None:
    handler, seen = systemone({"blocking": 0.8, "minor": 0.2}, 0.9)
    reg = ToolRegistry([decide(handler)], ToolContext(worktree=tmp_path))
    args = {
        "question": "severity",
        "state": "qty == 0 reaches the division",
        "options": {"blocking": "breaks users", "minor": "cosmetic"},
    }
    res = await reg.call(ToolCall(id="c1", name="decide", arguments=json.dumps(args)))
    assert res.ok
    assert json.loads(res.output) == {
        "choice": "blocking",
        "decision": "clear",
        "confidence": 0.9,
        "probabilities": {"blocking": 0.8, "minor": 0.2},
    }
    assert seen[0]["questions"]["severity"]["criteria"] == args["options"]


async def test_decide_needs_at_least_two_options(tmp_path: Path) -> None:
    handler, _ = systemone({"a": 1.0}, 1.0)
    res = await decide(handler).run(
        {"question": "q", "state": "s", "options": {"a": "only"}}, ToolContext(worktree=tmp_path)
    )
    assert not res.ok and "two options" in res.output


async def test_decide_reports_a_down_decision_engine_as_an_observation(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    res = await decide(boom).run(
        {"question": "q", "state": "s", "options": {"a": "x", "b": "y"}}, ToolContext(worktree=tmp_path)
    )
    assert not res.ok and "decision engine" in res.output


def test_attach_decide_only_with_a_llama_server_router_engine(tmp_path: Path) -> None:
    import yaml

    from airlie.config import load_config
    from airlie.tools.decide import attach_decide

    (tmp_path / "p.md").write_text("x")
    base = {
        "version": 1,
        "engines": {"main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]}},
        "models": {"team": {"repo": "org/t", "revision": "r"}},
        "roles": {
            r: {"prompt": "p.md", "model": "team"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(base))
    reg = ToolRegistry([], ToolContext(worktree=tmp_path))
    assert attach_decide(reg, load_config(tmp_path / "airlie.yaml")) is False and reg.names() == []
    with_kev = {
        **base,
        "engines": {**base["engines"], "decision": {"kind": "llama_server", "port": 8081, "models": ["kev"]}},
        "models": {
            **base["models"],
            "kev": {"repo": "ggml-org/Kev-4B-GGUF", "revision": "d9", "file": "k.gguf"},
        },
        "router": {"kind": "jev", "model": "kev"},
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(with_kev))
    assert attach_decide(reg, load_config(tmp_path / "airlie.yaml")) is True and reg.names() == ["decide"]


async def test_decide_marks_an_off_set_choice_invalid_and_logs(tmp_path: Path) -> None:
    from airlie.router import RoutingLog

    handler, _ = systemone({"a": 0.6, "b": 0.4}, 0.9, choice="chief")
    client = JevClient("http://k/v1", transport=httpx.MockTransport(handler))
    log = RoutingLog(tmp_path / "routing.jsonl")
    tool = Decide(client, ambiguous_gap=0.15, min_confidence=0.5, log=log)
    res = await tool.run(
        {"question": "q", "state": "s", "options": {"a": "x", "b": "y"}}, ToolContext(worktree=tmp_path)
    )
    out = json.loads(res.output)
    assert res.ok and out["choice"] == "a" and out["decision"] == "invalid"
    rec = json.loads((tmp_path / "routing.jsonl").read_text().splitlines()[0])
    assert rec["subtask"] == "decide:q" and rec["decision"] == "invalid"
