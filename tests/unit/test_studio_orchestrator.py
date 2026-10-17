"""Studio topology end to end on FakeEngine: plan → route → specialists (writers serialised) → review →
synthesis; report and events."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from airlie.config import load_config
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.router import ChiefRouter, RouteDecision
from airlie.topology.protocol import build_topology
from airlie.topology.studio import StudioOrchestrator, check_plan
from airlie.topology.studio_plan import Plan, PlanError, Subtask
from tests.unit.proc_helpers import eventually, running, unique_sleep
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
        "studio": {"parallel": 2, "review": {"enabled": True, "roles": ["coder"], "rounds": 1}, **studio},
        "repo_map": {"enabled": False},
        "observability": {"state_dir": str(tmp_path / "state")},
        **(extra or {}),
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


async def test_a_fix_round_on_a_long_conversation_starts_fresh_with_the_findings(tmp_path: Path) -> None:
    """HDS-5, 2026-10-16: fix rounds resent a 58-72k-token conversation and were cut off before acting. Over
    teams.fresh_continuation_tokens the round starts a new conversation: the brief, where the earlier work is,
    and the findings."""
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {
            "chief": [PLAN, SYNTH],
            "coder": [WRITE_PASS, RUN, DONE, WRITE_PASS, DONE],
            "docs": [DONE],
            "reviewer": [CHANGES, APPROVE],
        }
    )
    teams = {"orchestrator": "native", "max_steps": 6, "fresh_continuation_tokens": 1}
    orch = StudioOrchestrator(cfg_for(tmp_path, extra={"teams": teams}), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    fix_req = [r for r in engine.requests if r.role == "coder"][3]
    assert [m.role for m in fix_req.messages] == ["system", "user"]  # a new conversation, not the old one
    goal = str(fix_req.messages[1].content)
    assert "A reviewer found blocking problems" in goal and "PASS:1" in goal and "worktree" in goal
    fresh = [e for e in events(report) if e["kind"] == "fresh_continuation"]
    assert len(fresh) == 1 and fresh[0]["subtask"] == "fix"
    assert report.final_answer == "all done"


async def test_a_fix_round_on_a_short_conversation_continues_it(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {
            "chief": [PLAN, SYNTH],
            "coder": [WRITE_PASS, RUN, DONE, WRITE_PASS, DONE],
            "docs": [DONE],
            "reviewer": [CHANGES, APPROVE],
        }
    )
    report = await StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter()).execute_task(
        "fix the build", repo=repo, test_cmd="./check.sh"
    )
    fix_req = [r for r in engine.requests if r.role == "coder"][3]
    assert len(fix_req.messages) > 2  # the earlier turns are still there
    assert not [e for e in events(report) if e["kind"] == "fresh_continuation"]


async def test_a_reviewer_reply_cut_off_with_no_visible_text_is_an_error_review_that_says_why(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cut = FakeReply(reasoning="Checking the diff. ", finish_reason="length")
    engine = FakeEngine(
        {"chief": [PLAN, SYNTH], "coder": [WRITE_PASS, RUN, DONE], "docs": [DONE], "reviewer": [cut]}
    )
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    why = (
        "engine error: the reply was cut off at max_tokens (16384) before it ended, still thinking; "
        "raise roles.reviewer.max_tokens"
    )
    review = orch.runs["fix"].review
    assert review is not None and (review.verdict, review.raw) == ("error", why)
    assert [e["verdict"] for e in events(report) if e["kind"] == "review"] == ["error"]
    trace = json.loads((report.run_dir.path / "trace" / "fix" / "review-0.json").read_text())
    assert trace["raw"] == why and orch.runs["fix"].fix_rounds == 0  # no blocking findings, no fix round


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


# ---- fix pass after the whole-branch review ----


async def test_chief_router_without_a_usable_hint_falls_back_instead_of_raising(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    plan = FakeReply(
        text=json.dumps(
            {
                "type": "plan",
                "subtasks": [
                    {"id": "a", "role": "chief", "brief": "do a"},  # unroutable hint
                    {"id": "b", "brief": "do b"},  # no hint at all
                    {"id": "t", "role": "qa", "brief": "write tests"},  # qa has a skill library
                ],
            }
        )
    )
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "unit.md").write_text("---\nsummary: Unit tests\n---\n# unit\n")
    cfg = cfg_for(tmp_path, review={"enabled": False})
    cfg.roles["qa"].skills = tmp_path / "skills"
    engine = FakeEngine({"chief": [plan, SYNTH], "coder": [DONE, DONE], "qa": [DONE]})
    report = await StudioOrchestrator(cfg, engine, router=ChiefRouter()).execute_task("g", repo=repo)
    assert report.final_answer == "all done"
    routes = {e["subtask"]: e for e in events(report) if e["kind"] == "route"}
    assert routes["a"]["chosen"] == "coder" and routes["a"]["decision"] == "fallback"
    assert routes["b"]["chosen"] == "coder" and routes["b"]["decision"] == "fallback"
    skill = [e for e in events(report) if e["kind"] == "skill"][0]
    assert skill["subtask"] == "t" and skill["decision"] == "none"


async def test_engine_errors_in_chief_and_reviewer_do_not_kill_the_run(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    # no reviewer script: FakeEngine raises EngineError on the review call
    engine = FakeEngine({"chief": [PLAN, SYNTH], "coder": [WRITE_PASS, RUN, DONE], "docs": [DONE]})
    report = await StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter()).execute_task(
        "g", repo=repo, test_cmd="./check.sh"
    )
    assert report.final_answer == "all done"
    assert [e["verdict"] for e in events(report) if e["kind"] == "review"] == ["error"]
    # no chief script at all: the run ends with a readable failure, not a traceback
    engine = FakeEngine({"coder": [DONE]})
    report = await StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter()).execute_task(
        "g", repo=repo
    )
    assert not report.any_ok and "engine error" in report.final_answer
    assert [e["kind"] for e in events(report)].count("plan_error") == 2


async def test_delegating_every_round_still_ends_with_a_synthesis(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    delegate = FakeReply(
        text=json.dumps({"type": "delegate", "subtask": {"id": "x1", "role": "docs", "brief": "more"}})
    )
    delegate2 = FakeReply(
        text=json.dumps({"type": "delegate", "subtask": {"id": "x2", "role": "docs", "brief": "more"}})
    )
    cfg = cfg_for(tmp_path, review={"enabled": False}, max_rounds=2)
    engine = FakeEngine({"chief": [PLAN, delegate, delegate2, SYNTH], "coder": [DONE], "docs": [DONE] * 3})
    report = await StudioOrchestrator(cfg, engine, router=ChiefRouter()).execute_task("g", repo=repo)
    assert report.final_answer == "all done"
    assert [e["kind"] for e in events(report)].count("synthesis") == 1
    last = [r for r in engine.requests if r.role == "chief"][-1].messages[-1].content
    assert "synthesis" in last and "delegate" not in last.split("Reply with")[-1]


async def test_rejected_delegate_reason_is_fed_back_to_the_chief(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    bad = FakeReply(
        text=json.dumps(
            {"type": "delegate", "subtask": {"id": "x", "role": "docs", "brief": "m", "depends_on": ["zz"]}}
        )
    )
    cfg = cfg_for(tmp_path, review={"enabled": False})
    engine = FakeEngine({"chief": [PLAN, bad, SYNTH], "coder": [DONE], "docs": [DONE]})
    report = await StudioOrchestrator(cfg, engine, router=ChiefRouter()).execute_task("g", repo=repo)
    assert report.final_answer == "all done"
    third = [r for r in engine.requests if r.role == "chief"][2].messages[-1].content
    assert "zz" in third and "rejected" in third


async def test_plan_rejected_twice_is_a_failed_run_with_feedback(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    bad = FakeReply(
        text=json.dumps(
            {"type": "plan", "subtasks": [{"id": "a", "role": "coder", "brief": "x", "depends_on": ["zz"]}]}
        )
    )
    engine = FakeEngine({"chief": [bad, bad]})
    report = await StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter()).execute_task(
        "g", repo=repo
    )
    assert not report.any_ok and "runnable plan" in report.final_answer and "zz" in report.final_answer
    second = [r for r in engine.requests if r.role == "chief"][1].messages[-1].content
    assert "zz" in second


# ---- a chief reply cut off at max_tokens (HDS-3 follow-up, 2026-10-15) ----

CHIEF_CUT = FakeReply(reasoning="The plan needs three layers. No, four. ", finish_reason="length")
CHIEF_WHY = (
    "the reply was cut off at max_tokens (4096) before it ended, still thinking; "
    "raise roles.chief.max_tokens above its thinking_budget (8192)"
)


def chief_as_hds_3(tmp_path: Path):  # type: ignore[no-untyped-def]
    """The config with the chief as the first live story had its architect: thinking on, a budget of 8192 and
    room for only 4096 tokens."""
    cfg = cfg_for(tmp_path, review={"enabled": False})
    chief = cfg.roles["chief"]
    chief.thinking, chief.thinking_budget, chief.max_tokens = True, 8192, 4096
    return cfg


async def test_a_chief_reply_cut_off_with_no_visible_text_fails_the_run_saying_why(tmp_path: Path) -> None:
    """It used to parse as "no plan or answer": the run ended with an empty answer and no reason."""
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"chief": [CHIEF_CUT, CHIEF_CUT]})
    orch = StudioOrchestrator(chief_as_hds_3(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo)
    assert report.final_answer == f"engine error: {CHIEF_WHY}" and not report.any_ok and not report.teams
    errors = [e["error"] for e in events(report) if e["kind"] == "plan_error"]
    assert errors == [f"engine error: {CHIEF_WHY}"] * 2  # asked twice, as for any engine error


async def test_the_plan_door_reports_a_cut_off_chief_as_failed_with_why(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"chief": [CHIEF_CUT, CHIEF_CUT]})
    out = await StudioOrchestrator(chief_as_hds_3(tmp_path), engine, router=ChiefRouter()).plan(
        "g", repo=repo
    )
    assert out.plan is None and out.failed and out.answer == f"engine error: {CHIEF_WHY}"


async def test_a_chief_reply_cut_off_in_synthesis_is_an_error_answer_not_an_empty_one(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine(
        {"chief": [PLAN, CHIEF_CUT], "coder": [WRITE_PASS, RUN, DONE], "docs": [DONE], "reviewer": [APPROVE]}
    )
    orch = StudioOrchestrator(chief_as_hds_3(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    assert report.final_answer == f"engine error during synthesis: {CHIEF_WHY}"
    assert report.any_ok and report.tests == {"studio": True}  # what was built is still in the report


CHIEF_WHY_TEXT = (  # a reply with text was not still thinking when it was cut off
    "the reply was cut off at max_tokens (4096) before it ended; "
    "raise roles.chief.max_tokens above its thinking_budget (8192)"
)
HALF_PLAN = (
    'I will split it.\n```json\n{"type": "plan", '
    '"subtasks": [{"id": "fix", "role": "coder", "brief": "make the che'
)


@pytest.mark.parametrize(
    "text",
    ["Split it into three lay", HALF_PLAN],
    ids=["prose cut off in a sentence", "a plan block cut off inside its json"],
)
async def test_a_chief_reply_cut_off_before_it_parses_fails_like_one_with_no_text(
    tmp_path: Path, text: str
) -> None:
    """A truncated plan block parses to nothing and used to be taken for a prose answer (design §4)."""
    repo = repo_with_marker_tests(tmp_path)
    half = FakeReply(text=text, reasoning="Three layers. ", finish_reason="length")
    engine = FakeEngine({"chief": [half, half]})
    out = await StudioOrchestrator(chief_as_hds_3(tmp_path), engine, router=ChiefRouter()).plan(
        "g", repo=repo
    )
    assert (out.plan, out.answer, out.failed) == (None, f"engine error: {CHIEF_WHY_TEXT}", True)


async def test_a_chief_synthesis_cut_off_inside_its_json_is_an_error_answer(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    half = FakeReply(
        text='{"type": "synthesis", "answer": "All done and the tests pa', finish_reason="length"
    )
    engine = FakeEngine(
        {"chief": [PLAN, half], "coder": [WRITE_PASS, RUN, DONE], "docs": [DONE], "reviewer": [APPROVE]}
    )
    orch = StudioOrchestrator(chief_as_hds_3(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task("fix the build", repo=repo, test_cmd="./check.sh")
    assert report.final_answer == f"engine error during synthesis: {CHIEF_WHY_TEXT}"


async def test_a_chief_reply_cut_off_after_a_whole_plan_or_answer_keeps_its_handling(tmp_path: Path) -> None:
    """What parses is an answer or a plan, however the reply ended: the cut-off was after it."""
    repo = repo_with_marker_tests(tmp_path)
    answer = FakeReply(
        text='{"type": "answer", "answer": "Use the cache."}\nAnd I would also men', finish_reason="length"
    )
    out = await StudioOrchestrator(
        chief_as_hds_3(tmp_path), FakeEngine({"chief": [answer]}), router=ChiefRouter()
    ).plan("g", repo=repo)
    assert (out.plan, out.answer, out.failed) == (None, "Use the cache.", False)
    plan = FakeReply(text=PLAN.text + "\nThat covers it, and I would als", finish_reason="length")
    out = await StudioOrchestrator(
        chief_as_hds_3(tmp_path), FakeEngine({"chief": [plan]}), router=ChiefRouter()
    ).plan("g", repo=repo)
    assert out.plan is not None and [s.id for s in out.plan.subtasks] == ["fix", "note"] and not out.failed


async def test_a_chief_reply_that_stopped_with_no_text_is_handled_as_before(tmp_path: Path) -> None:
    """Only a reply that max_tokens ended is an error: one that stopped by itself keeps its handling."""
    repo = repo_with_marker_tests(tmp_path)
    quiet = FakeReply(reasoning="All of it was thinking. ", finish_reason="stop")
    out = await StudioOrchestrator(
        chief_as_hds_3(tmp_path), FakeEngine({"chief": [quiet, quiet]}), router=ChiefRouter()
    ).plan("g", repo=repo)
    assert (out.plan, out.answer, out.failed) == (None, "", False)


async def test_failed_dependency_is_labelled_in_the_next_brief(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, review={"enabled": False})
    cfg.teams.max_steps = 2
    note2 = FakeReply(
        tool_calls=[ToolCall("n2", "write_file", json.dumps({"path": "n2.txt", "content": "b\n"}))]
    )
    # offered no tools for its final answer, it calls one anyway: no answer, so it failed at max_steps
    engine = FakeEngine({"chief": [PLAN, SYNTH], "coder": [WRITE_PASS, note2, note2], "docs": [DONE]})
    await StudioOrchestrator(cfg, engine, router=ChiefRouter()).execute_task("g", repo=repo)
    docs_goal = [r for r in engine.requests if r.role == "docs"][0].messages[1].content
    assert "FAILED: max_steps" in docs_goal


async def test_a_subtask_whose_reply_is_cut_off_failed_and_its_dependents_and_the_chief_are_told(
    tmp_path: Path,
) -> None:
    """HDS-3 (2026-10-15): a reply cut off at max_tokens is not a finished one. The subtask failed, the report
    and the events say how, the next brief says its result is unreliable, and the chief sees why."""
    repo = repo_with_marker_tests(tmp_path)
    cut = FakeReply(reasoning="Thinking about the fix. ", finish_reason="length")
    engine = FakeEngine({"chief": [PLAN, SYNTH], "coder": [cut], "docs": [DONE]})
    orch = StudioOrchestrator(cfg_for(tmp_path, review={"enabled": False}), engine, router=ChiefRouter())
    report = await orch.execute_task("g", repo=repo)
    fix, note = report.teams
    assert (fix.ok, fix.stop_reason) == (False, "cut_off") and (note.ok, note.stop_reason) == (True, "done")
    assert "the reply was cut off at max_tokens" in fix.final_text
    done = [e for e in events(report) if e["kind"] == "subtask_done"][0]
    assert (done["subtask"], done["ok"], done["stop_reason"]) == ("fix", False, "cut_off")
    docs_goal = [r for r in engine.requests if r.role == "docs"][0].messages[1].content
    assert "FAILED: cut_off, treat as unreliable" in docs_goal and "cut off at max_tokens" in docs_goal
    asked = [r for r in engine.requests if r.role == "chief"][1].messages[-1].content
    assert "### fix (coder; cut_off, 0 steps)" in asked and "cut off at max_tokens" in asked
    assert "| fix | coder |" in (report.run_dir.path / "report.md").read_text()
    assert report.any_ok  # the other subtask finished; the cut-off one is in the table, not hidden


async def test_each_review_sees_only_its_own_subtasks_diff(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    plan = FakeReply(
        text=json.dumps(
            {
                "type": "plan",
                "subtasks": [
                    {"id": "a", "role": "coder", "brief": "write PASS"},
                    {"id": "b", "role": "coder", "brief": "write note", "depends_on": ["a"]},
                ],
            }
        )
    )
    note = FakeReply(
        tool_calls=[ToolCall("n", "write_file", json.dumps({"path": "note.txt", "content": "second\n"}))]
    )
    engine = FakeEngine(
        {"chief": [plan, SYNTH], "coder": [WRITE_PASS, DONE, note, DONE], "reviewer": [APPROVE, APPROVE]}
    )
    await StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter()).execute_task("g", repo=repo)
    reviews = [r.messages[1].content for r in engine.requests if r.role == "reviewer"]
    assert "+ok" in reviews[0] and "note.txt" not in reviews[0]
    assert "note.txt" in reviews[1] and "+ok" not in reviews[1]


# ---- plans from outside the chief (2026-10-14) ----


def _git(repo: Path, *args: str) -> str:
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    out = subprocess.run(["git", *env, *args], cwd=repo, capture_output=True, text=True, check=True)
    return out.stdout.strip()


async def test_plan_returns_the_chiefs_plan_without_building_anything(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"chief": [PLAN]})
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    out = await orch.plan("fix the build", repo=repo)
    assert out.plan is not None and [s.id for s in out.plan.subtasks] == ["fix", "note"] and not out.failed
    assert [r.role for r in engine.requests] == ["chief"]
    assert not (orch.run_dir.path / "studio").exists()
    kinds = [json.loads(x)["kind"] for x in orch.run_dir.events_path.read_text().splitlines()]
    assert kinds == ["run_start", "plan", "run_done"]


async def test_a_given_plan_skips_the_chief_and_its_roles_beat_the_router(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    given = Plan((), (Subtask("t", "write the tests", "qa"), Subtask("b", "make them pass", "coder", ("t",))))

    class Overrider:  # a router that is sure everything is docs work
        async def route(self, brief, criteria, hint=None, *, question="role", instructions=""):  # type: ignore[no-untyped-def]
            return RouteDecision(question, "docs", "docs", "clear", {"docs": 1.0}, 0.99, hint)

    engine = FakeEngine({"qa": [DONE], "coder": [WRITE_PASS, RUN, DONE], "reviewer": [APPROVE]})
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=Overrider())
    report = await orch.execute_task("g", repo=repo, test_cmd="./check.sh", plan=given, synthesise=False)
    routes = [e for e in events(report) if e["kind"] == "route"]
    assert [(r["chosen"], r["decision"]) for r in routes] == [("qa", "pinned"), ("coder", "pinned")]
    assert "chief" not in {r.role for r in engine.requests}  # no plan call, no synthesis
    assert report.final_answer.startswith("### t (qa)\ndone") and report.tests == {"studio": True}


async def test_a_given_plan_that_cannot_run_raises_before_any_work(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    orch = StudioOrchestrator(cfg_for(tmp_path, max_subtasks=1), FakeEngine({}), router=ChiefRouter())
    with pytest.raises(PlanError, match="role 'chief'"):
        await orch.execute_task("g", repo=repo, plan=Plan((), (Subtask("a", "x", "chief"),)))
    with pytest.raises(PlanError, match="not in the plan"):
        await orch.execute_task("g", repo=repo, plan=Plan((), (Subtask("a", "x", "coder", ("zz",)),)))
    two = Plan((), (Subtask("a", "x", "coder"), Subtask("b", "y", "coder")))
    with pytest.raises(PlanError, match="max_subtasks is 1"):
        await orch.execute_task("g", repo=repo, plan=two)
    with pytest.raises(PlanError, match="dependency cycle"):
        await orch.execute_task("g", repo=repo, plan=Plan((), (Subtask("a", "x", "coder", ("a",)),)))
    with pytest.raises(PlanError, match="no subtasks"):
        await orch.execute_task("g", repo=repo, plan=Plan((), ()))
    assert not (orch.run_dir.path / "studio").exists()


def test_check_plan_judges_a_plan_from_outside_the_chief_by_every_role_pinned_or_not(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, max_subtasks=2)
    check_plan(cfg, Plan((), (Subtask("a", "x", "coder"), Subtask("b", "y", "docs", ("a",)))))  # runnable
    with pytest.raises(PlanError, match="no subtasks"):
        check_plan(cfg, Plan((), ()))
    with pytest.raises(PlanError, match="role 'chief'"):  # not pinned, and still rejected
        check_plan(cfg, Plan((), (Subtask("a", "x", "chief"),)))
    with pytest.raises(PlanError, match="role None"):
        check_plan(cfg, Plan((), (Subtask("a", "x"),)))
    with pytest.raises(PlanError, match="max_subtasks is 2"):
        check_plan(cfg, Plan((), tuple(Subtask(i, "x", "coder") for i in "abc")))
    with pytest.raises(PlanError, match="not in the plan"):
        check_plan(cfg, Plan((), (Subtask("a", "x", "coder", ("zz",)),)))
    with pytest.raises(PlanError, match="dependency cycle"):
        check_plan(cfg, Plan((), (Subtask("a", "x", "coder", ("a",)),)))


async def test_base_and_branch_place_the_worktree_and_detach_leaves_no_branch(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    _git(repo, "branch", "layer-00")  # the base: today's HEAD
    (repo / "later.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "later")  # HEAD moves past the base
    cfg = cfg_for(tmp_path, review={"enabled": False})
    engine = FakeEngine({"coder": [WRITE_PASS, DONE], "docs": [DONE]})
    one = Plan((), (Subtask("b", "write PASS", "coder"),))
    orch = StudioOrchestrator(cfg, engine, router=ChiefRouter())
    report = await orch.execute_task(
        "g", repo=repo, plan=one, base="layer-00", branch="s-01-x", synthesise=False
    )
    assert report.winning_branch == "s-01-x"
    assert _git(repo, "merge-base", "s-01-x", "HEAD") == _git(repo, "rev-parse", "layer-00")
    assert not (orch.run_dir.path / "studio" / "later.txt").exists()
    before = _git(repo, "branch", "--format=%(refname:short)")
    docs = Plan((), (Subtask("d", "describe the repo", "docs"),))
    detached = await StudioOrchestrator(cfg, engine, router=ChiefRouter()).execute_task(
        "g", repo=repo, plan=docs, detach=True, synthesise=False
    )
    assert detached.winning_branch == "(detached)"
    assert _git(repo, "branch", "--format=%(refname:short)") == before


async def test_the_repo_map_is_of_the_worktree_the_roles_build_on_not_of_the_persons_checkout(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature_only.py").write_text("def only_on_feature() -> None: ...\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feature")  # the person's checkout has moved on from the base
    cfg = cfg_for(tmp_path, review={"enabled": False}, extra={"repo_map": {"enabled": True}})
    look = Plan((), (Subtask("d", "describe the repo", "docs"),))

    def goal_message(engine: FakeEngine) -> str:  # the first user message starts with the repo map
        return next(r for r in engine.requests if r.role == "docs").messages[1].content

    at_base = FakeEngine({"docs": [DONE]})
    orch = StudioOrchestrator(cfg, at_base, router=ChiefRouter())
    await orch.execute_task("g", repo=repo, plan=look, base=base, detach=True, synthesise=False)
    seen = goal_message(at_base)
    assert seen.startswith("[Repository map]") and "check.sh" in seen and "feature_only.py" not in seen
    assert orch.preamble is not None and "feature_only.py" not in orch.preamble
    # with no base the worktree starts at HEAD, so the map follows the worktree, not a fixed copy of the repo
    at_head = FakeEngine({"docs": [DONE]})
    await StudioOrchestrator(cfg, at_head, router=ChiefRouter()).execute_task(
        "g", repo=repo, plan=look, detach=True, synthesise=False
    )
    assert "feature_only.py" in goal_message(at_head)
    # a plan has no worktree: its map is of the checkout it is asked about
    chief = FakeEngine({"chief": [FakeReply(text=json.dumps({"type": "answer", "answer": "x"}))]})
    await StudioOrchestrator(cfg, chief, router=ChiefRouter()).plan("g", repo=repo)
    assert "feature_only.py" in chief.requests[0].messages[1].content


# ---- the test gate ----


def gate(tmp_path: Path, timeout_s: float) -> StudioOrchestrator:
    """A studio whose test gate runs in `tmp_path` and gives the test command `timeout_s`."""
    tools = {"builtin": ["read_file"], "shell_timeout_s": timeout_s}
    orch = StudioOrchestrator(cfg_for(tmp_path, extra={"tools": tools}), FakeEngine({}))
    orch.worktree, orch.runs = tmp_path, {"done": None}  # type: ignore[dict-item]  # the gate runs after a subtask
    return orch


async def test_the_test_gate_kills_what_the_test_command_started_when_it_times_out(tmp_path: Path) -> None:
    sleeper = unique_sleep()
    started = time.monotonic()
    assert await asyncio.wait_for(gate(tmp_path, 1)._tests_pass(f"{sleeper} & wait"), 20) is False
    assert time.monotonic() - started < 5  # it did not sit out the sleep, which holds the output pipe
    assert await eventually(lambda: not running(sleeper))


async def test_cancelling_the_test_gate_kills_what_the_test_command_started(tmp_path: Path) -> None:
    sleeper = unique_sleep()
    task = asyncio.create_task(gate(tmp_path, 60)._tests_pass(f"{sleeper} & wait"))
    assert await eventually(lambda: running(f"^{sleeper}"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 20)
    assert await eventually(lambda: not running(sleeper))


ONE_BUILD = Plan((), (Subtask("build", "make the check pass", "coder"),))


async def test_run_tests_reports_each_gate_step_while_the_final_gate_runs_the_whole_command(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"coder": [RUN, DONE], "reviewer": [APPROVE]})
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    steps = ("echo first", "./check.sh", "echo third")
    report = await orch.execute_task(
        "g", repo=repo, test_cmd="./check.sh", test_steps=steps, plan=ONE_BUILD, synthesise=False
    )
    tool_output = engine.requests[1].messages[-1].content
    assert "[2/3] FAIL (exit 1): ./check.sh" in tool_output and "[3/3] PASS: echo third" in tool_output
    assert report.tests == {"studio": False}


async def test_before_gate_can_send_a_subtask_back_once_more_before_the_gate(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"coder": [DONE, WRITE_PASS, DONE], "reviewer": [APPROVE]})
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())

    async def before_gate() -> bool:
        await orch.follow_up("build", "write PASS, then stop")
        return True

    report = await orch.execute_task(
        "g", repo=repo, test_cmd="./check.sh", plan=ONE_BUILD, synthesise=False, before_gate=before_gate
    )
    assert report.tests == {"studio": True}
    assert engine.requests[-2].messages[-1].content == "write PASS, then stop"
    assert [e["subtask"] for e in events(report) if e["kind"] == "follow_up"] == ["build"]


async def test_a_before_gate_that_says_no_skips_the_gate(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"coder": [WRITE_PASS, DONE], "reviewer": [APPROVE]})
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())

    async def before_gate() -> bool:
        return False

    report = await orch.execute_task(
        "g", repo=repo, test_cmd="./check.sh", plan=ONE_BUILD, synthesise=False, before_gate=before_gate
    )
    assert report.tests == {"studio": False}  # the check passes, but it never ran
    assert [e["tests"] for e in events(report) if e["kind"] == "run_done"] == [False]


async def test_a_malformed_tool_call_reaches_the_runs_events_with_its_text(tmp_path: Path) -> None:
    bad = "<function=open_file>\n<file>PASS</file>\n</function>"
    engine = FakeEngine(
        {
            "chief": [PLAN, SYNTH],
            "coder": [FakeReply(text=bad), WRITE_PASS, RUN, DONE],
            "docs": [DONE],
            "reviewer": [APPROVE],
        }
    )
    orch = StudioOrchestrator(cfg_for(tmp_path), engine, router=ChiefRouter())
    report = await orch.execute_task(
        "fix the build", repo=repo_with_marker_tests(tmp_path), test_cmd="./check.sh"
    )
    [ev] = [e for e in events(report) if e["kind"] == "malformed_call"]
    assert (ev["team_id"], ev["role"], ev["step"], ev["text"]) == ("fix", "coder", 1, bad)
    assert ev["repaired"] is False
