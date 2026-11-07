"""The story workflow's registry and runners (config/workflows/story.yaml), and `compose`, which runs a story
through the composer when `story.composer` is on (docs/superpowers/specs/2026-11-06-generic-nodes-design.md).

A strangler: the runners here call today's `StoryPipeline` steps by node name, with today's arguments, and
return the node's outputs read back from the state, so a story runs the same either way by construction and
the composer only orders the steps; each type's generic loop is lifted into `airlie.flow.runners` in turn,
with the story's pieces (the brief, the parser, the after-hook) defined here once and called by both the
registry and the driver's step. The ask type is lifted (survey and refine), the check type (each check is a
`(ctx, **args)` callable that reads its inputs from the store and calls the step the driver calls), and the
conversation type: the layer plan's rounds and the packages' drafting are turn bodies (`args.turns`) that
open their conversation through the runner's `Talk`, the drafting one held for the run as the
`drafts_session` the layer's package node continues."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, Literal

from airlie.flow.composer import FAIL_KEY, Composer, Registry, RunContext, Runner
from airlie.flow.runners import (
    Hold,
    Opening,
    Session,
    Talk,
    ask_runner,
    check_runner,
    conversation_runner,
    gate_runner,
    meeting_runner,
)
from airlie.flow.spec import NodeSpec, NodeType
from airlie.flow.store import Sessions, StateStore
from airlie.story.amigos import Agreement
from airlie.story.board import Card, note
from airlie.story.briefs import (
    drafts_brief,
    layer_sources,
    layers_brief,
    refine_brief,
    story_text,
    survey_brief,
)
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
Step = Callable[[NodeSpec, RunContext], Awaitable[Mapping[str, Any]]]
Opener = Callable[[Opening], AbstractAsyncContextManager[Session]]
"""How a conversation step opens its conversation: the runner's `Talk.open`, or the driver's `converse`."""


# ---- the ask nodes' pieces (survey, refine): the registry's and the driver's step's one implementation ----


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


# ---- the conversation nodes' pieces (layers, drafts): the registry's and the driver's step's one
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


def story_registry(pipe: StoryPipeline, st: StoryState, card: Card, *, until: Until) -> Registry:
    """Every `story.*` id the story and layer workflows name. The checks, the meeting's piece, the layer's
    package and build, and the review gate call today's steps; the predicates are today's conditions; the
    asks' and conversations' briefs, parsers, after-hooks and turn bodies are their pieces."""
    from airlie.story.pipeline import _covered_below, _to_build  # the pipeline imports this module

    # ---- the checks (`check_runner`: `fn(ctx, **node.args)`, a declared input read from the store, the
    # outputs returned). Each calls the pipeline's step, the one implementation the driver calls too.
    async def intake(ctx: RunContext) -> Mapping[str, Any]:
        await pipe._intake(st, card)
        return {}

    async def base(ctx: RunContext) -> Mapping[str, Any]:
        return {"base": await pipe._base(st, card)}

    def gate(ctx: RunContext) -> Mapping[str, Any]:
        pipe._gate(st, ctx.store.get("base"))
        return {"gate": st.gate}

    async def costed(ctx: RunContext, *, stages: list[str]) -> Mapping[str, Any]:
        await pipe._costed(st, card, *stages)
        return {}

    def plain(ctx: RunContext) -> Mapping[str, Any]:
        agree_plain(st, *_planned(ctx))
        return {"agreement": st.agreement}

    # ---- the meeting's piece (`meeting_runner`: `fn(ctx, hold)`): `_agree`, holding the meeting through
    # the runner's `hold`; the agreement rewrites the story and the layers
    async def amigos(ctx: RunContext, hold: Hold) -> Mapping[str, Any]:
        await pipe._agree(st, card, *_planned(ctx), hold)
        return {"agreement": st.agreement, "story": st.story, "layers": st.layers}

    def planned(ctx: RunContext) -> Mapping[str, Any]:
        # the planning lane's last word, as the driver says it; a run that goes on to build says nothing here
        if until == "planned":
            st.emit("story_planned", layers=len(st.layers or ()))
        return {}

    # ---- the layer bundle (config/workflows/layer.yaml), one run per layer: `_build`'s steps by name ----
    sites: dict[str, tuple[tuple[str, str, str, Any], str, str]] = {}  # layer -> `_layer_site`
    attempts: dict[str, int] = {}  # layer -> builds started this run, for `_build_once`'s last-build stop

    def at(ctx: RunContext) -> tuple[str, int, Story, tuple[Layer, ...]]:
        """The layer the bundle is on (its number and index) and the story and layers as they now stand
        in the store (`_current`: a criterion reworded on the way is read back)."""
        assert ctx.item is not None  # an each item
        return ctx.item, int(ctx.item) - 1, *_planned(ctx)

    def open_layer(ctx: RunContext) -> bool:
        """Neither built nor satisfied: `_build` returns at once for a layer that is either."""
        return ctx.item is not None and ctx.item not in st.built and ctx.item not in st.satisfied

    async def covered(ctx: RunContext) -> Mapping[str, Any]:
        """Satisfied by the layers below (`_covered_below`), or else where it is built (`_layer_site`, which
        finds its author and clears what an earlier run left of it): no outputs, so nothing is skipped."""
        nn, i, story, layers = at(ctx)
        if (below := _covered_below(st, layers, i)) is not None:
            await pipe._satisfy(st, card, layers, i, below)
            return {"satisfied.{item}": st.satisfied[nn]}
        sites[nn] = await pipe._layer_site(st, card, story, layers, i)
        return {}

    async def ready_package(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        """`_ready_package` at the layer's base, with the build's feedback when the build node looped back
        here, continuing the drafting conversation the node names (`session`) while the run holds it, else
        asking afresh (a story built again by another process); an architect's satisfied reply is
        `_satisfy`'d, and the build node is then skipped."""
        nn, i, story, layers = at(ctx)
        where, built, guide = sites[nn]
        drafting = ctx.sessions.get(node.session) if node.session is not None else None
        got = await pipe._ready_package(
            st, story, layers, i, where[1], built, guide, feedback=ctx.feedback, drafting=drafting
        )
        if isinstance(got, Satisfied):
            await pipe._satisfy(st, card, layers, i, got)
            return {"satisfied.{item}": st.satisfied[nn]}
        return {"package.{item}": got.to_dict()}

    async def build_once(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        """One `_build_once` from the stored package: the layer's built record, or a failure that carries
        the feedback back to the package node. The attempt counts this run's builds, so the last of
        story.layer_attempts stops the story there, as the driver's loop does."""
        nn, i, story, layers = at(ctx)
        where, built, _ = sites[nn]
        attempts[nn] = attempt = attempts.get(nn, 0) + 1
        package = Package.from_dict(ctx.store.get(f"package.{nn}"))
        again = await pipe._build_once(st, card, story, layers, i, package, where, built, attempt)
        if again is not None:
            return {FAIL_KEY: "layer red", "feedback": again}
        return {"built.{item}": st.built[nn]}

    def build_done(ctx: RunContext) -> Mapping[str, Any]:
        build_finished(st)
        return {}

    async def publish(ctx: RunContext) -> Mapping[str, Any]:
        await pipe._publish(st, card, *_planned(ctx))
        return {"prs": st.prs} if st.prs else {}  # set only once all are opened, then the node is skipped

    async def finish(ctx: RunContext) -> None:
        """The review gate's piece (`gate_runner`, which then ends the run): the PR links posted, the card
        moved to review and the stage finished, as `_finish` does for the driver."""
        await pipe._finish(st, card)

    async def take_card(ctx: RunContext) -> Mapping[str, Any]:
        await take_card_as_story(pipe, st, card)
        return {"story": st.story}

    # ---- the conversations' pieces: the opening briefs, the plan's parser, the turn bodies (ctx, talk) ----
    guides: dict[str, str] = {}  # the layer plan's guide, read once a planning as the driver reads it

    def layers_brief_for(ctx: RunContext, feedback: str | None) -> str:
        story = Story.from_dict(ctx.store.get("story"))
        if "layers" not in guides:
            guides["layers"] = layers_guide(pipe, st, story)
        return layers_opening(pipe, st, story, guides["layers"], feedback)

    async def layers_turns(ctx: RunContext, talk: Talk) -> Any:
        assert talk.node.prompt is not None and talk.node.parse is not None  # the layers node's
        story = Story.from_dict(ctx.store.get("story"))
        layers = await plan_layers(
            pipe,
            st,
            story,
            brief=functools.partial(ctx.registry[talk.node.prompt], ctx),
            parse=ctx.registry[talk.node.parse],
            open=talk.open,
            rounds=talk.count,
        )
        await layers_planned(pipe, st, card, layers)
        return st.layers

    async def drafts_turns(ctx: RunContext, talk: Talk) -> Any:
        await draft_packages(pipe, st, *_planned(ctx), await talk.opening(), talk.open)
        return True

    registry = Registry()
    # the ask nodes' pieces: briefs (ctx, feedback), the parser, the after-hooks (ctx, value)
    registry.add("story.survey", lambda ctx, feedback: survey_brief(card))
    registry.add("story.surveyed", lambda ctx, text: surveyed(st, text))
    registry.add("story.survey_failed", lambda ctx, error: survey_failed(st, error))
    registry.add("story.refine", lambda ctx, feedback: refine_brief(card, feedback))
    registry.add("story.story", parse_refined)
    registry.add("story.refined", lambda ctx, story: refined(pipe, st, card, story))
    # the conversations' pieces: opening briefs (ctx, feedback), the plan's parser, turn bodies (ctx, talk)
    registry.add("story.layers", layers_brief_for)
    registry.add("story.layer_plan", lambda text: parse_plan(pipe, Story.from_dict(st.story or {}), text))
    registry.add("story.layers_turns", layers_turns)
    registry.add("story.drafts", lambda ctx, feedback: drafts_opening(pipe, st, *_planned(ctx)))
    registry.add("story.drafts_turns", drafts_turns)
    checks: dict[str, Callable[..., Any]] = {  # (ctx, **args)
        "story.intake": intake,
        "story.take_card_as_story": take_card,
        "story.base": base,
        "story.gate": gate,
        "story.agree_plain": plain,
        "story.costed": costed,
        "story.planned": planned,
        "story.covered": covered,
        "story.build_done": build_done,
        "story.publish": publish,
        "story.finish": finish,  # the review gate's (`gate_runner`)
    }
    steps: dict[str, Step] = {  # the layer's package and build: (node, ctx)
        "story.ready_package": ready_package,
        "story.build_once": build_once,
    }
    for id, fn in {**checks, **steps}.items():
        registry.add(id, fn)
    registry.add("story.amigos", amigos)
    registry.add("story.amigos_on", lambda ctx: pipe.s.amigos.enabled)
    registry.add("story.amigos_off", lambda ctx: not pipe.s.amigos.enabled)
    registry.add("story.to_build", lambda ctx: _to_build(st))
    # `_survey`'s own skips: a story surveyed already (its notes may be "", a survey that failed) or
    # refined (a resume, or a story card taken by `take_card_as_story`) is not surveyed
    registry.add("story.survey_on", lambda ctx: pipe.s.survey and st.survey is None and st.story is None)
    registry.add("story.is_story_card", lambda ctx: pipe._story_card(card) is not None)
    registry.add("story.not_story_card", lambda ctx: pipe._story_card(card) is None)
    registry.add("story.open_layer", open_layer)
    # the whole plan is drafted once, before the first layer is built (`_build`)
    registry.add("story.not_built", lambda ctx: not st.drafted and not st.built and open_layer(ctx))
    registry.add("story.has_package", lambda ctx: open_layer(ctx) and ctx.store.has(f"package.{ctx.item}"))
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
    """One runner per node type. A check is the generic `check_runner` and a gate the generic `gate_runner`
    (the review, which ends the run); a write runs its `run` id from the registry; an ask is the generic
    `ask_runner` (the layer's package ask still its `run` id), asking through `_ask` for the stage's map as
    the driver does; a conversation is the generic `conversation_runner` (the plan's rounds and the drafting
    are its turn bodies), and a meeting the generic `meeting_runner` over the story's meeting parts
    (`_meeting_parts`, the driver's too)."""

    async def by_run(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        assert node.run is not None  # spec.py: a write and the package ask need run
        step: Step = ctx.registry[node.run]
        return await step(node, ctx)

    asked = functools.partial(
        ask_runner, asker=pipe.asker, st=st, seat=functools.partial(seat, pipe), ask=pipe._ask
    )

    async def ask(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        if ctx.node == "package":  # the layer's step, which needs the layer's site from `covered`
            return await by_run(node, ctx)
        return await asked(node, ctx)

    conversation = functools.partial(
        conversation_runner, asker=pipe.asker, st=st, seat=functools.partial(seat, pipe)
    )

    async def later(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        raise NotImplementedError(f"{node.type} node '{ctx.node}': no runner yet")

    return {
        "check": check_runner,
        "meeting": functools.partial(meeting_runner, **pipe._meeting_parts(st)),
        "ask": ask,
        "conversation": conversation,
        "write": by_run,  # the layer's build (`_build_once`)
        "gate": gate_runner,  # the review (`story.finish`), which ends the run
        "each": later,  # the composer runs an each node's bundle itself
        "parallel": later,  # and a parallel node's children
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
    name, specs = workflow_for(pipe, st, card)
    composer = Composer(
        pipe.cfg,
        runners=story_runners(pipe, st, card),
        registry=story_registry(pipe, st, card, until=until),
        store=StateStore(st, loose={"card": card, "repo": pipe.repo}),
        sessions=Sessions(),
        workflows=specs,
    )
    _restore(pipe, st)
    planned_lane = until == "planned" and has_node(specs[name], "planned")
    await composer.run(name, until="planned" if planned_lane else None)
