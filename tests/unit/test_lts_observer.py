"""LTSObserver: inject keys before each step; summarize, judge, admit after; orchestrator wiring."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from airlie.config import ControllerSpec, SummarizerSpec, load_config
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.lts import SharedMemoryBank
from airlie.lts.controller import build_controller
from airlie.lts.log import AdmissionLog
from airlie.lts.observer import LTSObserver
from airlie.lts.summarizer import build_summarizer
from airlie.obs import EventSink, RunDir
from airlie.orchestrator import ParallelAgentTeamOrchestrator
from airlie.teams import NullObserver, StepRecord


def make_observer(tmp_path: Path, max_keys: int = 32) -> tuple[LTSObserver, SharedMemoryBank]:
    rd = RunDir.create(tmp_path / "state", run_id="r1")
    bank = SharedMemoryBank(persist_path=rd.path / "memory.jsonl")
    obs = LTSObserver(
        bank=bank,
        summarizer=build_summarizer(SummarizerSpec(kind="truncate", truncate_chars=40)),
        controller=build_controller(ControllerSpec(kind="heuristic")),
        log=AdmissionLog(rd.path / "lts"),
        sink=EventSink(rd.events_path, "r1"),
        run_id="r1",
        query="implement it",
        max_keys=max_keys,
        inner=NullObserver(),
    )
    return obs, bank


def rec(team: str, n: int, tool: str, out: str, ok: bool = True) -> StepRecord:
    return StepRecord(team_id=team, step_num=n, u_t=ToolCall(f"{team}-{n}", tool, "{}"), o_t=out, ok=ok)


async def test_steps_are_admitted_and_keys_injected_for_peers(tmp_path: Path) -> None:
    obs, bank = make_observer(tmp_path)
    assert await obs.before_step("team-1", 1) is None  # nothing shared yet
    await obs.after_step(rec("team-1", 1, "run_tests", "FAILED test_a: expected 3 got 2"))
    await obs.after_step(rec("team-1", 2, "read_file", "1: x = 1"))  # private tool class → not admitted
    assert len(bank) == 1
    text = await obs.before_step("team-2", 1)
    assert text is not None and text.startswith("\n[Shared Memory Keys Available]:\n- Key [")
    assert "FAILED test_a" in text
    adm = [
        json.loads(x)
        for x in (tmp_path / "state" / "runs" / "r1" / "lts" / "admission.jsonl").read_text().splitlines()
    ]
    assert [a["decision"] for a in adm] == [True, False]
    assert adm[0]["entry_id"] and adm[1]["entry_id"] is None
    assert adm[0]["controller"] == "heuristic"


async def test_injection_is_bounded(tmp_path: Path) -> None:
    obs, bank = make_observer(tmp_path, max_keys=2)
    outputs = [
        "apple pie recipe found",
        "git status clean tree",
        "pytest three passed",
        "lint warnings none",
        "build ok",
    ]
    for i, out in enumerate(outputs, 1):
        await obs.after_step(rec("team-1", i, "shell", out))
    text = await obs.before_step("team-2", 1)
    assert text and text.count("- Key [") == 2


async def test_orchestrator_wires_lts(tmp_path: Path) -> None:
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
        (cfg_dir / f"{r}.md").write_text(f"you are {r}")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base", "thinking": False}
            for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "teams": {"k": 2, "orchestrator": "native", "max_steps": 5},
        "tools": {"builtin": ["shell"]},
        "lts": {"controller": {"kind": "always"}, "summarizer": {"kind": "truncate"}},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (cfg_dir / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(cfg_dir / "airlie.yaml")
    engine = FakeEngine(
        {
            "coder": [
                FakeReply(tool_calls=[ToolCall("a", "shell", json.dumps({"cmd": "echo one"}))]),
                FakeReply(tool_calls=[ToolCall("b", "retrieve_value", json.dumps({"entry_id": "nope"}))]),
                FakeReply(text="done 1"),
                FakeReply(text="done 2"),
            ]
        }
    )
    orch = ParallelAgentTeamOrchestrator(cfg, engine, run_id="r2")
    report = await orch.execute_task("goal", repo=repo, test_cmd="true")
    assert report.any_ok
    assert [t.name for t in engine.requests[0].tools or []] == ["shell", "retrieve_value"]
    events = [json.loads(x) for x in report.run_dir.events_path.read_text().splitlines()]
    kinds = [e["kind"] for e in events]
    assert "admission" in kinds and "retrieval" in kinds
    assert (report.run_dir.path / "memory.jsonl").is_file()
    assert (report.run_dir.path / "lts" / "admission.jsonl").is_file()
    # a later turn of some team saw the injected keys
    injected = [
        m for r in engine.requests for m in r.messages if "[Shared Memory Keys Available]" in m.content
    ]
    assert injected
    orch.finalize_rewards({"team-1": 1.0, "team-2": 0.0})
    adm = [json.loads(x) for x in (report.run_dir.path / "lts" / "admission.jsonl").read_text().splitlines()]
    assert all(a["episode_reward"] is not None for a in adm)


async def test_run_step_blueprint(tmp_path: Path) -> None:
    """Spec 04 §3: run_step injects keys, generates, evaluates admission; execute_task-free entry point."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    for r in ("coder", "summarizer", "judge", "aggregator"):
        (cfg_dir / f"{r}.md").write_text("x")
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": {
            r: {"prompt": f"{r}.md", "model": "base"} for r in ("coder", "summarizer", "judge", "aggregator")
        },
        "lts": {"controller": {"kind": "always"}, "summarizer": {"kind": "truncate"}},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (cfg_dir / "airlie.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(cfg_dir / "airlie.yaml")
    engine = FakeEngine(
        {"coder": [FakeReply(text="Parsed the CSV into a DataFrame"), FakeReply(text="second")]}
    )
    orch = ParallelAgentTeamOrchestrator(cfg, engine, run_id="r3")
    out = await orch.run_step("team-1", "parse the csv", [])
    assert out == "Parsed the CSV into a DataFrame"
    assert len(orch.memory_bank) == 1
    assert orch.evaluate_lts_admission("q", "summary", "output") is True
    await orch.run_step("team-2", "next", [])
    assert any("[Shared Memory Keys Available]" in m.content for m in engine.requests[1].messages)
