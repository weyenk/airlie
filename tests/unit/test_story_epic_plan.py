"""The story plan: the architect's reply for an epic parsed into a StoryPlan with refusals it can act on, and
the plan-time checks against the trunk (D2 and D4 of the plans phase, 2026-10-24)."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from airlie.story.epic import EpicCriterion, EpicSpec
from airlie.story.epic_plan import (
    STORY_PLAN_CONTRACT,
    StoryPlan,
    check_story_plan,
    parse_story_plan,
    plan_feedback,
)
from airlie.story.model import Criterion, Story, StoryError
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
