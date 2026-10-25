"""The story plan: the architect's reply for an epic parsed into a StoryPlan with refusals it can act on, and
the plan-time checks against the trunk (D2 and D4 of the plans phase, 2026-10-24)."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from airlie.story.epic import EpicCriterion, EpicSpec
from airlie.story.epic_plan import (
    STORY_CARD_CONTRACT,
    STORY_PLAN_CONTRACT,
    PlannedStory,
    StoryPlan,
    after_keys,
    check_story_plan,
    epic_key_of,
    parse_story_plan,
    plan_feedback,
    plan_id_of,
    render_story_card,
    story_card_of,
)
from airlie.story.model import Criterion, EpicHeader, Story, StoryError
from tests.unit.git_helpers import git

EPIC = EpicSpec(
    ("o/r",),
    (EpicCriterion("EC1", "A user can upload an avatar"), EpicCriterion("EC2", "It is shown")),
    ("PNG only",),
)


def story(
    sid: str, *, depends: list[str] | None = None, covers: list[str] | None = None, **kw: Any
) -> dict[str, Any]:
    return {
        "id": sid,
        "title": f"Do {sid}",
        "why": f"The epic needs {sid}.",
        "repository": "o/r",
        "epic_criteria": covers if covers is not None else ["EC1"],
        "scope": [{"path": f"src/{sid.lower()}.py", "new": True}],
        "consumes": [],
        "produces": [f"def {sid.lower()}() -> int"],
        "acceptance": [{"id": "AC1", "given": "a user", "when": "they act", "then": "it works"}],
        "out_of_scope": [],
        "assumptions": [],
        "depends_on": depends or [],
        "size": "S",
        **kw,
    }


def plan(*stories: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {
        "type": "story_plan",
        "summary": "Avatars, in two stories.",
        "constraints": ["PNG only"],
        "risks": [{"risk": "a huge file", "pinned": "S2/AC1"}],
        "stories": list(stories) or [story("S1"), story("S2", depends=["S1"], covers=["EC2"])],
        **kw,
    }


def parse(obj: dict[str, Any] | None, **kw: Any) -> StoryPlan:
    return parse_story_plan(
        obj, EPIC, max_stories=kw.get("max_stories", 8), max_scope_files=kw.get("max_files", 8)
    )


def test_a_story_plan_parses_in_dependency_order_and_each_story_is_a_story() -> None:
    # S2 is listed first but depends on S1: the order puts S1 first; ties keep the plan's position
    obj = plan(story("S2", depends=["S1"], covers=["EC2"]), story("S1"), story("S3", covers=["EC1"]))
    parsed = parse(obj)
    assert [s.id for s in parsed.stories] == ["S2", "S1", "S3"]
    assert [s.id for s in parsed.order()] == ["S1", "S2", "S3"]
    s1 = parsed.order()[0]
    assert s1.story() == Story(
        "Do S1", "The epic needs S1.", (Criterion("AC1", "a user", "they act", "it works"),), (), ()
    )
    assert (s1.size, s1.scope[0].path, s1.scope[0].new, s1.repository) == ("S", "src/s1.py", True, "o/r")
    assert parsed.risk_for("S2") == "a huge file" and parsed.risk_for("S1") is None
    assert StoryPlan.from_dict(parsed.to_dict()) == parsed
    assert "smallest unit that ends in a pull request stack" in STORY_PLAN_CONTRACT


def test_a_cycle_or_an_unknown_dependency_is_refused() -> None:
    with pytest.raises(StoryError, match=r"cycle \(S1 -> S2 -> S1\)"):
        parse(plan(story("S1", depends=["S2"]), story("S2", depends=["S1"], covers=["EC2"])))
    with pytest.raises(StoryError, match=r"depends on \['S9'\], which the plan does not have"):
        parse(plan(story("S1", depends=["S9"]), story("S2", covers=["EC2"])))
    with pytest.raises(StoryError, match="depends on itself"):
        parse(plan(story("S1", depends=["S1"]), story("S2", covers=["EC2"])))
    with pytest.raises(StoryError, match=r"consumes from S9, which the plan does not have"):
        parse(plan(story("S1", consumes=["S9: def x() -> int"]), story("S2", covers=["EC2"])))


def test_a_consumed_interface_of_a_story_in_the_plan_is_fine() -> None:
    parse(plan(story("S1"), story("S2", depends=["S1"], covers=["EC2"], consumes=["S1: def s1() -> int"])))


def test_an_epic_criterion_no_story_covers_is_refused() -> None:
    with pytest.raises(StoryError, match=r"no story covers epic criteria \['EC2'\]"):
        parse(plan(story("S1"), story("S2", depends=["S1"], covers=["EC1"])))
    with pytest.raises(StoryError, match=r"unknown epic criteria \['EC7'\]"):
        parse(plan(story("S1", covers=["EC1", "EC7"]), story("S2", covers=["EC2"])))


def _mutate(f: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    obj = plan()
    f(obj)
    return obj


@pytest.mark.parametrize(
    ("obj", "kw", "why"),
    [
        (_mutate(lambda o: o["stories"][1].update(id="S1")), {}, r"story ids repeat \(\['S1'\]\)"),
        (
            _mutate(lambda o: o["stories"][0].update(acceptance=[])),
            {},
            "needs at least one acceptance criterion",
        ),
        (_mutate(lambda o: o["stories"][0].update(repository="x/y")), {}, "not one of the epic's"),
        (plan(), {"max_stories": 1}, "2 stories; at most 1"),
        (
            _mutate(lambda o: o["stories"][0].update(scope=[{"path": f"a{i}.py"} for i in range(3)])),
            {"max_files": 2},
            "scope has 3 paths; at most 2",
        ),
        (
            _mutate(lambda o: o["stories"][0].update(scope=[{"path": "../etc/passwd"}])),
            {},
            "inside the repository",
        ),
        (
            _mutate(lambda o: o["stories"][0].update(scope=[{"path": "/etc/passwd"}])),
            {},
            "inside the repository",
        ),
        (_mutate(lambda o: o["stories"][0].update(scope=[{"path": "-rf"}])), {}, "inside the repository"),
        (_mutate(lambda o: o["stories"][0].update(size="L")), {}, "size L: split it"),
        (_mutate(lambda o: o["stories"][0].update(size="XL")), {}, "must be S or M"),
        (_mutate(lambda o: o["risks"][0].update(pinned="S2/AC9")), {}, "pinned to 'S2/AC9'"),
        (_mutate(lambda o: o["risks"][0].update(pinned="nothing")), {}, "pinned to 'nothing'"),
        (_mutate(lambda o: o["stories"].clear()), {}, "non-empty"),
        (_mutate(lambda o: o.update(type="story")), {}, '"type": "story_plan"'),
        (_mutate(lambda o: o["stories"][0].update(title="x" * 100)), {}, "title must be one line"),
        (_mutate(lambda o: o["stories"][0].update(id="a b")), {}, "needs an 'id'"),
        (None, {}, "no JSON object"),
    ],
)
def test_a_flawed_plan_is_refused_with_a_reason(
    obj: dict[str, Any] | None, kw: dict[str, Any], why: str
) -> None:
    with pytest.raises(StoryError, match=why):
        parse(copy.deepcopy(obj), **kw)


def test_a_story_that_depends_on_two_independent_stories_is_refused() -> None:
    """A story stacks on one branch, so the stories it depends on must be a chain: S3 on top of S1 and S2
    could only be built on one of them, without the other's code."""
    obj = plan(story("S1"), story("S2", covers=["EC2"]), story("S3", depends=["S1", "S2"], covers=["EC2"]))
    with pytest.raises(StoryError, match=r"S3 depends on S1 and S2, which are independent of each other"):
        parse(obj)


