"""Prompt files, the second kind of prompt a node may name
(docs/superpowers/specs/2026-11-07-workflow-builder-design.md, "Prompts without code", D13, D19):
`prompt: "prompt:<id>"` is a Markdown file of the prompt hub's `rules/`, or one a draft workflow carries
under that id, rendered with the node's inputs as its fields (`airlie.flow.render`, as inline text is), so a
workflow built in the GUI needs no Python for its briefs.

A field named `feedback` is the reason the last reply did not parse, then the loop's carried feedback, a
blank line between, blank on a first try; every other field is one of the node's inputs (`check_workflows`
holds a file to them), filled with its value in the store as `render_value` shows it."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from airlie.prompts import hub
from airlie.prompts.hub import PromptError, PromptText

if TYPE_CHECKING:
    pass

PREFIX = "prompt:"
FEEDBACK = "feedback"


def rule_ids() -> set[str]:
    """The ids of the hub's rules: the files a node may name without a draft carrying them."""
    return {p.id for p in hub.load().values() if p.kind == "rule"}


def prompt_text(id: str, texts: Mapping[str, PromptText]) -> PromptText:
    """The hub's rule `id`, else the draft's file of that id in `texts`; `PromptError` when neither is (or
    the hub's is steering). The hub comes first (D19); a draft may not carry a hub id at all
    (`story.workflows.closure_prompts`)."""
    found = hub.load().get(id)
    if found is None:
        if id in texts:
            return texts[id]
        return hub.entry(id)  # raises, naming the id
    if found.kind != "rule":
        raise PromptError(f"prompt file {id!r} is steering, not a rule a node may name")
    return found


def render_value(value: object) -> str:
    """A field's value as a brief shows it: a string as is; a card (anything with a `title` and a
    `description`) as its title, a blank line and its description; a mapping or a list as indented JSON;
    anything else `str()`."""
    if isinstance(value, str):
        return value
    title, description = getattr(value, "title", None), getattr(value, "description", None)
    if isinstance(title, str) and isinstance(description, str):
        return f"{title}\n\n{description}"
    if isinstance(value, Mapping | Sequence):
        return json.dumps(value, indent=1, default=str)
    return str(value)


def _feedback(reason: str | None, carried: object) -> str:
    """The `feedback` field: why the last reply did not parse, then what a loop carried back, a blank line
    between; blank on a first try."""
    parts = [reason or "", render_value(carried) if carried is not None else ""]
    return "\n\n".join(p for p in parts if p)


def parse_draft_prompts(prompts: Mapping[str, str]) -> dict[str, PromptText]:
    """A draft's prompt files (id -> the file's whole text), each checked as a rule of the hub would be
    (`hub.parse`): `PromptError` naming the first that is not."""
    return {id: hub.parse(id, "rule", raw) for id, raw in prompts.items()}
