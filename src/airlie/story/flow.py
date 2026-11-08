"""The story workflow's checks, runners and registry (config/workflows/story.yaml, layer.yaml), and `compose`,
which runs a story through the composer when `story.composer` is on (nodes-are-config design, "The story
workflow rewritten", "Checks", D12-D20).

Every check is a catalog operation (`STORY_CATALOG`): a `(ctx, **args)` callable that reads its declared
inputs from the store and calls the step the driver calls, so a story runs the same either way. The model
nodes run from their own fields through the generic runners (`story_runners`); the survey is an ask from its
prompt file. Refine, the layer plan and the packages' drafting are catalog operations that wrap the driver's
steps (their briefs, parsers and protocols: the plan's rounds, the drafting's turn per layer), interim until
Part 2 makes them model nodes, as are the layer's package and build. The pieces those steps share with the
driver (`surveyed`, `refined`, `plan_layers`, `draft_packages`, ...) are defined here once; the pipeline
imports them."""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

from airlie.flow.catalog import STORY_CATALOG as STORY_CATALOG  # re-exported: the catalog with these declared
from airlie.flow.catalog import Check, check
from airlie.flow.composer import FAIL_KEY, Composer, MissingInput, Registry, RunContext, Runner
from airlie.flow.prompt_files import parse_draft_prompts
from airlie.flow.runners import (
    Hold,
    Opening,
    Session,
    ask_runner,
    check_runner,
    conversation_runner,
    converse,
    gate_runner,
    meeting_runner,
    write_runner,
)
from airlie.flow.spec import NodeType
from airlie.flow.store import Sessions, StateStore
from airlie.flow.team import team_runner
from airlie.prompts.hub import PromptText
from airlie.story.amigos import ROLES, Agreement
from airlie.story.board import Card, note
from airlie.story.briefs import drafts_brief, layer_sources, layers_brief, request_section, story_text
from airlie.story.context import guide_budget, repo_guide
from airlie.story.model import Layer, Story, parse_layers, parse_story
from airlie.story.package import Package, Satisfied
from airlie.story.rounds import plan_verdict, same_layers
from airlie.story.state import StoryState
from airlie.story.tester_lint import lint_note
from airlie.story.workflows import has_node, workflow_for
from airlie.topology.studio_plan import extract_json_block

# the pipeline imports this module for `compose`
if TYPE_CHECKING:
    from airlie.story.pipeline import StoryPipeline

Until = Literal["planned", "review"]
Opener = Callable[[Opening], AbstractAsyncContextManager[Session]]
"""How a conversation step opens its conversation: `converse`, held where the caller holds it."""

# ---- survey's and refine's pieces: the catalog's checks' and the driver's steps' one implementation ----


def surveyed(st: StoryState, text: str) -> None:
    """The survey's notes kept ("" when it failed: no notes, and it is not asked again)."""
    st.survey = text
    st.save()
    st.emit("survey_done", chars=len(text))


def survey_failed(st: StoryState, error: Exception) -> None:
    """A survey is a head start: whatever fails, the story goes on with no notes."""
    from airlie.story.pipeline import _cap, _reason  # the pipeline imports this module

    st.emit("survey_failed", reason=_cap(_reason(error)))
    surveyed(st, "")


def parse_refined(text: str) -> Story:
    """Product's reply as the story: the fenced JSON, its criteria numbered afresh."""
    return parse_story(extract_json_block(text), renumber=True)


async def take_card_as_story(pipe: StoryPipeline, st: StoryState, card: Card) -> Story | None:
    """The story an epic's plan wrote on the card (`refine_skipped`): the meeting reviews it, refine does
    not. None for a card that carries none; a flawed block stops the story with its reason."""
    from airlie.story.pipeline import _story_note  # the pipeline imports this module

    taken = pipe._story_card(card)
    if taken is None:
        return None
    st.story = taken.story.to_dict()
    st.emit("refine_skipped", reason="story card")
    st.finish("refine")
    epic = taken.story.epic.key if taken.story.epic else "its epic"
    await pipe.board.comment(
        card, note(f"story taken from its card, planned under {epic}: {_story_note(taken.story)}")
    )
    return taken.story


async def refined(pipe: StoryPipeline, st: StoryState, card: Card, story: Story) -> None:
    """Refine's story kept and its stage finished, then told to the issue as product."""
    from airlie.story.pipeline import _story_note  # the pipeline imports this module

    st.story = story.to_dict()
    st.finish("refine")
    # after the finish, so a note that cannot be posted never has the model asked again.
    # Each note is posted as the role that wrote it (the prefix keeps it recognisable as airlie's):
    # story → product, layers → architect, layer built → build_role, the PR stack in _finish → review.
    await pipe.board.comment(card, note(_story_note(story)), role=pipe.s.product_role)


# ---- the layer plan's and the drafting's pieces: the catalog's checks' and the driver's steps' one
# implementation ----


def layers_guide(pipe: StoryPipeline, st: StoryState, story: Story) -> str:
    """The files the story names, read at its base for the layer plan."""
    sha = (st.base or {}).get("sha", "HEAD")
    budget = guide_budget(pipe.cfg, [pipe.s.architect_role])
    return repo_guide(pipe.repo, sha, story_text(story), budget_chars=budget)