def test_a_story_may_depend_on_two_stories_when_one_builds_on_the_other() -> None:
    obj = plan(
        story("S1"),
        story("S2", depends=["S1"], covers=["EC2"]),
        story("S3", depends=["S2", "S1"], covers=["EC2"]),
    )
    assert [s.id for s in parse(obj).order()] == ["S1", "S2", "S3"]


def test_a_dependency_in_another_repository_is_refused() -> None:
    two = EpicSpec(("o/r", "o/other"), EPIC.criteria)
    obj = plan(story("S1"), story("S2", depends=["S1"], covers=["EC2"], repository="o/other"))
    with pytest.raises(StoryError, match=r"S2 .*o/other.* depends on S1 .*o/r.*cannot stack"):
        parse_story_plan(obj, two, max_stories=8, max_scope_files=8)


def test_a_story_card_carries_the_story_its_files_its_dependencies_and_the_epic_header() -> None:
    s2_obj = story("S2", depends=["S1"], covers=["EC2"], consumes=["S1: def s1() -> int"])
    plan_ = parse(plan(story("S1"), s2_obj))
    s2 = plan_.order()[1]
    header = EpicHeader("AIR-3", plan_.summary, plan_.constraints, plan_.risk_for("S2"))
    text = render_story_card(s2, header, ["AIR-4"])
    assert "Acceptance criteria" in text and "src/s2.py (new)" in text and "AIR-4" in text
    card = story_card_of(text)
    assert card is not None
    assert card.story == replace(s2.story(), epic=header)
    assert (card.id, card.depends_on, card.size, card.epic_criteria) == ("S2", ("AIR-4",), "S", ("EC2",))
    assert card.scope == s2.scope and card.consumes == s2.consumes and card.produces == s2.produces
    assert story_card_of("prose only") is None
    assert story_card_of(text + "\nmore prose after the block\n") is None
    assert STORY_CARD_CONTRACT.startswith("The story's card ends")


