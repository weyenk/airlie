"""The supervisor in a story run: Muse's advice at the tester's pre-code retry, the coder's static retry and
the tester's mend, and its diagnosis on the card when a layer blocks. Its section is added to today's text
and never replaces it; whatever Muse does wrong, the role gets today's text and the run goes on. On the calc
repository and the scripted replies of test_story_layer_checks_flow, with a `supervisor` role scripted by
name on FakeEngine."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from airlie.engine import FakeEngine, FakeReply
from airlie.story.board import text_card
from airlie.story.layer_checks import feedback
from airlie.story.state import StoryState
from airlie.supervisor.advice import SUPERVISOR
from airlie.supervisor.ledger import Ledger, Row
from airlie.teams import native_team
from tests.unit.test_story_layer_checks_flow import (
    FEATURE,
    LAYERS,
    LISTED,
    MENDED,
    NO_ASSERT,
    PYTEST,
    RED,
    VACUOUS,
    WRONG,
    DownForTester,
    calc_repo,
)
from tests.unit.test_story_pipeline import STORY, TESTER_ROLES, kinds, pipeline
from tests.unit.test_studio_orchestrator import APPROVE, DONE

SUPERVISOR_ROLES = {
    **TESTER_ROLES,
    "supervisor": ("---\nsummary: no\nroutable: false\n---\n# supervisor\nJSON only.", []),
}


def answer(message: str, *, owner: str = "tester") -> FakeReply:
    """Muse's answer to a retry: a diagnosis and the role's next step."""
    data = {
        "category": "wrong_acceptance_test",
        "owner": owner,
        "diagnosis": "the test checks something that already exists",
        "evidence": ["tests/test_calc.py"],
        "message": message,
    }
    return FakeReply(text="```json\n" + json.dumps(data) + "\n```")


def block_answer(diagnosis: str, *, fix: str) -> FakeReply:
    """Muse's reading of a block: no message (no role reads it), and the fix one more attempt would try."""
    data = {
        "category": "wrong_acceptance_test",
        "owner": "tester",
        "diagnosis": diagnosis,
        "evidence": [],
        "message": None,
        "fix_if_attempt": fix,
    }
    return FakeReply(text="```json\n" + json.dumps(data) + "\n```")


def scripted(
    *,
    tester: list[FakeReply],
    supervisor: list[FakeReply] | None = None,
    coder: list[FakeReply] | None = None,
    engine: type[FakeEngine] = FakeEngine,
) -> FakeEngine:
    """The one-layer calc story's replies, by role; no `supervisor` key when `supervisor` is None."""
    scripts = {
        "product": [STORY],
        "architect": [LAYERS],
        "tester": tester,
        "coder": coder if coder is not None else [FEATURE, DONE],
        "reviewer": [APPROVE, APPROVE],
    }
    if supervisor is not None:
        scripts["supervisor"] = supervisor
    return engine(scripts)


async def run_layer(
    tmp_path: Path, engine: FakeEngine, *, supervisor: bool = True, tester: bool = True
) -> StoryState:
    repo = calc_repo(tmp_path)
    story: dict[str, object] = {"tester_role": "tester" if tester else None}
    if supervisor:
        story["supervisor"] = {"enabled": True}
    return await pipeline(tmp_path, repo, engine, test_cmd=PYTEST, story=story, roles=SUPERVISOR_ROLES).run(
        text_card("multiply")
    )


def user_messages(engine: FakeEngine, role: str) -> list[str]:
    return [r.messages[-1].content for r in engine.requests if r.role == role]


def last_user_message(engine: FakeEngine, role: str) -> str:
    """The last message the role was sent by the pipeline (a follow-up), not a tool result."""
    sent = [
        m.content
        for r in engine.requests
        if r.role == role
        for m in r.messages
        if m.role == "user" and "airlie checked the tests" in m.content
    ]
    return sent[-1]


