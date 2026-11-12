"""`airlie bench trace-diff`'s pure comparisons: what counts as a difference between two runs' events,
prompts and ledger rows."""

from __future__ import annotations

from typing import Any

from airlie.bench.trace_diff import TraceDiff, diff_events, diff_ledger, diff_prompts


def ev(kind: str, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, **fields}


def test_identical_events_prompts_and_ledger_rows_differ_nowhere() -> None:
    events = [ev("story_start", n=1), ev("stage", name="build")]
    assert diff_events(events, list(events)) == []
    assert diff_prompts(["a\nb"], ["a\nb"]) == []
    assert diff_ledger([("red_streak", "x", "helped")], [("red_streak", "x", "helped")]) == []
    assert TraceDiff().clean


def test_volatile_fields_run_ids_and_shas_are_not_differences() -> None:
    a = [ev("run", ts=1.0, ms=5, studio_run="20261112-010101-abcd", head="a" * 40, note="1 passed in 1.20s")]
    b = [ev("run", ts=9.0, ms=7, studio_run="20261113-020202-ef01", head="b" * 40, note="1 passed in 3.40s")]
    assert diff_events(a, b) == []


def test_a_changed_field_is_one_line_naming_the_event_and_the_field() -> None:
    a = [ev("stage", name="build"), ev("layer_done", layer="01", tests=2)]
    b = [ev("stage", name="build"), ev("layer_done", layer="01", tests=3)]
    assert diff_events(a, b) == ["event 1 (layer_done): tests: 2 != 3"]


def test_a_different_kind_is_one_line() -> None:
    assert diff_events([ev("stage")], [ev("block")]) == ["event 0: kind 'stage' != 'block'"]


def test_an_extra_event_is_one_line() -> None:
    assert diff_events([ev("stage")], [ev("stage"), ev("block")]) == ["event 1: only B has 1 more"]


def test_node_events_on_either_side_are_ignored() -> None:
    a = [ev("stage"), ev("node_started", node="x"), ev("node_finished", node="x"), ev("run")]
    b = [ev("node_skipped", node="y"), ev("stage"), ev("run")]
    assert diff_events(a, b) == []


def test_a_changed_prompt_line_names_the_prompt_and_its_first_differing_line() -> None:
    lines = diff_prompts(["same\nold line\nrest"], ["same\nnew line\nrest"])
    assert lines == ["prompt 0 line 2: 'old line' != 'new line'"]


def test_prompts_only_one_side_has_are_one_line_and_the_lines_are_capped() -> None:
    assert diff_prompts(["a"], ["a", "b"]) == ["prompt 1: only B has 1 more"]
    many = diff_prompts([str(i) for i in range(10)], [f"x{i}" for i in range(10)], cap=3)
    assert len(many) == 4 and many[-1] == "... 7 more prompt lines"


def test_a_different_ledger_outcome_is_one_line() -> None:
    a = [("red_streak", "missing_code", "helped"), ("block", "", "blocked")]
    b = [("red_streak", "missing_code", "not_helped"), ("block", "", "blocked")]
    [line] = diff_ledger(a, b)
    assert line.startswith("ledger row 0: ") and "'helped'" in line and "'not_helped'" in line
    assert diff_ledger(a, a[:1]) == ["ledger row 1: only A has 1 more"]
