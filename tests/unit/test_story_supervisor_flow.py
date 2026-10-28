"""The supervisor in a story run (P1, 2026-10-24): Muse's advice at the coder's gate retry and its
diagnosis on the card when a layer blocks. Its section is added to today's text and never replaces it;
whatever Muse does wrong, the role gets today's text and the run goes on. The tester's pre-code retry, the
coder's static retry and the tester's mend it also covered are retired with the tester: a package that
fails its readiness check goes back to the architect, which the supervisor does not read (its evidence is
a layer run's trace, and there is none at package time). On the calc repository and the scripted replies
of test_story_layer_checks_flow, with a `supervisor` role scripted by name on FakeEngine."""

from __future__ import annotations

import json
import re
from pathlib import Path

from airlie.engine import FakeEngine, FakeReply
from airlie.story.board import NOTE_PREFIX, text_card
from airlie.story.state import StoryState
from airlie.supervisor.advice import SUPERVISOR
from airlie.supervisor.ledger import Ledger, Row
from tests.unit.test_story_layer_checks_flow import (
    BROKEN,
    FEATURE,
    FIXED,
    LAYERS,
    PYTEST,
    RED,
    STILL_BROKEN,
    VACUOUS,
    calc_repo,
)
from tests.unit.test_story_pipeline import STORY, kinds, pipeline
from tests.unit.test_studio_orchestrator import APPROVE, DONE, ROLES

SUPERVISOR_ROLES = {
    **ROLES,
    "product": ("---\nsummary: Stories and acceptance criteria\n---\n# product", ["read_file", "list_dir"]),
    "architect": ("---\nsummary: Layered plans for a change\n---\n# architect", ["read_file", "list_dir"]),
    "supervisor": ("---\nsummary: no\nroutable: false\n---\n# supervisor\nJSON only.", []),
}


def answer(
    message: str | None,
    *,
    owner: str = "coder",
    fix: str | None = None,
    class_agrees: bool | None = None,
    class_reason: str | None = None,
) -> FakeReply:
    """Muse's answer: a diagnosis, the role's next step and, for a block, the fix another try would make."""
    data = {
        "category": "wrong_acceptance_test",
        "owner": owner,
        "diagnosis": "the code breaks add, which the whole suite checks",
        "evidence": ["calc.py"],
        "message": message,
        "fix_if_attempt": fix,
        "class_agrees": class_agrees,
        "class_reason": class_reason,
    }
    return FakeReply(text="```json\n" + json.dumps(data) + "\n```")


def scripted(
    *,
    coder: list[FakeReply],
    supervisor: list[FakeReply] | None = None,
    packages: list[FakeReply] | None = None,
) -> FakeEngine:
    """The one-layer calc story's replies, by role; no `supervisor` key when `supervisor` is None."""
    scripts = {
        "product": [STORY],
        "architect": [LAYERS, *(packages or [RED])],
        "coder": coder,
        "reviewer": [APPROVE, APPROVE],
    }
    if supervisor is not None:
        scripts["supervisor"] = supervisor
    return FakeEngine(scripts)


async def run_layer(tmp_path: Path, engine: FakeEngine, *, supervisor: bool = True) -> StoryState:
    repo = calc_repo(tmp_path)
    story: dict[str, object] = {"supervisor": {"enabled": True}} if supervisor else {}
    return await pipeline(tmp_path, repo, engine, test_cmd=PYTEST, story=story, roles=SUPERVISOR_ROLES).run(
        text_card("multiply")
    )


def user_messages(engine: FakeEngine, role: str) -> list[str]:
    return [r.messages[-1].content for r in engine.requests if r.role == role]


def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "state" / "supervisor.sqlite")


def rows(tmp_path: Path) -> list[Row]:
    return ledger(tmp_path).recent()


