"""The workflow a card runs: named by its `workflow:<name>` label (a story card without one runs `story`),
found among the repo's workflows and the board's drafts, and snapshotted into the card's state the first time
it is used, so every resume runs what the card started on whatever the drafts and files say since
(docs/superpowers/specs/2026-11-07-workflow-builder-design.md, D16, D17, D21)."""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

from airlie.flow.prompt_files import PREFIX, parse_draft_prompts
from airlie.flow.spec import NodeSpec, WorkflowSpec, check_workflows
from airlie.flow.upgrade import upgrade_topology_types, upgrade_v1
from airlie.prompts.hub import PromptError
from airlie.prompts.hub import load as hub_prompts
from airlie.story.board import Card, note
from airlie.story.model import StoryError, StoryRunning

if TYPE_CHECKING:
    from airlie.board.model import WorkflowDraft
    from airlie.config import AirlieConfig
    from airlie.flow.catalog import Catalog
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


Snapshot = dict[str, Any]
"""What `StoryState.workflow` keeps (`snapshot`)."""


def _catalog() -> Catalog:
    from airlie.story.flow import STORY_CATALOG  # the story's checks; story.flow imports this module

    return STORY_CATALOG


def upgraded_draft(name: str, text: str) -> tuple[WorkflowSpec, list[str]]:
    """Draft `name`'s YAML text (`workflows:` holding that one workflow), an old-schema one upgraded
    (`upgrade_v1`, D21), and what the upgrade left for a person to write ("workflow <name>: node <n>: ...").
    A draft that does not parse or validate is a WorkflowError."""
    _, body = draft_body(text, name)
    data, problems = upgrade_v1(body, catalog=_catalog())
    data = upgrade_topology_types(data)
    try:
        spec = WorkflowSpec.model_validate(data)
    except ValidationError as e:
        raise WorkflowError(f"draft workflow {name!r}: {e}") from None
    return spec, [f"workflow {name}: {p}" for p in problems]


def draft_spec(name: str, text: str) -> WorkflowSpec:
    """Draft `name`'s YAML text, upgraded when it is the old schema (`upgraded_draft`; what the upgrade left
    is not said here)."""
    return upgraded_draft(name, text)[0]


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
    deprecations: bool = False,
) -> dict[str, WorkflowSpec]:
    """Workflow `name` and the bundles it calls through its `bundle` nodes, transitively: each from the repo's
    workflows first, else from the drafts (name -> YAML text), and checked together. Nothing else is read,
    so a half-edited draft no other card names never stops this one; the repo's files were checked when
    the config loaded. A file wins over a draft of its name, which is an edit of it waiting to be published
    or merged (`edits_file`), and that draft's prompt files are not read. `prompts` are the
    drafts' prompt files (`DraftPrompts`): those of the drafts resolved must each be a valid rule file, and
    a node may name them as `prompt:<id>` beside the hub's rules. `own`: `name` itself is read from its
    draft even when a file has the name, as the builder checks an edit of a file (never as a card runs)."""
    specs: dict[str, WorkflowSpec] = {}
    from_drafts: list[str] = []
    upgrade: list[str] = []  # what an old-schema draft's upgrade left for a person to write (D21)
    todo = [name]
    while todo:
        wanted = todo.pop()
        if wanted in specs:
            continue
        if wanted in cfg.workflows and not (own and wanted == name and wanted in drafts):
            spec = cfg.workflows[wanted]  # a same-name draft is a pending edit of the file: never run
        elif wanted in drafts:
            spec, left = upgraded_draft(wanted, drafts[wanted])
            upgrade += left
            from_drafts.append(wanted)
        elif wanted == name:
            raise WorkflowError(f"no workflow named {name!r}")
        else:  # a bundle that is nowhere: check_workflows names it on the node that calls it
            continue
        specs[wanted] = spec
        todo += [n.workflow for n in spec.nodes.values() if n.type == "bundle" and n.workflow is not None]
    try:
        texts = parse_draft_prompts(closure_prompts(prompts, from_drafts))
    except PromptError as e:
        raise WorkflowError(f"draft workflow {name!r}: prompt file {e}") from None
    if problems := upgrade + check_workflows(
        specs, prompts=texts, catalog=_catalog(), deprecations=deprecations
    ):
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
        resolve_workflows(cfg, drafts, name, prompts=prompts, own=own, deprecations=True)
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
    """The name, specs and prompt files a snapshot keeps (none in one saved before snapshots kept them). A
    spec saved in the old schema is upgraded (`upgrade_v1`, D21); one whose upgrade leaves a problem (a node
    that named code) is a WorkflowError listing them, and the card can adopt the current workflow
    (`adopt_current`) or start over."""
    name = str(data["name"])
    upgraded: dict[str, dict[str, Any]] = {}
    left: list[str] = []
    for wname, raw in data["specs"].items():
        upgraded[wname], problems = upgrade_v1(raw, catalog=_catalog())
        left += [f"workflow {wname}: {p}" for p in problems]
    if left:
        raise WorkflowError(f"snapshot of {name!r} is the old schema and cannot run: {'; '.join(left)}")
    try:
        specs = {n: WorkflowSpec.model_validate(s) for n, s in upgraded.items()}
    except ValidationError as e:  # saved by an older airlie whose spec this one no longer reads
        raise WorkflowError(f"snapshot of {name!r} no longer validates: {e}") from None
    return name, specs, {str(id): str(text) for id, text in data.get("prompts", {}).items()}


