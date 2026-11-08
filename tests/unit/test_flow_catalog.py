"""The catalog of checks (nodes-are-config design, "Checks", D2, D13, D18): declared operations with their
inputs and outputs, and the story's declarations of every check its workflows name."""

from __future__ import annotations

import contextlib
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from airlie.flow.catalog import STORY_CATALOG, Catalog, Check, check
from airlie.flow.composer import MissingInput, RunContext
from airlie.flow.store import MemoryStore, Sessions, StateStore
from airlie.story.amigos import Agreement, ProofTest
from airlie.story.board import text_card
from airlie.story.flow import PREDICATES as TESTS
from airlie.story.flow import StoryRegistry, story_registry
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

READS: dict[str, tuple[str, ...]] = {
    "amigos_on": (),
    "amigos_off": (),
    "to_build": ("layers", "built", "satisfied"),
    "survey_on": ("survey", "story"),
    "is_story_card": ("card",),
    "not_story_card": ("card",),
    "open_layer": ("built", "satisfied"),
    "not_built": ("drafted", "built", "satisfied"),
    "has_package": ("built", "satisfied", "package.{item}"),
}
"""What each predicate reads from the store."""

DECLARED: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # today's checks, the layer's package and build (an interim adapter until Part 2), and the review gate's;
    # every store and loose name each reads is an input (D23), `card` and `repo` included
    "story.intake": (("card",), ()),
    "story.take_card_as_story": (("card",), ("story",)),
    "story.base": (("card", "repo"), ("base",)),
    "story.gate": (("repo", "base"), ("gate",)),
    "story.agree_plain": (("story", "layers"), ("agreement",)),
    "story.costed": (("card",), ()),
    "story.planned": (("layers",), ()),
    "story.covered": (("card", "repo", "story", "layers"), ("satisfied.{item}",)),
    "story.build_done": ((), ()),
    "story.publish": (("card", "repo", "story", "layers"), ("prs",)),
    "story.finish": (("card",), ()),
    "story.ready_package": (("card", "repo", "story", "layers"), ("package.{item}", "satisfied.{item}")),
    "story.build_once": (("card", "repo", "package.{item}", "story", "layers"), ("built.{item}",)),
    # the computed inputs of the briefs (D13)
    "story.repo_guide": (("repo", "story", "base"), ("guide",)),
    "story.scope_guide": (("repo", "base", "layers"), ("guide",)),
    "story.lessons": ((), ("lessons",)),
    "story.dossier": (("repo", "base"), ("dossier",)),
    "story.coder_budget": ((), ("budget",)),
    "story.layer_sources": (("repo", "base", "layers"), ("sources.{item}",)),
    "story.layer_proof": (("agreement", "layers"), ("proof.{item}",)),
    "story.lint_note": (("repo", "base"), ("lint",)),
    # the steps that stay code until Part 2 (refine, the plan in rounds, the drafting), and a layer's start
    "story.refine": (("card",), ("story",)),
    "story.plan_layers": (("card", "repo", "story", "base", "survey"), ("layers",)),
    "story.draft_packages": (("repo", "base", "story", "layers", "agreement"), ("drafted", "drafts_session")),
    "story.layer_start": ((), ()),
    # the computed input of the survey's prompt file
    "story.request": (("card",), ("request",)),
    # the board notes and phase marks the asks' after-hooks made
    "story.survey_note": (("survey",), ()),
    "story.refined_note": (("card", "story"), ()),
    "story.layers_note": (("card", "layers"), ()),
    # the predicates, as checks with one bool output (D16)
    **{f"story.{p}": (READS[p], (p,)) for p in PREDICATES},
}


OLD_CHECKS = (
    "story.intake",
    "story.take_card_as_story",
    "story.base",
    "story.gate",
    "story.agree_plain",
    "story.costed",
    "story.planned",
    "story.covered",
    "story.build_done",
    "story.publish",
    "story.finish",
    "story.ready_package",
    "story.build_once",
)
"""The checks the old registry named by `run:`."""


def test_every_check_the_old_registry_named_is_declared() -> None:
    assert set(OLD_CHECKS) <= set(STORY_CATALOG.names())


def test_every_predicate_is_a_check_with_one_bool_output() -> None:
    for p in PREDICATES:
        got = STORY_CATALOG[f"story.{p}"]
        assert (got.inputs, got.outputs, dict(got.formats)) == (READS[p], (p,), {p: "bool"}), p
    assert set(TESTS) == set(PREDICATES)


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
            store=MemoryStore({"card": self.card, "repo": self.repo, **values}),
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