def board_notes(st: StoryState) -> list[str]:
    """The story's notes on the NullBoard, one per note (a note can run over several lines)."""
    text = (st.path / "notes.md").read_text(encoding="utf-8")
    return [n for n in re.split(r"^- (?=\d{4}-\d\d-\d\d )", text, flags=re.MULTILINE) if n.strip()]


# ---- the coder's gate retry ----


async def test_the_gate_retry_carries_muses_advice(tmp_path: Path) -> None:
    engine = scripted(coder=[BROKEN, DONE, FIXED, DONE], supervisor=[answer("Put add back as it was.")])
    st = await run_layer(tmp_path, engine)
    assert st.blocked is None and "01" in st.built, st.blocked
    [retry] = [p for p in user_messages(engine, "coder") if "the whole suite fails with your change" in p]
    assert "Put add back as it was." in retry and SUPERVISOR in retry
    [row] = rows(tmp_path)
    assert (row.trigger, row.subtask, row.role, row.outcome) == ("gate_retry", "build", "coder", "helped")
    assert row.kinds == ("gate_fails",) and row.layer == "01"


async def test_a_package_that_comes_back_to_the_architect_asks_muse_nothing(tmp_path: Path) -> None:
    """The supervisor's triggers on the layer's tests (precode_retry, static_retry, amend) are retired."""
    engine = scripted(coder=[FEATURE, DONE], supervisor=[], packages=[VACUOUS, RED])
    st = await run_layer(tmp_path, engine)
    assert st.blocked is None and "01" in st.built, st.blocked
    assert not [r for r in engine.requests if r.role == "supervisor"]
    assert rows(tmp_path) == []


# ---- the diagnosis on a block ----


async def test_a_layer_that_stays_red_is_blocked_with_muses_diagnosis_beside_the_reason(
    tmp_path: Path,
) -> None:
    engine = scripted(
        coder=[BROKEN, DONE, STILL_BROKEN, DONE],
        supervisor=[
            answer("Put add back."),
            answer(None, fix="restore add to return a + b"),
        ],
    )
    st = await run_layer(tmp_path, engine)
    assert st.blocked is not None and "fails on" in st.blocked
    assert "Supervisor:" not in st.blocked  # the reason is the reason; the diagnosis is beside it
    found = rows(tmp_path)
    assert [r.trigger for r in found] == ["block", "gate_retry"]
    assert [r.outcome for r in found] == ["blocked", "not_helped"]
    assert found[0].kinds == ("gate_fails",) and found[0].subtask == "build"
    note = board_notes(st)[-2]  # the stop's note, then the move to blocked
    assert note.index("stopped after") < note.index("Supervisor: the code breaks add")
    assert "restore add to return a + b" in note and note.index("restore add") < note.index("Resume:")
    [blocked] = kinds(st.path / "events.jsonl", "blocked")
    assert "the code breaks add" in blocked["diagnosis"]


# ---- off, or broken ----


async def test_with_the_supervisor_off_nothing_changes(tmp_path: Path) -> None:
    engine = scripted(coder=[BROKEN, DONE, FIXED, DONE])  # no supervisor script
    st = await run_layer(tmp_path, engine, supervisor=False)
    assert st.blocked is None and "01" in st.built, st.blocked
    assert not (tmp_path / "state" / "supervisor.sqlite").exists()
    [retry] = [p for p in user_messages(engine, "coder") if "the whole suite fails with your change" in p]
    assert SUPERVISOR not in retry
    assert not kinds(st.path / "events.jsonl", "supervisor_fallback")


async def test_when_muse_fails_the_coder_gets_exactly_todays_text(tmp_path: Path) -> None:
    engine = scripted(coder=[BROKEN, DONE, FIXED, DONE], supervisor=[FakeReply(text="no json")])
    st = await run_layer(tmp_path, engine)
    assert st.blocked is None and "01" in st.built, st.blocked
    [retry] = [p for p in user_messages(engine, "coder") if "the whole suite fails with your change" in p]
    assert SUPERVISOR not in retry
    [fallback] = kinds(st.path / "events.jsonl", "supervisor_fallback")
    assert (fallback["trigger"], fallback["reason"]) == ("gate_retry", "unparsable")