def spec_hash(data: Mapping[str, Any] | None) -> str | None:
    """A short hash of a snapshot's specs as JSON (None for no snapshot): what `workflow_adopted` names."""
    if data is None:
        return None
    text = json.dumps(data.get("specs"), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def changed_nodes(
    old: Mapping[str, Any],
    name: str,
    specs: Mapping[str, WorkflowSpec],
    *,
    prompts: Mapping[str, str] | None = None,
) -> set[str]:
    """The nodes of workflow `name` in `specs` whose definition differs from the one snapshot `old` keeps
    (their JSON compared; an old-schema snapshot is upgraded first), those whose `prompt`, `turn` or
    `worker` names a `prompt:<id>` whose text in `prompts` (the new snapshot's drafts' files) is not the
    text `old` keeps for it, and the bundle nodes whose workflow differs at all (a node of it naming such
    a file too). A node `old` does not have is not among them: it has no outputs yet, so it runs anyway. A
    snapshot of another workflow shares no nodes with `specs`; one that cannot be read is compared as it
    was saved."""
    if old.get("name") != name:
        return set()
    try:
        kept = {w: s.model_dump(mode="json") for w, s in from_snapshot(old)[1].items()}
    except (WorkflowError, KeyError, TypeError, AttributeError):
        kept = dict(old.get("specs") or {})
    was = (kept.get(name) or {}).get("nodes") or {}
    texts_were, texts = dict(old.get("prompts") or {}), dict(prompts or {})

    def reworded(node: NodeSpec) -> bool:
        ids = [
            t.removeprefix(PREFIX)
            for t in (node.prompt, node.turn, node.worker)
            if t and t.startswith(PREFIX)
        ]
        return any(texts_were.get(id) != texts.get(id) for id in ids)

    found: set[str] = set()
    for n, node in specs[name].nodes.items():
        if n not in was:
            continue
        inner = specs.get(node.workflow) if node.type == "bundle" and node.workflow is not None else None
        if (
            node.model_dump(mode="json") != was[n]
            or reworded(node)
            or (
                inner is not None
                and (
                    kept.get(str(node.workflow)) != inner.model_dump(mode="json")
                    or any(reworded(m) for m in inner.nodes.values())
                )
            )
        ):
            found.add(n)
    return found


def _below(spec: WorkflowSpec, names: Iterable[str]) -> set[str]:
    """`names` and every node their forward lines reach."""
    seen: set[str] = set()
    todo = list(names)
    while todo:
        at = todo.pop()
        if at in seen or at not in spec.nodes:
            continue
        seen.add(at)
        todo += [ln.node for ln in spec.nodes[at].to if ln.loop is None]
    return seen


def _outputs(spec: WorkflowSpec, specs: Mapping[str, WorkflowSpec], node: str) -> list[str]:
    """What node `node` of `spec` writes to the story's store: its outputs and, for a bundle node, every
    output of the workflow it runs (nested bundles' too). An item-scoped name (`built.{item}`) is its head
    (`built`), whole: every item runs again."""
    n = spec.nodes[node]
    names = list(n.outputs)
    if n.type == "bundle" and n.workflow is not None and n.workflow in specs:
        inner = specs[n.workflow]
        names += [o for m in inner.nodes for o in _outputs(inner, specs, m)]
    return list(dict.fromkeys(o.partition(".")[0] if "{item}" in o else o for o in names))


def send_back_changed(
    st: StoryState,
    card: Card,
    repo: Any,
    name: str,
    specs: Mapping[str, WorkflowSpec],
    changed: Iterable[str],
) -> list[str]:
    """Treat each of the `changed` nodes of workflow `name` like a loop's target (the composer's
    `send_back`): its outputs and those of every node below it are cleared, and they leave `st.nodes_done`
    (a bundle node's items too), so the next run runs them again. When one of them runs an operation a stage
    gates (`restart.OPERATIONS`: refine, the layer split, the meeting, the build's end, the publish) the
    stages from the earliest such point on are cleared as a restart from there clears them
    (`restart.rewind`), so the operation does not find its stage done and skip itself. Returns the nodes
    that will run again, sorted: those sent back, and any other node that ran and whose output this
    cleared (refine, when the story is cleared). Nothing is saved here."""
    from airlie.flow.spec import transient_heads
    from airlie.flow.store import MEMORY, StateStore
    from airlie.story.restart import past, rewind, rewind_point  # restart imports this, through the board

    spec = specs[name]
    again = _below(spec, changed)
    store = StateStore(st, loose={"card": card, "repo": repo}, transient=transient_heads(specs))
    ran = {n for n in spec.nodes if n in st.nodes_done}
    present = {n: [o for o in _written(spec, n) if store.has(o)] for n in ran - again}
    for n in again:
        for out in _outputs(spec, specs, n):
            if out.partition(".")[0] in MEMORY:
                continue
            with contextlib.suppress(KeyError):  # a loose or reserved name holds nothing of the node's
                store.put(out, None)
        st.nodes_done = [d for d in st.nodes_done if d != n and not d.startswith((f"{n}/", f"{n}@"))]
    point = rewind_point({n: spec.nodes[n] for n in again})
    if point is not None and past(st, point):
        rewind(st, point)
    cleared = {n for n, outs in present.items() if any(not store.has(o) for o in outs)}
    return sorted(again | cleared)


def _written(spec: WorkflowSpec, node: str) -> list[str]:
    """The outputs of `node` the story's state keeps (not an item's, a transient one or a session)."""
    return [
        o
        for o, out in spec.nodes[node].outputs.items()
        if "{item}" not in o and not out.transient and out.format != "session"
    ]


async def adopt_current(
    pipe: StoryPipeline,
    st: StoryState,
    card: Card,
    *,
    running: Callable[[str], bool] | None = None,
    by: str | None = None,
) -> Snapshot:
    """The card takes the current workflow in place of its snapshot (D24): `card_workflow(card)` resolved now
    over the pipeline's drafts and their prompts, snapshotted into `st`. A node whose definition changed
    (`changed_nodes`) is treated like a loop's target: its outputs and those of every node below it are
    cleared and they leave the done set (`send_back_changed`), so the new definition runs. A new node runs
    because it has no outputs; an unchanged one is skipped by its outputs. A changed node that runs an
    operation a stage gates clears the stages from its point on, as a restart from there would; the note
    names every node that will run again. Every other field of the state is kept. The note goes on the
    card first (a board that refuses it changes nothing), then the state is saved and `workflow_adopted
    {name, from, to}` (the old and new specs' `spec_hash`) emitted; returns the new snapshot. From
    the running check to the end the card waits out of the line, as a restart holds it (`restart._hold`),
    and then goes back where it was.

    Refused with StoryRunning while the story runs (its serve job, by `running`, or a lane: restart's rule),
    and with a WorkflowError when the card keeps no snapshot (it runs without one, so there is nothing to
    replace) or the current workflow does not resolve; nothing is changed by a refusal."""
    from airlie.story.restart import (
        _hold,
        _running_now,
    )  # restart imports the local board, which imports this

    why = _running_now(card.key, st.path.parent, running)
    if why is not None:
        raise StoryRunning(
            f"story {card.key} is running now ({why}): use the current workflow once it has stopped"
        )
    old = st.workflow
    if old is None:
        raise WorkflowError(
            f"story {card.key} keeps no workflow snapshot: it runs without one, so nothing is replaced"
        )
    back = await _hold(pipe.board, card, by or "person")
    try:
        name = card_workflow(card)
        prompts = pipe.draft_prompts()
        specs = resolve_workflows(pipe.cfg, pipe.drafts(), name, prompts=prompts)
        new = snapshot(name, specs, closure_prompts(prompts, drafted(pipe.cfg, specs)))
        before, after = spec_hash(old), spec_hash(new)
        was = f"{old.get('name')} {before}" if old.get("name") != name else str(before)
        changed = changed_nodes(old, name, specs, prompts=new["prompts"])
        trial = copy.deepcopy(st)  # what the adoption leaves, worked out before the note: none if refused
        again = send_back_changed(trial, card, pipe.repo, name, specs, changed)
        rerun = f"; changed, so run again: {', '.join(again)}" if again else ""
        asked = f", as {by} asked" if by else ""
        said = f"now runs the current {name} workflow ({after}, in place of {was}){asked}{rerun}"
        await pipe.board.comment(card, note(f"{said}; its next run goes on from what it has"))
        for f in dataclasses.fields(st):
            setattr(st, f.name, getattr(trial, f.name))
        st.workflow = new
        st.save()
        st.emit("workflow_adopted", name=name, **{"from": before, "to": after})
    finally:
        with contextlib.suppress(Exception):  # the refusal (or the adoption) is what the person needs
            await back()
    return new


def bundle_users(cfg: AirlieConfig, drafts: Mapping[str, str]) -> dict[str, list[str]]:
    """Each workflow a `bundle` node runs (an old draft's `each` node, as upgraded, too) -> the other
    workflows, the repo's and the drafts' (name -> YAML text), whose nodes run it, sorted. A draft that does
    not read runs none."""
    specs: list[tuple[str, WorkflowSpec]] = list(cfg.workflows.items())
    for wname, text in drafts.items():
        try:
            specs.append((wname, draft_spec(wname, text)))
        except WorkflowError:
            continue
    found: dict[str, set[str]] = {}
    for wname, spec in specs:
        for n in spec.nodes.values():
            if n.type == "bundle" and n.workflow is not None and n.workflow != wname:
                found.setdefault(n.workflow, set()).add(wname)
    return {name: sorted(users) for name, users in found.items()}


def used_by(cfg: AirlieConfig, drafts: Mapping[str, str], name: str) -> list[str]:
    """The other workflows whose `bundle` nodes run workflow `name` (`bundle_users`)."""
    return bundle_users(cfg, drafts).get(name, [])


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
