"""Studio topology end to end on FakeEngine: plan → route → specialists (writers serialised) → review →
synthesis; report and events."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from airlie.config import load_config
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.router import ChiefRouter, RouteDecision
from airlie.topology.protocol import build_topology
from airlie.topology.studio import StudioOrchestrator
from tests.unit.test_fork_orchestrator import repo_with_marker_tests

ROLES: dict[str, tuple[str, list[str] | None]] = {
    "chief": ("---\nsummary: no\nroutable: false\n---\n# chief\nJSON only.", []),
    "coder": ("---\nsummary: Implement or change code\n---\n# coder", None),
    "qa": ("---\nsummary: Tests and edge cases\n---\n# qa", ["read_file", "write_file", "run_tests"]),
    "docs": ("---\nsummary: README and guides\n---\n# docs", ["read_file", "list_dir"]),
    "reviewer": ("---\nsummary: no\nroutable: false\n---\n# reviewer", []),
    "summarizer": ("# summarizer", None),
    "judge": ("# judge", None),
    "aggregator": ("# aggregator", None),
}


def cfg_for(tmp_path: Path, **studio: object):  # type: ignore[no-untyped-def]
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    roles: dict[str, dict[str, object]] = {}
    for name, (text, tools) in ROLES.items():
        (d / f"{name}.md").write_text(text)
        roles[name] = {"prompt": f"{name}.md", "model": "base", "thinking": False}
        if tools is not None:
            roles[name]["tools"] = tools
    data = {
        "version": 1,
        "engines": {"main": {"kind": "openai_http", "base_url": "http://x/v1", "models": ["base"]}},
        "models": {"base": {"repo": "org/m", "revision": "r"}},
        "roles": roles,
        "teams": {"orchestrator": "native", "max_steps": 6},
        "tools": {"builtin": ["read_file", "write_file", "list_dir", "run_tests"]},
        "topology": {"kind": "studio"},
        "router": {"kind": "chief"},
        "studio": {"parallel": 2, "review": {"enabled": True, "roles": ["coder"], "rounds": 1}, **studio},
        "repo_map": {"enabled": False},
        "observability": {"state_dir": str(tmp_path / "state")},
    }
    (d / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(d / "airlie.yaml")


PLAN = FakeReply(
    text=json.dumps(
        {
            "type": "plan",
            "assumptions": ["a1"],
            "subtasks": [
                {"id": "fix", "role": "coder", "brief": "make the check pass"},
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


def events(report):  # type: ignore[no-untyped-def]
    return [json.loads(x) for x in report.run_dir.events_path.read_text().splitlines()]


async def test_plan_route_run_review_synthesis(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {"chief": [PLAN, SYNTH], "coder": [WRITE_PASS, RUN, DONE], "docs": [DONE], "reviewer": [APPROVE]}
    )
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    assert report.final_answer == "all done" and report.winner == "studio"
    assert [t.team_id for t in report.teams] == ["fix", "note"] and all(t.ok for t in report.teams)
    assert report.tests == {"studio": True}
    kinds = [e["kind"] for e in events(report)]
    assert kinds[:2] == ["run_start", "plan"] and "review" in kinds and kinds[-1] == "run_done"
    route = [e for e in events(report) if e["kind"] == "route"]
    assert [r["chosen"] for r in route] == ["coder", "docs"] and route[0]["decision"] == "hint"
    assert (report.run_dir.path / "routing.jsonl").read_text().count("\n") == 2
    assert (report.run_dir.path / "report.md").read_text().count("| fix |") == 1
    assert (report.run_dir.path / "studio" / "PASS").exists()


async def test_request_changes_gives_one_fix_round_then_continues(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {
            "chief": [PLAN, SYNTH],
            "coder": [WRITE_PASS, RUN, DONE, WRITE_PASS, DONE],  # second DONE ends the fix round
            "docs": [DONE],
            "reviewer": [CHANGES, APPROVE],
        }
    )
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    ev = events(report)
    assert [e["verdict"] for e in ev if e["kind"] == "review"] == ["request_changes", "approve"]
    fix = [e for e in ev if e["kind"] == "fix_round"][0]
    assert fix["subtask"] == "fix" and fix["blocking"] == 1
    nudge = [r for r in engine.requests if r.role == "coder"][3].messages[-1].content
    assert nudge.startswith("A reviewer found blocking problems") and "PASS:1" in nudge
    assert report.final_answer == "all done"


async def test_unparsable_review_skips_the_fix_round(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {
            "chief": [PLAN, SYNTH],
            "coder": [WRITE_PASS, RUN, DONE],
            "docs": [DONE],
            "reviewer": [FakeReply(text="fine")],
        }
    )
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("g", repo=repo, test_cmd="./check.sh")
    ev = events(report)
    assert [e["verdict"] for e in ev if e["kind"] == "review"] == ["unparsable"]
    assert not [e for e in ev if e["kind"] == "fix_round"] and report.final_answer == "all done"


async def test_unparsable_plan_twice_ends_the_run_with_the_text(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"chief": [FakeReply(text="I would read the code."), FakeReply(text="Still prose.")]})
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("g", repo=repo)
    assert report.final_answer == "Still prose." and report.teams == []
    assert [e["kind"] for e in events(report)].count("plan_error") == 2


async def test_hint_to_an_unroutable_role_is_ignored(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    plan = FakeReply(
        text=json.dumps(
            {"type": "plan", "subtasks": [{"id": "x", "role": "chief", "brief": "write docs for the repo"}]}
        )
    )
    engine = FakeEngine({"chief": [plan, SYNTH], "docs": [DONE], "coder": [DONE]})

    class TopRouter:  # a jev-like router: ignores the hint when it is not in the closed set
        async def route(self, brief, criteria, hint=None, *, question="role", instructions=""):  # type: ignore[no-untyped-def]
            chosen = hint if hint in criteria else "docs"
            return RouteDecision(question, chosen, chosen, "clear", {chosen: 1.0}, 0.9, hint)

    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=TopRouter())
    report = await orch.execute_task("g", repo=repo)
    route = [e for e in events(report) if e["kind"] == "route"][0]
    assert route["chosen"] == "docs" and "chief" not in route["criteria"]


async def test_writers_are_serialised_readers_run_in_parallel(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    plan = FakeReply(
        text=json.dumps(
            {
                "type": "plan",
                "subtasks": [
                    {"id": "a", "role": "coder", "brief": "write a"},
                    {"id": "b", "role": "coder", "brief": "write b"},
                    {"id": "r", "role": "docs", "brief": "read"},
                ],
            }
        )
    )
    order: list[str] = []

    class SlowEngine(FakeEngine):
        async def generate(self, req, on_token=None):  # type: ignore[no-untyped-def]
            order.append(f"start:{req.role}")
            await asyncio.sleep(0.02)
            order.append(f"end:{req.role}")
            return await super().generate(req, on_token)

    engine = SlowEngine({"chief": [plan, SYNTH], "coder": [DONE, DONE], "docs": [DONE]})
    cfg = cfg_for(tmp_path, review={"enabled": False})
    await StudioOrchestrator(cfg, engine, router=ChiefRouter()).execute_task("g", repo=repo)
    coder_starts = [i for i, x in enumerate(order) if x == "start:coder"]
    coder_ends = [i for i, x in enumerate(order) if x == "end:coder"]
    assert coder_starts[1] > coder_ends[0]  # the second writer waited for the first
    assert order.index("start:docs") < coder_ends[0]  # the reader did not wait for the writer


def test_build_topology_returns_studio(tmp_path: Path) -> None:
    orch = build_topology(cfg_for(tmp_path), FakeEngine({}))
    assert isinstance(orch, StudioOrchestrator)


async def test_delegate_may_depend_on_a_finished_subtask_and_reviews_are_traced(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    delegate = FakeReply(
        text=json.dumps(
            {
                "type": "delegate",
                "subtask": {"id": "more", "role": "docs", "brief": "add a note", "depends_on": ["fix"]},
            }
        )
    )
    engine = FakeEngine(
        {
            "chief": [PLAN, delegate, SYNTH],
            "coder": [WRITE_PASS, RUN, DONE],
            "docs": [DONE, DONE],
            "reviewer": [APPROVE],
        }
    )
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    ev = events(report)
    assert not [e for e in ev if e["kind"] == "plan_error"]
    assert [t.team_id for t in report.teams] == ["fix", "note", "more"] and report.final_answer == "all done"
    traced = list((report.run_dir.path / "trace" / "fix").glob("review-*.json"))
    assert len(traced) == 1 and json.loads(traced[0].read_text())["verdict"] == "approve"
