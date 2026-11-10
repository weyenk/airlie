"""A card names its workflow (a `workflow:<name>` label, else the story); the pipeline resolves it from the
repo's workflows and the board's drafts, snapshots it into the card's state on first use and runs the
snapshot on every resume (docs/superpowers/specs/2026-11-07-workflow-builder-design.md, D16, D17, D21)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from airlie.board import BoardStore
from airlie.config import load_config
from airlie.engine import FakeReply
from airlie.story.board import text_card
from airlie.story.workflows import (
    WorkflowError,
    card_workflow,
    closure_prompts,
    from_snapshot,
    has_node,
    resolve_workflows,
    running_workflow,
    snapshot,
    workflow_label,
    workflow_problems,
)
from tests.support.story import (
    RecordingBoard,
    happy_engine,
    kinds_all,
    pipeline,
    repo_with_marker_tests,
)
from tests.support.story_workflows import (
    MINI,
    MINI_EACH,
    MINI_MORE,
    MINI_NOTE,
    NOTE,
    TINY,
    before_review,
    mini_card,
    started,
)

MINI_REPRO = before_review(
    MINI,
    "repro: {type: ask, role: product, prompt: 'prompt:mini-note', inputs: [card], outputs: {repro: text}}",
    "read_repro: {type: check, run: story.costed, args: {stages: [plan]}, inputs: [card, repro]}",
)


def test_card_workflow_defaults_to_story_and_reads_the_label() -> None:
    assert card_workflow(text_card("x")) == "story"
    assert card_workflow(replace(text_card("x"), labels=("repo:o/r", "workflow:bug-fix"))) == "bug-fix"


@pytest.mark.parametrize(
    ("saved", "labels", "runs"),
    [
        (None, ("workflow:mini",), "mini"),  # not started: the card's label
        (None, (), "story"),  # no label: the story
        ({"stage": "new"}, ("workflow:mini",), "mini"),  # a record that never got past new: not started
        ({"stage": "intake", "workflow": None}, ("workflow:mini",), "story"),  # started with no snapshot
        ({"stage": "intake", "workflow": {"name": "fix"}}, ("workflow:mini",), "fix"),  # its snapshot
        ({"stage": "intake", "workflow": {"name": "fix"}}, (), "fix"),
    ],
)
def test_the_workflow_a_card_runs_is_its_snapshot_else_story_once_started_else_its_label(
    saved: dict[str, Any] | None, labels: tuple[str, ...], runs: str
) -> None:
    """One rule for `_drive`, `Line._workflow_hold` and `LocalBoard._unrunnable`: a card that started with no
    snapshot (the driver ran it, or an airlie from before workflows) has run the story's stages."""
    assert running_workflow(saved, labels) == runs


def test_workflow_label_is_the_first_named_one_and_none_without_one() -> None:
    assert workflow_label(()) is None and workflow_label(("repo:o/r", "workflow:", "workflow: ")) is None
    assert workflow_label(("workflow:", "workflow: bug-fix", "workflow:other")) == "bug-fix"
    assert card_workflow(replace(text_card("x"), labels=("workflow:",))) == "story"


def test_resolve_adds_a_valid_draft_and_refuses_an_invalid_or_missing_one() -> None:
    cfg = load_config(Path("airlie.yaml"))
    good = (
        "workflows:\n  mini:\n    type: bug\n    inputs: [card, repo]\n    nodes:\n"
        "      intake: {type: check, run: story.intake, inputs: [card]}\n"
    )
    assert set(resolve_workflows(cfg, {"mini": good}, "mini")) == {"mini"}
    with pytest.raises(WorkflowError, match="node intake: check needs run"):
        resolve_workflows(cfg, {"mini": good.replace(" run: story.intake,", "")}, "mini")
    with pytest.raises(WorkflowError, match="no workflow named 'nope'"):
        resolve_workflows(cfg, {}, "nope")


def test_only_the_named_workflow_and_the_bundles_it_calls_are_resolved() -> None:
    cfg = load_config(Path("airlie.yaml"))
    broken = {"junk": "workflows: [1", "half": "workflows:\n  half:\n    start: nowhere\n    nodes: {}\n"}
    assert set(resolve_workflows(cfg, {"mini": MINI, **broken}, "mini")) == {"mini"}
    assert set(resolve_workflows(cfg, {"mini": MINI_EACH, "tiny": TINY, **broken}, "mini")) == {
        "mini",
        "tiny",
    }
    assert set(resolve_workflows(cfg, broken, "story")) == {"story", "layer"}  # the files' own, first
    with pytest.raises(WorkflowError, match="unknown workflow 'tiny'"):
        resolve_workflows(cfg, {"mini": MINI_EACH}, "mini")


