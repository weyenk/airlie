"""The workflow a card runs: named by its `workflow:<name>` label (a story card without one runs `story`),
found among the repo's workflows and the board's drafts, and snapshotted into the card's state the first time
it is used, so every resume runs what the card started on whatever the drafts and files say since
(docs/superpowers/specs/2026-11-07-workflow-builder-design.md, D16, D17, D21)."""

# This module still reads the schema v1 fields; Task 9 rewrites it and removes the mypy ignore below.
# mypy: ignore-errors
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from airlie.flow.prompt_files import parse_draft_prompts
from airlie.flow.spec import WorkflowSpec, check_workflows
from airlie.prompts.hub import PromptError
from airlie.prompts.hub import load as hub_prompts
from airlie.story.board import Card
from airlie.story.model import StoryError

if TYPE_CHECKING:
    from airlie.board.model import WorkflowDraft
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


def running_workflow(saved: Mapping[str, Any] | None, labels: Iterable[str]) -> str:
    """The workflow a card runs, given its saved state (None: none yet) and its labels: the snapshot's name
    when the state holds one (D21); `story` when the state got past `new` with no snapshot, as a card the
    driver started (or an airlie from before workflows) has run the story's stages, which no snapshot
    records, so a label it gained since cannot apply; else the card's label, or `story`. `_drive`,
    `Line._workflow_hold` and `LocalBoard._unrunnable` all decide with this."""
    snap = (saved or {}).get("workflow")
    if isinstance(snap, Mapping) and snap.get("name"):
        return str(snap["name"])
    if saved is not None and saved.get("stage", "new") != "new":
        return DEFAULT
    return workflow_label(labels) or DEFAULT


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


DraftPrompts = Mapping[str, Mapping[str, str]]
"""The prompt files the drafts carry: draft name -> {prompt id: the file's whole text}."""


def closure_prompts(prompts: DraftPrompts | None, names: Iterable[str]) -> dict[str, str]:
    """The prompt files the drafts among `names` carry (a resolved workflow and its bundles), by id. Two of
    them carrying different texts under one id is a WorkflowError: a node could not say which it means; so is
    one carrying an id the hub has (a rule or steering): the hub's file comes first (D19), and publishing
    would overwrite it."""
    hub_ids = hub_prompts().keys()
    found: dict[str, str] = {}
    owner: dict[str, str] = {}
    for name in names:
        for id, text in (prompts or {}).get(name, {}).items():
            if id in hub_ids:
                raise WorkflowError(f"workflow {name}: prompt file '{id}' is already a hub prompt")
            if id in found and found[id] != text:
                raise WorkflowError(
                    f"draft workflows {owner[id]!r} and {name!r} carry different prompt files {id!r}"
                )
            found[id], owner[id] = text, name
    return found


def resolve_workflows(
    cfg: AirlieConfig,
    drafts: Mapping[str, str],
    name: str,
    *,
    prompts: DraftPrompts | None = None,
    own: bool = False,
) -> dict[str, WorkflowSpec]:
    """Workflow `name` and the bundles it calls through its `each` nodes, transitively: each from the repo's
    workflows first, else from the drafts (name -> YAML text), and checked together. Nothing else is read,
    so a half-edited draft no other card names never stops this one; the repo's files were checked when
    the config loaded. A file wins over a draft of its name, which is an edit of it waiting to be published
    or merged (`edits_file`), and that draft's prompt files are not read. `prompts` are the
    drafts' prompt files (`DraftPrompts`): those of the drafts resolved must each be a valid rule file, and
    a node may name them as `prompt:<id>` beside the hub's rules. `own`: `name` itself is read from its
    draft even when a file has the name, as the builder checks an edit of a file (never as a card runs)."""
    specs: dict[str, WorkflowSpec] = {}
    from_drafts: list[str] = []
    todo = [name]
    while todo:
        wanted = todo.pop()
        if wanted in specs:
            continue
        if wanted in cfg.workflows and not (own and wanted == name and wanted in drafts):
            spec = cfg.workflows[wanted]  # a same-name draft is a pending edit of the file: never run
        elif wanted in drafts:
            spec = draft_spec(wanted, drafts[wanted])
            from_drafts.append(wanted)
        elif wanted == name:
            raise WorkflowError(f"no workflow named {name!r}")
        else:  # a bundle that is nowhere: check_workflows names it on the node that calls it
            continue
        specs[wanted] = spec
        todo += [n.workflow for n in spec.nodes.values() if n.type == "each" and n.workflow is not None]
    try:
        texts = parse_draft_prompts(closure_prompts(prompts, from_drafts))
    except PromptError as e:
        raise WorkflowError(f"draft workflow {name!r}: prompt file {e}") from None
    from airlie.story.flow import STORY_CATALOG  # the story's checks; story.flow imports this module

    if problems := check_workflows(specs, prompts=texts, catalog=STORY_CATALOG):
        raise WorkflowError("; ".join(problems))
    return specs


