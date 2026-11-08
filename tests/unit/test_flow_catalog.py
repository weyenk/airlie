"""The catalog of checks (nodes-are-config design, "Checks", D2, D13, D18): declared operations with their
inputs and outputs, and the story's declarations of every check its workflows name."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from airlie.flow.catalog import STORY_CATALOG, Catalog, Check, check
from airlie.flow.composer import RunContext
from airlie.flow.store import MemoryStore, Sessions
from airlie.story.amigos import Agreement, ProofTest
from airlie.story.board import text_card
from airlie.story.flow import CATALOG, StoryRegistry, story_registry
from airlie.story.model import parse_layers, parse_story
from airlie.story.pipeline import StoryPipeline
from airlie.story.state import StoryState
from airlie.topology.studio_plan import extract_json_block
from tests.unit.git_helpers import git
from tests.unit.test_story_pipeline import (
    LAYERS,
    STORY,
    happy_engine,
    kinds_all,
    pipeline,
    repo_with_marker_tests,
)

# ---- the catalog itself ----


def test_a_declared_checks_inputs_and_outputs_are_read_back() -> None:
    cat = Catalog()

    @cat.declare(
        "w.gate", inputs=["repo", "base"], outputs=["gate"], formats={"gate": "json"}, title="Run it"
    )
    def gate(ctx: Any) -> dict[str, Any]:
        return {"gate": {"ok": True}}

    got = cat["w.gate"]
    assert isinstance(got, Check)
    assert (got.name, got.inputs, got.outputs, got.title) == ("w.gate", ("repo", "base"), ("gate",), "Run it")
    assert got.formats == {"gate": "json"}
    assert got.fn is gate and gate(None) == {"gate": {"ok": True}}  # the function is returned unchanged
    assert "w.gate" in cat and "w.other" not in cat and cat.names() == ["w.gate"]


def test_describe_lists_every_check_in_declaration_order() -> None:
    cat = Catalog()
    cat.add(Check("b", ("x",), ("y.{item}",), "B", lambda ctx: {}))
    cat.add(Check("a", (), (), "A", lambda ctx: {}))
    assert cat.names() == ["b", "a"]
    assert cat.describe() == [
        {"name": "b", "title": "B", "inputs": ["x"], "outputs": ["y.{item}"], "formats": {}},
        {"name": "a", "title": "A", "inputs": [], "outputs": [], "formats": {}},
    ]


def test_a_check_declared_twice_or_formatting_what_it_does_not_output_is_refused() -> None:
    cat = Catalog()
    cat.add(Check("a", (), (), "A", lambda ctx: {}))
    with pytest.raises(ValueError, match="check 'a' is declared twice"):
        cat.add(Check("a", (), (), "A again", lambda ctx: {}))
    with pytest.raises(ValueError, match="check 'b': formats name z, which it does not output"):
        cat.add(Check("b", (), ("y",), "B", lambda ctx: {}, {"z": "bool"}))
    with pytest.raises(KeyError, match="the catalog has no check 'c'"):
        cat["c"]


def test_the_check_decorator_declares_on_the_story_catalog() -> None:
    assert check.__module__ == "airlie.flow.catalog"
    assert "story.intake" in STORY_CATALOG  # airlie.story.flow declared its checks when imported


# ---- the story's declarations ----

PREDICATES = (
    "amigos_on",
    "amigos_off",
    "to_build",
    "survey_on",
    "is_story_card",
    "not_story_card",
    "open_layer",
    "not_built",
    "has_package",
)

DECLARED: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # today's checks, the layer's package and build (an interim adapter until Part 2), and the review gate's
    "story.intake": ((), ()),
    "story.take_card_as_story": ((), ("story",)),
    "story.base": ((), ("base",)),
    "story.gate": (("base",), ("gate",)),
    "story.agree_plain": (("story", "layers"), ("agreement",)),
    "story.costed": ((), ()),
    "story.planned": ((), ()),
    "story.covered": (("story", "layers"), ("satisfied.{item}",)),
    "story.build_done": ((), ()),
    "story.publish": (("story", "layers"), ("prs",)),
    "story.finish": ((), ()),
    "story.ready_package": (("base", "story", "layers"), ("package.{item}", "satisfied.{item}")),
    "story.build_once": (("package.{item}", "story", "layers"), ("built.{item}",)),
    # the computed inputs of the briefs (D13)
    "story.repo_guide": (("story", "base"), ("guide",)),
    "story.scope_guide": (("base", "layers"), ("guide",)),
    "story.lessons": ((), ("lessons",)),
    "story.dossier": (("base",), ("dossier",)),
    "story.coder_budget": ((), ("budget",)),
    "story.layer_sources": (("base", "layers"), ("sources.{item}",)),
    "story.layer_proof": (("agreement", "layers"), ("proof.{item}",)),
    "story.lint_note": (("base", "gate"), ("lint",)),
    # the board notes and phase marks the asks' after-hooks made
    "story.survey_note": (("survey",), ()),
    "story.refined_note": (("story",), ()),
    "story.layers_note": (("layers",), ()),
    # the predicates, as checks with one bool output (D16)
    **{f"story.{p}": ((), (p,)) for p in PREDICATES},
}


def test_every_check_the_old_catalog_named_is_declared() -> None:
    assert set(CATALOG["checks"]) <= set(STORY_CATALOG.names())


def test_every_predicate_is_a_check_with_one_bool_output() -> None:
    for p in PREDICATES:
        got = STORY_CATALOG[f"story.{p}"]
        assert (got.inputs, got.outputs, dict(got.formats)) == ((), (p,), {p: "bool"}), p
    assert {f"story.{p}" for p in PREDICATES} == set(CATALOG["predicates"])


def test_the_story_catalog_declares_these_inputs_and_outputs() -> None:
    got = {c.name: (c.inputs, c.outputs) for c in STORY_CATALOG}
    assert got == DECLARED
    assert all(c.title and c.title[0].isupper() for c in STORY_CATALOG)


def test_every_story_check_is_called_with_the_context_and_its_args() -> None:
    for c in STORY_CATALOG:
        params = list(inspect.signature(c.fn).parameters.values())
        assert params and params[0].name == "ctx", c.name
        assert all(p.kind is p.KEYWORD_ONLY for p in params[1:]), c.name


def test_describe_gives_the_builder_every_story_check() -> None:
    rows = STORY_CATALOG.describe()
    assert [r["name"] for r in rows] == STORY_CATALOG.names()
    assert {r["name"]: (tuple(r["inputs"]), tuple(r["outputs"])) for r in rows} == DECLARED


# ---- the story's checks run ----


class Run:
    """A story's registry and a context maker over a memory store."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = repo_with_marker_tests(tmp_path)
        self.pipe: StoryPipeline = pipeline(tmp_path, self.repo, happy_engine())
        self.card = text_card("add a marker")
        self.st = StoryState(path=tmp_path / "story", card={}, repo=str(self.repo))
        self.st.path.mkdir()
        self.comments: list[tuple[str, str | None]] = []

        async def comment(card: Any, text: str, role: str | None = None) -> None:
            self.comments.append((text, role))

        self.pipe.board.comment = comment  # type: ignore[method-assign]
        self.registry = story_registry(self.pipe, self.st, self.card, until="review")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.story = parse_story(extract_json_block(STORY.text), renumber=True)
        self.layers = parse_layers(extract_json_block(LAYERS.text), self.story, 5)

    def ctx(
        self, values: dict[str, Any], *, item: str | None = None, feedback: str | None = None
    ) -> RunContext:
        return RunContext(
            cfg=self.pipe.cfg,
            store=MemoryStore(values),
            sessions=Sessions(),
            registry=self.registry,
            workflow="story",
            node="n",
            item=item,
            feedback=feedback,
        )

    def planned(self) -> dict[str, Any]:
        return {
            "story": self.story.to_dict(),
            "layers": [x.to_dict() for x in self.layers],
            "base": {"ref": "origin/main", "sha": self.sha},
        }

    def kinds(self) -> list[str]:
        return [e["kind"] for e in kinds_all(self.st.path / "events.jsonl")]