def test_a_file_wins_over_a_draft_of_the_same_name() -> None:
    """A draft named as a loaded file is a pending edit of that file (merged, or not yet published): the
    file runs, the draft and its prompt files are left out, and nothing is refused."""
    cfg = load_config(Path("airlie.yaml"))
    edit = MINI_NOTE.replace("  mini:", "  story:")
    prompts = {"story": {"mini-note": "not a rule file"}}
    specs = resolve_workflows(cfg, {"story": edit}, "story", prompts=prompts)
    assert specs["story"] == cfg.workflows["story"] and set(specs) == {"story", "layer"}
    assert workflow_problems(cfg, {"story": "workflows: [1"}, "story", prompts=prompts) == []


def test_a_draft_must_hold_its_one_workflow() -> None:
    cfg = load_config(Path("airlie.yaml"))
    with pytest.raises(WorkflowError, match="mini"):
        resolve_workflows(cfg, {"mini": MINI + TINY.removeprefix("workflows:\n")}, "mini")
    with pytest.raises(WorkflowError, match="other"):
        resolve_workflows(cfg, {"other": MINI}, "other")
    with pytest.raises(WorkflowError, match="mini"):
        resolve_workflows(cfg, {"mini": "workflows: [1"}, "mini")


def test_a_snapshot_round_trips_and_names_its_nodes() -> None:
    cfg = load_config(Path("airlie.yaml"))
    specs = resolve_workflows(cfg, {"mini": MINI}, "mini")
    data = json.loads(json.dumps(snapshot("mini", specs, {"mini-note": NOTE})))
    name, back, prompts = from_snapshot(data)
    assert name == "mini" and back == specs and prompts == {"mini-note": NOTE}
    assert not has_node(back["mini"], "planned") and not has_node(back["mini"], "publish")
    _, story, none = from_snapshot(snapshot("story", resolve_workflows(cfg, {}, "story")))
    assert has_node(story["story"], "planned") and has_node(story["story"], "publish") and none == {}
    older = {"name": "mini", "specs": data["specs"]}  # saved before snapshots carried prompt files
    assert from_snapshot(older)[2] == {}


def test_a_draft_is_held_to_the_story_catalog() -> None:
    """Rule 9 runs where drafts resolve: a check whose `run` the catalog lacks is refused, one that matches
    its declaration is not."""
    cfg = load_config(Path("airlie.yaml"))
    draft = "workflows:\n  nope:\n    inputs: [card]\n    nodes:\n      a: {type: check, run: story.nope}\n"
    assert workflow_problems(cfg, {"nope": draft}, "nope") == [
        "workflow nope: node a: run 'story.nope' is not in the catalog"
    ]
    fine = draft.replace("run: story.nope}", "run: story.intake, inputs: [card]}")
    assert workflow_problems(cfg, {"nope": fine}, "nope") == []


def test_a_drafts_own_prompt_file_resolves_and_an_unknown_or_broken_one_does_not() -> None:
    cfg = load_config(Path("airlie.yaml"))
    own = {"mini": {"mini-note": NOTE}}
    assert set(resolve_workflows(cfg, {"mini": MINI_NOTE}, "mini", prompts=own)) == {"mini"}
    assert workflow_problems(cfg, {"mini": MINI_NOTE}, "mini") == [
        "workflow mini: node note: unknown prompt file 'mini-note'"
    ]
    assert closure_prompts({**own, "other": {"x": "y"}}, ["mini", "story"]) == {"mini-note": NOTE}
    broken = workflow_problems(cfg, {"mini": MINI_NOTE}, "mini", prompts={"mini": {"mini-note": "Hi {card}"}})
    assert len(broken) == 1 and "rules/mini-note.md" in broken[0] and "---" in broken[0]


def test_a_snapshot_that_no_longer_validates_is_a_workflow_error() -> None:
    with pytest.raises(WorkflowError, match="snapshot of 'mini' no longer validates"):
        from_snapshot({"name": "mini", "specs": {"mini": {"nodes": {}, "gone": 1}}})


async def test_a_workflow_without_a_planned_node_passes_the_plan_lane_at_once(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False)
    pipe.drafts = lambda: {"mini": MINI}
    st = await pipe.plan(mini_card())
    events = kinds_all(st.path / "events.jsonl")
    assert [e["kind"] for e in events][-3:] == ["story_planned", "stage", "story_done"]
    assert next(e for e in events if e["kind"] == "story_planned")["layers"] == 0
    assert st.stage == "layers" and st.blocked is None
    assert started(st.path / "events.jsonl") == []
    assert st.workflow is not None and st.workflow["name"] == "mini"  # snapshotted when the card started


