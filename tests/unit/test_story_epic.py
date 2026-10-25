"""The epic contract: an epic card's closing JSON block parsed into an EpicSpec, and `task epic:add`'s reading
of a Markdown spec (P3 of the planning line, 2026-10-24)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from airlie.story.epic import EPIC_CONTRACT, EpicCriterion, EpicSpec, epic_from_markdown, epic_of, parse_epic
from airlie.story.model import StoryError

BLOCK: dict[str, Any] = {
    "type": "epic",
    "repositories": ["o/r", "o/other"],
    "criteria": [{"id": "EC1", "text": "A user can upload an avatar"}, {"id": "EC2", "text": "It is shown"}],
    "constraints": ["PNG only"],
    "links": ["https://example.org/mockup"],
}

SPEC = """# Avatars

Why: users want faces.

## Constraints

- PNG only
- 1 MB at most

## Acceptance criteria

- A user can upload an avatar
- The avatar is shown on the profile
  and on comments

## Left open

- the size
"""


def fenced(obj: dict[str, Any]) -> str:
    return "```json\n" + json.dumps(obj) + "\n```\n"


def test_an_epic_block_parses_and_round_trips() -> None:
    spec = parse_epic(BLOCK)
    assert spec == EpicSpec(
        ("o/r", "o/other"),
        (EpicCriterion("EC1", "A user can upload an avatar"), EpicCriterion("EC2", "It is shown")),
        ("PNG only",),
        ("https://example.org/mockup",),
    )
    assert EpicSpec.from_dict(spec.to_dict()) == spec
    assert spec.to_dict()["type"] == "epic"
    assert epic_of("The epic.\n\n" + fenced(BLOCK)) == spec
    assert '"type": "epic"' in EPIC_CONTRACT and "criteria" in EPIC_CONTRACT


def test_a_card_with_no_epic_block_has_no_epic() -> None:
    assert epic_of("just prose") is None
    assert epic_of("prose\n\n" + fenced({"type": "story", "title": "x"})) is None
    assert epic_of("```json\n{not json\n```\n") is None


def test_a_block_that_is_not_at_the_end_of_the_card_is_not_the_epic() -> None:
    assert epic_of(fenced(BLOCK) + "\nand then more prose\n") is None


def test_a_markdown_spec_gets_its_criteria_from_the_acceptance_section() -> None:
    description, spec = epic_from_markdown(SPEC, "o/r")
    assert spec.repositories == ("o/r",)
    assert spec.criteria == (
        EpicCriterion("EC1", "A user can upload an avatar"),
        EpicCriterion("EC2", "The avatar is shown on the profile and on comments"),
    )
    assert spec.constraints == ("PNG only", "1 MB at most")
    assert description.startswith(SPEC.rstrip("\n"))
    assert epic_of(description) == spec  # the generated block closes the card


def test_a_spec_with_its_own_block_keeps_it_and_takes_the_repo_when_the_block_names_none() -> None:
    own = {k: v for k, v in BLOCK.items() if k != "repositories"}
    text = "# E\n\nprose\n\n" + fenced(own)
    description, spec = epic_from_markdown(text, "o/r")
    assert spec.repositories == ("o/r",) and epic_of(description) == spec
    assert description.count("```json") == 1
    description, spec = epic_from_markdown("# E\n\n" + fenced(BLOCK), None)
    assert spec.repositories == ("o/r", "o/other")


@pytest.mark.parametrize(
    ("text", "repo", "why"),
    [
        ("# E\n\nNo criteria here.\n", "o/r", "Acceptance criteria"),
        ("# E\n\n## Acceptance criteria\n\nprose only\n", "o/r", "Acceptance criteria"),
        (SPEC, None, "--repo"),
        (SPEC, "not a repo", "owner/name"),
    ],
)
def test_a_spec_without_criteria_makes_no_card_and_says_why(text: str, repo: str | None, why: str) -> None:
    with pytest.raises(StoryError, match=why):
        epic_from_markdown(text, repo)


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"type": "story"}, 'needs "type": "epic"'),
        ({"repositories": []}, "at least one repository"),
        ({"repositories": ["nope"]}, "owner/name"),
        ({"repositories": ["o/r; rm -rf"]}, "owner/name"),
        ({"repositories": ["o/.."]}, "owner/name"),
        ({"repositories": "o/r"}, "list"),
        ({"criteria": []}, "at least one criterion"),
        ({"criteria": [{"id": "EC1"}]}, "'text'"),
        ({"criteria": [{"text": "x"}]}, "'id'"),
        ({"criteria": [{"id": "EC1", "text": "a"}, {"id": "EC1", "text": "b"}]}, "repeat"),
        ({"criteria": ["just text"]}, "object"),
        ({"constraints": {"a": 2}}, "list"),
    ],
)
def test_a_flawed_epic_block_is_refused_with_a_reason(change: dict[str, Any], why: str) -> None:
    with pytest.raises(StoryError, match=why):
        parse_epic({**BLOCK, **change})


def test_no_block_at_all_is_refused() -> None:
    with pytest.raises(StoryError, match="JSON"):
        parse_epic(None)


def test_a_flawed_epic_block_on_a_card_is_an_error_not_a_missing_epic() -> None:
    with pytest.raises(StoryError, match="at least one criterion"):
        epic_of("prose\n\n" + fenced({**BLOCK, "criteria": []}))


def test_a_p4_epic_block_round_trips_through_epic_spec_and_a_p3_block_still_parses() -> None:
    from airlie.story.epic import Estimate, Mockups, StorySketch, Viability

    p3 = {
        "type": "epic",
        "repositories": ["o/r"],
        "criteria": [{"id": "EC1", "text": "a user can upload"}],
        "constraints": ["PNG only"],
        "links": [],
    }
    plain = parse_epic(p3)
    assert plain.initiative is None and plain.kind is None and plain.stories == () and plain.extra == {}
    assert plain.to_dict() == p3  # a hand-written epic writes back exactly as it was read
    full = {
        **p3,
        "initiative": "AIR-40",
        "kind": "extension",
        "depends_on": ["AIR-41"],
        "stories": [{"title": "keys", "size": "S", "scope": "keyboard moves"}],
        "estimate": {"hours": 12.5, "basis": "3 S at 2.1 h (median of 7)", "at": "2026-10-24T18:02:00Z"},
        "viability": {"verdict": "risky", "reasons": ["the store is old"]},
        "mockups": {"options": ["/a/option-1.html"], "picked": None},
        "a_key_the_parser_does_not_know": {"keep": ["me"]},
    }
    spec = parse_epic(full)
    assert spec.initiative == "AIR-40" and spec.kind == "extension" and spec.depends_on == ("AIR-41",)
    assert spec.stories == (StorySketch("keys", "S", "keyboard moves"),)
    assert spec.estimate == Estimate(12.5, "3 S at 2.1 h (median of 7)", "2026-10-24T18:02:00Z")
    assert spec.viability == Viability("risky", ("the store is old",))
    assert spec.mockups == Mockups(("/a/option-1.html",), None)
    assert spec.extra == {"a_key_the_parser_does_not_know": {"keep": ["me"]}}
    assert spec.to_dict() == full  # nothing is lost on a rewrite, the unknown key included
    assert EpicSpec.from_dict(spec.to_dict()) == spec
    picked = EpicSpec.from_dict({**spec.to_dict(), "mockups": {"options": ["x"], "picked": 2}})
    assert picked.mockups == Mockups(("x",), 2)
    # the block in a card is found and read by epic_of, as P3's planner does
    assert epic_of("# E\n\n" + fenced(full)) == spec


@pytest.mark.parametrize(
    ("patch", "why"),
    [
        ({"kind": "side"}, "core or extension"),
        ({"initiative": 4}, "board key"),
        ({"stories": [{"title": "x", "size": "XL"}]}, "size of S, M or L"),
        ({"stories": "x"}, "list of"),
        ({"estimate": {"hours": "lots"}}, "number of hours"),
        ({"viability": {"verdict": "great"}}, "verdict"),
        ({"mockups": {"options": ["x"], "picked": "two"}}, "picked"),
    ],
)
def test_a_flawed_p4_field_is_a_reason_not_a_guess(patch: dict[str, object], why: str) -> None:
    block = {"type": "epic", "repositories": ["o/r"], "criteria": [{"id": "EC1", "text": "x"}], **patch}
    with pytest.raises(StoryError, match=why):
        parse_epic(block)