def test_a_story_card_block_with_a_flawed_story_is_an_error() -> None:
    with pytest.raises(StoryError, match="story card"):
        story_card_of('```json\n{"type": "story_card", "story": 3}\n```\n')


def test_the_labels_of_a_story_card_name_its_epic_its_plan_id_and_what_it_comes_after() -> None:
    labels = ("repo:o/r", "epic:AIR-3", "plan:S2", "after:AIR-4", "after:AIR-5", "after:no key", "resumed:1")
    found = (epic_key_of(labels), plan_id_of(labels), after_keys(labels))
    assert found == ("AIR-3", "S2", ["AIR-4", "AIR-5"])
    assert (epic_key_of(("repo:o/r",)), plan_id_of(()), after_keys(("repo:o/r",))) == (None, None, [])


# ---- the checks against the trunk ----


@pytest.fixture
def trunk(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "old.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo, git(repo, "rev-parse", "HEAD")


def scoped(*paths: tuple[str, bool]) -> list[dict[str, Any]]:
    return [{"path": p, "new": new} for p, new in paths]


def test_a_scope_path_the_trunk_lacks_is_a_finding_unless_an_earlier_story_creates_it(
    trunk: tuple[Path, str],
) -> None:
    s1 = story("S1", scope=scoped(("src/old.py", False), ("src/gone.py", False), ("src/made.py", False)))
    s2 = story("S2", covers=["EC2"], scope=scoped(("src/made.py", True)))
    s3 = story("S3", depends=["S2"], covers=["EC2"], scope=scoped(("src/made.py", False)))
    s4 = story("S4", covers=["EC2"], scope=scoped(("src/other.py", False)))
    found = check_story_plan(parse(plan(s1, s2, s3, s4)), trunk_of={"o/r": trunk}.get)
    # S1 names made.py before S2 creates it; S3 finds it made by S2; S4's file is nowhere
    assert [(f.kind, f.story) for f in found] == [
        ("file_missing", "S1"),
        ("file_missing", "S1"),
        ("file_missing", "S4"),
    ]
    assert "src/gone.py" in found[0].why and "src/made.py" in found[1].why and "src/other.py" in found[2].why


def test_a_repository_without_a_checkout_passes_its_paths_unchecked() -> None:
    s1 = story("S1", scope=scoped(("src/anything.py", False)))
    assert check_story_plan(parse(plan(s1, story("S2", covers=["EC2"]))), trunk_of=lambda r: None) == []


def test_a_dependency_later_in_the_plans_order_is_a_finding() -> None:
    # order() sorts the stories, but the architect was told to list them in dependency order, and a plan
    # that does not is sent back
    obj = plan(story("S1", depends=["S2"], covers=["EC2"]), story("S2"))
    found = check_story_plan(parse(obj), trunk_of=lambda r: None)
    assert [(f.kind, f.story) for f in found] == [("order_violation", "S1")]
    assert "S2" in found[0].why


def test_an_empty_criterion_part_is_a_finding() -> None:
    blank = story("S1", acceptance=[{"id": "AC1", "given": "a user", "when": "  ", "then": "it works"}])
    found = check_story_plan(parse(plan(blank, story("S2", covers=["EC2"]))), trunk_of=lambda r: None)
    assert [(f.kind, f.story) for f in found] == [("empty_criterion", "S1")]
    assert "AC1" in found[0].why and "when" in found[0].why


def test_plan_feedback_is_one_line_per_finding(trunk: tuple[Path, str]) -> None:
    blank = story(
        "S1",
        acceptance=[{"id": "AC1", "given": "", "when": "x", "then": "y"}],
        scope=scoped(("nope.py", False)),
    )
    found = check_story_plan(parse(plan(blank, story("S2", covers=["EC2"]))), trunk_of={"o/r": trunk}.get)
    assert {f.kind for f in found} == {"file_missing", "empty_criterion"}
    text = plan_feedback(found)
    assert text.count("\n") == len(found) - 1 and text.startswith("- S1: ")


# ---- a re-plan lists only what is still to be done (D8) ----


def kept_of(*stories: dict[str, Any]) -> dict[str, PlannedStory]:
    """Stories already built, in review or merged, as an earlier plan wrote them."""
    return {d["id"]: PlannedStory.from_dict(d) for d in stories}


def reparse(obj: dict[str, Any], **kw: Any) -> StoryPlan:
    return parse_story_plan(obj, EPIC, max_stories=8, max_scope_files=8, **kw)


def test_a_replan_lists_only_what_is_still_to_be_done_and_may_depend_on_kept_stories() -> None:
    kept = kept_of(story("S1"), story("S2", depends=["S1"], covers=["EC2"]))
    new = reparse(plan(story("S4", depends=["S2"], covers=[]), risks=[]), kept=kept)
    assert [s.id for s in new.order()] == ["S4"] and new.stories[0].depends_on == ("S2",)


def test_a_replan_may_not_list_a_kept_or_a_retired_story_again() -> None:
    kept = kept_of(story("S1"), story("S2", depends=["S1"], covers=["EC2"]))
    with pytest.raises(StoryError, match="S2 is already built or under review"):
        reparse(plan(story("S2", covers=["EC2"]), risks=[]), kept=kept)
    with pytest.raises(StoryError, match="S5 was retired or dropped"):
        reparse(plan(story("S5", covers=["EC2"]), risks=[]), kept={"S1": kept["S1"]}, retired=("S5",))


def test_a_replan_still_covers_every_epic_criterion_with_the_kept_stories_counted() -> None:
    kept = kept_of(story("S1"))  # EC1 is covered by a kept story
    with pytest.raises(StoryError, match=r"no story covers epic criteria \['EC2'\]"):
        reparse(plan(story("S4", covers=["EC1"]), risks=[]), kept=kept)
    reparse(plan(story("S4", covers=["EC2"]), risks=[]), kept=kept)


def test_a_replan_risk_may_be_pinned_to_a_kept_stories_criterion() -> None:
    kept = kept_of(story("S1"))
    reparse(plan(story("S4", covers=["EC2"]), risks=[{"risk": "r", "pinned": "S1/AC1"}]), kept=kept)


def test_a_replan_cycle_among_the_new_stories_is_still_refused() -> None:
    kept = kept_of(story("S1"))
    obj = plan(story("S4", depends=["S5"], covers=["EC2"]), story("S5", depends=["S4"]), risks=[])
    with pytest.raises(StoryError, match="cycle"):
        reparse(obj, kept=kept)


def test_a_story_that_depends_on_a_kept_story_is_not_an_order_violation() -> None:
    kept = kept_of(story("S1"))
    new = reparse(plan(story("S4", depends=["S1"], covers=["EC2"]), risks=[]), kept=kept)
    assert check_story_plan(new, trunk_of=lambda r: None) == []
