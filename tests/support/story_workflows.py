"""Shared helpers extracted from tests/unit/test_story_workflows.py for tests.support.story_workflows."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from airlie.story.board import Card, text_card
from tests.support.story import kinds_all

MINI = """\
workflows:
  mini:
    type: bug
    inputs: [card, repo]
    nodes:
      intake: {type: check, run: story.intake, inputs: [card], to: [base]}
      base: {type: check, run: story.base, inputs: [card, repo], outputs: {base: json}, to: [gate]}
      gate: {type: check, run: story.gate, inputs: [repo, base], outputs: {gate: json}, to: [review]}
      review: {type: gate, run: story.finish, inputs: [card]}
"""


def before_review(text: str, *nodes: str) -> str:
    """`text` (MINI, or one made from it) with `nodes` (each `name: {...}`, with no `to`) drawn in order
    between the gate and the review."""
    names = [n.split(":", 1)[0] for n in nodes]
    drawn = "".join(
        f"      {n[:-1]}, to: [{to}]}}\n" for n, to in zip(nodes, [*names[1:], "review"], strict=True)
    )
    return text.replace("to: [review]}", f"to: [{names[0]}]}}").replace(
        "      review:", drawn + "      review:"
    )


MINI_MORE = before_review(
    MINI, "costed: {type: check, run: story.costed, inputs: [card], args: {stages: [plan]}}"
)

MINI_EACH = before_review(
    MINI.replace("    inputs: [card, repo]\n", "    inputs: [card, repo, layers]\n"),
    "build_layers: {type: bundle, workflow: tiny, each: layers, inputs: [layers, card]}",
)

NOTE = """\
---
kind: rule
roles: [product]
fields: [card]
why: The mini workflow's note, written in the builder.
---
Write one line about this card: {card}
"""

MINI_NOTE = before_review(
    MINI,
    "note: {type: ask, role: product, prompt: 'prompt:mini-note', inputs: [card],"
    " outputs: {artifacts.note: text}}",
)

TINY = """\
workflows:
  tiny:
    inputs: [card]
    nodes:
      note: {type: check, run: story.costed, inputs: [card], args: {stages: [layer]}}
"""


def mini_card(text: str = "fix the marker") -> Card:
    return replace(text_card(text), labels=("workflow:mini",))


def started(path: Path) -> list[tuple[str, str | None]]:
    return [(e["node"], e.get("item")) for e in kinds_all(path) if e["kind"] == "node_started"]