def layers_opening(
    pipe: StoryPipeline, st: StoryState, story: Story, guide: str, feedback: str | None
) -> str:
    """The layer plan's brief (`layers_brief`), with the last try's reason when it went back."""
    coder_steps, coder_context = pipe._coder_budget()
    return layers_brief(
        story,
        pipe.s.max_layers,
        feedback,
        guide=guide,
        survey=st.survey or "",
        round_steps=pipe.s.plan_round_steps,
        lessons=pipe.lessons.recent(),
        max_steps=coder_steps,
        context_budget=coder_context,
        dossier=pipe._dossier(st, (st.base or {}).get("sha")),
        max_criteria=pipe.s.max_layer_criteria,
    )


def parse_plan(pipe: StoryPipeline, story: Story, text: str) -> tuple[Layer, ...]:
    """The architect's reply as the story's layers."""
    return parse_layers(extract_json_block(text), story, pipe.s.max_layers)


async def plan_layers(
    pipe: StoryPipeline,
    st: StoryState,
    story: Story,
    *,
    brief: Callable[[str | None], str],
    parse: Callable[[str], tuple[Layer, ...]],
    open: Opener,
    rounds: int,
) -> tuple[Layer, ...]:
    """The layer plan, asked until it parses and its sizes stand (`_usable`, `_size_review` once a
    planning). With story.plan_round_steps each try is a conversation (`open`) whose turns are the rounds
    (`Asker.in_rounds`): at most `rounds` (story.plan_max_rounds) and what the role's step allowance gives;
    with 0, one plain ask."""
    reviewed: list[bool] = []  # the size review is asked once in this planning (`_size_review`)

    async def sized(layers: tuple[Layer, ...]) -> None:
        await pipe._size_review(st, story, layers, asked=reviewed)

    ask: Callable[..., Awaitable[str]] | None = None
    round_steps = pipe.s.plan_round_steps
    if round_steps:
        cap = pipe.cfg.roles[pipe.s.architect_role].max_steps or pipe.cfg.teams.max_steps
        turns = max(1, min(cap // round_steps, rounds))

        async def in_rounds(st: StoryState, stage: str, role: str, text: str, at: str | None = None) -> str:
            options = {"repo_map": pipe._map(stage, text), "capped_follow_ups": False}
            async with open(Opening(text, round_steps, stage=stage, at=at, options=options)) as session:
                return await pipe.asker.in_rounds(
                    st,
                    stage,
                    role,
                    session.say,
                    parse=parse,
                    same=same_layers,
                    rounds=turns,
                    signal=plan_verdict,  # the plan's own verdict ends the rounds early
                )

        ask = in_rounds
    return await pipe._usable(
        st, "layers", pipe.s.architect_role, "layer plan", brief, parse, ask=ask, check=sized
    )


async def layers_planned(pipe: StoryPipeline, st: StoryState, card: Card, layers: tuple[Layer, ...]) -> None:
    """The plan kept (the stage finishes once the layers are agreed, `_agree`) and told to the issue."""
    from airlie.story.pipeline import _layers_note  # the pipeline imports this module

    st.layers = [x.to_dict() for x in layers]
    st.save()
    await pipe.board.comment(card, note(_layers_note(layers)), role=pipe.s.architect_role)


async def drafts_opening(
    pipe: StoryPipeline, st: StoryState, story: Story, layers: tuple[Layer, ...]
) -> Opening:
    """The drafting conversation's opening at the story's base: layer 01's package brief (`drafts_brief`),
    the packages' steps a turn, and with story.package_tools the checkout's turns (`ToolTurns`) it carries
    and what it may write. Event: packages_drafting."""
    from airlie.story.pipeline import ToolTurns  # the pipeline imports this module

    sha = (await pipe._base(st))["sha"]
    guide = repo_guide(
        pipe.repo,
        sha,
        "\n".join(f"{x.scope}\n{x.brief}" for x in layers),
        budget_chars=guide_budget(pipe.cfg, [pipe.s.architect_role, pipe.s.build_role]),
    )
    max_steps, context = pipe._coder_budget()
    first = drafts_brief(
        story,
        layers,
        guide=guide,
        lessons=pipe.lessons.recent(),
        max_steps=max_steps,
        context_budget=context,
        proof=pipe._layer_proof(st, layers[0]),
        sources=layer_sources(pipe.repo, sha, layers[0]),  # lever 2: its files, not read by tool
        dossier=pipe._dossier(st, sha),
        lint=lint_note(pipe.repo, sha, pipe.commands),
        tools=pipe.s.package_tools,
    )
    st.emit("packages_drafting", layers=len(layers))
    turns = ToolTurns(st, sha, pipe._keep_tests) if pipe.s.package_tools else None
    options = {"repo_map": pipe._map("packages", first), "writes": pipe._writes(turns) if turns else None}
    return Opening(first, pipe.s.package_steps, stage="packages", at=sha, options=options, state=turns)


async def draft_packages(
    pipe: StoryPipeline,
    st: StoryState,
    story: Story,
    layers: tuple[Layer, ...],
    opening: Opening,
    open: Opener,
) -> Session:
    """Every layer's package drafted in the conversation `opening` starts (`open`), its turns
    `_draft_each`'s; the story is then drafted. The session, for a hold that keeps it."""
    assert opening.at is not None  # drafts_opening: the story's base
    async with open(opening) as session:
        await pipe._draft_each(st, story, layers, opening.at, session.say, session.run, session.state)
    st.drafted = True
    st.save()
    return session


# ---- the meeting's pieces (amigos, agree_plain): `_agree`'s branches, shared with the composer ----


def kept_agreement(st: StoryState) -> Agreement | None:
    """The agreement already stored (a resume), with the `layers` stage finished if a state saved it without
    that (one from before the stage and the agreement were saved together); None when there is none."""
    if st.agreement is None:
        return None
    if not st.done("layers"):
        st.finish("layers")
    return Agreement.from_dict(st.agreement)


def keep_refined(st: StoryState, story: Story, layers: tuple[Layer, ...]) -> None:
    """The story and the plan as refine and the split left them, before the agreement (and later a reworded
    criterion) replaces them: a restart from the layer plan or the meeting goes back to these."""
    st.refined_story, st.refined_layers = story.to_dict(), [x.to_dict() for x in layers]
    st.save()


def agree_plain(st: StoryState, story: Story, layers: tuple[Layer, ...]) -> Agreement:
    """No meeting (story.amigos off, or a story past `layers` from before the meeting existed): the split is
    the agreement, with an empty proof plan, and the `layers` stage finishes if it has not."""
    keep_refined(st, story, layers)
    plain = Agreement(story, layers, ())
    st.agreement = plain.to_dict()
    if st.done("layers"):
        st.save()
    else:
        st.finish("layers")
    return plain


def seat(pipe: StoryPipeline, role: str) -> str:
    """The configured role a workflow's seat names: `story.<seat>_role` when the config has one (product,
    architect), else the role of that name."""
    return str(getattr(pipe.s, f"{role}_role", role))


@dataclass
class StoryRun:
    """What the story's catalog checks act on in one run: the pipeline, the story's state, its card, how far
    the run goes (`until`), and what the layer bundle's steps keep between nodes of a run (the layer's site
    from `covered`, the builds started per layer)."""

    pipe: StoryPipeline
    st: StoryState
    card: Card
    until: Until
    sites: dict[str, tuple[tuple[str, str, str, Any], str, str]] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)


class StoryRegistry(Registry):
    """The story's registry: the catalog's checks, and the run (`StoryRun`) they act on, which a check
    reaches from its context (`_run`)."""

    def __init__(self, run: StoryRun, prompts: Mapping[str, PromptText] | None = None) -> None:
        super().__init__(prompts=prompts, catalog=STORY_CATALOG)
        self.run = run


def _run(ctx: RunContext, name: str) -> StoryRun:
    """The story run a catalog check `name` acts on: its context's registry is the story's."""
    if not isinstance(ctx.registry, StoryRegistry):
        raise TypeError(f"node '{ctx.node}': {name} runs only on a story's registry (story_registry)")
    return ctx.registry.run


def _peek(ctx: RunContext, name: str, default: Any = None) -> Any:
    """A name the check reads that may be absent from the store: its value, else `default`."""
    try:
        return ctx.store.get(name)
    except KeyError:
        return default


def _in(ctx: RunContext, check: str, name: str) -> Any:
    """A declared input of `check` the store must hold; `MissingInput` naming the node, the check and the
    input when it does not."""
    value = _peek(ctx, name)
    if value is None:
        raise MissingInput(f"node '{ctx.node}': {check} needs '{name}', which the store does not hold")
    return value


def _card(ctx: RunContext, check: str) -> Card:
    card: Card = _in(ctx, check, "card")
    return card


def _repo(ctx: RunContext, check: str) -> Path:
    return Path(_in(ctx, check, "repo"))


def _story(ctx: RunContext, check: str) -> Story:
    return Story.from_dict(_in(ctx, check, "story"))


def _layers(ctx: RunContext, check: str) -> tuple[Layer, ...]:
    return tuple(Layer.from_dict(d) for d in _in(ctx, check, "layers"))


def _sha(ctx: RunContext, check: str) -> str:
    """The commit the story's base names, as the store holds it."""
    base = _in(ctx, check, "base")
    if not isinstance(base, Mapping) or not base.get("sha"):
        raise MissingInput(f"node '{ctx.node}': {check} needs 'base' with its sha, and the store's has none")
    return str(base["sha"])


def _item(ctx: RunContext, check: str) -> tuple[str, int]:
    """The layer the bundle is on: its number and its index."""
    if ctx.item is None:
        raise MissingInput(f"node '{ctx.node}': {check} runs per layer, and the node has no item")
    return ctx.item, int(ctx.item) - 1


def _at(ctx: RunContext, check: str) -> tuple[str, int, Story, tuple[Layer, ...]]:
    """The layer the bundle is on (its number and index) and the story and layers as they now stand in the
    store (`_current`: a criterion reworded on the way is read back)."""
    return *_item(ctx, check), _story(ctx, check), _layers(ctx, check)


# ---- the checks (`check_runner`: `fn(ctx, **node.args)`, every store and loose name it reads declared as
# an input (D23), the outputs returned). Each calls the pipeline's step, the one implementation the driver
# calls too. `repo` is declared where the step works on the repository (the store's loose `repo` is the
# pipeline's), read by the step itself where the body hands it nothing.


@check(
    "story.intake", inputs=("card",), title="Take the card in: move it to building, say where its progress is"
)
async def intake(ctx: RunContext) -> Mapping[str, Any]:
    r = _run(ctx, "story.intake")
    await r.pipe._intake(r.st, _card(ctx, "story.intake"))
    return {}


@check(
    "story.take_card_as_story",
    inputs=("card",),
    outputs=("story",),
    formats={"story": "json"},
    title="Take the story an epic's plan wrote on the card (refine is skipped)",
)
async def take_card(ctx: RunContext) -> Mapping[str, Any]:
    r = _run(ctx, "story.take_card_as_story")
    await take_card_as_story(r.pipe, r.st, _card(ctx, "story.take_card_as_story"))
    return {"story": r.st.story}


@check(
    "story.base",
    inputs=("card", "repo"),
    outputs=("base",),
    formats={"base": "json"},
    title="Resolve the commit the story builds on: its base",
)
async def base(ctx: RunContext) -> Mapping[str, Any]:
    r = _run(ctx, "story.base")
    return {"base": await r.pipe._base(r.st, _card(ctx, "story.base"))}


@check(
    "story.gate",
    inputs=("repo", "base"),
    outputs=("gate",),
    formats={"gate": "json"},
    title="Read the test gate the repository's AGENTS.md names at the base",
)
def gate(ctx: RunContext) -> Mapping[str, Any]:
    r = _run(ctx, "story.gate")
    r.pipe._gate(r.st, _in(ctx, "story.gate", "base"))
    return {"gate": r.st.gate}


@check(
    "story.agree_plain",
    inputs=("story", "layers"),
    outputs=("agreement",),
    formats={"agreement": "json"},
    title="Take the split as the agreement, with no proof plan (no meeting)",
)
def plain(ctx: RunContext) -> Mapping[str, Any]:
    r = _run(ctx, "story.agree_plain")
    agree_plain(r.st, _story(ctx, "story.agree_plain"), _layers(ctx, "story.agree_plain"))
    return {"agreement": r.st.agreement}


@check(
    "story.costed",
    inputs=("card",),
    title="Record what the stages named (args.stages) cost against their budgets",
)
async def costed(ctx: RunContext, *, stages: list[str]) -> Mapping[str, Any]:
    r = _run(ctx, "story.costed")
    await r.pipe._costed(r.st, _card(ctx, "story.costed"), *stages)
    return {}


@check("story.planned", inputs=("layers",), title="Mark the story planned (the planning lane's last word)")
def planned(ctx: RunContext) -> Mapping[str, Any]:
    r = _run(ctx, "story.planned")
    # the planning lane's last word, as the driver says it; a run that goes on to build says nothing here
    if r.until == "planned":
        r.st.emit("story_planned", layers=len(_peek(ctx, "layers") or ()))
    return {}


@check(
    "story.covered",
    inputs=("card", "repo", "story", "layers"),
    outputs=("satisfied.{item}",),
    formats={"satisfied.{item}": "json"},
    title="Find the layer satisfied by the layers below, or else where it is built",
)
async def covered(ctx: RunContext) -> Mapping[str, Any]:
    """Satisfied by the layers below (`_covered_below`), or else where it is built (`_layer_site`, which
    finds its author and clears what an earlier run left of it): no outputs, so nothing is skipped."""
    from airlie.story.pipeline import _covered_below  # the pipeline imports this module

    r = _run(ctx, "story.covered")
    nn, i, story, layers = _at(ctx, "story.covered")
    card = _card(ctx, "story.covered")
    if (below := _covered_below(r.st, layers, i)) is not None:
        await r.pipe._satisfy(r.st, card, layers, i, below)
        return {"satisfied.{item}": r.st.satisfied[nn]}
    r.sites[nn] = await r.pipe._layer_site(r.st, card, story, layers, i)
    return {}


# ---- the layer's package and build: today's steps until Part 2 dissolves them into model nodes. The
# package node names the drafting session in `args.session`; the build node's loop line carries its
# feedback back to the package node (`ctx.feedback`).


@check(
    "story.ready_package",
    inputs=("card", "repo", "story", "layers"),
    outputs=("package.{item}", "satisfied.{item}"),
    formats={"package.{item}": "json", "satisfied.{item}": "json"},
    title="Ready the layer's package where it is built (interim: Part 2 makes it a model node)",
)
async def ready_package(ctx: RunContext, *, session: str | None = None) -> Mapping[str, Any]:
    """`_ready_package` where `covered` found the layer is built, with the build's feedback when the build
    node looped back here (`ctx.feedback`), continuing the drafting conversation `session` names
    (`ctx.sessions`) while the run holds it, else asking afresh (a story built again by another process);
    an architect's satisfied reply is `_satisfy`'d, and the build node is then skipped."""
    r = _run(ctx, "story.ready_package")
    nn, i, story, layers = _at(ctx, "story.ready_package")
    card = _card(ctx, "story.ready_package")
    where, built, guide = r.sites[nn]
    drafting = ctx.sessions.get(session) if session is not None else None
    got = await r.pipe._ready_package(
        r.st, story, layers, i, where[1], built, guide, feedback=ctx.feedback, drafting=drafting
    )
    if isinstance(got, Satisfied):
        await r.pipe._satisfy(r.st, card, layers, i, got)
        return {"satisfied.{item}": r.st.satisfied[nn]}
    return {"package.{item}": got.to_dict()}


@check(
    "story.build_once",
    inputs=("card", "repo", "package.{item}", "story", "layers"),
    outputs=("built.{item}",),
    formats={"built.{item}": "json"},
    title="Build the layer once from its package (interim: Part 2 makes it a write node)",
)
async def build_once(ctx: RunContext) -> Mapping[str, Any]:
    """One `_build_once` from the stored package: the layer's built record, or a failure that carries the
    feedback back to the package node. The attempt counts this run's builds, so the last of
    story.layer_attempts stops the story there, as the driver's loop does."""
    r = _run(ctx, "story.build_once")
    nn, i, story, layers = _at(ctx, "story.build_once")
    where, built, _ = r.sites[nn]
    r.attempts[nn] = attempt = r.attempts.get(nn, 0) + 1
    package = Package.from_dict(_in(ctx, "story.build_once", f"package.{nn}"))
    card = _card(ctx, "story.build_once")
    again = await r.pipe._build_once(r.st, card, story, layers, i, package, where, built, attempt)
    if again is not None:
        return {FAIL_KEY: "layer red", "feedback": again}
    return {"built.{item}": r.st.built[nn]}


@check("story.build_done", title="Finish the build stage once every layer is built or satisfied")
def build_done(ctx: RunContext) -> Mapping[str, Any]:
    build_finished(_run(ctx, "story.build_done").st)
    return {}


@check(
    "story.publish",
    inputs=("card", "repo", "story", "layers"),
    outputs=("prs",),
    formats={"prs": "list"},
    title="Open the layers' stacked draft PRs",
)
async def publish(ctx: RunContext) -> Mapping[str, Any]:
    r = _run(ctx, "story.publish")
    card = _card(ctx, "story.publish")
    await r.pipe._publish(r.st, card, _story(ctx, "story.publish"), _layers(ctx, "story.publish"))
    return {"prs": r.st.prs} if r.st.prs else {}  # set only once all are opened, then the node is skipped


@check("story.finish", inputs=("card",), title="Post the PR links, move the card to review, finish the stage")
async def finish(ctx: RunContext) -> None:
    """The review gate's piece (`gate_runner`, which then ends the run): the PR links posted, the card
    moved to review and the stage finished, as `_finish` does for the driver."""
    r = _run(ctx, "story.finish")
    await r.pipe._finish(r.st, _card(ctx, "story.finish"))


# ---- the steps that stay code until Part 2 makes them model nodes: refine (its brief, parser and board
# note), the layer plan (its rounds and size review) and the packages' drafting (a turn per layer, the
# critic's revision in it)


@check(
    "story.refine",
    inputs=("card",),
    outputs=("story",),
    formats={"story": "json"},
    title="Refine the request into a story, as product (interim: Part 2 makes it an ask node)",
)
async def refine(ctx: RunContext) -> Mapping[str, Any]:
    """`_refine`: product asked until its story parses (each refusal a `retry`), a refusal to build it as
    written a block, then the stage finished and the story told to the card."""
    r = _run(ctx, "story.refine")
    await r.pipe._refine(r.st, _card(ctx, "story.refine"))
    return {"story": r.st.story}


@check(
    "story.plan_layers",
    inputs=("card", "repo", "story", "base", "survey"),
    outputs=("layers",),
    formats={"layers": "list"},
    title="Split the story into layers, as the architect (interim: Part 2 makes it a conversation node)",
)
async def plan(ctx: RunContext) -> Mapping[str, Any]:
    """`_layers`: the plan asked until it parses and its sizes stand, in rounds with story.plan_round_steps
    (`plan_layers`), then kept and told to the card. It reads the survey's notes the state keeps."""
    name = "story.plan_layers"
    r = _run(ctx, name)
    await r.pipe._layers(r.st, _card(ctx, name), _story(ctx, name))
    return {"layers": r.st.layers}


@check(
    "story.draft_packages",
    inputs=("repo", "base", "story", "layers", "agreement"),
    outputs=("drafted", "drafts_session"),
    formats={"drafted": "bool", "drafts_session": "session"},
    title="Draft every layer's package in one architect conversation (interim: Part 2 splits it)",
)
async def drafts(ctx: RunContext) -> Mapping[str, Any]:
    """`_draft_packages`: the drafting conversation opened at the story's base (`drafts_opening`) and held on
    the run's stack (`ctx.held`) for the layers' package retries, each layer drafted in it (`draft_packages`).
    The session leaves the run's sessions when the stack closes it."""
    name = "story.draft_packages"
    r = _run(ctx, name)
    if ctx.held is None:
        raise RuntimeError(f"node '{ctx.node}' outputs a session, but the run holds nothing open")
    story, layers = _story(ctx, name), _layers(ctx, name)
    opening = await drafts_opening(r.pipe, r.st, story, layers)
    ctx.held.callback(ctx.sessions.pop, "drafts_session")  # after the conversation closes (LIFO)
    talk = functools.partial(
        converse, r.pipe.asker, r.st, r.pipe.s.architect_role, stage="packages", held=ctx.held
    )
    session = await draft_packages(r.pipe, r.st, story, layers, opening, talk)
    return {"drafted": True, "drafts_session": session}


@check(
    "story.layer_start", title="Begin a layer: the lines out of it say which of its steps are still to run"
)
def layer_start(ctx: RunContext) -> Mapping[str, Any]:
    return {}


# ---- the computed inputs of the briefs (D13): what a brief computes from the repository or the state, as
# an artifact a check outputs before the node that reads it


@check(
    "story.request",
    inputs=("card",),
    outputs=("request",),
    formats={"request": "text"},
    title="The card's request as a brief quotes it: its title on one line, its description quoted",
)
def request(ctx: RunContext) -> Mapping[str, Any]:
    return {"request": request_section(_card(ctx, "story.request"))}


@check(
    "story.repo_guide",
    inputs=("repo", "story", "base"),
    outputs=("guide",),
    formats={"guide": "text"},
    title="The repository guide to the files the story names, at its base (the layer plan's)",
)
def repo_guide_check(ctx: RunContext) -> Mapping[str, Any]:
    name = "story.repo_guide"
    r = _run(ctx, name)
    text = story_text(_story(ctx, name))
    budget = guide_budget(r.pipe.cfg, [r.pipe.s.architect_role])
    return {"guide": repo_guide(_repo(ctx, name), _sha(ctx, name), text, budget_chars=budget)}


@check(
    "story.scope_guide",
    inputs=("repo", "base", "layers"),
    outputs=("guide",),
    formats={"guide": "text"},
    title="The repository guide to the layers' scope, at the base (the packages' drafting)",
)
def scope_guide(ctx: RunContext) -> Mapping[str, Any]:
    name = "story.scope_guide"
    r = _run(ctx, name)
    scope = "\n".join(f"{x.scope}\n{x.brief}" for x in _layers(ctx, name))
    budget = guide_budget(r.pipe.cfg, [r.pipe.s.architect_role, r.pipe.s.build_role])
    return {"guide": repo_guide(_repo(ctx, name), _sha(ctx, name), scope, budget_chars=budget)}


@check(
    "story.lessons",
    outputs=("lessons",),
    formats={"lessons": "list"},
    title="The repository's recent lessons (size blocks and how they ran)",
)
def lessons(ctx: RunContext) -> Mapping[str, Any]:
    return {"lessons": [dataclasses.asdict(x) for x in _run(ctx, "story.lessons").pipe.lessons.recent()]}


@check(
    "story.dossier",
    inputs=("repo", "base"),
    outputs=("dossier",),
    formats={"dossier": "json"},
    title="The story's dossier: the files its studio runs read so far, at the base",
)
def dossier(ctx: RunContext) -> Mapping[str, Any]:
    r = _run(ctx, "story.dossier")
    return {"dossier": dict(r.pipe._dossier(r.st, _sha(ctx, "story.dossier")))}


@check(
    "story.coder_budget",
    outputs=("budget",),
    formats={"budget": "json"},
    title="The coder's steps and context per layer, what a layer is sized to",
)
def coder_budget(ctx: RunContext) -> Mapping[str, Any]:
    steps, context = _run(ctx, "story.coder_budget").pipe._coder_budget()
    return {"budget": {"max_steps": steps, "context_budget": context}}


@check(
    "story.layer_sources",
    inputs=("repo", "base", "layers"),
    outputs=("sources.{item}",),
    formats={"sources.{item}": "json"},
    title="The files the layer changes, as they are at the base",
)
def sources(ctx: RunContext) -> Mapping[str, Any]:
    name = "story.layer_sources"
    _run(ctx, name)
    _, i = _item(ctx, name)
    return {"sources.{item}": layer_sources(_repo(ctx, name), _sha(ctx, name), _layers(ctx, name)[i])}


@check(
    "story.layer_proof",
    inputs=("agreement", "layers"),
    outputs=("proof.{item}",),
    formats={"proof.{item}": "list"},
    title="The tests the agreement holds for the layer",
)
def layer_proof(ctx: RunContext) -> Mapping[str, Any]:
    name = "story.layer_proof"
    _run(ctx, name)
    _, i = _item(ctx, name)
    layer = _layers(ctx, name)[i]
    agreement = Agreement.from_dict(_in(ctx, name, "agreement"))  # `_layer_proof`, read from the store
    return {"proof.{item}": [t.to_dict() for t in agreement.proof_for(layer.slug)]}


@check(
    "story.lint_note",
    inputs=("repo", "base"),
    outputs=("lint",),
    formats={"lint": "text"},
    title="What the repository's linters say a test file must keep to",
)
def lint(ctx: RunContext) -> Mapping[str, Any]:
    name = "story.lint_note"
    r = _run(ctx, name)
    return {"lint": lint_note(_repo(ctx, name), _sha(ctx, name), r.pipe.commands)}


# ---- the board notes and phase marks the asks' after-hooks made (D14)


@check(
    "story.survey_note",
    inputs=("survey",),
    title="Keep no notes for a survey that failed, and say so (survey_done), as the story goes on",
)
def survey_note(ctx: RunContext) -> Mapping[str, Any]:
    """After the survey node: notes it stored were said done by its own `survey_done` event (its `events`);
    a survey that failed stored none (its `survey_failed` event said why), so it is kept as no notes and
    said done, as `survey_failed` does for the driver, and not asked again."""
    st = _run(ctx, "story.survey_note").st
    if _peek(ctx, "survey") is None:
        surveyed(st, "")
    return {}


@check(
    "story.refined_note",
    inputs=("card", "story"),
    title="Finish refine and tell the card the story, as product",
)
async def refined_note(ctx: RunContext) -> Mapping[str, Any]:
    name = "story.refined_note"
    r = _run(ctx, name)
    await refined(r.pipe, r.st, _card(ctx, name), _story(ctx, name))
    return {}


@check(
    "story.layers_note",
    inputs=("card", "layers"),
    title="Keep the layer plan and tell the card the layers, as the architect",
)
async def layers_note(ctx: RunContext) -> Mapping[str, Any]:
    name = "story.layers_note"
    r = _run(ctx, name)
    await layers_planned(r.pipe, r.st, _card(ctx, name), _layers(ctx, name))
    return {}


# ---- the predicates (`(ctx) -> bool`), each a check with one bool output (D16), which a line's condition
# reads. What they read of the state they read from the store by name.


def _amigos_on(ctx: RunContext) -> bool:
    return bool(_run(ctx, "story.amigos_on").pipe.s.amigos.enabled)


def _amigos_off(ctx: RunContext) -> bool:
    return not _amigos_on(ctx)


def _to_build_now(ctx: RunContext) -> bool:
    """`_to_build`: the layers are not planned yet, or one is neither built nor satisfied."""
    from airlie.story.pipeline import _to_build  # the pipeline imports this module

    _run(ctx, "story.to_build")
    seen = SimpleNamespace(
        layers=_peek(ctx, "layers"), built=_peek(ctx, "built") or {}, satisfied=_peek(ctx, "satisfied") or {}
    )
    return bool(_to_build(cast("StoryState", seen)))  # `_to_build` reads only these three


def _survey_on(ctx: RunContext) -> bool:
    # `_survey`'s own skips: a story surveyed already (its notes may be "", a survey that failed) or refined
    # (a resume, or a story card taken by `take_card_as_story`) is not surveyed
    r = _run(ctx, "story.survey_on")
    survey, story = _peek(ctx, "survey"), _peek(ctx, "story")
    fresh = survey is None and story is None
    return bool(r.pipe.s.survey and fresh)


def _is_story_card(ctx: RunContext) -> bool:
    r = _run(ctx, "story.is_story_card")
    return r.pipe._story_card(_card(ctx, "story.is_story_card")) is not None


def _not_story_card(ctx: RunContext) -> bool:
    return not _is_story_card(ctx)


def _open_layer(ctx: RunContext) -> bool:
    """Neither built nor satisfied: `_build` returns at once for a layer that is either."""
    _run(ctx, "story.open_layer")
    built, satisfied = _peek(ctx, "built") or {}, _peek(ctx, "satisfied") or {}
    return ctx.item is not None and ctx.item not in built and ctx.item not in satisfied


def _not_built(ctx: RunContext) -> bool:
    # the whole plan is drafted once, before the first layer is built (`_build`)
    _run(ctx, "story.not_built")
    return not _peek(ctx, "drafted") and not _peek(ctx, "built") and _open_layer(ctx)


def _has_package(ctx: RunContext) -> bool:
    return _open_layer(ctx) and ctx.store.has(f"package.{ctx.item}")


PREDICATES: Mapping[str, tuple[Callable[[RunContext], bool], tuple[str, ...], str]] = {
    "amigos_on": (_amigos_on, (), "Whether the three amigos meet (story.amigos.enabled)"),
    "amigos_off": (_amigos_off, (), "Whether the three amigos do not meet"),
    "to_build": (_to_build_now, ("layers", "built", "satisfied"), "Whether the story goes on to build"),
    "survey_on": (_survey_on, ("survey", "story"), "Whether the architect surveys the story first"),
    "is_story_card": (_is_story_card, ("card",), "Whether the card carries a story an epic's plan wrote"),
    "not_story_card": (_not_story_card, ("card",), "Whether the card carries no story of its own"),
    "open_layer": (_open_layer, ("built", "satisfied"), "Whether the layer is neither built nor satisfied"),
    "not_built": (
        _not_built,
        ("drafted", "built", "satisfied"),
        "Whether no layer is built and the packages are not drafted yet",
    ),
    "has_package": (
        _has_package,
        ("built", "satisfied", "package.{item}"),
        "Whether the open layer has its package ready",
    ),
}
"""The predicates by name, with the names they read: each the catalog's `story.<name>` check with the one bool
output `<name>`, which a line's condition reads."""


def _flag(name: str, test: Callable[[RunContext], bool]) -> Callable[[RunContext], Mapping[str, Any]]:
    def flag(ctx: RunContext) -> Mapping[str, Any]:
        return {name: test(ctx)}

    flag.__name__ = flag.__qualname__ = name
    return flag


for _name, (_test, _reads, _title) in PREDICATES.items():
    STORY_CATALOG.add(Check(f"story.{_name}", _reads, (_name,), _title, _flag(_name, _test), {_name: "bool"}))


def story_registry(
    pipe: StoryPipeline,
    st: StoryState,
    card: Card,
    *,
    until: Until,
    prompts: Mapping[str, PromptText] | None = None,
) -> Registry:
    """The story's registry over one story run (`StoryRegistry`): every check of the catalog
    (`STORY_CATALOG`) by its name. `prompts` are the drafts' prompt files a `prompt:<id>` may name beside
    the hub's rules (`Registry`)."""
    registry = StoryRegistry(StoryRun(pipe, st, card, until), prompts=prompts)
    for c in STORY_CATALOG:
        registry.add(c.name, c.fn)
    return registry


def _planned(ctx: RunContext) -> tuple[Story, tuple[Layer, ...]]:
    """The story and its agreed layers as the store holds them (a node's `story` and `layers`)."""
    return Story.from_dict(ctx.store.get("story")), tuple(Layer.from_dict(d) for d in ctx.store.get("layers"))


def build_finished(st: StoryState) -> None:
    """The build stage finished, once every layer is built or satisfied: after the layers, not in the last
    one, so a note that failed there never skips it. The driver's step and the `built` check's."""
    if not st.done("build"):
        st.finish("build")


def story_runners(pipe: StoryPipeline, st: StoryState, card: Card) -> dict[NodeType, Runner]:
    """One runner per node type, each the generic one bound to the story: a check is `check_runner` and a
    gate `gate_runner` (the review, which ends the run); an ask, a conversation and a write are the model
    runners over the pipeline's asker and the story's state, a seat named as the story names it (`seat`),
    the ask through `_ask` for the stage's map as the driver does; a meeting is `meeting_runner` over the
    amigos protocol (`_agree`, D20) with the story's meeting parts (`_meeting_parts`, the driver's too); a
    team is `team_runner` over the same asker and state. A bundle is the composer's own."""
    seated = functools.partial(seat, pipe)

    async def agree(ctx: RunContext, hold: Hold, opening: str | None) -> Mapping[str, Any]:
        await pipe._agree(st, card, *_planned(ctx), hold, opening=opening)
        return {"agreement": st.agreement, "story": st.story, "layers": st.layers}

    return {
        "ask": functools.partial(ask_runner, asker=pipe.asker, st=st, seat=seated, ask=pipe._ask),
        "conversation": functools.partial(conversation_runner, asker=pipe.asker, st=st, seat=seated),
        "write": functools.partial(write_runner, asker=pipe.asker, st=st, seat=seated),
        "meeting": functools.partial(meeting_runner, agree=agree, roles=ROLES, **pipe._meeting_parts(st)),
        "check": check_runner,
        "gate": gate_runner,  # the review (`story.finish`), which ends the run
        "team": functools.partial(team_runner, asker=pipe.asker, st=st, seat=seated),
    }


def _restore(pipe: StoryPipeline, st: StoryState) -> None:
    """What the driver's steps do on a resume that a skipped node does not (a node whose outputs are all
    present is not run): the gate read into the pipeline (`commands`, which the meeting's opening and every
    layer read), and the `layers` stage of a story whose agreement is stored without it (`kept_agreement`,
    `_agree`'s first branch). Only a state saved before the stage and the agreement were saved together has
    that (every agreement airlie stores now finishes the stage in the same save, and a restart from
    `layers` or `amigos` clears the agreement), so its `stage` event comes here, before intake, and not at
    the meeting's place: the meeting's nodes are skipped once the agreement is stored."""
    from airlie.story.pipeline import _to_build  # the pipeline imports this module

    if st.gate is not None and st.base is not None and _to_build(st):
        pipe._gate(st, st.base)
    kept_agreement(st)


async def compose(pipe: StoryPipeline, st: StoryState, card: Card, *, until: Until) -> None:
    """Run the card's workflow over `st` (its snapshot, taken now if the card has none: `workflow_for`): up
    to and including `planned` for the planning lane when the workflow has that node, else to the end. The
    driver's prologue and its stop mapping stay in `_drive`, around this call."""
    name, specs, prompts = workflow_for(pipe, st, card)
    composer = Composer(
        pipe.cfg,
        runners=story_runners(pipe, st, card),
        registry=story_registry(pipe, st, card, until=until, prompts=parse_draft_prompts(prompts)),
        store=StateStore(st, loose={"card": card, "repo": pipe.repo}),
        sessions=Sessions(),
        workflows=specs,
    )
    _restore(pipe, st)
    planned_lane = until == "planned" and has_node(specs[name], "planned")
    await composer.run(name, until="planned" if planned_lane else None)
