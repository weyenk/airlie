"""Spec 04 §4 test_lts_admission: binary decisions; the heuristic filters redundant steps."""

from __future__ import annotations

from airlie.config import ControllerSpec, SummarizerSpec
from airlie.engine import FakeEngine, FakeReply, ToolCall
from airlie.lts import SharedMemoryBank
from airlie.lts.controller import AdmissionContext, Decision, build_controller
from airlie.lts.summarizer import build_summarizer
from airlie.teams import StepRecord


def step(name: str, args: str, out: str, ok: bool = True, team: str = "team-1", n: int = 1) -> StepRecord:
    return StepRecord(team_id=team, step_num=n, u_t=ToolCall(f"c{n}", name, args), o_t=out, ok=ok)


def ctx(s: StepRecord, keys: list[str] | None = None, summary: str = "summary") -> AdmissionContext:
    return AdmissionContext(
        query="implement langdetect",
        memory_keys=keys or [],
        u_t=s.u_t,
        s_t=summary,
        o_t=s.o_t,
        team_id=s.team_id,
        step_num=s.step_num,
        ok=s.ok,
    )


async def test_lts_admission() -> None:
    """Binary decision with p_yes in [0,1]; admits a useful test result, rejects a redundant step."""
    h = build_controller(ControllerSpec(kind="heuristic"))
    useful = step(
        "run_tests", "{}", "FAILED tests/test_x.py::test_a - AssertionError: expected 3 got 2\n1 failed"
    )
    d = await h.decide(ctx(useful, summary="Ran tests: test_a fails, expected 3 got 2"))
    assert isinstance(d, Decision) and d.admit is True and 0.0 <= d.p_yes <= 1.0
    dup = await h.decide(
        ctx(
            useful,
            keys=["Ran tests: test_a fails, expected 3 got 2"],
            summary="Ran tests: test_a fails, expected 3 got 2",
        )
    )
    assert dup.admit is False and "duplicate" in dup.reason


async def test_heuristic_rules_are_named() -> None:
    h = build_controller(ControllerSpec(kind="heuristic"))
    empty = await h.decide(ctx(step("shell", '{"cmd": "ls"}', "   ")))
    assert empty.admit is False and empty.reason == "empty_observation"
    err = await h.decide(ctx(step("read_file", '{"path": "x"}', "error: path escapes", ok=False)))
    assert err.admit is False and err.reason == "tool_error"
    listing = await h.decide(ctx(step("list_dir", "{}", "a.py (10 bytes)\nb.py (20 bytes)")))
    assert listing.admit is True and listing.reason == "tool_class"
    cat = await h.decide(ctx(step("read_file", '{"path": "a.py"}', "1: x = 1")))
    assert cat.admit is False and cat.reason == "tool_class"


async def test_always_and_never() -> None:
    s = step("shell", "{}", "out")
    assert (await build_controller(ControllerSpec(kind="always")).decide(ctx(s))).admit is True
    assert (await build_controller(ControllerSpec(kind="never")).decide(ctx(s))).admit is False


async def test_judge_uses_engine_yes_no_with_the_step_triplet() -> None:
    engine = FakeEngine({"judge": [FakeReply(p_yes=0.9), FakeReply(p_yes=0.2)]})
    j = build_controller(ControllerSpec(kind="judge", judge_role="judge", threshold=0.5), engine=engine)
    s = step("run_tests", "{}", "3 passed")
    d1 = await j.decide(ctx(s, keys=["existing key"], summary="tests pass"))
    d2 = await j.decide(ctx(s, summary="tests pass"))
    assert d1.admit is True and d1.p_yes == 0.9
    assert d2.admit is False and d2.p_yes == 0.2


async def test_truncate_summarizer_is_the_spec_blueprint() -> None:
    s = build_summarizer(SummarizerSpec(kind="truncate", truncate_chars=10))
    out = await s.summarize("q", step("shell", "{}", "x" * 50))
    assert out == "Team team-1 step output summary: " + "x" * 10


async def test_role_summarizer_asks_the_summarizer_role() -> None:
    engine = FakeEngine(
        {"summarizer": [FakeReply(text="Ran tests; one failure in test_a (expected 3 got 2)")]}
    )
    s = build_summarizer(SummarizerSpec(kind="role", role="summarizer", max_words=20), engine=engine)
    out = await s.summarize("implement langdetect", step("run_tests", "{}", "FAILED test_a"))
    assert out.startswith("Ran tests")
    req = engine.requests[0]
    assert (
        req.role == "summarizer"
        and "FAILED test_a" in req.messages[-1].content
        and "20 words" in req.messages[-1].content
    )


async def test_role_summarizer_falls_back_to_truncate_on_engine_error() -> None:
    engine = FakeEngine({})
    s = build_summarizer(SummarizerSpec(kind="role", role="summarizer"), engine=engine)
    out = await s.summarize("q", step("shell", "{}", "hello world"))
    assert "hello world" in out


def test_bank_is_importable_from_lts() -> None:
    assert SharedMemoryBank().get_summaries() == []