async def test_the_predicates_answer_as_their_tests_do(tmp_path: Path) -> None:
    run = Run(tmp_path)
    ctx = run.ctx({}, item="01")
    for p in PREDICATES:
        assert await call(f"story.{p}", ctx) == {p: TESTS[p][0](ctx)}, p
    assert await call("story.amigos_on", ctx) == {"amigos_on": False}  # the test pipeline's meeting is off
    assert await call("story.open_layer", ctx) == {"open_layer": True}
    # over the story's state, the state fields are read by name, as the old predicates read them
    ctx.store = StateStore(run.st, loose={"card": run.card, "repo": run.repo})
    run.st.built["01"] = {"sha": "x"}
    assert await call("story.open_layer", ctx) == {"open_layer": False}
    assert await call("story.to_build", ctx) == {"to_build": True}  # no layers planned yet
    run.st.layers = [x.to_dict() for x in run.layers[:1]]
    assert await call("story.to_build", ctx) == {"to_build": False}


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


async def test_the_survey_note_keeps_no_notes_for_a_survey_that_failed(tmp_path: Path) -> None:
    """Notes the survey node stored were said done by its own event: nothing here. A survey that failed
    stored none: kept as no notes and said done, as the driver's `survey_failed` does after its event."""
    run = Run(tmp_path)
    assert await call("story.survey_note", run.ctx({"survey": "notes"})) == {}
    assert run.st.survey is None and not (run.st.path / "events.jsonl").exists()
    assert await call("story.survey_note", run.ctx({})) == {}
    events = kinds_all(run.st.path / "events.jsonl")
    assert [(e["kind"], e.get("chars")) for e in events] == [("survey_done", 0)]
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


def test_the_registry_holds_each_check_itself_the_package_taking_its_session_as_an_arg(
    tmp_path: Path,
) -> None:
    run = Run(tmp_path)
    assert all(run.registry.check(c.name) is c.fn for c in STORY_CATALOG)
    assert "session" in inspect.signature(STORY_CATALOG["story.ready_package"].fn).parameters


async def test_the_request_is_the_card_as_the_survey_brief_quotes_it(tmp_path: Path) -> None:
    from airlie.prompts import text
    from airlie.story.briefs import request_section, survey_brief

    run = Run(tmp_path)
    got = await call("story.request", run.ctx({}))
    assert got == {"request": request_section(run.card)}
    assert text("story-survey", request=got["request"]) == survey_brief(run.card)
    assert await call("story.layer_start", run.ctx({})) == {}


async def test_the_interim_steps_call_the_drivers_and_the_drafting_is_held_for_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """refine and the layer plan are the driver's `_refine` and `_layers`; the drafting conversation is held
    on the run's stack under `drafts_session`, dropped when the stack closes it, and needs a stack."""
    run = Run(tmp_path)
    pipe: Any = run.pipe
    asked: list[str] = []

    async def refine(st: StoryState, card: Any) -> None:
        asked.append("refine")
        st.story = run.story.to_dict()

    async def plan(st: StoryState, card: Any, story: Any) -> None:
        asked.append(f"layers of {story.title}")
        st.layers = [x.to_dict() for x in run.layers]

    monkeypatch.setattr(pipe, "_refine", refine)
    monkeypatch.setattr(pipe, "_layers", plan)
    assert await call("story.refine", run.ctx({})) == {"story": run.story.to_dict()}
    planned = await call("story.plan_layers", run.ctx(run.planned()))
    assert planned == {"layers": [x.to_dict() for x in run.layers]}
    assert asked == ["refine", f"layers of {run.story.title}"]

    session = SimpleNamespace(say=None, run="r1", state=None)
    opened: list[Any] = []

    async def opening(pipe: Any, st: Any, story: Any, layers: Any) -> str:
        return "the opening"

    async def drafted(pipe: Any, st: Any, story: Any, layers: Any, first: str, talk: Any) -> Any:
        opened.append((first, talk.keywords["stage"], talk.keywords["held"]))
        return session

    monkeypatch.setattr("airlie.story.flow.drafts_opening", opening)
    monkeypatch.setattr("airlie.story.flow.draft_packages", drafted)
    with pytest.raises(RuntimeError, match="holds nothing open"):
        await call("story.draft_packages", run.ctx(run.planned()))
    async with contextlib.AsyncExitStack() as held:
        ctx = run.ctx(run.planned())
        ctx.held = held
        assert await call("story.draft_packages", ctx) == {"drafted": True, "drafts_session": session}
        ctx.sessions.put("drafts_session", session)  # the composer stores the session output
        assert opened == [("the opening", "packages", held)]
    assert ctx.sessions.get("drafts_session") is None  # dropped with the conversation


class Undeclared(Exception):
    """A check read a name its declaration does not give."""


class Recording:
    """A memory store that records every name read and refuses one outside `allowed`."""

    def __init__(self, values: dict[str, Any], allowed: set[str]) -> None:
        self.inner = MemoryStore(values)
        self.allowed = allowed
        self.read: set[str] = set()

    def _reading(self, name: str) -> None:
        if name not in self.allowed:
            raise Undeclared(name)
        self.read.add(name)

    def has(self, name: str) -> bool:
        self._reading(name)
        return self.inner.has(name)

    def get(self, name: str) -> Any:
        self._reading(name)
        return self.inner.get(name)

    def put(self, name: str, value: Any) -> None:
        self.inner.put(name, value)

    def save(self) -> None:
        pass

    def emit(self, kind: str, **fields: Any) -> None:
        self.inner.emit(kind, **fields)


