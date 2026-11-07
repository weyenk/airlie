"""The story workflow's registry and runners (config/workflows/story.yaml), and `compose`, which runs a story
through the composer when `story.composer` is on (docs/superpowers/specs/2026-11-06-generic-nodes-design.md).

A strangler: the runners here call today's `StoryPipeline` steps by node name, with today's arguments, and
return the node's outputs read back from the state, so a story runs the same either way by construction and
the composer only orders the steps; each type's generic loop is lifted into `airlie.flow.runners` in turn,
with the story's pieces (the brief, the parser, the after-hook) defined here once and called by both the
registry and the driver's step. The ask type is lifted: survey and refine."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from airlie.flow.composer import FAIL_KEY, Composer, EndRun, Registry, RunContext, Runner
from airlie.flow.runners import ask_runner
from airlie.flow.spec import NodeSpec, NodeType
from airlie.flow.store import Sessions, StateStore
from airlie.story.board import Card, note
from airlie.story.briefs import refine_brief, survey_brief
from airlie.story.model import Layer, Story, parse_story
from airlie.story.package import Package, Satisfied
from airlie.story.state import StoryState
from airlie.topology.studio_plan import extract_json_block

# the pipeline imports this module for `compose`
if TYPE_CHECKING:
    from airlie.story.pipeline import StoryPipeline

Until = Literal["planned", "review"]
Step = Callable[[NodeSpec, RunContext], Awaitable[Mapping[str, Any]]]

LATER = (  # ids the YAML names whose callables come with the generic runners
    "story.layers",  # the conversation ids: today's by-name calls build their own briefs and parse
    "story.layers_again",
    "story.drafts",
    "story.next_package",
)


def _later(id: str) -> Callable[..., Any]:
    def fn(*_: Any, **__: Any) -> Any:
        raise NotImplementedError(f"registry '{id}': not lifted out of the pipeline yet")

    return fn


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


def seat(pipe: StoryPipeline, role: str) -> str:
    """The configured role a workflow's seat names: `story.<seat>_role` when the config has one (product,
    architect), else the role of that name."""
    return str(getattr(pipe.s, f"{role}_role", role))


def story_registry(pipe: StoryPipeline, st: StoryState, card: Card, *, until: Until = "planned") -> Registry:
    """Every `story.*` id the story and layer workflows name. The checks, the meeting, the layer's package
    and build, and the review gate are today's steps by name; the predicates are today's conditions; the
    rest (prompt, turn and parse ids) raise until their runners are lifted."""
    from airlie.story.pipeline import _covered_below, _to_build  # the pipeline imports this module

    async def intake(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        await pipe._intake(st, card)
        return {}

    async def base(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        await pipe._base(st, card)
        return {"base": st.base}

    async def gate(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        assert st.base is not None  # the node's input
        pipe._gate(st, st.base)
        return {"gate": st.gate}

    async def costed(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        await pipe._costed(st, card, *node.args["stages"])
        return {}

    async def amigos(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        assert st.story is not None and st.layers is not None  # the node's inputs
        layers = tuple(Layer.from_dict(d) for d in st.layers)
        await pipe._agree(st, card, Story.from_dict(st.story), layers)
        return {"agreement": st.agreement, "story": st.story, "layers": st.layers}

    async def planned(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        # the planning lane's last word, as the driver says it; a run that goes on to build says nothing here
        if until == "planned":
            st.emit("story_planned", layers=len(st.layers or ()))
        return {}

    # ---- the layer bundle (config/workflows/layer.yaml), one run per layer: `_build`'s steps by name ----
    sites: dict[str, tuple[tuple[str, str, str, Any], str, str]] = {}  # layer -> `_layer_site`
    attempts: dict[str, int] = {}  # layer -> builds started this run, for `_build_once`'s last-build stop

    def at(ctx: RunContext) -> tuple[str, int, Story, tuple[Layer, ...]]:
        """The layer the bundle is on (its number and index) and the story and layers as they now stand
        (`_current`: a criterion reworded on the way is read back)."""
        assert ctx.item is not None and st.story is not None and st.layers is not None  # an each item
        return ctx.item, int(ctx.item) - 1, Story.from_dict(st.story), _plan(st)

    def open_layer(ctx: RunContext) -> bool:
        """Neither built nor satisfied: `_build` returns at once for a layer that is either."""
        return ctx.item is not None and ctx.item not in st.built and ctx.item not in st.satisfied

    async def covered(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
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
        here; an architect's satisfied reply is `_satisfy`'d, and the build node is then skipped."""
        nn, i, story, layers = at(ctx)
        where, built, guide = sites[nn]
        got = await pipe._ready_package(st, story, layers, i, where[1], built, guide, feedback=ctx.feedback)
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

    async def build_done(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        if not st.done("build"):  # here, not in the last layer: a note that failed there must not skip it
            st.finish("build")
        return {}

    async def publish(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        assert st.story is not None  # planned
        await pipe._publish(st, card, Story.from_dict(st.story), _plan(st))
        return {"prs": st.prs} if st.prs else {}  # set only once all are opened, then the node is skipped

    async def finish(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        await pipe._finish(st, card)
        raise EndRun()

    async def take_card(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        await take_card_as_story(pipe, st, card)
        return {"story": st.story}

    registry = Registry()
    # the ask nodes' pieces: briefs (ctx, feedback), the parser, the after-hooks (ctx, value)
    registry.add("story.survey", lambda ctx, feedback: survey_brief(card))
    registry.add("story.surveyed", lambda ctx, text: surveyed(st, text))
    registry.add("story.survey_failed", lambda ctx, error: survey_failed(st, error))
    registry.add("story.refine", lambda ctx, feedback: refine_brief(card, feedback))
    registry.add("story.story", parse_refined)
    registry.add("story.refined", lambda ctx, story: refined(pipe, st, card, story))
    for id in LATER:
        registry.add(id, _later(id))
    steps: dict[str, Step] = {
        "story.intake": intake,
        "story.take_card_as_story": take_card,
        "story.base": base,
        "story.gate": gate,
        "story.costed": costed,
        "story.amigos": amigos,
        "story.planned": planned,
        "story.covered": covered,
        "story.ready_package": ready_package,
        "story.build_once": build_once,
        "story.build_done": build_done,
        "story.publish": publish,
        "story.finish": finish,
    }
    for id, fn in steps.items():
        registry.add(id, fn)
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


def _plan(st: StoryState) -> tuple[Layer, ...]:
    """The agreed layers (the meeting stores them; nothing rewrites them while the layers are built)."""
    assert st.layers is not None  # planned
    return tuple(Layer.from_dict(d) for d in st.layers)


def story_runners(pipe: StoryPipeline, st: StoryState, card: Card) -> dict[NodeType, Runner]:
    """One runner per node type. A check, a meeting, a write or a gate runs its `run` id from the registry;
    an ask is the generic `ask_runner` (the layer's package ask still its `run` id), asking through `_ask`
    for the stage's map as the driver does, and a conversation calls today's step by node name."""

    async def by_run(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        assert node.run is not None  # spec.py: a check, a gate and a meeting need run
        step: Step = ctx.registry[node.run]
        return await step(node, ctx)

    asked = functools.partial(
        ask_runner, asker=pipe.asker, st=st, seat=functools.partial(seat, pipe), ask=pipe._ask
    )

    async def ask(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        if ctx.node == "package":  # the layer's step, which needs the layer's site from `covered`
            return await by_run(node, ctx)
        return await asked(node, ctx)

    async def conversation(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        if ctx.node == "layers":
            assert st.story is not None  # the node's input
            await pipe._layers(st, card, Story.from_dict(st.story))
            return {"layers": st.layers}
        if ctx.node == "drafts":  # the architect's drafting conversation, held for the layers' retries
            assert st.story is not None  # planned
            await pipe._draft_packages(st, Story.from_dict(st.story), _plan(st))
            return {"drafted": True, "drafts_session": pipe._drafting}
        raise NotImplementedError(f"conversation node '{ctx.node}': no runner yet")

    async def later(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        raise NotImplementedError(f"{node.type} node '{ctx.node}': no runner yet")

    return {
        "check": by_run,
        "meeting": by_run,
        "ask": ask,
        "conversation": conversation,
        "write": by_run,  # the layer's build (`_build_once`)
        "gate": by_run,  # the review (`_finish`), which ends the run
        "each": later,  # the composer runs an each node's bundle itself
        "parallel": later,  # and a parallel node's children
    }


def _restore(pipe: StoryPipeline, st: StoryState) -> None:
    """What the driver's steps do on a resume that a skipped node does not (a node whose outputs are all
    present is not run): the gate read into the pipeline (`commands`, which the meeting's opening and every
    layer read), and the `layers` stage of a story whose agreement is stored without it (`_agree`)."""
    from airlie.story.pipeline import _to_build  # the pipeline imports this module

    if st.gate is not None and st.base is not None and _to_build(st):
        pipe._gate(st, st.base)
    if st.agreement is not None and not st.done("layers"):
        st.finish("layers")


async def compose(pipe: StoryPipeline, st: StoryState, card: Card, *, until: Until) -> None:
    """Run the story workflow over `st`: up to and including `planned` for the planning lane, else to the
    end. The driver's prologue and its stop mapping stay in `_drive`, around this call."""
    if "story" not in pipe.cfg.workflows:
        from airlie.config import ConfigError

        raise ConfigError(
            "story.composer is on, but the config has no 'story' workflow: include "
            "config/workflows/story.yaml and config/workflows/layer.yaml"
        )
    composer = Composer(
        pipe.cfg,
        runners=story_runners(pipe, st, card),
        registry=story_registry(pipe, st, card, until=until),
        store=StateStore(st, loose={"card": card, "repo": pipe.repo}),
        sessions=Sessions(),
    )
    _restore(pipe, st)
    await composer.run("story", until="planned" if until == "planned" else None)
