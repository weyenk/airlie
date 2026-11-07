"""The workflow a card runs: named by its `workflow:<name>` label (a story card without one runs `story`),
found among the repo's workflows and the board's drafts, and snapshotted into the card's state the first time
it is used, so every resume runs what the card started on whatever the drafts and files say since
(docs/superpowers/specs/2026-11-07-workflow-builder-design.md, D16, D17, D21)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from airlie.flow.spec import WorkflowSpec, check_workflows
from airlie.story.board import Card
from airlie.story.model import StoryError

if TYPE_CHECKING:
    from airlie.config import AirlieConfig
    from airlie.story.pipeline import StoryPipeline
    from airlie.story.state import StoryState

LABEL = "workflow:"
DEFAULT = "story"


class WorkflowError(StoryError):
    """A card's workflow that cannot be run: not there, or a draft that does not parse or check. The message
    names the workflow and the problem; the story is blocked with it."""


def workflow_label(labels: Iterable[str]) -> str | None:
    """The name the first `workflow:` label that names one gives, else None (the card runs `story`)."""
    named = (x.removeprefix(LABEL).strip() for x in labels if x.startswith(LABEL))
    return next((n for n in named if n), None)


def card_workflow(card: Card) -> str:
    """The name the card's first `workflow:` label gives, else `story` (D17)."""
    return workflow_label(card.labels) or DEFAULT


def draft_body(text: str, name: str | None = None) -> tuple[str, Mapping[str, Any]]:
    """The one workflow a draft's YAML text holds: `workflows:` with a single name mapped to a mapping (that
    name `name`, when given). Its name and mapping; WorkflowError when the text is not YAML or holds anything
    else. The mapping is not yet checked as a workflow."""
    called = repr(name) if name is not None else "draft"
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise WorkflowError(f"draft workflow {called} is not YAML: {e}") from None
    found = data.get("workflows") if isinstance(data, dict) else None
    names = sorted(map(str, found)) if isinstance(found, dict) else []
    if not isinstance(found, dict) or len(found) != 1 or (name is not None and list(found) != [name]):
        one = f"the one workflow {name!r}" if name is not None else "one workflow"
        raise WorkflowError(f"draft workflow {called} must hold `workflows:` with {one}, not {names}")
    [(got, body)] = found.items()
    if not isinstance(body, dict):
        raise WorkflowError(f"draft workflow {str(got)!r} must be a mapping, not {type(body).__name__}")
    return str(got), body


def draft_spec(name: str, text: str) -> WorkflowSpec:
    """Draft `name`'s YAML text: `workflows:` holding that one workflow."""
    _, body = draft_body(text, name)
    try:
        return WorkflowSpec.model_validate(body)
    except ValidationError as e:
        raise WorkflowError(f"draft workflow {name!r}: {e}") from None


def resolve_workflows(cfg: AirlieConfig, drafts: Mapping[str, str], name: str) -> dict[str, WorkflowSpec]:
    """Workflow `name` and the bundles it calls through its `each` nodes, transitively: each from the repo's
    workflows first, else from the drafts (name -> YAML text), and checked together. Nothing else is read,
    so a half-edited draft no other card names never stops this one; the repo's files were checked when
    the config loaded. A draft may not shadow a workflow the repo's files define."""
    specs: dict[str, WorkflowSpec] = {}
    todo = [name]
    while todo:
        wanted = todo.pop()
        if wanted in specs:
            continue
        if wanted in cfg.workflows:
            if wanted in drafts:
                raise WorkflowError(
                    f"draft workflow {wanted!r} shadows the workflow of that name in the repo's files"
                )
            spec = cfg.workflows[wanted]
        elif wanted in drafts:
            spec = draft_spec(wanted, drafts[wanted])
        elif wanted == name:
            raise WorkflowError(f"no workflow named {name!r}")
        else:  # a bundle that is nowhere: check_workflows names it on the node that calls it
            continue
        specs[wanted] = spec
        todo += [n.workflow for n in spec.nodes.values() if n.type == "each" and n.workflow is not None]
    if problems := check_workflows(specs):
        raise WorkflowError("; ".join(problems))
    return specs


def workflow_problems(cfg: AirlieConfig, drafts: Mapping[str, str], name: str) -> list[str]:
    """What stops workflow `name` resolving (`resolve_workflows`), as the builder shows it: [] or the one
    WorkflowError's message."""
    try:
        resolve_workflows(cfg, drafts, name)
    except WorkflowError as e:
        return [str(e)]
    return []


def snapshot(name: str, specs: Mapping[str, WorkflowSpec]) -> dict[str, Any]:
    """What `StoryState.workflow` keeps: the workflow's name and the specs resolved with it (it and the
    bundles it calls), as JSON."""
    return {"name": name, "specs": {n: s.model_dump(mode="json") for n, s in specs.items()}}


def from_snapshot(data: Mapping[str, Any]) -> tuple[str, dict[str, WorkflowSpec]]:
    name = str(data["name"])
    try:
        return name, {n: WorkflowSpec.model_validate(s) for n, s in data["specs"].items()}
    except ValidationError as e:  # saved by an older airlie whose spec this one no longer reads
        raise WorkflowError(f"snapshot of {name!r} no longer validates: {e}") from None


def has_node(spec: WorkflowSpec, name: str) -> bool:
    return name in spec.nodes


def workflow_for(pipe: StoryPipeline, st: StoryState, card: Card) -> tuple[str, dict[str, WorkflowSpec]]:
    """The workflow `st` runs: its snapshot, else the card's, resolved (the pipeline's drafts read now) and
    snapshotted into the state, saved."""
    if st.workflow is not None:
        return from_snapshot(st.workflow)
    name = card_workflow(card)
    specs = resolve_workflows(pipe.cfg, pipe.drafts(), name)
    st.workflow = snapshot(name, specs)
    st.save()
    return name, specs
