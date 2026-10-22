"""Fork-when-stuck: trunk forks after two failing run_tests; children share state and tree; first pass
wins."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from airlie.config import load_config
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.orchestrator import ParallelAgentTeamOrchestrator
from airlie.teams.protocol import TeamResult
from airlie.topology.fork import ForkOrchestrator
from airlie.topology.protocol import build_topology
from tests.unit.proc_helpers import StartGate, cancel_phases, eventually, running, unique_sleep


def repo_with_marker_tests(tmp_path: Path) -> Path:
    """Tests pass only once a file named PASS exists in the worktree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hi\n")
    (repo / "check.sh").write_text(
        "#!/bin/sh\nif [ -f PASS ]; then echo '1 passed'; exit 0; fi\necho '1 failed'; exit 1\n"
    )
    os.chmod(repo / "check.sh", 0o755)
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", *env, "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", *env, "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def cfg_for(tmp_path: Path, **fork: object):  # type: ignore[no-untyped-def]
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(exist_ok=True)
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
        "teams": {"orchestrator": "native", "max_steps": 10},
        "tools": {"builtin": ["write_file", "run_tests"]},
        "topology": {"kind": "fork"},
        "fork": {
            "width": 2,
            "after_failures": 2,
            "child_steps": 4,
            "max_rounds": 2,
            "diversify": "temperature",
            **fork,
        },
        "repo_map": {"enabled": False},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (cfg_dir / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(cfg_dir / "airlie.yaml")


RUN = FakeReply(tool_calls=[ToolCall("t", "run_tests", "{}")])
WRITE_PASS = FakeReply(
    tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "PASS", "content": "ok\n"}))]
)
WRITE_NOTE = FakeReply(
    tool_calls=[ToolCall("n", "write_file", json.dumps({"path": "note.txt", "content": "trunk\n"}))]
)


def events(report):  # type: ignore[no-untyped-def]
    return [json.loads(x) for x in report.run_dir.events_path.read_text().splitlines()]


async def test_a_trunk_whose_reply_is_cut_off_ends_the_run_with_why_instead_of_forking(
    tmp_path: Path,
) -> None:
    """HDS-3 (2026-10-15): a reply cut off at max_tokens is not "done", and it is not stuck: more branches
    would run into the same cap. The run ends, unsuccessfully, saying what to raise."""
    engine = FakeEngine(
        {"coder": [WRITE_NOTE, FakeReply(reasoning="Thinking, still. ", finish_reason="length")] + [RUN] * 6}
    )
    orch = ForkOrchestrator(cfg_for(tmp_path), engine, run_id="f9")
    report = await orch.execute_task(
        "make check.sh pass", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh"
    )
    [trunk] = report.teams
    assert (trunk.team_id, trunk.ok, trunk.stop_reason, trunk.steps) == ("trunk", False, "cut_off", 1)
    assert not report.any_ok and "the reply was cut off at max_tokens" in report.final_answer
    assert not [e for e in events(report) if e["kind"] == "fork_point"] and len(engine.requests) == 2
    assert [e["stop_reason"] for e in events(report) if e["kind"] == "branch_done"] == ["cut_off"]


async def test_trunk_forks_after_two_failures_and_passing_child_wins(tmp_path: Path) -> None:
    # Replies are consumed in request order: trunk (note, run, run) → fork → children interleave.
    # One child writes PASS and runs the tests (pass) and finishes; the other only runs failing tests
    # until it is cancelled.
    engine = FakeEngine(
        {
            "coder": [
                WRITE_NOTE,
                RUN,
                RUN,  # trunk: note written, two failures → fork point
                WRITE_PASS,  # first child request
                RUN,  # second child request (fails)
                RUN,  # first child again (passes) → sibling cancelled
                FakeReply(text="fixed it"),  # first child final
                RUN,
                RUN,
                RUN,  # spare replies if the sibling is still running
            ]
        }
    )
    orch = ForkOrchestrator(cfg_for(tmp_path), engine, run_id="f1")
    report = await orch.execute_task(
        "make check.sh pass", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh"
    )
    ids = [t.team_id for t in report.teams]
    assert ids[0] == "trunk" and set(ids[1:]) == {"r1-1", "r1-2"}
    trunk = report.teams[0]
    assert trunk.stop_reason == "fork_point" and trunk.steps == 3
    assert report.winner in {"r1-1", "r1-2"}
    assert (orch.run_dir.path / report.winner / "PASS").exists()
    assert report.winning_branch == f"airlie/f1/{report.winner}"
    # FakeEngine hands replies out in request order, so the sibling may consume a reply before its
    # cancellation lands. Either way the winner passed and is reported ok, and the sibling never passed.
    assert report.any_ok and next(t for t in report.teams if t.team_id == report.winner).ok
    assert (orch.run_dir.path / "r1-1" / "note.txt").read_text() == "trunk\n"  # child from the WIP commit
    assert (orch.run_dir.path / "r1-2" / "note.txt").read_text() == "trunk\n"
    ev = events(report)
    kinds = [e["kind"] for e in ev]
    assert kinds.count("fork_point") == 1 and kinds.count("fork_resolved") == 1
    fp = next(e for e in ev if e["kind"] == "fork_point")
    assert fp["round"] == 1 and fp["parent"] == "trunk" and fp["children"] == ["r1-1", "r1-2"]
    assert "1 failed" in fp["test_tail"]
    fr = next(e for e in ev if e["kind"] == "fork_resolved")
    assert fr["winner"] == report.winner and fr["how"] == "visible_pass"
    done = next(e for e in ev if e["kind"] == "run_done")
    assert done["forks"] == 1 and done["rounds"] == 1 and done["ok"] is True
    # a child resumed the trunk's conversation: its first request carries the history plus a nudge
    child_first = next(r for r in engine.requests[3:] if "You are now branch" in r.messages[-1].content)
    assert [m.role for m in child_first.messages[:2]] == ["system", "user"]
    assert len(child_first.messages) == 9  # system, goal, 3×(assistant, tool), nudge
    assert (orch.run_dir.path / "report.md").is_file()
    loser = next(t for t in report.teams if t.team_id not in ("trunk", report.winner))
    assert not orch.observer.passed.get(loser.team_id, False)


