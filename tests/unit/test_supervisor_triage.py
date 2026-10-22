"""The Muse call: a scripted answer comes back parsed, Muse may run one test and read it, and every way the
call can fail (engine error, slow, cut off, not an answer) is a TriageFailed with its reason."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from airlie.config import RoleSpec, load_config
from airlie.engine.fake import FakeEngine, FakeReply
from airlie.engine.types import GenRequest, GenResult, ToolCall
from airlie.supervisor.evidence import TriageInput
from airlie.supervisor.triage import RunOneTest, Triage, TriageFailed, triage_for
from airlie.tools.fs import ReadFile
from airlie.tools.protocol import ToolContext
from airlie.tools.registry import ToolRegistry

REPO = Path(__file__).resolve().parents[2]

GOOD: dict[str, Any] = {
    "category": "missing_import",
    "owner": "tester",
    "diagnosis": "Path is not imported.",
    "evidence": ["NameError: name 'Path' is not defined"],
    "message": "Import Path, then edit.",
    "would_route_to": "tester",
    "fix_if_attempt": None,
}


def an_input(**kw: object) -> TriageInput:
    base: dict[str, object] = dict(
        trigger="precode_retry", role="tester", subtask="tests", told="TOLD", evidence="FINDINGS",
        kinds=("passes_before_code",), brief="BRIEF", trace="", diff="DIFF", events="", earlier=(), recall=(),
    )  # fmt: skip
    base.update(kw)
    return TriageInput(**base)  # type: ignore[arg-type]


class StubChecks:
    def __init__(self, out: str = "") -> None:
        self.ran: list[str] = []
        self.out = out

    async def run_one(self, worktree: Path, test: str) -> str:
        self.ran.append(test)
        return self.out


class SlowEngine(FakeEngine):
    def __init__(self, seconds: float) -> None:
        super().__init__()
        self.seconds = seconds

    async def generate(self, req: GenRequest, on_token: Any = None) -> GenResult:
        await asyncio.sleep(self.seconds)
        return await super().generate(req, on_token)


def triage(engine: FakeEngine, checks: StubChecks | None = None, timeout_s: float = 5.0) -> Triage:
    stub = checks or StubChecks()
    role = RoleSpec(prompt=Path("prompts/roles/supervisor.md"), model="muse", thinking=False, max_tokens=512)

    def tools(worktree: Path) -> ToolRegistry:
        return ToolRegistry([ReadFile(), RunOneTest(stub)], ToolContext(worktree))

    return Triage(engine, "supervisor", role, tools, timeout_s)


async def test_a_scripted_answer_comes_back_parsed(tmp_path: Path) -> None:
    engine = FakeEngine({"supervisor": [FakeReply(text=json.dumps(GOOD))]})
    t = triage(engine)
    out = await t(an_input(), tmp_path)
    assert out.answer.category == "missing_import" and engine.requests[0].role == "supervisor"
    assert out.tokens > 0 and out.seconds >= 0 and t.model == "muse"


async def test_muse_may_run_one_test_before_it_answers(tmp_path: Path) -> None:
    engine = FakeEngine(
        {
            "supervisor": [
                FakeReply(
                    tool_calls=[ToolCall("c", "run_test", json.dumps({"test": "tests/test_a.py::test_a"}))]
                ),
                FakeReply(text=json.dumps(GOOD)),
            ]
        }
    )
    checks = StubChecks("NameError")
    await triage(engine, checks)(an_input(), tmp_path)
    assert checks.ran == ["tests/test_a.py::test_a"]
    assert "NameError" in engine.requests[1].messages[-1].content


async def test_run_test_refuses_a_blank_node_id(tmp_path: Path) -> None:
    checks = StubChecks()
    res = await RunOneTest(checks).run({"test": "  "}, ToolContext(tmp_path))
    assert (res.ok, res.output) == (False, "error: give one pytest node id") and checks.ran == []


@pytest.mark.parametrize(
    "reply,reason",
    [
        (FakeReply(text="no json here"), "unparsable"),
        (FakeReply(text="", finish_reason="length"), "cut_off"),
    ],
)
async def test_an_unusable_answer_is_a_triage_failure(tmp_path: Path, reply: FakeReply, reason: str) -> None:
    with pytest.raises(TriageFailed) as e:
        await triage(FakeEngine({"supervisor": [reply]}))(an_input(), tmp_path)
    assert e.value.reason == reason


async def test_an_engine_error_is_a_triage_failure(tmp_path: Path) -> None:
    with pytest.raises(TriageFailed, match="engine_error"):
        await triage(FakeEngine({}))(an_input(), tmp_path)


async def test_a_slow_answer_times_out(tmp_path: Path) -> None:
    with pytest.raises(TriageFailed) as e:
        await triage(SlowEngine(seconds=5), timeout_s=0.05)(an_input(), tmp_path)
    assert e.value.reason == "timeout"


async def test_an_answer_with_no_message_is_usable_only_at_a_block(tmp_path: Path) -> None:
    reply = json.dumps({**GOOD, "message": ""})
    with pytest.raises(TriageFailed) as e:
        await triage(FakeEngine({"supervisor": [FakeReply(text=reply)]}))(an_input(), tmp_path)
    assert e.value.reason == "unparsable"
    out = await triage(FakeEngine({"supervisor": [FakeReply(text=reply)]}))(
        an_input(trigger="block"), tmp_path
    )
    assert out.answer.message is None


async def test_a_missing_prompt_file_is_a_triage_failure(tmp_path: Path) -> None:
    t = triage(FakeEngine({"supervisor": [FakeReply(text=json.dumps(GOOD))]}))
    t.role = t.role.model_copy(update={"prompt": tmp_path / "gone.md"})
    with pytest.raises(TriageFailed) as e:
        await t(an_input(), tmp_path)
    assert e.value.reason == "engine_error"


async def test_a_tools_factory_that_raises_is_a_triage_failure(tmp_path: Path) -> None:
    def broken(worktree: Path) -> ToolRegistry:
        raise RuntimeError("no registry")

    t = triage(FakeEngine({"supervisor": [FakeReply(text=json.dumps(GOOD))]}))
    t.tools = broken
    with pytest.raises(TriageFailed, match="RuntimeError: no registry") as e:
        await t(an_input(), tmp_path)
    assert e.value.reason == "engine_error"


class CountingEngine(FakeEngine):
    """A FakeEngine that keeps every result it gave, to add up the tokens of a whole tool loop."""

    def __init__(self, scripts: dict[str, list[FakeReply]]) -> None:
        super().__init__(scripts)
        self.results: list[GenResult] = []

    async def generate(self, req: GenRequest, on_token: Any = None) -> GenResult:
        res = await super().generate(req, on_token)
        self.results.append(res)
        return res


async def test_the_tokens_are_those_of_every_request_in_the_tool_loop(tmp_path: Path) -> None:
    call = ToolCall("c", "run_test", json.dumps({"test": "tests/test_a.py::test_a"}))
    engine = CountingEngine({"supervisor": [FakeReply(tool_calls=[call]), FakeReply(text=json.dumps(GOOD))]})
    out = await triage(engine, StubChecks("NameError"))(an_input(), tmp_path)
    assert len(engine.results) == 2
    assert out.tokens == sum(r.prompt_tokens + r.completion_tokens for r in engine.results)
    last = engine.results[-1]
    assert out.tokens > last.prompt_tokens + last.completion_tokens


async def test_a_failure_still_says_how_long_it_took_and_what_it_spent(tmp_path: Path) -> None:
    with pytest.raises(TriageFailed) as e:
        await triage(CountingEngine({"supervisor": [FakeReply(text="no json here")]}))(an_input(), tmp_path)
    assert e.value.seconds > 0 and e.value.tokens > 0
    with pytest.raises(TriageFailed) as slow:
        await triage(SlowEngine(seconds=5), timeout_s=0.05)(an_input(), tmp_path)
    assert slow.value.seconds >= 0.05


def test_the_ledger_names_the_model_muse_runs_on_not_its_seat() -> None:
    cfg = load_config(REPO / "airlie.yaml")
    seat = cfg.roles[cfg.story.supervisor.role].model
    t = triage_for(cfg, FakeEngine({}), None)
    assert t.model == cfg.models[seat].repo and t.model.startswith("mlx-community/")