def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "state" / "supervisor.sqlite")


def rows(tmp_path: Path) -> list[Row]:
    return ledger(tmp_path).recent()


def board_notes(st: StoryState) -> list[str]:
    """The story's notes on the NullBoard, one per note (a note can run over several lines)."""
    text = (st.path / "notes.md").read_text(encoding="utf-8")
    return [n for n in re.split(r"^- (?=\d{4}-\d\d-\d\d )", text, flags=re.MULTILINE) if n.strip()]


# ---- the tester's pre-code retry ----


async def test_the_testers_retry_carries_muses_advice_and_every_rule(tmp_path: Path) -> None:
    engine = scripted(tester=[VACUOUS, LISTED, RED, LISTED], supervisor=[answer("Assert mul, not add.")])
    st = await run_layer(tmp_path, engine)
    assert st.blocked is None and "01" in st.built, st.blocked
    retry = last_user_message(engine, "tester")
    assert "Assert mul, not add." in retry and SUPERVISOR in retry
    assert "tests/test_calc.py::test_add_again" in retry  # today's findings, whole
    assert feedback([]).split("\n")[-1] in retry  # the rule paragraph, whole
    assert retry.index("test_add_again") < retry.index(SUPERVISOR)
    assert retry.index(SUPERVISOR) < retry.index("Every test you wrote must fail now")
    [row] = rows(tmp_path)
    assert (row.trigger, row.subtask, row.role, row.outcome) == ("precode_retry", "tests", "tester", "helped")
    assert row.kinds == ("passes_before_code",) and row.layer == "01"
    [triaged] = kinds(st.path / "events.jsonl", "supervisor_triage")
    assert (triaged["trigger"], triaged["layer"]) == ("precode_retry", "01")
    # Muse was shown the findings and asked with its own prompt
    [asked] = [r for r in engine.requests if r.role == "supervisor"]
    assert "# supervisor" in asked.messages[0].content
    assert "test_add_again" in asked.messages[-1].content


async def test_when_muse_fails_the_tester_gets_exactly_todays_text(tmp_path: Path) -> None:
    engine = scripted(tester=[VACUOUS, LISTED, RED, LISTED], supervisor=[FakeReply(text="no json")])
    st = await run_layer(tmp_path, engine)
    assert st.blocked is None and "01" in st.built, st.blocked
    retry = last_user_message(engine, "tester")
    assert SUPERVISOR not in retry
    assert retry.endswith(feedback([]).split("\n")[-1])
    [fallback] = kinds(st.path / "events.jsonl", "supervisor_fallback")
    assert (fallback["trigger"], fallback["reason"]) == ("precode_retry", "unparsable")
    [row] = rows(tmp_path)
    assert row.fallback == "unparsable" and row.outcome == "helped"  # the retry without Muse, counted too


async def test_a_retry_that_still_fails_is_not_helped_and_the_block_carries_the_diagnosis(
    tmp_path: Path,
) -> None:
    engine = scripted(
        tester=[VACUOUS, LISTED, VACUOUS, LISTED],
        supervisor=[
            answer("Assert mul."),
            block_answer("Tests assert add, which exists.", fix="assert mul(2,3)"),
        ],
    )
    st = await run_layer(tmp_path, engine)
    assert st.blocked is not None and "the tester's tests do not hold" in st.blocked
    assert "Supervisor:" not in st.blocked  # the reason is the reason; the diagnosis is beside it
    found = rows(tmp_path)
    assert [r.trigger for r in found] == ["block", "precode_retry"]
    assert [r.outcome for r in found] == ["blocked", "not_helped"]
    assert found[0].kinds == ("passes_before_code",) and found[0].subtask == "tests"
    note = board_notes(st)[-2]  # the stop's note, then the move to blocked
    assert note.index("stopped after") < note.index("Supervisor: Tests assert add, which exists.")
    assert "assert mul(2,3)" in note and note.index("assert mul(2,3)") < note.index("Resume:")
    [blocked] = kinds(st.path / "events.jsonl", "blocked")
    assert "Tests assert add, which exists." in blocked["diagnosis"]


