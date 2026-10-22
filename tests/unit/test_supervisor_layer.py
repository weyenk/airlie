"""One supervisor per layer: Muse's advice is returned, recorded and announced; any failure (Muse, the
evidence, the ledger, the cap) is a recorded fallback and no advice; every row is settled or closed, never
left open."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from airlie.config import RoleSpec
from airlie.config.schema import SupervisorSpec
from airlie.engine.fake import FakeEngine, FakeReply
from airlie.engine.types import Message
from airlie.supervisor.layer import LayerSupervisor
from airlie.supervisor.ledger import Intervention, Ledger
from airlie.supervisor.triage import Triage
from airlie.tools.fs import ReadFile
from airlie.tools.protocol import ToolContext
from airlie.tools.registry import ToolRegistry

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "roles" / "supervisor.md"

GOOD: dict[str, Any] = {
    "category": "missing_import",
    "owner": "tester",
    "diagnosis": "Path is not imported.",
    "evidence": ["NameError: name 'Path' is not defined"],
    "message": "Import Path, then edit.",
    "would_route_to": "tester",
    "fix_if_attempt": None,
}

Events = list[tuple[str, dict[str, Any]]]


def git_repo(path: Path) -> tuple[Path, str]:
    path.mkdir()
    (path / "README.md").write_text("hi\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=path, check=True)
    subprocess.run([*git, "add", "-A"], cwd=path, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=path, check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True)
    return path, sha.stdout.strip()


class Supervised(LayerSupervisor):
    engine: FakeEngine


def layer_supervisor(
    tmp_path: Path,
    answers: list[dict[str, Any] | str],
    per_layer: int = 6,
    *,
    ledger: Ledger | None = None,
    worktree: Path | None = None,
) -> tuple[Supervised, Ledger, Events]:
    replies = [FakeReply(text=a if isinstance(a, str) else json.dumps(a)) for a in answers]
    engine = FakeEngine({"supervisor": replies})
    role = RoleSpec(prompt=PROMPT, model="muse", thinking=False, max_tokens=512)

    def tools(wt: Path) -> ToolRegistry:
        return ToolRegistry([ReadFile()], ToolContext(wt))

    led = ledger or Ledger(tmp_path / "supervisor.sqlite")
    repo, sha = git_repo(tmp_path / "wt")
    events: Events = []
    sup = Supervised(
        ledger=led,
        triage=Triage(engine, "supervisor", role, tools, timeout_s=5.0),
        spec=SupervisorSpec(enabled=True, per_layer=per_layer),
        story="AIR-1",
        layer="02",
        studio_run="run-1",
        trace_dir=tmp_path / "trace",
        worktree=worktree or repo,
        base_sha=sha,
        brief="BRIEF",
        events=tmp_path / "events.jsonl",
        emit=lambda kind, **fields: events.append((kind, fields)),
    )
    sup.engine = engine
    return sup, led, events


async def test_advice_is_returned_recorded_and_announced(tmp_path: Path) -> None:
    sup, led, events = layer_supervisor(tmp_path, answers=[GOOD])
    msg = await sup.advise(
        "precode_retry", "tests", "tester", "TOLD", kinds=("passes_before_code",), evidence="F"
    )
    assert msg == GOOD["message"]
    [row] = led.recent()
    assert row.trigger == "precode_retry" and row.outcome is None and row.model == "muse"
    assert row.story == "AIR-1" and row.layer == "02" and row.kinds == ("passes_before_code",)
    assert events == [
        (
            "supervisor_triage",
            {
                "trigger": "precode_retry",
                "category": "missing_import",
                "owner": "tester",
                "ledger_id": row.id,
                "layer": "02",
            },
        )
    ]


async def test_settle_writes_the_outcome_once(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD])
    await sup.advise("precode_retry", "tests", "tester", "TOLD")
    sup.settle("precode_retry", "helped")
    sup.settle("precode_retry", "not_helped")
    assert led.recent()[0].outcome == "helped"


async def test_settle_closes_the_latest_open_row_for_its_trigger_only(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD, GOOD, GOOD])
    await sup.advise("precode_retry", "tests", "tester", "TOLD")
    await sup.advise("gate_retry", "build", "coder", "TOLD")
    await sup.advise("precode_retry", "tests", "tester", "TOLD")
    sup.settle("precode_retry", "helped")
    newest, gate, oldest = led.recent()
    assert (newest.outcome, gate.outcome, oldest.outcome) == ("helped", None, None)


async def test_a_failed_triage_is_a_recorded_fallback_and_no_advice(tmp_path: Path) -> None:
    sup, led, events = layer_supervisor(tmp_path, answers=["not json"])
    assert await sup.advise("gate_retry", "build", "coder", "TOLD") is None
    [row] = led.recent()
    assert row.fallback == "unparsable" and row.category is None
    assert events == [
        (
            "supervisor_fallback",
            {"trigger": "gate_retry", "reason": "unparsable", "ledger_id": row.id, "layer": "02"},
        )
    ]


async def test_a_fallback_row_is_still_settled(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=["not json"])
    await sup.advise("gate_retry", "build", "coder", "TOLD")
    sup.settle("gate_retry", "not_helped")
    assert led.recent()[0].outcome == "not_helped"


async def test_past_the_per_layer_cap_muse_is_not_asked(tmp_path: Path) -> None:
    sup, led, events = layer_supervisor(tmp_path, answers=[GOOD] * 6, per_layer=6)
    for _ in range(7):
        await sup.advise("repeat_guard", "build", "coder", "TOLD")
    assert led.recent(1)[0].fallback == "cap" and sup.triage_calls == 6
    assert len(sup.engine.requests) == 6 and len(led.recent()) == 7
    assert events[-1][0] == "supervisor_fallback" and events[-1][1]["reason"] == "cap"


async def test_the_cap_counts_requests_made_not_rows(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=["not json", GOOD], per_layer=2)
    await sup.advise("gate_retry", "build", "coder", "TOLD")
    assert await sup.advise("gate_retry", "build", "coder", "TOLD") == GOOD["message"]
    assert await sup.advise("gate_retry", "build", "coder", "TOLD") is None
    assert [r.fallback for r in led.recent()] == ["cap", None, "unparsable"] and sup.triage_calls == 2


async def test_a_block_gets_the_diagnosis_and_the_fix_it_would_have_tried(tmp_path: Path) -> None:
    ans = {**GOOD, "message": None, "fix_if_attempt": "import Path"}
    sup, led, _ = layer_supervisor(tmp_path, answers=[ans])
    text = await sup.at_block("tests", "tester", "layer 02: ...")
    assert text == f"{GOOD['diagnosis']}\n\nIf it had one more attempt: import Path"
    [row] = led.recent()
    assert row.trigger == "block" and row.outcome == "blocked"


async def test_a_block_without_a_fix_is_the_diagnosis_alone(tmp_path: Path) -> None:
    sup, _, _ = layer_supervisor(tmp_path, answers=[{**GOOD, "message": None}])
    assert await sup.at_block("tests", "tester", "layer 02: ...") == GOOD["diagnosis"]


async def test_a_block_that_falls_back_is_still_blocked(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=["not json"])
    assert await sup.at_block("tests", "tester", "layer 02: ...") is None
    [row] = led.recent()
    assert row.fallback == "unparsable" and row.outcome == "blocked"
    sup.close()
    assert led.recent()[0].outcome == "blocked"


async def test_close_leaves_no_intervention_without_an_outcome(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD, GOOD])
    await sup.advise("amend", "tests", "tester", "TOLD")
    await sup.advise("static_retry", "tests", "tester", "TOLD")
    sup.settle("static_retry", "helped")
    sup.close()
    assert [r.outcome for r in led.recent()] == ["helped", "unknown"]


async def test_earlier_interventions_on_the_layer_reach_the_next_triage(tmp_path: Path) -> None:
    sup, _, _ = layer_supervisor(tmp_path, answers=[GOOD, GOOD])
    await sup.advise("precode_retry", "tests", "tester", "TOLD")
    await sup.advise("gate_retry", "build", "coder", "TOLD")
    assert "Earlier on this layer" in sup.engine.requests[-1].messages[1].content


async def test_the_evidence_reaches_muse(tmp_path: Path) -> None:
    sup, _, _ = layer_supervisor(tmp_path, answers=[GOOD])
    (tmp_path / "events.jsonl").write_text(
        json.dumps({"kind": "precode", "layer": "02"})
        + "\n"
        + json.dumps({"kind": "x", "layer": "01"})
        + "\n"
    )
    (sup.worktree / "a.py").write_text("x = 1\n")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=sup.worktree, check=True
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a"],
        cwd=sup.worktree,
        check=True,
    )
    await sup.advise("precode_retry", "tests", "tester", "TOLD", evidence="FINDINGS")
    sent = sup.engine.requests[0].messages[1].content
    assert "BRIEF" in sent and "FINDINGS" in sent and "+x = 1" in sent
    assert '"layer":"02"' in sent and '"layer":"01"' not in sent


async def test_fixes_that_helped_before_are_recalled(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "supervisor.sqlite")
    past = Intervention(
        story="AIR-0", layer="01", studio_run="r0", subtask="tests", role="tester", trigger="precode_retry",
        kinds=("passes_before_code",), answer=None, fallback=None, model="muse", seconds=1.0, tokens=1,
    )  # fmt: skip
    led.outcome(led.record(past), "helped")
    sup, _, _ = layer_supervisor(tmp_path, answers=[GOOD], ledger=led)
    await sup.advise("precode_retry", "tests", "tester", "TOLD", kinds=("passes_before_code",))
    assert "Fixes that helped before" in sup.engine.requests[0].messages[1].content


async def test_evidence_that_cannot_be_built_is_a_fallback(tmp_path: Path) -> None:
    sup, led, events = layer_supervisor(tmp_path, answers=[GOOD], worktree=tmp_path / "gone")
    assert await sup.advise("gate_retry", "build", "coder", "TOLD") is None
    assert led.recent()[0].fallback == "evidence: FileNotFoundError" and sup.triage_calls == 0
    assert events[0][1]["reason"] == "evidence: FileNotFoundError"


async def test_a_ledger_that_fails_does_not_stop_the_run(tmp_path: Path) -> None:
    sup, led, events = layer_supervisor(tmp_path, answers=[GOOD])
    led.close()
    assert await sup.advise("gate_retry", "build", "coder", "TOLD") is None
    assert events == [
        (
            "supervisor_fallback",
            {"trigger": "gate_retry", "reason": "ledger: ProgrammingError", "ledger_id": None, "layer": "02"},
        )
    ]
    sup.settle("gate_retry", "helped")
    sup.close()


async def test_stuck_shows_muse_the_last_eight_messages(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD])
    messages = [Message(role="user", content=f"m{i}") for i in range(10)]
    messages.append(Message(role="assistant", content="y" * 900))
    assert await sup.stuck("build", "REPEATED", messages) == GOOD["message"]
    sent = sup.engine.requests[0].messages[1].content
    assert "user: m3" in sent and "user: m2" not in sent
    assert "assistant: " + "y" * 800 in sent and "y" * 801 not in sent
    assert led.recent()[0].trigger == "repeat_guard"
    sup.stuck_settled("build", False)
    sup.stuck_settled("build", True)
    assert led.recent()[0].outcome == "not_helped"


async def test_an_unknown_trigger_is_refused(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD])
    with pytest.raises(ValueError):
        await sup.advise("retry", "tests", "tester", "TOLD")
    assert led.recent() == []


def test_an_unknown_trigger_or_outcome_cannot_be_settled(tmp_path: Path) -> None:
    sup, _, _ = layer_supervisor(tmp_path, answers=[])
    with pytest.raises(ValueError):
        sup.settle("retry", "helped")
    with pytest.raises(ValueError):
        sup.settle("gate_retry", "fine")


async def test_parallel_subtasks_each_get_their_own_outcome(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD, GOOD])
    await sup.stuck("tests", "REPEATED", [])
    await sup.stuck("build", "REPEATED", [])
    sup.stuck_settled("tests", True)
    sup.stuck_settled("build", False)
    by_subtask = {r.subtask: r.outcome for r in led.recent()}
    assert by_subtask == {"tests": "helped", "build": "not_helped"}


async def test_settle_without_a_subtask_takes_the_latest_row_of_any(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD, GOOD])
    await sup.advise("gate_retry", "build", "coder", "TOLD")
    await sup.advise("gate_retry", "docs", "coder", "TOLD")
    sup.settle("gate_retry", "helped")
    assert [(r.subtask, r.outcome) for r in led.recent()] == [("docs", "helped"), ("build", None)]


async def test_a_block_cannot_be_advised(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD])
    with pytest.raises(ValueError):
        await sup.advise("block", "tests", "tester", "TOLD")
    assert led.recent() == [] and sup.engine.requests == []


async def test_an_emit_that_raises_does_not_stop_the_run(tmp_path: Path) -> None:
    sup, led, _ = layer_supervisor(tmp_path, answers=[GOOD, "not json"])

    def broken(kind: str, **fields: Any) -> None:
        raise RuntimeError("events.jsonl is gone")

    sup.emit = broken
    assert await sup.advise("precode_retry", "tests", "tester", "TOLD") == GOOD["message"]
    assert await sup.advise("gate_retry", "build", "coder", "TOLD") is None
    assert len(led.recent()) == 2


async def test_an_outcome_the_ledger_cannot_write_is_a_fallback(tmp_path: Path) -> None:
    sup, led, events = layer_supervisor(tmp_path, answers=[GOOD, GOOD])
    await sup.advise("precode_retry", "tests", "tester", "TOLD")
    await sup.advise("amend", "tests", "tester", "TOLD")
    [amend, precode] = led.recent()
    led.close()
    sup.settle("precode_retry", "helped")
    sup.close()
    after = [(kind, f["trigger"], f["reason"], f["ledger_id"]) for kind, f in events[2:]]
    assert after == [
        ("supervisor_fallback", "precode_retry", "ledger: ProgrammingError", precode.id),
        ("supervisor_fallback", "amend", "ledger: ProgrammingError", amend.id),
    ]
