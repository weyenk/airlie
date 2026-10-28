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


def test_plan_from_dict_reads_lists_the_way_a_person_or_a_model_writes_them() -> None:
    p = plan_from_dict(
        {
            "assumptions": "the API is Flask",  # models drop the brackets around a lone item
            "subtasks": [
                {"id": "setup", "brief": "x", "depends_on": None},
                {"id": "b", "brief": "y", "depends_on": "setup", "inputs": "img:1"},
                {"id": "c", "brief": "z", "depends_on": ["setup", 2], "inputs": []},
            ],
        }
    )
    assert p.assumptions == ("the API is Flask",)
    assert [s.depends_on for s in p.subtasks] == [(), ("setup",), ("setup", "2")]
    assert [s.inputs for s in p.subtasks] == [(), ("img:1",), ()]
    assert plan_from_dict({"assumptions": None, "subtasks": [{"id": "a", "brief": "x"}]}).assumptions == ()


def test_plan_from_dict_names_a_list_field_of_the_wrong_type() -> None:
    sub = {"id": "a", "brief": "x"}
    with pytest.raises(PlanError, match="'assumptions' must be a list of strings"):
        plan_from_dict({"assumptions": 5, "subtasks": [sub]})
    with pytest.raises(PlanError, match="subtask 2 'depends_on' must be a list of ids"):
        plan_from_dict({"subtasks": [sub, {**sub, "id": "b", "depends_on": {"a": 1}}]})
    with pytest.raises(PlanError, match="subtask 1 'inputs' must be a list of strings"):
        plan_from_dict({"subtasks": [{**sub, "inputs": 5}]})


def test_pin_marks_every_subtask_and_keeps_roles() -> None:
    p = pin(Plan((), (Subtask("a", "x", "coder"), Subtask("b", "y", "qa"))))
    assert all(s.pinned for s in p.subtasks) and [s.role_hint for s in p.subtasks] == ["coder", "qa"]


def test_extract_json_block_prefers_the_last_fence() -> None:
    text = 'I read {"not": "this"}.\n```json\n{"a": 1}\n```\nthen\n```json\n{"layers": []}\n```'
    assert extract_json_block(text) == {"layers": []}
    assert extract_json_block('plain {"b": 2} text') == {"b": 2}
    assert extract_json_block("no json here") is None
    # a fence quoted inside a JSON string (backslash-n, not a newline) does not end the answer early
    quoted = '```json\n{"layers": [{"id": "api", "brief": "Add it. Example:\\n```json\\n{...}\\n```"}]}\n```'
    assert extract_json_block(quoted) == {
        "layers": [{"id": "api", "brief": "Add it. Example:\n```json\n{...}\n```"}]
    }
    # the last fence is cut off at max tokens: no object, not the stale draft above it
    assert extract_json_block('```json\n{"a": 1}\n```\nrevised:\n```json\n{"layers": [') is None
    # CRLF after the opener
    assert extract_json_block('```json\r\n{"a": 1}\r\n```') == {"a": 1}
    # the last fence holds an array, not an object
    assert extract_json_block('```json\n{"a": 1}\n```\n```json\n[1, 2]\n```') is None


def test_a_json_block_problem_names_the_error_and_the_text_before_it() -> None:
    from airlie.topology.studio_plan import json_block_problem

    assert json_block_problem('```json\n{"a": [1, 2}\n```') is not None
    problem = json_block_problem('think {not this}\n```json\n{"a": [1, 2}\n```')
    assert problem is not None and problem.startswith("the ```json block is not valid JSON: ")
    assert "'{\"a\": [1, 2'" in problem  # the text just before where it broke
    assert json_block_problem('```json\n{"a": [1, 2]}\n```') is None  # it parses
    assert json_block_problem("no fence {at all") is None  # nothing to mend: NO_JSON stands
    assert json_block_problem("```json\nplain words\n```") is None