async def call(name: str, ctx: RunContext, **args: Any) -> Any:
    out = STORY_CATALOG[name].fn(ctx, **args)
    return await out if inspect.isawaitable(out) else out


def test_story_registry_carries_the_run_its_checks_act_on(tmp_path: Path) -> None:
    run = Run(tmp_path)
    assert isinstance(run.registry, StoryRegistry)
    assert (run.registry.run.pipe, run.registry.run.st, run.registry.run.card) == (run.pipe, run.st, run.card)


async def test_a_story_check_outside_a_story_registry_is_refused(tmp_path: Path) -> None:
    from airlie.flow.composer import Registry

    run = Run(tmp_path)
    ctx = run.ctx({})
    ctx.registry = Registry()
    with pytest.raises(TypeError, match="node 'n': story.lessons runs only on a story's registry"):
        await call("story.lessons", ctx)


async def test_the_predicates_answer_as_their_registry_entries_do(tmp_path: Path) -> None:
    run = Run(tmp_path)
    ctx = run.ctx({}, item="01")
    for p in PREDICATES:
        assert await call(f"story.{p}", ctx) == {p: run.registry[f"story.{p}"](ctx)}, p
    assert await call("story.amigos_on", ctx) == {"amigos_on": False}  # the test pipeline's meeting is off
    assert await call("story.open_layer", ctx) == {"open_layer": True}
    run.st.built["01"] = {"sha": "x"}
    assert await call("story.open_layer", ctx) == {"open_layer": False}


