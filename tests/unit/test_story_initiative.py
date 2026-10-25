"""The initiative read from its card, and the contracts of product's replies (the specs phase, P4 of the
planning line, 2026-10-25)."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.board import BoardStore, Item
from airlie.story.initiative import (
    Initiative,
    answer_ends_questions,
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
