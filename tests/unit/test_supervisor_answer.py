"""Muse's triage answer: parsed and checked before anything acts on it, and how its advice is added to a
role's message without touching the rules that message already carries."""

from __future__ import annotations

import json
from typing import Any

import pytest

from airlie.supervisor.advice import SUPERVISOR, with_advice
from airlie.supervisor.answer import CATEGORIES, OWNERS, AnswerError, parse_answer

GOOD: dict[str, Any] = {
    "category": "missing_import",
    "owner": "tester",
    "diagnosis": "Path is not imported.",
    "evidence": ["NameError: name 'Path' is not defined"],
    "message": "Import Path, then edit.",
    "would_route_to": "tester",
    "fix_if_attempt": None,
}


def test_a_fenced_answer_parses() -> None:
    a = parse_answer("thinking...\n```json\n" + json.dumps(GOOD) + "\n```", block=False)
    assert (a.category, a.owner, a.message) == ("missing_import", "tester", "Import Path, then edit.")
    assert a.evidence == ("NameError: name 'Path' is not defined",)
    assert a.would_route_to == "tester" and a.fix_if_attempt is None


def test_a_new_snake_case_category_is_kept() -> None:
    answer = parse_answer(json.dumps({**GOOD, "category": "fixture_leak"}), block=False)
    assert answer.category == "fixture_leak"


@pytest.mark.parametrize(
    "bad",
    [
        {"category": "Missing Import"},
        {"category": "other_thing"},
        {"owner": "chief"},
        {"message": None},
        {"message": "  "},
        {"diagnosis": ""},
        {"would_route_to": "chief"},
        {"evidence": "not a list"},
        {"evidence": [1]},
    ],
)
def test_an_answer_missing_or_breaking_a_rule_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(AnswerError):
        parse_answer(json.dumps({**GOOD, **bad}), block=False)


def test_at_a_block_no_message_is_needed() -> None:
    a = parse_answer(json.dumps({**GOOD, "message": None, "fix_if_attempt": "add it"}), block=True)
    assert a.message is None and a.fix_if_attempt == "add it"


def test_prose_with_no_json_is_refused() -> None:
    with pytest.raises(AnswerError):
        parse_answer("I think the tester should import Path.", block=False)


def test_missing_evidence_means_none_and_strings_are_stripped() -> None:
    raw = {k: v for k, v in GOOD.items() if k != "evidence"}
    a = parse_answer(json.dumps({**raw, "diagnosis": "  padded  "}), block=False)
    assert a.evidence == () and a.diagnosis == "padded"


def test_the_seed_categories_and_owners_are_fixed() -> None:
    assert CATEGORIES[0] == "missing_import" and CATEGORIES[-1] == "environment" and len(CATEGORIES) == 8
    assert OWNERS == ("tester", "coder", "existing_code", "environment", "unknown")


def test_with_advice_keeps_the_text_whole_and_adds_the_section_after_it() -> None:
    assert with_advice("RULES", "Import Path.") == f"RULES\n\n{SUPERVISOR}\nImport Path."
    assert with_advice("RULES", None) == "RULES" and with_advice("RULES", "") == "RULES"


def test_a_block_answer_may_carry_whether_it_agrees_with_the_class() -> None:
    a = parse_answer(
        json.dumps({**GOOD, "message": None, "class_agrees": False, "class_reason": "a test"}), block=True
    )
    assert (a.class_agrees, a.class_reason) == (False, "a test")
    plain = parse_answer(json.dumps(GOOD), block=False)
    assert (plain.class_agrees, plain.class_reason) == (None, None)  # optional: the other triggers never ask
    with pytest.raises(AnswerError, match="class_agrees"):
        parse_answer(json.dumps({**GOOD, "class_agrees": "yes"}), block=False)
