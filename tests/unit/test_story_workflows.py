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
from airlie.story.board import Card, text_card
from airlie.story.workflows import (
    WorkflowError,
    card_workflow,
    from_snapshot,
    has_node,
    resolve_workflows,
    snapshot,
    workflow_label,
)
from tests.unit.test_story_pipeline import (
    RecordingBoard,
    happy_engine,
    kinds_all,
    pipeline,
    repo_with_marker_tests,
)

MINI = """\
workflows:
  mini:
    type: bug
    start: intake
    loose: [card, repo]
    nodes:
      intake: {type: check, run: story.intake}
      base: {type: check, run: story.base, outputs: {base: json}}
      gate: {type: check, run: story.gate, inputs: [base], outputs: {gate: json}}
      review: {type: gate, run: story.finish}
"""
MINI_MORE = MINI.replace(
    "      review:", "      costed: {type: check, run: story.costed, args: {stages: [plan]}}\n      review:"
)
MINI_EACH = MINI.replace(
    "      review:", "      build_layers: {type: each, over: layers, workflow: tiny}\n      review:"
)
TINY = """\
workflows:
  tiny:
    start: note
    loose: [card]
    nodes:
      note: {type: check, run: story.costed, args: {stages: [layer]}}
"""


def mini_card(text: str = "fix the marker") -> Card:
    return replace(text_card(text), labels=("workflow:mini",))


def started(path: Path) -> list[tuple[str, str | None]]:
    return [(e["node"], e.get("item")) for e in kinds_all(path) if e["kind"] == "node_started"]


def test_card_workflow_defaults_to_story_and_reads_the_label() -> None:
    assert card_workflow(text_card("x")) == "story"
    assert card_workflow(replace(text_card("x"), labels=("repo:o/r", "workflow:bug-fix"))) == "bug-fix"


def test_workflow_label_is_the_first_named_one_and_none_without_one() -> None:
    assert workflow_label(()) is None and workflow_label(("repo:o/r", "workflow:", "workflow: ")) is None
    assert workflow_label(("workflow:", "workflow: bug-fix", "workflow:other")) == "bug-fix"
    assert card_workflow(replace(text_card("x"), labels=("workflow:",))) == "story"


def test_resolve_adds_a_valid_draft_and_refuses_an_invalid_or_missing_one() -> None:
    cfg = load_config(Path("airlie.yaml"))
    good = (
        "workflows:\n  mini:\n    type: bug\n    start: intake\n    loose: [card, repo]\n    nodes:\n"
        "      intake: {type: check, run: story.intake}\n"
    )
    assert set(resolve_workflows(cfg, {"mini": good}, "mini")) == {"mini"}
    with pytest.raises(WorkflowError, match="node intake: check needs run"):
        resolve_workflows(cfg, {"mini": good.replace(", run: story.intake", "")}, "mini")
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


def test_a_draft_must_hold_its_one_workflow_and_not_shadow_a_file() -> None:
    cfg = load_config(Path("airlie.yaml"))
    with pytest.raises(WorkflowError, match="story"):
        resolve_workflows(cfg, {"story": MINI.replace("  mini:", "  story:")}, "story")
    with pytest.raises(WorkflowError, match="mini"):
        resolve_workflows(cfg, {"mini": MINI + TINY.removeprefix("workflows:\n")}, "mini")
    with pytest.raises(WorkflowError, match="other"):
        resolve_workflows(cfg, {"other": MINI}, "other")
    with pytest.raises(WorkflowError, match="mini"):
        resolve_workflows(cfg, {"mini": "workflows: [1"}, "mini")


def test_a_snapshot_round_trips_and_names_its_nodes() -> None:
    cfg = load_config(Path("airlie.yaml"))
    specs = resolve_workflows(cfg, {"mini": MINI}, "mini")
    data = json.loads(json.dumps(snapshot("mini", specs)))
    name, back = from_snapshot(data)
    assert name == "mini" and back == specs
    assert not has_node(back["mini"], "planned") and not has_node(back["mini"], "publish")
    _, story = from_snapshot(snapshot("story", resolve_workflows(cfg, {}, "story")))
    assert has_node(story["story"], "planned") and has_node(story["story"], "publish")


def test_a_snapshot_that_no_longer_validates_is_a_workflow_error() -> None:
    with pytest.raises(WorkflowError, match="snapshot of 'mini' no longer validates"):
        from_snapshot({"name": "mini", "specs": {"mini": {"start": "a", "nodes": {}, "gone": 1}}})


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


async def test_make_pipeline_reads_the_boards_drafts(tmp_path: Path) -> None:
    from airlie.story.factory import make_pipeline, stories_dir
    from airlie.story.local_board import LocalBoard
    from tests.unit.test_story_factory import STORY_ROLES, cfg_for

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
