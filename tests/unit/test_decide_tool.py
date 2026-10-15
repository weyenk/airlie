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


ARGS = {"question": "severity", "state": "qty == 0", "options": {"a": "x", "b": "y"}}


def _tool_config(tmp_path: Path, **router):  # type: ignore[no-untyped-def]
    import yaml

    from airlie.config import load_config

    (tmp_path / "p.md").write_text("x")
    cfg = {
        "version": 1,
        "engines": {
            "main": {"kind": "vllm_mlx", "port": 8080, "models": ["team"]},
            "decision": {"kind": "llama_server", "port": 8081, "models": ["kev"]},
        },
        "models": {
            "team": {"repo": "org/t", "revision": "r"},
            "kev": {"repo": "ggml-org/Kev-4B-GGUF", "revision": "d9", "file": "k.gguf"},
            "loose": {"repo": "org/l", "revision": "r"},
        },
        "roles": {
            r: {"prompt": "p.md", "model": "team"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "router": router,
    }
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(cfg))
    return load_config(tmp_path / "airlie.yaml")


async def test_decide_rejects_non_dict_options_with_the_exact_message(tmp_path: Path) -> None:
    handler, seen = systemone({"a": 1.0}, 1.0)
    for options in (None, ["a", "b"], "a,b", {}):
        res = await decide(handler).run({**ARGS, "options": options}, ToolContext(worktree=tmp_path))
        assert res.ok is False
        assert res.output == "error: decide needs at least two options ({name: criterion})"
    assert seen == []


async def test_decide_sends_state_question_instruction_and_stringified_criteria(tmp_path: Path) -> None:
    handler, seen = systemone({"1": 0.7, "b": 0.3}, 0.9)
    await decide(handler).run(
        {"question": "severity", "state": "qty == 0", "options": {1: 2, "b": "y"}},
        ToolContext(worktree=tmp_path),
    )
    body = seen[0]
    assert body["state"] == "qty == 0"
    assert body["questions"] == {
        "severity": {
            "type": "choice",
            "instructions": "Pick the option that fits best.",
            "criteria": {"1": "2", "b": "y"},
        }
    }


async def test_decide_stringifies_a_non_string_state(tmp_path: Path) -> None:
    handler, seen = systemone({"a": 0.7, "b": 0.3}, 0.9)
    await decide(handler).run({**ARGS, "state": 42}, ToolContext(worktree=tmp_path))
    assert seen[0]["state"] == "42"


async def test_decide_unavailable_message_carries_the_cause(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("kev is down")

    res = await decide(boom).run(ARGS, ToolContext(worktree=tmp_path))
    assert res.ok is False and res.output == "error: decision engine unavailable: kev is down"

    def bad_shape(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"answers": {}})

    res = await decide(bad_shape).run(ARGS, ToolContext(worktree=tmp_path))
    assert res.ok is False and res.output.startswith("error: decision engine unavailable: systemone answered")


async def test_decide_off_set_choice_with_no_in_set_probabilities_has_empty_choice(tmp_path: Path) -> None:
    handler, _ = systemone({"zzz": 1.0}, 0.9, choice="zzz")
    res = await decide(handler).run(ARGS, ToolContext(worktree=tmp_path))
    out = json.loads(res.output)
    assert res.ok and out["choice"] == "" and out["decision"] == "invalid"


async def test_decide_off_set_choice_picks_the_top_in_set_option(tmp_path: Path) -> None:
    handler, _ = systemone({"zzz": 0.6, "b": 0.3, "a": 0.1}, 0.9, choice="zzz")
    out = json.loads((await decide(handler).run(ARGS, ToolContext(worktree=tmp_path))).output)
    assert out["choice"] == "b" and out["decision"] == "invalid"


async def test_decide_log_record_carries_every_field(tmp_path: Path) -> None:
    from airlie.router import RoutingLog

    handler, _ = systemone({"a": 0.6, "b": 0.4}, 0.8, choice="chief")
    client = JevClient("http://k/v1", transport=httpx.MockTransport(handler))
    tool = Decide(client, ambiguous_gap=0.15, min_confidence=0.5, log=RoutingLog(tmp_path / "r.jsonl"))
    await tool.run(ARGS, ToolContext(worktree=tmp_path))
    rec = json.loads((tmp_path / "r.jsonl").read_text().splitlines()[0])
    assert rec["subtask"] == "decide:severity"
    assert rec["question"] == "severity"
    assert rec["choice"] == "chief" and rec["chosen"] == "a" and rec["decision"] == "invalid"
    assert rec["probabilities"] == {"a": 0.6, "b": 0.4}
    assert rec["confidence"] == 0.8
    assert rec["hint"] is None
    assert rec["input_tokens"] == 42


async def test_decide_logs_a_clear_decision_too(tmp_path: Path) -> None:
    from airlie.router import RoutingLog

    handler, _ = systemone({"a": 0.9, "b": 0.1}, 0.95)
    client = JevClient("http://k/v1", transport=httpx.MockTransport(handler))
    tool = Decide(client, ambiguous_gap=0.15, min_confidence=0.5, log=RoutingLog(tmp_path / "r.jsonl"))
    await tool.run(ARGS, ToolContext(worktree=tmp_path))
    rec = json.loads((tmp_path / "r.jsonl").read_text().splitlines()[0])
    assert (rec["choice"], rec["chosen"], rec["decision"]) == ("a", "a", "clear")


def test_attach_decide_declines_unusable_router_models(tmp_path: Path) -> None:
    from airlie.tools.decide import attach_decide

    reg = ToolRegistry([], ToolContext(worktree=tmp_path))
    # not in the catalog / in the catalog but served by no engine / served by a non-llama_server engine
    for model in ("ghost", "loose", "team"):
        cfg = _tool_config(tmp_path, kind="chief", model=model)
        assert attach_decide(reg, cfg) is False
    assert reg.names() == []


async def test_attach_decide_wires_thresholds_and_transport_from_the_config(tmp_path: Path) -> None:
    from airlie.tools.decide import attach_decide

    handler, seen = systemone({"a": 0.6, "b": 0.4}, 0.9)  # gap 0.2, confidence 0.9
    reg = ToolRegistry([], ToolContext(worktree=tmp_path))
    wide = _tool_config(tmp_path, kind="jev", model="kev", ambiguous_gap=0.3, min_confidence=0.5)
    assert attach_decide(reg, wide, transport=httpx.MockTransport(handler)) is True
    res = await reg.call(ToolCall(id="1", name="decide", arguments=json.dumps(ARGS)))
    assert json.loads(res.output)["decision"] == "ambiguous"
    assert seen[0]["model"] == "kev"

    strict = _tool_config(tmp_path, kind="jev", model="kev", ambiguous_gap=0.05, min_confidence=0.95)
    reg2 = ToolRegistry([], ToolContext(worktree=tmp_path))
    assert attach_decide(reg2, strict, transport=httpx.MockTransport(handler)) is True
    res = await reg2.call(ToolCall(id="2", name="decide", arguments=json.dumps(ARGS)))
    assert json.loads(res.output)["decision"] == "low-confidence"