async def test_a_card_on_a_draft_runs_it_and_keeps_its_snapshot_when_the_draft_changes(
    tmp_path: Path,
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False)  # the flag off: the label wins
    pipe.drafts = lambda: {"mini": MINI}
    card = mini_card()
    await pipe.plan(card)
    st = await pipe.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    assert st.workflow is not None and st.workflow["name"] == "mini"
    assert started(st.path / "events.jsonl") == [
        ("intake", None),
        ("base", None),
        ("gate", None),
        ("review", None),
    ]
    before = len(kinds_all(st.path / "events.jsonl"))

    again = pipeline(tmp_path, repo, happy_engine(), composer=False)
    again.drafts = lambda: {"mini": MINI_MORE}
    st = await again.build(card)
    assert st.stage == "review" and st.workflow["name"] == "mini"
    # a finished card's resume walks its snapshot again (each step sees it finished), never the new node
    later = {e["node"] for e in kinds_all(st.path / "events.jsonl")[before:] if e["kind"] == "node_started"}
    assert later <= {"intake", "base", "gate", "review"}
    assert "costed" not in st.workflow["specs"]["mini"]["nodes"]


async def test_a_started_card_runs_its_snapshot_not_the_changed_draft(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False)
    pipe.drafts = lambda: {"mini": MINI}
    card = mini_card()
    await pipe.plan(card)
    pipe.drafts = lambda: {"mini": MINI_MORE}
    st = await pipe.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    assert [n for n, _ in started(st.path / "events.jsonl")] == ["intake", "base", "gate", "review"]
    assert "costed" not in st.workflow["specs"]["mini"]["nodes"]


async def test_a_card_whose_label_is_removed_after_it_started_still_runs_its_snapshot(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False)
    pipe.drafts = lambda: {"mini": MINI}
    card = mini_card()
    await pipe.plan(card)
    st = await pipe.build(replace(card, labels=()))  # the same card, its workflow: label gone (D21)
    assert st.blocked is None and st.stage == "review", st.blocked
    assert [n for n, _ in started(st.path / "events.jsonl")] == ["intake", "base", "gate", "review"]


async def test_a_drafts_prompt_file_is_the_brief_and_a_started_card_keeps_its_text(tmp_path: Path) -> None:
    """A draft's ask node names the draft's own prompt file: the brief is that file with the card filled
    in, and the snapshot keeps the text, so an edit to the draft's prompts after the card started changes
    nothing for it (D21)."""
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(product=[FakeReply(text="one line")])
    pipe = pipeline(tmp_path, repo, engine, composer=False)
    pipe.drafts = lambda: {"mini": MINI_NOTE}
    pipe.draft_prompts = lambda: {"mini": {"mini-note": NOTE}}
    card = mini_card()
    await pipe.plan(card)
    pipe.draft_prompts = lambda: {"mini": {"mini-note": NOTE.replace("Write one line", "Write a poem")}}
    st = await pipe.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    [asked] = [r for r in engine.requests if r.role == "product"]
    assert "Write one line about this card: " in asked.messages[-1].content
    assert st.workflow is not None and st.workflow["prompts"] == {"mini-note": NOTE}


async def test_a_card_the_driver_started_stays_the_story_when_it_gains_a_label(tmp_path: Path) -> None:
    """A card that started with no snapshot (the driver ran it: the flag off, no label) has run the story's
    stages, which no snapshot records; a `workflow:` label it gains after (the board API refuses that, a
    Jira card's labels do not ask) leaves it on the story, run as it started."""
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False)
    called: list[Any] = []
    pipe.drafts = lambda: called.append(1) or {"mini": MINI}  # type: ignore[func-returns-value]
    card = text_card("add a marker")
    st = await pipe.plan(card)
    assert st.workflow is None and st.stage != "new"
    st = await pipe.build(replace(card, labels=("workflow:mini",)))
    assert st.blocked is None and st.stage == "review", st.blocked
    assert started(st.path / "events.jsonl") == [] and st.workflow is None and called == []