async def test_an_engine_down_block_asks_muse_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_team, "ENGINE_RETRY_S", 0.0)
    engine = scripted(tester=[], supervisor=[], engine=DownForTester)
    st = await run_layer(tmp_path, engine)
    assert st.blocked is not None and "task up" in st.blocked
    assert not [r for r in engine.requests if r.role == "supervisor"]
    assert rows(tmp_path) == []
    [blocked] = kinds(st.path / "events.jsonl", "blocked")
    assert "diagnosis" not in blocked


# ---- the coder's static retry (no tester) ----


async def test_the_coders_static_retry_carries_muses_advice(tmp_path: Path) -> None:
    engine = scripted(
        tester=[],
        coder=[FEATURE, NO_ASSERT, DONE, RED, DONE],
        supervisor=[answer("Assert what mul returns.", owner="coder")],
    )
    st = await run_layer(tmp_path, engine, tester=False)
    assert st.blocked is None and "01" in st.built, st.blocked
    [retry] = [p for p in user_messages(engine, "coder") if "airlie checked the tests" in p]
    assert "Assert what mul returns." in retry
    assert retry.index(SUPERVISOR) < retry.index("Run run_tests until your tests pass")
    [row] = rows(tmp_path)
    assert (row.trigger, row.subtask, row.role, row.outcome) == ("static_retry", "build", "coder", "helped")


# ---- the tester's mend ----


async def test_the_mend_carries_muses_advice(tmp_path: Path) -> None:
    engine = scripted(tester=[WRONG, LISTED, RED, MENDED], supervisor=[answer("Unpack both values.")])
    st = await run_layer(tmp_path, engine)
    assert st.blocked is None and "01" in st.built, st.blocked
    [asked] = [p for p in user_messages(engine, "tester") if "fails only in your tests" in p]
    assert "Unpack both values." in asked and asked.index("Failing:") < asked.index(SUPERVISOR)
    [row] = rows(tmp_path)
    assert (row.trigger, row.kinds, row.outcome) == ("amend", ("locked_tests_fail",), "helped")


# ---- off, or broken ----


async def test_with_the_supervisor_off_nothing_changes(tmp_path: Path) -> None:
    engine = scripted(tester=[VACUOUS, LISTED, RED, LISTED])  # no supervisor script
    st = await run_layer(tmp_path, engine, supervisor=False)
    assert st.blocked is None and "01" in st.built, st.blocked
    assert not (tmp_path / "state" / "supervisor.sqlite").exists()
    assert SUPERVISOR not in last_user_message(engine, "tester")
    assert not kinds(st.path / "events.jsonl", "supervisor_fallback")


async def test_a_supervisor_that_cannot_be_set_up_leaves_the_layer_as_today(tmp_path: Path) -> None:
    engine = scripted(tester=[VACUOUS, LISTED, RED, LISTED], supervisor=[])
    repo = calc_repo(tmp_path)
    story: dict[str, Any] = {"tester_role": "tester", "supervisor": {"enabled": True}}
    pipe = pipeline(tmp_path, repo, engine, test_cmd=PYTEST, story=story, roles=SUPERVISOR_ROLES)
    (tmp_path / "cfg" / "supervisor.md").unlink()  # its prompt is gone after the config was read
    st = await pipe.run(text_card("multiply"))
    assert st.blocked is None and "01" in st.built, st.blocked
    [fallback] = kinds(st.path / "events.jsonl", "supervisor_fallback")
    assert fallback["reason"] == "setup: FileNotFoundError" and fallback["layer"] == "01"
    assert SUPERVISOR not in last_user_message(engine, "tester")
    assert not [r for r in engine.requests if r.role == "supervisor"]
