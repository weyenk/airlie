"""Prompt files, the second kind of prompt a node may name
(docs/superpowers/specs/2026-11-07-workflow-builder-design.md, "Prompts without code", D13, D19):
`prompt: "prompt:<id>"` is a Markdown file of the prompt hub's `rules/`, or one a draft workflow carries
under that id, rendered with the node's inputs as its fields. One generic brief builder serves every such
file, so a workflow built in the GUI needs no Python for its briefs.

A field named `feedback` is the carried loop feedback (or the reason the last reply did not parse), blank on a
first try; every other field is a name the node reads (one of its inputs, or a loose name of its workflow),
filled with `str()` of its value in the store."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from airlie.prompts import hub
from airlie.prompts.hub import PromptError, PromptText

if TYPE_CHECKING:
    from airlie.flow.composer import RunContext

PREFIX = "prompt:"
FEEDBACK = "feedback"


def rule_ids() -> set[str]:
    """The ids of the hub's rules: the files a node may name without a draft carrying them."""
    return {p.id for p in hub.load().values() if p.kind == "rule"}


def prompt_text(id: str, texts: Mapping[str, PromptText]) -> PromptText:
    """The draft's file `id` when `texts` holds one, else the hub's rule; `PromptError` when neither is."""
    if id in texts:
        return texts[id]
    found = hub.entry(id)
    if found.kind != "rule":
        raise PromptError(f"prompt file {id!r} is steering, not a rule a node may name")
    return found


def prompt_builder(id: str, texts: Mapping[str, PromptText]) -> Callable[[RunContext, str | None], str]:
    """The brief builder for `prompt:<id>`: `(ctx, feedback) -> str`, as a registered brief is called. The
    file is looked up now, so an unknown id fails when the node resolves its pieces, before any ask."""
    prompt = prompt_text(id, texts)

    def brief(ctx: RunContext, feedback: str | None) -> str:
        fields: dict[str, str] = {}
        for name in prompt.fields:
            if name == FEEDBACK:
                fields[name] = feedback or ""
            elif name not in ctx.inputs:
                raise PromptError(
                    f"node {ctx.node} of workflow {ctx.workflow}: prompt file {id!r} reads {name!r}, "
                    "which is not an input of the node"
                )
            else:
                fields[name] = str(ctx.store.get(name))
        return hub.render(prompt, **fields)

    return brief


def parse_draft_prompts(prompts: Mapping[str, str]) -> dict[str, PromptText]:
    """A draft's prompt files (id -> the file's whole text), each checked as a rule of the hub would be
    (`hub.parse`): `PromptError` naming the first that is not."""
    return {id: hub.parse(id, "rule", raw) for id, raw in prompts.items()}
