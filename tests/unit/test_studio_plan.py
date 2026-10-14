"""Chief protocol (spec §5): one JSON object per turn; waves in dependency order."""

import json

import pytest

from airlie.topology.studio_plan import (
    Answer,
    Delegate,
    Plan,
    PlanError,
    Subtask,
    Synthesis,
    extract_json_block,
    parse_chief,
    pin,
    plan_from_dict,
    plan_to_dict,
    schedule,
)

PLAN = """Here is my plan.
{"type": "plan", "assumptions": ["the API is Flask"], "subtasks": [
 {"id": "map", "role": "architect", "brief": "Map app/ and tests/."},
 {"id": "fix", "role": "coder", "brief": "Fix the 500.", "depends_on": ["map"]},
 {"id": "ux", "role": "ux", "brief": "Critique the screenshot.", "inputs": ["img:1"]}]}"""


def test_parse_plan_with_prose_around_it() -> None:
    p = parse_chief(PLAN)
    assert isinstance(p, Plan) and p.assumptions == ("the API is Flask",)
    assert [s.id for s in p.subtasks] == ["map", "fix", "ux"]
    assert p.subtasks[1].depends_on == ("map",) and p.subtasks[1].role_hint == "coder"
    assert p.subtasks[2].inputs == ("img:1",) and p.subtasks[0].depends_on == ()


def test_parse_synthesis_delegate_and_answer() -> None:
    s = parse_chief('{"type": "synthesis", "answer": "done", "open_questions": ["q"]}')
    assert isinstance(s, Synthesis) and s.answer == "done" and s.open_questions == ("q",)
    d = parse_chief('{"type": "delegate", "subtask": {"id": "more", "role": "qa", "brief": "add tests"}}')
    assert isinstance(d, Delegate) and d.subtask.id == "more"
    a = parse_chief('{"type": "answer", "answer": "42"}')
    assert isinstance(a, Answer) and a.text == "42"


def test_parse_chief_falls_back_to_text() -> None:
    assert parse_chief("I would start by reading the code.") is None
    assert parse_chief('{"type": "poem", "lines": []}') is None
    assert parse_chief('{"type": "plan", "subtasks": [{"brief": "no id"}]}') is None


def test_schedule_waves_in_dependency_order() -> None:
    plan = Plan(
        (),
        (
            Subtask("a", "x"),
            Subtask("b", "y", depends_on=("a",)),
            Subtask("c", "z"),
            Subtask("d", "w", depends_on=("b", "c")),
        ),
    )
    assert [[s.id for s in wave] for wave in schedule(plan)] == [["a", "c"], ["b"], ["d"]]


def test_schedule_rejects_unknown_dependency_and_cycle() -> None:
    with pytest.raises(PlanError, match="'b'.*'zz'"):
        schedule(Plan((), (Subtask("b", "y", depends_on=("zz",)),)))
    with pytest.raises(PlanError, match="cycle"):
        schedule(Plan((), (Subtask("a", "x", depends_on=("b",)), Subtask("b", "y", depends_on=("a",)))))
    with pytest.raises(PlanError, match="duplicate"):
        schedule(Plan((), (Subtask("a", "x"), Subtask("a", "y"))))


def test_schedule_accepts_dependencies_already_satisfied() -> None:
    plan = Plan((), (Subtask("more", "x", depends_on=("fix",)),))
    with pytest.raises(PlanError):
        schedule(plan)
    assert [[s.id for s in w] for w in schedule(plan, satisfied=frozenset({"fix"}))] == [["more"]]


# ---- plan exchange (2026-10-14): a plan leaves the studio, a person edits it, it comes back ----


def test_plan_round_trips_through_a_dict() -> None:
    p = Plan(("a1",), (Subtask("t", "write tests", "qa"), Subtask("b", "build", "coder", ("t",), ("img:1",))))
    d = plan_to_dict(p)
    assert d["type"] == "plan" and d["assumptions"] == ["a1"]
    assert d["subtasks"][1] == {
        "id": "b",
        "role": "coder",
        "brief": "build",
        "depends_on": ["t"],
        "inputs": ["img:1"],
    }
    assert plan_from_dict(json.loads(json.dumps(d))) == p


def test_plan_from_dict_names_the_problem() -> None:
    with pytest.raises(PlanError, match="non-empty 'subtasks'"):
        plan_from_dict({"subtasks": []})
    with pytest.raises(PlanError, match="subtask 2 needs an 'id' and a 'brief'"):
        plan_from_dict({"subtasks": [{"id": "a", "brief": "x"}, {"id": "b"}]})


def test_pin_marks_every_subtask_and_keeps_roles() -> None:
    p = pin(Plan((), (Subtask("a", "x", "coder"), Subtask("b", "y", "qa"))))
    assert all(s.pinned for s in p.subtasks) and [s.role_hint for s in p.subtasks] == ["coder", "qa"]


def test_extract_json_block_prefers_the_last_fence() -> None:
    text = 'I read {"not": "this"}.\n```json\n{"a": 1}\n```\nthen\n```json\n{"layers": []}\n```'
    assert extract_json_block(text) == {"layers": []}
    assert extract_json_block('plain {"b": 2} text') == {"b": 2}
    assert extract_json_block("no json here") is None