async def test_a_supervisor_that_cannot_be_set_up_leaves_the_layer_as_today(tmp_path: Path) -> None:
    engine = scripted(coder=[FEATURE, DONE], supervisor=[])
    repo = calc_repo(tmp_path)
    story = {"supervisor": {"enabled": True}}
    pipe = pipeline(tmp_path, repo, engine, test_cmd=PYTEST, story=story, roles=SUPERVISOR_ROLES)
    (tmp_path / "cfg" / "supervisor.md").unlink()  # its prompt is gone after the config was read
    st = await pipe.run(text_card("multiply"))
    assert st.blocked is None and "01" in st.built, st.blocked
    [fallback] = kinds(st.path / "events.jsonl", "supervisor_fallback")
    assert fallback["reason"] == "setup: FileNotFoundError" and fallback["layer"] == "01"
    assert not [r for r in engine.requests if r.role == "supervisor"]


# ---- the diagnosis (P5a, 2026-10-25) ----


def diagnosis_of(st: StoryState, n: int = 1) -> dict[str, object]:
    out: dict[str, object] = json.loads((st.path / "diagnosis" / f"{n}.json").read_text())
    return out


async def test_a_block_writes_its_diagnosis_and_the_note_leads_with_the_class(tmp_path: Path) -> None:
    engine = scripted(coder=[BROKEN, DONE, STILL_BROKEN, DONE])
    st = await run_layer(tmp_path, engine, supervisor=False)
    assert st.blocked is not None and "fails on" in st.blocked
    [blocked] = kinds(st.path / "events.jsonl", "blocked")
    assert (blocked["class"], blocked["block_kind"], blocked["n"]) == ("content", "gate_failed", 1)
    assert str(blocked["signature"]).startswith("content:gate_failed:")
    md = (st.path / "diagnosis" / "1.md").read_text()
    assert md.startswith(f"# {st.path.name} blocked: content / gate_failed (block 1)")
    for section in ("## The class", "## Muse's reading", "## What happens next", "## Evidence", "## Paths"):
        assert section in md
    assert "The end of the gate's output" in md  # the gate's tail is on record for a diagnosis read from disk
    note = board_notes(st)[-2]
    assert f"{NOTE_PREFIX}content: gate_failed" in note and "Next: " in note
    assert note.index("content: gate_failed") < note.index("stopped after") < note.index("Resume:")
    assert kinds(st.path / "events.jsonl", "gate_tail")  # the final gate's output, for the diagnosis


async def test_muse_reads_the_pack_at_block_and_its_agreement_is_recorded(tmp_path: Path) -> None:
    engine = scripted(
        coder=[BROKEN, DONE, STILL_BROKEN, DONE],
        supervisor=[
            answer("Put add back."),
            answer(
                None, fix="restore add", class_agrees=False, class_reason="the test is wrong, not the model"
            ),
        ],
    )
    st = await run_layer(tmp_path, engine)
    asked = user_messages(engine, "supervisor")[-1]
    assert "Mechanical class: content / gate_failed" in asked and "The end of the gate's output" in asked
    block = [r for r in rows(tmp_path) if r.trigger == "block"][0]
    assert (block.class_, block.class_agrees) == ("content", False)
    assert block.signature is not None and block.signature.startswith("content:gate_failed:")
    d = diagnosis_of(st)
    assert d["muse"]["class_agrees"] is False  # type: ignore[index]
    note = board_notes(st)[-2]
    assert "Supervisor does not agree with the class: the test is wrong, not the model" in note
    # the mechanical class is read first; its kind is Muse's category once Muse has one
    assert note.index("content: wrong_acceptance_test") < note.index("Supervisor:")