async def test_no_child_passes_best_effort_then_max_rounds(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [RUN] * 40})  # everyone only ever runs failing tests
    orch = ForkOrchestrator(cfg_for(tmp_path, child_steps=2, max_rounds=2), engine, run_id="f2")
    report = await orch.execute_task("goal", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh")
    ev = events(report)
    resolved = [e for e in ev if e["kind"] == "fork_resolved"]
    assert [e["how"] for e in resolved] == ["best_effort", "best_effort"]
    assert sum(1 for e in ev if e["kind"] == "fork_point") == 2
    assert report.winner in {"r2-1", "r2-2"} and not report.any_ok
    assert {t.team_id for t in report.teams} == {"trunk", "r1-1", "r1-2", "r2-1", "r2-2"}


async def test_resolve_uses_last_run_tests_outcome(tmp_path: Path) -> None:
    """A child that passed once and then broke the tests is not the winner."""
    from airlie.obs import EventSink
    from airlie.teams import NullObserver
    from airlie.teams.protocol import StepRecord
    from airlie.topology.fork import ForkObserver

    obs = ForkObserver(NullObserver(), EventSink(tmp_path / "e.jsonl", "r"), after_failures=2)
    ok = StepRecord("r1-1", 1, ToolCall("a", "run_tests", "{}"), "1 passed", True)
    bad = StepRecord("r1-1", 2, ToolCall("b", "run_tests", "{}"), "1 failed", False)
    await obs.after_step(ok)
    assert obs.passed["r1-1"] is True
    await obs.after_step(bad)
    assert obs.passed["r1-1"] is False and obs.failing["r1-1"] == 1 and obs.stuck(bad) is False
    await obs.after_step(bad)
    assert obs.stuck(bad) is True


async def test_total_step_cap_ends_the_run(tmp_path: Path) -> None:
    engine = FakeEngine({"coder": [RUN] * 40})
    orch = ForkOrchestrator(cfg_for(tmp_path, max_total_steps=5, max_rounds=5), engine, run_id="f3")
    report = await orch.execute_task("goal", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh")
    assert sum(1 for e in events(report) if e["kind"] == "fork_point") == 1  # trunk 2 + kids 2×4 ≥ cap
    assert sum(t.steps for t in report.teams) <= 2 + 2 * 4


def test_build_topology_selects_by_kind(tmp_path: Path) -> None:
    from airlie.orchestrator import ParallelAgentTeamOrchestrator

    engine = FakeEngine({})
    cfg = cfg_for(tmp_path)
    assert isinstance(build_topology(cfg, engine, run_id="b1"), ForkOrchestrator)
    cfg.topology.kind = "teams"
    assert isinstance(build_topology(cfg, engine, run_id="b2"), ParallelAgentTeamOrchestrator)


async def test_repeated_action_fork_trims_the_repeated_tail_and_names_it(tmp_path: Path) -> None:
    """Live finding 2026-10-13: children inherited the trunk's three identical failing calls and repeated
    them."""
    bad = FakeReply(tool_calls=[ToolCall("x", "write_file", "{}")])
    engine = FakeEngine(
        {"coder": [WRITE_NOTE, bad, bad, bad, WRITE_PASS, RUN, FakeReply(text="ok now"), RUN, RUN]}
    )
    orch = ForkOrchestrator(cfg_for(tmp_path, max_rounds=1), engine, run_id="f4")
    cfg = orch.cfg
    cfg.tools.builtin = ["write_file", "read_file", "run_tests"]
    report = await orch.execute_task("goal", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh")
    assert report.teams[0].stop_reason == "repeated_action" and report.teams[0].steps == 4
    child_first = next(r for r in engine.requests[4:] if "You are now branch" in r.messages[-1].content)
    roles = [m.role for m in child_first.messages]
    assert roles == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
    ]  # note step kept, 3 write_file pairs dropped
    nudge = child_first.messages[-1].content
    assert "write_file" in nudge and "requires: path" in nudge
    fp = next(e for e in events(report) if e["kind"] == "fork_point")
    assert fp["reason"] == "repeated_action" and fp["repeated"]["name"] == "write_file"


async def test_a_child_that_quits_without_passing_is_forked_again_not_accepted(tmp_path: Path) -> None:
    """Review finding 2026-10-13: when no child had test output, best-effort picked the one that said 'done'
    after a few steps, and the run ended on it. A 'done' without a passing test run is stuck too: snapshot it
    and fork again."""
    bad = FakeReply(tool_calls=[ToolCall("x", "read_file", "{}")])
    engine = FakeEngine(
        {
            "coder": [
                RUN,
                RUN,  # trunk: two failures → fork
                FakeReply(text="I give up"),  # one child quits at once
                bad,
                bad,
                bad,  # the other loops on a failing call → repeated_action; nobody ran tests
                WRITE_PASS,  # round 2
                RUN,
                RUN,
                FakeReply(text="fixed"),
                RUN,
                RUN,
                RUN,
            ]
        }
    )
    cfg = cfg_for(tmp_path, max_rounds=3, child_steps=4)
    cfg.tools.builtin = ["write_file", "read_file", "run_tests"]
    orch = ForkOrchestrator(cfg, engine, run_id="f5")
    report = await orch.execute_task("goal", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh")
    ev = events(report)
    resolved = [(e["round"], e["winner"], e["how"]) for e in ev if e["kind"] == "fork_resolved"]
    assert len(resolved) >= 2, resolved  # the quitter did not end the run
    assert report.any_ok and (orch.run_dir.path / report.winner / "PASS").exists()


async def test_best_effort_prefers_branches_that_ran_tests_and_worked(tmp_path: Path) -> None:
    from airlie.teams.protocol import TeamResult
    from airlie.topology.fork import Branch

    orch = ForkOrchestrator(cfg_for(tmp_path), FakeEngine({}), run_id="f6")
    quitter = Branch("r1-1", tmp_path, "b1", result=TeamResult("r1-1", True, "done", "gave up", 3, 0, 0, 0.0))
    worker = Branch(
        "r1-2", tmp_path, "b2", result=TeamResult("r1-2", False, "repeated_action", "", 6, 0, 0, 0.0)
    )
    winner, how = orch._resolve([quitter, worker])  # noqa: SLF001  # no test output anywhere: more work wins
    assert (winner.id, how) == ("r1-2", "best_effort")
    orch.observer.last_test["r1-2"] = "2 failed, 1 passed"  # ran tests beats never ran
    assert orch._resolve([quitter, worker])[0].id == "r1-2"  # noqa: SLF001
    orch.observer.last_test["r1-1"] = "1 failed, 2 passed"  # fewer failures beats more steps
    assert orch._resolve([quitter, worker])[0].id == "r1-1"  # noqa: SLF001


# ---- the test gate of a team ----


async def team_tests(tmp_path: Path, timeout_s: float, test_cmd: str) -> object:
    """The test gate of one finished team, whose test command gets `timeout_s`."""
    cfg = cfg_for(tmp_path)
    cfg.tools.shell_timeout_s = timeout_s
    orch = ParallelAgentTeamOrchestrator(cfg, FakeEngine({}))
    (orch.run_dir.path / "t").mkdir()
    return await orch._outcome(TeamResult("t", True, "done", "", 0, 0, 0, 0.0), test_cmd)  # noqa: SLF001


async def test_a_team_test_gate_kills_what_the_test_command_started_when_it_times_out(tmp_path: Path) -> None:
    sleeper = unique_sleep()
    started = time.monotonic()
    out = await asyncio.wait_for(team_tests(tmp_path, 1, f"{sleeper} & wait"), 20)
    assert (out.tests_passed, out.test_output) == (False, "test command timed out")  # type: ignore[attr-defined]
    assert time.monotonic() - started < 5  # it did not sit out the sleep, which holds the output pipe
    assert await eventually(lambda: not running(sleeper))


@pytest.mark.parametrize(("hold", "cancel"), cancel_phases())
async def test_cancelling_a_team_test_gate_kills_what_the_test_command_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold: bool, cancel: Any
) -> None:
    import airlie.orchestrator as orchestrator_mod

    gate = StartGate(monkeypatch, orchestrator_mod, hold=hold)
    sleeper = unique_sleep()
    task = asyncio.create_task(team_tests(tmp_path, 60, f"{sleeper} & wait"))
    await cancel(task, gate, sleeper)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    assert await eventually(lambda: not running(sleeper))


async def test_a_malformed_tool_call_reaches_the_runs_events(tmp_path: Path) -> None:
    bad = "<function=open_file>\n<file>PASS</file>\n</function>"
    engine = FakeEngine({"coder": [FakeReply(text=bad), WRITE_PASS, RUN, FakeReply(text="done")]})
    orch = ForkOrchestrator(cfg_for(tmp_path), engine, run_id="f10")
    report = await orch.execute_task(
        "make check.sh pass", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh"
    )
    [ev] = [e for e in events(report) if e["kind"] == "malformed_call"]
    assert (ev["team_id"], ev["text"]) == ("trunk", bad)