async def test_the_coder_budget_lessons_and_dossier_are_the_briefs_values(tmp_path: Path) -> None:
    run = Run(tmp_path)
    ctx = run.ctx(run.planned())
    steps, context = run.pipe._coder_budget()
    assert await call("story.coder_budget", ctx) == {
        "budget": {"max_steps": steps, "context_budget": context}
    }
    assert await call("story.lessons", ctx) == {"lessons": []}
    assert await call("story.dossier", ctx) == {"dossier": dict(run.pipe._dossier(run.st, run.sha))}


async def test_the_repo_guides_read_the_base_as_the_briefs_do(tmp_path: Path) -> None:
    from airlie.story.briefs import story_text
    from airlie.story.context import guide_budget, repo_guide

    run = Run(tmp_path)
    ctx = run.ctx(run.planned())
    s = run.pipe.s
    story_guide = repo_guide(
        run.repo, run.sha, story_text(run.story), budget_chars=guide_budget(run.pipe.cfg, [s.architect_role])
    )
    assert await call("story.repo_guide", ctx) == {"guide": story_guide}
    scope = "\n".join(f"{x.scope}\n{x.brief}" for x in run.layers)
    budget = guide_budget(run.pipe.cfg, [s.architect_role, s.build_role])
    assert await call("story.scope_guide", ctx) == {
        "guide": repo_guide(run.repo, run.sha, scope, budget_chars=budget)
    }


async def test_a_layers_sources_proof_and_lint_are_per_item(tmp_path: Path) -> None:
    from airlie.story.briefs import layer_sources
    from airlie.story.tester_lint import lint_note

    run = Run(tmp_path)
    proof = ProofTest("AC2", "profile", "tests/test_profile.py", "test_shows", "unit", None, "it shows", "")
    agreement = Agreement(run.story, run.layers, (proof,)).to_dict()
    ctx = run.ctx({**run.planned(), "agreement": agreement, "gate": {"commands": []}}, item="02")
    assert await call("story.layer_sources", ctx) == {
        "sources.{item}": layer_sources(run.repo, run.sha, run.layers[1])
    }
    assert await call("story.layer_proof", ctx) == {"proof.{item}": [proof.to_dict()]}
    assert await call("story.layer_proof", run.ctx(ctx.store._values, item="01")) == {"proof.{item}": []}
    assert await call("story.lint_note", ctx) == {"lint": lint_note(run.repo, run.sha, run.pipe.commands)}


async def test_the_survey_note_says_done_or_failed_as_the_after_hooks_did(tmp_path: Path) -> None:
    run = Run(tmp_path)
    assert await call("story.survey_note", run.ctx({"survey": "notes"})) == {}
    assert run.st.survey == "notes" and run.kinds() == ["survey_done"]
    run.st.survey = None
    assert await call("story.survey_note", run.ctx({}, feedback="RuntimeError: down")) == {}
    events = kinds_all(run.st.path / "events.jsonl")
    assert [(e["kind"], e.get("reason"), e.get("chars")) for e in events[1:]] == [
        ("survey_failed", "RuntimeError: down", None),
        ("survey_done", None, 0),
    ]
    assert run.st.survey == ""  # no notes, and it is not asked again


async def test_the_refined_and_layers_notes_mark_and_tell_the_card(tmp_path: Path) -> None:
    from airlie.story.board import note
    from airlie.story.pipeline import _layers_note, _story_note

    run = Run(tmp_path)
    assert await call("story.refined_note", run.ctx({"story": run.story.to_dict()})) == {}
    assert run.st.done("refine") and run.st.story == run.story.to_dict()
    assert await call("story.layers_note", run.ctx({"layers": [x.to_dict() for x in run.layers]})) == {}
    assert run.st.layers == [x.to_dict() for x in run.layers]
    assert run.comments == [
        (note(_story_note(run.story)), run.pipe.s.product_role),
        (note(_layers_note(run.layers)), run.pipe.s.architect_role),
    ]


async def test_the_layers_steps_keep_their_old_registry_shape_through_the_interim_adapter(
    tmp_path: Path,
) -> None:
    run = Run(tmp_path)
    for id in ("story.ready_package", "story.build_once"):
        step = run.registry[id]
        assert list(inspect.signature(step).parameters) == ["node", "ctx"], id
        assert step is not STORY_CATALOG[id].fn
    assert "session" in inspect.signature(STORY_CATALOG["story.ready_package"].fn).parameters