THROUGH_THE_PIPELINE = {
    # declared names a check's body hands to the pipeline's step, which reads them itself (`repo`: the
    # store's loose repo is the pipeline's), or reads only on one path
    "story.base": {"repo"},
    "story.gate": {"repo"},
    "story.covered": {"repo"},
    "story.ready_package": {"repo"},
    "story.build_once": {"repo"},
    "story.publish": {"repo"},
    "story.dossier": {"repo"},
    "story.planned": {"layers"},  # read for the planning lane's story_planned only (`until: planned`)
    "story.plan_layers": {"repo", "base", "survey"},  # `_layers` reads them from the state
    "story.draft_packages": {"repo", "base", "agreement"},  # `drafts_opening` reads them from the state
}


async def test_every_story_check_reads_only_what_it_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each check of the story catalog, in declaration order on one layer, over a store that refuses any name
    its declaration does not give; the pipeline's steps are fakes (they read the state, not the store). It
    reads every name it declares but those the pipeline's step reads itself (`THROUGH_THE_PIPELINE`)."""
    run = Run(tmp_path)
    pipe: Any = run.pipe

    async def nothing(*args: Any, **kwargs: Any) -> None:
        return None

    async def site(*args: Any) -> Any:
        return ("a", "b", "c", None), "built", "guide"

    async def package(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(to_dict=lambda: {"package": 1})

    async def build(st: StoryState, *args: Any) -> None:
        st.built["01"] = {"sha": "x"}

    async def resolved(*args: Any) -> dict[str, str]:
        return {"ref": "origin/main", "sha": run.sha}

    async def opening(*args: Any) -> str:
        return "opening"

    async def drafted(*args: Any) -> Any:
        return SimpleNamespace(say=None, run="r", state=None)

    for name in ("_intake", "_costed", "_satisfy", "_publish", "_finish", "_refine", "_layers"):
        monkeypatch.setattr(pipe, name, nothing)
    monkeypatch.setattr("airlie.story.flow.drafts_opening", opening)
    monkeypatch.setattr("airlie.story.flow.draft_packages", drafted)
    monkeypatch.setattr(pipe, "_base", resolved)
    monkeypatch.setattr(pipe, "_gate", lambda st, base: None)
    monkeypatch.setattr(pipe, "_layer_site", site)
    monkeypatch.setattr(pipe, "_ready_package", package)
    monkeypatch.setattr(pipe, "_build_once", build)
    monkeypatch.setattr(pipe, "_story_card", lambda card: None)
    monkeypatch.setattr("airlie.story.flow.Package", SimpleNamespace(from_dict=lambda d: d))
    values = {
        **run.planned(),
        "card": run.card,
        "repo": run.repo,
        "gate": {"commands": []},
        "agreement": Agreement(run.story, run.layers, ()).to_dict(),
        "survey": "notes",
        "built": {},
        "satisfied": {},
        "drafted": False,
        "package.01": {"package": 1},
    }
    args: dict[str, dict[str, Any]] = {"story.costed": {"stages": ["plan"]}}
    unread: dict[str, set[str]] = {}
    for c in STORY_CATALOG:
        allowed = {name.replace("{item}", "01") for name in c.inputs}
        store = Recording(values, allowed)
        ctx = run.ctx({}, item="01")
        ctx.store = store
        ctx.held = contextlib.AsyncExitStack()
        try:
            await call(c.name, ctx, **args.get(c.name, {}))
        except Undeclared as e:
            pytest.fail(f"{c.name} reads '{e}', which it does not declare")
        if left := allowed - store.read:
            unread[c.name] = left
    assert unread == THROUGH_THE_PIPELINE


async def test_a_missing_input_names_the_node_the_check_and_the_input(tmp_path: Path) -> None:
    run = Run(tmp_path)
    with pytest.raises(MissingInput, match="story.repo_guide needs 'story', which the store does not"):
        await call("story.repo_guide", run.ctx({"base": {"sha": run.sha}}))
    with pytest.raises(MissingInput, match="node 'n': story.scope_guide needs 'base' with its sha"):
        await call("story.scope_guide", run.ctx({**run.planned(), "base": {"ref": "x"}}))
    with pytest.raises(MissingInput, match="node 'n': story.layer_sources needs 'layers'"):
        await call("story.layer_sources", run.ctx({"base": {"sha": run.sha}}, item="01"))
    with pytest.raises(MissingInput, match="story.layer_proof runs per layer, and the node has no"):
        await call("story.layer_proof", run.ctx(run.planned()))
