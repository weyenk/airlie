"""The story workflow's registry and runners (config/workflows/story.yaml), and `compose`, which runs a story
through the composer when `story.composer` is on (docs/superpowers/specs/2026-11-06-generic-nodes-design.md).

A strangler: every runner here calls today's `StoryPipeline` step by node name, with today's arguments, and
returns the node's outputs read back from the state, so a story runs the same either way by construction and
the composer only orders the steps. Later tasks lift each type's generic loop into its runner."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from airlie.flow.composer import Composer, Registry, RunContext, Runner
from airlie.flow.spec import NodeSpec, NodeType
from airlie.flow.store import Sessions, StateStore
from airlie.story.board import Card
from airlie.story.model import Layer, Story
from airlie.story.state import StoryState

# the pipeline imports this module for `compose`
if TYPE_CHECKING:
    from airlie.story.pipeline import StoryPipeline

Until = Literal["planned", "review"]
Step = Callable[[NodeSpec, RunContext], Awaitable[Mapping[str, Any]]]

LATER = (  # ids the YAML names whose callables come with the generic runners (the build half: Task 7 on)
    "story.survey",  # the ask/conversation ids: today's by-name calls build their own briefs and parse
    "story.refine",
    "story.story",
    "story.refined",
    "story.layers",
    "story.layers_again",
    "story.build_done",
    "story.publish",
    "story.finish",
    "story.covered",
    "story.drafts",
    "story.next_package",
    "story.not_built",
    "story.ready_package",
    "story.build_once",
    "story.has_package",
)


def _later(id: str) -> Callable[..., Any]:
    def fn(*_: Any, **__: Any) -> Any:
        raise NotImplementedError(f"registry '{id}': not lifted out of the pipeline yet")

    return fn


def story_registry(pipe: StoryPipeline, st: StoryState, card: Card, *, until: Until = "planned") -> Registry:
    """Every `story.*` id the story and layer workflows name. The checks and the meeting are today's steps
    by name; the predicates are today's conditions; the rest raise until their runners are lifted."""
    from airlie.story.pipeline import _to_build  # the pipeline imports this module

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

    registry = Registry()
    for id in LATER:
        registry.add(id, _later(id))
    steps: dict[str, Step] = {
        "story.intake": intake,
        "story.base": base,
        "story.gate": gate,
        "story.costed": costed,
        "story.amigos": amigos,
        "story.planned": planned,
    }
    for id, fn in steps.items():
        registry.add(id, fn)
    registry.add("story.to_build", lambda ctx: _to_build(st))
    registry.add("story.survey_on", lambda ctx: pipe.s.survey)
    return registry


def story_runners(pipe: StoryPipeline, st: StoryState, card: Card) -> dict[NodeType, Runner]:
    """One runner per node type. A check or a meeting runs its `run` id from the registry; an ask or a
    conversation calls today's step by node name."""

    async def by_run(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        assert node.run is not None  # spec.py: a check, a gate and a meeting need run
        step: Step = ctx.registry[node.run]
        return await step(node, ctx)

    async def ask(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        if ctx.node == "survey":
            # `_refine_with_survey`: nothing is surveyed beside a refine that has run, or beside a story card
            # (an epic's story, whose architect read the code already); a flawed card stops it as it does
            # there. `_survey` never raises and leaves the notes ("" when it failed).
            if st.story is not None or pipe._story_card(card) is not None:
                return {}
            await pipe._survey(st, card)
            return {"survey": st.survey} if st.survey is not None else {}
        if ctx.node == "refine":
            await pipe._refine(st, card)
            return {"story": st.story}
        raise NotImplementedError(f"ask node '{ctx.node}': no runner yet")

    async def conversation(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        if ctx.node == "layers":
            assert st.story is not None  # the node's input
            await pipe._layers(st, card, Story.from_dict(st.story))
            return {"layers": st.layers}
        raise NotImplementedError(f"conversation node '{ctx.node}': no runner yet")

    async def later(node: NodeSpec, ctx: RunContext) -> Mapping[str, Any]:
        raise NotImplementedError(f"{node.type} node '{ctx.node}': no runner yet")

    return {
        "check": by_run,
        "meeting": by_run,
        "ask": ask,
        "conversation": conversation,
        "each": later,
        "write": later,
        "gate": later,
        "parallel": later,  # the composer runs a parallel node's children itself
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