def edits_file(cfg: AirlieConfig, name: str) -> bool:
    """Whether a draft named `name` is an edit of a loaded file: it never runs (the file does) and its
    Publish changes that file."""
    return name in cfg.workflows


def drafted(cfg: AirlieConfig, names: Iterable[str]) -> list[str]:
    """The names among `names` that resolve to drafts, not files."""
    return [n for n in names if not edits_file(cfg, n)]


def workflow_problems(
    cfg: AirlieConfig,
    drafts: Mapping[str, str],
    name: str,
    *,
    prompts: DraftPrompts | None = None,
    own: bool = False,
) -> list[str]:
    """What stops workflow `name` resolving (`resolve_workflows`), as the builder shows it: [] or the one
    WorkflowError's message."""
    try:
        resolve_workflows(cfg, drafts, name, prompts=prompts, own=own)
    except WorkflowError as e:
        return [str(e)]
    return []


def board_problems(cfg: AirlieConfig, drafts: Iterable[WorkflowDraft], name: str) -> list[str]:
    """`workflow_problems` over the board's drafts as they are: their YAML and the prompt files they carry."""
    rows = list(drafts)
    texts = {d.name: d.yaml for d in rows}
    return workflow_problems(cfg, texts, name, prompts={d.name: d.prompts for d in rows})


def snapshot(
    name: str, specs: Mapping[str, WorkflowSpec], prompts: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """What `StoryState.workflow` keeps: the workflow's name, the specs resolved with it (it and the
    bundles it calls), as JSON, and the drafts' prompt files they may name (id -> text), so a draft's
    prompts edited after the card started change nothing for it."""
    return {
        "name": name,
        "specs": {n: s.model_dump(mode="json") for n, s in specs.items()},
        "prompts": dict(prompts or {}),
    }


def from_snapshot(data: Mapping[str, Any]) -> tuple[str, dict[str, WorkflowSpec], dict[str, str]]:
    """The name, specs and prompt files a snapshot keeps (none in one saved before snapshots kept them)."""
    name = str(data["name"])
    try:
        specs = {n: WorkflowSpec.model_validate(s) for n, s in data["specs"].items()}
    except ValidationError as e:  # saved by an older airlie whose spec this one no longer reads
        raise WorkflowError(f"snapshot of {name!r} no longer validates: {e}") from None
    return name, specs, {str(id): str(text) for id, text in data.get("prompts", {}).items()}


def has_node(spec: WorkflowSpec, name: str) -> bool:
    return name in spec.nodes


def workflow_for(
    pipe: StoryPipeline, st: StoryState, card: Card, name: str | None = None
) -> tuple[str, dict[str, WorkflowSpec], dict[str, str]]:
    """The workflow `st` runs and the drafts' prompt files it may name: its snapshot, else `name` (the
    card's, `card_workflow`, unless given), resolved (the pipeline's drafts and their prompts read now) and
    snapshotted into the state, saved."""
    if st.workflow is not None:
        return from_snapshot(st.workflow)
    name = name or card_workflow(card)
    prompts = pipe.draft_prompts()
    specs = resolve_workflows(pipe.cfg, pipe.drafts(), name, prompts=prompts)
    texts = closure_prompts(prompts, drafted(pipe.cfg, specs))
    st.workflow = snapshot(name, specs, texts)
    st.save()
    return name, specs, texts