async def test_a_drafts_own_output_is_stored_read_by_a_later_node_and_skipped_on_resume(
    tmp_path: Path,
) -> None:
    """D15: an output that is no state field (`repro`) lands in `st.artifacts`, a later node reads it, and
    a resume skips the node that made it."""
    repo = repo_with_marker_tests(tmp_path)
    engine = happy_engine(product=[FakeReply(text="click save twice")])
    pipe = pipeline(tmp_path, repo, engine, composer=False)
    pipe.drafts = lambda: {"mini": MINI_REPRO}
    pipe.draft_prompts = lambda: {"mini": {"mini-note": NOTE}}
    card = mini_card()
    await pipe.plan(card)
    st = await pipe.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    assert st.artifacts == {"repro": "click save twice"}
    assert json.loads((st.path / "state.json").read_text())["artifacts"] == {"repro": "click save twice"}
    assert [n for n, _ in started(st.path / "events.jsonl")] == [
        "intake",
        "base",
        "gate",
        "repro",
        "read_repro",
        "review",
    ]
    before = len(kinds_all(st.path / "events.jsonl"))

    again = pipeline(tmp_path, repo, happy_engine(), composer=False)  # no product reply: an ask would fail
    again.drafts = pipe.drafts
    again.draft_prompts = pipe.draft_prompts
    st = await again.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    later = kinds_all(st.path / "events.jsonl")[before:]
    assert {"node": "repro", "kind": "node_skipped"} in [
        {"node": e.get("node"), "kind": e["kind"]} for e in later
    ]
    assert "repro" not in {e["node"] for e in later if e["kind"] == "node_started"}
    assert st.artifacts == {"repro": "click save twice"}


async def test_the_plan_lane_shortcut_takes_the_card_in_and_says_so_once(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    board = RecordingBoard()
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False, board=board)
    pipe.drafts = lambda: {"mini": MINI}
    card = mini_card()
    await pipe.plan(card)
    assert board.moves == ["building"]
    assert sum("picked this up" in n for n in board.notes) == 1
    st = await pipe.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    assert sum("picked this up" in n for n in board.notes) == 1  # the build lane's intake posts nothing more


async def test_a_drafts_each_node_runs_its_draft_bundle_per_item(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False)
    pipe.drafts = lambda: {"mini": MINI_EACH, "tiny": TINY}
    card = mini_card()
    st = await pipe.plan(card)
    saved = json.loads((st.path / "state.json").read_text())  # the layers artifact, as a node would leave it
    saved["layers"] = [{"slug": "a"}, {"slug": "b"}]
    (st.path / "state.json").write_text(json.dumps(saved))
    st = await pipe.build(card)
    assert st.blocked is None and st.stage == "review", st.blocked
    assert started(st.path / "events.jsonl") == [
        ("intake", None),
        ("base", None),
        ("gate", None),
        ("build_layers", None),
        ("note", "01"),
        ("note", "02"),
        ("review", None),
    ]


async def test_a_card_on_a_workflow_that_is_not_there_is_blocked_with_the_reason(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False)
    st = await pipe.build(mini_card())
    assert st.blocked is not None and "no workflow named 'mini'" in st.blocked
    assert st.workflow is None


async def test_a_story_card_with_no_label_still_runs_the_driver_when_the_flag_is_off(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    pipe = pipeline(tmp_path, repo, happy_engine(), composer=False)
    called: list[Any] = []
    pipe.drafts = lambda: called.append(1) or {}  # type: ignore[func-returns-value]
    st = await pipe.run(text_card("add a marker"))
    assert st.blocked is None and st.stage == "review", st.blocked
    assert started(st.path / "events.jsonl") == []
    assert st.workflow is None and called == []
    saved = json.loads((st.path / "state.json").read_text())  # the base's bytes: no workflow keys at all
    assert "artifacts" not in saved and "workflow" not in saved


async def test_make_pipeline_reads_the_boards_drafts(tmp_path: Path) -> None:
    from airlie.story.factory import make_pipeline, stories_dir
    from airlie.story.local_board import LocalBoard
    from tests.support.story_factory import STORY_ROLES, cfg_for

    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"composer": False}})
    board = LocalBoard(BoardStore(tmp_path / "board.sqlite"), stories_dir=stories_dir(cfg))
    pipe = make_pipeline(cfg, board=board, repo=repo, test_cmd="./check.sh")
    assert pipe.drafts() == {}
    board.store.save_draft("mini", type="bug", yaml=MINI)
    assert pipe.drafts() == {"mini": MINI}  # read at use, not at build
    init = board.store.create(level="initiative", title="i")
    epic = board.store.create(level="epic", title="e", parent=init.key)
    story = board.store.create(
        level="story", title="fix the marker", parent=epic.key, labels=["workflow:mini"]
    )
    st = await pipe.plan(await board.card(story.key))
    assert st.workflow is not None and st.workflow["name"] == "mini" and st.blocked is None
