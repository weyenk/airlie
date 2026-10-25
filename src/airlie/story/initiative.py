"""The initiative and what product says about it (the specs phase, P4 of the planning line, 2026-10-25).

An initiative is a board item at level `initiative`: a person's paragraph as the description, one or more
`repo:<owner>/<name>` labels, an optional `budget:<hours>h` label. It is read as data. A repository name
reaches a checkout only through the labels and `story.repos_root`; an upload linked in the text is named by
its id and resolved through the upload store, never fetched by URL."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from airlie.board import Item
from airlie.story.board import Card
from airlie.story.checkout import repo_labels
from airlie.story.epic import REPOSITORY
from airlie.story.model import NO_JSON, StoryError, _encodable, _strs

BUDGET_LABEL = re.compile(r"budget:(\d+(?:\.\d+)?)h")
UPLOAD_LINK = re.compile(r"/v1/airlie/uploads/([A-Za-z0-9_-]+)")
SKIP_PHRASES = ("no questions", "skip the questions")
_END_WORDS = frozenset({"go", "skip"})
_BUDGET_PREFIX = "budget:"


@dataclass(frozen=True)
class Initiative:
    """What the phase reads from the card: the person's text is `text`, quoted into briefs, never paraphrased
    into an epic as theirs. `attachments` are the ids of the uploads the text links, in order, once each."""

    key: str
    title: str
    text: str
    repositories: tuple[str, ...]
    budget_hours: float | None
    skip_questions: bool
    attachments: tuple[str, ...]

    @classmethod
    def from_item(cls, item: Item) -> Initiative:
        """StoryError (a reason a person can act on): the item's level is not initiative; it has no repo:
        label, or one that is not owner/name; a budget label that is not a number of hours."""
        if item.level != "initiative":
            raise StoryError(f"{item.key} is a {item.level}, not an initiative")
        repos: list[str] = []
        for label in repo_labels(item.labels):
            named = label.split(":", 1)[1]
            if not REPOSITORY.fullmatch(named) or ".." in named.split("/"):
                raise StoryError(
                    f"the label {label!r} is not repo:<owner>/<name> (letters, digits, '.', '-', '_')"
                )
            if named not in repos:
                repos.append(named)
        if not repos:
            raise StoryError(
                "the initiative names no repository: add a label repo:<owner>/<name> for each repository it "
                "concerns"
            )
        budget: float | None = None
        for label in item.labels:
            if not label.lower().startswith(_BUDGET_PREFIX):
                continue
            m = BUDGET_LABEL.fullmatch(label)
            if m is None:
                raise StoryError(
                    f"the label {label!r} is not a budget: write budget:<hours>h, budget:20h say"
                )
            budget = float(m.group(1))
        lowered = item.description.lower()
        return cls(
            item.key,
            item.title,
            item.description,
            tuple(repos),
            budget,
            any(phrase in lowered for phrase in SKIP_PHRASES),
            tuple(dict.fromkeys(UPLOAD_LINK.findall(item.description))),
        )

    def card(self) -> Card:
        """The initiative as the briefs that take a Card read it (the architect's survey)."""
        return Card(key=self.key, id=self.key, ref=self.key, title=self.title, description=self.text)


def answer_ends_questions(text: str) -> bool:
    """A reply of "go" or "skip" alone, in any case, with trailing punctuation: the person has said enough."""
    return text.strip().strip(".!?,;: ").lower() in _END_WORDS


# ---- the question stage (D2) ----

QUESTION_CONTRACT = """End your reply with one ```json block holding exactly one object, either
{"type": "question", "text": "one open question"}
or, when everything that matters is in the request,
{"type": "questions_done", "assumptions": ["what you will work from, one line each"]}"""

MAX_QUESTION_CHARS = 600


@dataclass(frozen=True)
class QuestionReply:
    """Product's reply in the question stage: one question, or the end of the questions with the assumptions
    it goes on with."""

    question: str | None
    assumptions: tuple[str, ...]


def parse_question_reply(obj: dict[str, Any] | None) -> QuestionReply:
    """StoryError (product's next try): no JSON; a type that is neither `question` nor `questions_done`; a
    question that is empty, over 600 characters or holds two question marks (one question at a time);
    assumptions that are not strings."""
    if obj is None:
        raise StoryError(NO_JSON)
    kind = obj.get("type")
    if kind == "questions_done":
        raw = obj.get("assumptions")
        if raw is not None and not isinstance(raw, list | str):
            raise StoryError("'assumptions' must be a list of strings")
        return QuestionReply(None, _strs(raw, "'assumptions'"))
    if kind != "question":
        raise StoryError('reply with {"type": "question", ...} or {"type": "questions_done", ...}')
    text = obj.get("text")
    if not isinstance(text, str) or not text.strip():
        raise StoryError("a question needs a non-empty 'text'")
    text = _encodable(text.strip(), "the question")
    if len(text) > MAX_QUESTION_CHARS:
        raise StoryError(f"the question is {len(text)} characters; keep it under {MAX_QUESTION_CHARS}")
    if text.count("?") > 1:
        raise StoryError("ask one question at a time: that message holds more than one question mark")
    return QuestionReply(text, ())
