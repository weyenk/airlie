"""The initiative read from its card, and the contracts of product's replies (the specs phase, P4 of the
planning line, 2026-10-25)."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.board import BoardStore, Item
from airlie.story.costs import Estimate
from airlie.story.epic import StorySketch
from airlie.story.initiative import (
    Ask,
    EpicsReply,
    Initiative,
    answer_ends_questions,
    check_epics,
    parse_epics,
    parse_imagine_reply,
    parse_question_reply,
)
from airlie.story.model import StoryError


def initiative_item(
    tmp_path_store: BoardStore,
    text: str = "Make the board feel like Linear.",
    labels: tuple[str, ...] = ("repo:weyenk/airlie",),
) -> Item:
    return tmp_path_store.create(level="initiative", title="Linear feel", description=text, labels=labels)


def test_an_initiative_reads_its_repositories_budget_skip_and_attachments_from_its_card(
    tmp_path: Path,
) -> None:
    store = BoardStore(tmp_path / "b.sqlite")
    item = initiative_item(
        store,
        "Make the board feel like Linear. No questions please.\n"
        "See /v1/airlie/uploads/abc_123 and again /v1/airlie/uploads/abc_123 and /v1/airlie/uploads/Z9.",
        ("repo:weyenk/airlie", "repo:weyenk/other", "budget:20h", "unrelated"),
    )
    init = Initiative.from_item(item)
    assert init.key == item.key and init.title == "Linear feel"
    assert init.repositories == ("weyenk/airlie", "weyenk/other")
    assert init.budget_hours == 20.0 and init.skip_questions is True
    assert init.attachments == ("abc_123", "Z9")  # in order of appearance, each once
    card = init.card()
    assert (card.key, card.ref, card.title) == (item.key, item.key, "Linear feel")
    assert card.description == init.text
    plain = Initiative.from_item(initiative_item(store))
    assert plain.budget_hours is None and plain.skip_questions is False and plain.attachments == ()
    fractional = Initiative.from_item(initiative_item(store, labels=("repo:o/r", "budget:7.5h")))
    assert fractional.budget_hours == 7.5


def test_an_initiative_without_a_repo_label_is_refused_with_a_reason(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "b.sqlite")
    with pytest.raises(StoryError, match="repo:<owner>/<name>"):
        Initiative.from_item(initiative_item(store, labels=()))
    with pytest.raises(StoryError, match="budget"):
        Initiative.from_item(initiative_item(store, labels=("repo:o/r", "budget:lots")))
    epic = store.create(level="epic", title="e", labels=("repo:o/r",), parent=initiative_item(store).key)
    with pytest.raises(StoryError, match="not an initiative"):
        Initiative.from_item(epic)
    with pytest.raises(StoryError, match="repo:<owner>/<name>"):
        Initiative.from_item(initiative_item(store, labels=("repo:../etc",)))


@pytest.mark.parametrize(
    ("text", "ends"),
    [
        ("go", True),
        ("Go.", True),
        ("  SKIP!  ", True),
        ("skip", True),
        ("go ahead and use blue", False),
        ("I would go with option b", False),
        ("", False),
        ("no", False),
    ],
)
def test_go_or_skip_alone_ends_the_questions_and_a_sentence_does_not(text: str, ends: bool) -> None:
    assert answer_ends_questions(text) is ends


def test_a_question_reply_parses_and_a_flawed_one_says_why() -> None:
    ok = parse_question_reply({"type": "question", "text": "  What happens first?  "})
    assert ok.question == "What happens first?" and ok.assumptions == ()
    done = parse_question_reply({"type": "questions_done", "assumptions": ["one user", "online"]})
    assert done.question is None and done.assumptions == ("one user", "online")
    assert parse_question_reply({"type": "questions_done"}).assumptions == ()
    for obj, why in [
        (None, "no JSON"),
        ({"type": "plan"}, "questions_done"),
        ({"type": "question"}, "non-empty"),
        ({"type": "question", "text": "  "}, "non-empty"),
        ({"type": "question", "text": "a" * 601}, "under 600"),
        ({"type": "question", "text": "Who? And why?"}, "one question at a time"),
        ({"type": "questions_done", "assumptions": 3}, "list of strings"),
    ]:
        with pytest.raises(StoryError, match=why):
            parse_question_reply(obj)


INITIATIVE = Initiative("AIR-40", "Linear feel", "text", ("o/r", "o/s"), None, False, ())
SEATS = ["architect", "ux", "design"]


def epic_obj(eid: str = "E1", kind: str = "core", **over: object) -> dict[str, object]:
    """One epic as product writes it (D5)."""
    base: dict[str, object] = {
        "id": eid,
        "kind": kind,
        "title": f"Epic {eid}",
        "why": "The board feels slow.",
        "goals": ["feels fast"],
        "non_goals": ["a redesign"],
        "anti_goals": ["losing a card"],
        "decisions": [{"what": "keep the board", "why": "people know it"}],
        "constraints": ["no new dependency"],
        "criteria": [{"id": "EC1", "text": "a card moves in one keystroke"}],
        "repositories": ["o/r"],
        "stories": [{"title": "keys", "size": "S", "scope": "keyboard moves"}],
        "risks": ["focus bugs"],
        "viability": {"verdict": "viable", "reasons": ["the app is small"]},
        "needs_mockups": False,
        "depends_on": [],
        "open": [],
    }
    return {**base, **over}


def epics_obj(*epics: dict[str, object]) -> dict[str, object]:
    return {"type": "epics", "summary": "Make the board fast.", "epics": list(epics or [epic_obj()])}


def imagine(obj: dict[str, object] | None, **kw: object) -> object:
    return parse_imagine_reply(obj, initiative=INITIATIVE, consultable=SEATS, max_asks=kw.pop("max_asks", 2))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("obj", "why"),
    [
        (None, "no JSON"),
        ({"type": "plan"}, "consult"),
        ({"type": "consult", "asks": []}, "at least one ask"),
        (
            {"type": "consult", "asks": [{"seat": "security", "question": "q"}]},
            "'security' is not one you can",
        ),
        (
            {"type": "consult", "asks": [{"seat": "ux", "question": "q"}] * 3},
            "at most 2 asks",
        ),
        ({"type": "consult", "asks": [{"seat": "design", "kind": "mockups", "brief": "b"}]}, "'epic' id"),
        ({"type": "consult", "asks": [{"seat": "design", "kind": "mockups", "epic": "E1"}]}, "brief"),
        ({"type": "consult", "asks": [{"seat": "ux", "question": "  "}]}, "non-empty"),
        (
            {"type": "consult", "asks": [{"seat": "ux", "kind": "chat", "question": "q"}]},
            "question or mockups",
        ),
    ],
)
def test_a_consult_round_parses_and_a_flawed_one_is_refused_with_a_reason(
    obj: dict[str, object] | None, why: str
) -> None:
    with pytest.raises(StoryError, match=why):
        imagine(obj)
    ok = imagine(
        {
            "type": "consult",
            "asks": [
                {"seat": "ux", "question": "How would a person move?"},
                {"seat": "design", "kind": "mockups", "epic": "E1", "brief": "the board screen"},
            ],
        }
    )
    assert ok == (
        Ask("ux", "How would a person move?"),
        Ask("design", "the board screen", "mockups", "E1"),
    )


def test_an_epics_reply_goes_through_parse_imagine_reply() -> None:
    got = imagine(epics_obj())
    assert isinstance(got, EpicsReply) and got.summary == "Make the board fast."


@pytest.mark.parametrize(
    ("epics", "why"),
    [
        ([epic_obj("E1"), epic_obj("E2")], "exactly one epic must have kind core, not 2"),
        ([epic_obj("E1", "extension")], "exactly one epic must have kind core, not 0"),
        ([epic_obj("E1"), epic_obj("E1", "extension")], "ids repeat"),
        (
            [epic_obj("E1", depends_on=["E2"]), epic_obj("E2", "extension")],
            "core E1 depends on the extension E2",
        ),
        (
            [
                epic_obj("E1"),
                epic_obj("E2", "extension", depends_on=["E3"]),
                epic_obj("E3", "extension", depends_on=["E2"]),
            ],
            "cycle: E2 -> E3 -> E2",
        ),
        ([epic_obj("E1"), epic_obj("E2", "extension", depends_on=["E9"])], "'E9', which is not one of"),
        ([epic_obj(criteria=[])], "at least one criterion"),
        (
            [epic_obj(criteria=[{"id": "EC1", "text": "a"}, {"id": "EC1", "text": "b"}])],
            "criterion ids repeat",
        ),
        ([epic_obj(stories=[{"title": "x", "scope": "y"}])], "'size' of S, M or L"),
        ([epic_obj(stories=[{"title": "x", "size": "XL"}])], "'size' of S, M or L"),
        (
            [epic_obj(viability={"verdict": "great", "reasons": ["x"]})],
            "verdict of viable, risky, not_viable",
        ),
        ([epic_obj(viability={"verdict": "viable", "reasons": []})], "at least one reason"),
        ([epic_obj(repositories=["evil/other"])], "not the initiative's repositories"),
        ([epic_obj(repositories=[])], "at least one repository"),
        ([epic_obj(title=" ")], "'title' must be a non-empty"),
        ([epic_obj(why="")], "'why' must be a non-empty"),
        ([epic_obj(id="first")], "'id' like E1"),
        ([epic_obj(kind="side")], "core or extension"),
    ],
)
def test_the_d5_rules_are_enforced(epics: list[dict[str, object]], why: str) -> None:
    with pytest.raises(StoryError, match=why):
        parse_epics(epics_obj(*epics), initiative=INITIATIVE)


def test_an_epics_reply_parses_in_order_and_the_d5_rules_are_enforced() -> None:
    reply = parse_epics(
        epics_obj(
            epic_obj("E3", "extension", depends_on=["E2"]),
            epic_obj("E2", "extension", depends_on=["E1"]),
            epic_obj("E1", needs_mockups=True),
        ),
        initiative=INITIATIVE,
    )
    assert [d.id for d in reply.order()] == ["E1", "E2", "E3"]  # the core, then by dependency
    core = reply.order()[0]
    assert core.needs_mockups and core.criteria[0].text == "a card moves in one keystroke"
    assert core.stories == (StorySketch("keys", "S", "keyboard moves"),)
    assert core.viability.verdict == "viable" and core.decisions[0].why == "people know it"
    # a draft round-trips through the state's dict
    from airlie.story.initiative import EpicDraft

    assert EpicDraft.from_dict(core.to_dict()) == core
    # a not_viable core is allowed: it is written and marked, not hidden
    bad = parse_epics(
        epics_obj(epic_obj(viability={"verdict": "not_viable", "reasons": ["needs a rewrite"]})),
        initiative=INITIATIVE,
    )
    assert bad.epics[0].viability.verdict == "not_viable"
    # a revision is one epic of any kind; its dependencies need not be siblings
    one = parse_epics(
        epics_obj(epic_obj("E2", "extension", depends_on=["E1"])), initiative=INITIATIVE, single=True
    )
    assert one.epics[0].depends_on == ("E1",)
    with pytest.raises(StoryError, match="exactly one epic"):
        parse_epics(
            epics_obj(epic_obj("E1"), epic_obj("E2", "extension")), initiative=INITIATIVE, single=True
        )


def test_a_core_over_the_budget_is_one_finding() -> None:
    reply = parse_epics(
        epics_obj(
            epic_obj("E1", stories=[{"title": "a", "size": "M", "scope": "a"}] * 3),
            epic_obj("E2", "extension", stories=[{"title": "b", "size": "M", "scope": "b"}] * 9),
        ),
        initiative=INITIATIVE,
    )

    def estimate(sketch: tuple[StorySketch, ...]) -> Estimate:
        return Estimate(5.0 * len(sketch), f"{len(sketch)} M at 5.0 h (default)", "t")

    [finding] = check_epics(reply, budget_hours=10.0, estimate=estimate)
    assert finding.kind == "over_budget" and finding.epic == "E1"  # the extension is not held to it
    assert "15 h" in finding.why and "3 M at 5.0 h (default)" in finding.why and "10 h budget" in finding.why
    assert check_epics(reply, budget_hours=15.0, estimate=estimate) == []
    assert check_epics(reply, budget_hours=None, estimate=estimate) == []
