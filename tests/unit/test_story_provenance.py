"""A layer's provenance, gathered from a synthetic story directory: the board's chain, the package's
contract, the checks, the cost and blocks, and the stack."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airlie.board import BoardStore
from airlie.story.board import Card
from airlie.story.model import Criterion, Layer, Story
from airlie.story.package import Interface, Package, PackageFile
from airlie.story.provenance import SHORT, Provenance, gather_provenance, layer_cost
from airlie.story.state import StoryState
from tests.unit.test_story_pipeline import STORY_ROLES
from tests.unit.test_studio_orchestrator import cfg_for

BASE = "a" * 40
SHA1 = "b" * 40
STORY = Story(
    "Add avatar upload",
    "Users upload an avatar and see it.",
    (Criterion("AC1", "a user", "they upload a PNG", "it is stored"),),
)
LAYERS = (
    Layer("storage", "Storage", "store.py", "Store avatars.", ("AC1",)),
    Layer("docs", "Docs", "README.md", "Document avatars."),
)
PACKAGE = Package(
    "storage",
    {"tests/unit/test_store.py": "def test_a():\n    assert 1\n"},
    (PackageFile("src/store.py", "create", "the store"),),
    (Interface("src/store.py", "def put() -> int", "returns 1"),),
    ("write put",),
    {"AC1": ("tests/unit/test_store.py::test_a",)},
    "the tests pass",
)
TEST_A = "tests/unit/test_store.py::test_a"


def write_events(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def state(tmp_path: Path, *, package: bool = True) -> StoryState:
    nn = "01"
    card = Card("AIR-9", "9", "AIR-9", "Avatars", "")
    st = StoryState(tmp_path / "stories" / "AIR-9", card.to_dict(), str(tmp_path))
    st.path.mkdir(parents=True)
    st.base = {"ref": "origin/main", "sha": BASE}
    finding = "minor store.py:3: name"
    review = {"id": "build", "role": "coder", "stop": "done", "review": "approve", "fix_rounds": 1}
    row: dict[str, Any] = {
        "branch": "AIR-9-x-01-storage",
        "sha": SHA1,
        "run_id": "run-build",
        "subtasks": [{**review, "findings": [finding]}],
        "gate": "uv run pytest -q",
    }
    if package:
        row |= {"package": PACKAGE.to_dict(), "tests": list(PACKAGE.test_ids)}
    st.built[nn] = row
    st.runs = ["run-pkg", "run-build"]
    write_events(
        st.path / "events.jsonl",
        [
            {"ts": 0, "kind": "story_start", "build": "abc1234"},
            {"ts": 10, "kind": "package_start", "layer": nn, "attempt": 1},
            {"ts": 11, "kind": "assertion_free_test", "layer": nn, "test": TEST_A},
            {
                "ts": 12,
                "kind": "package_returned",
                "layer": nn,
                "kinds": ["assertion_free_test"],
                "studio_run": "run-pkg",
            },
            {"ts": 13, "kind": "package_start", "layer": nn, "attempt": 2},
            {"ts": 20, "kind": "package_ready", "layer": nn, "attempt": 2, "studio_run": "run-pkg"},
            {"ts": 100, "kind": "layer_start", "layer": nn, "studio_run": "run-build", "attempt": 1},
            {"ts": 220, "kind": "layer_built", "layer": nn},
            {"ts": 221, "kind": "supervisor_triage", "layer": nn},
        ],
    )
    steps = [
        {"kind": "step", "prompt_tokens": 1000, "completion_tokens": 50},
        {"kind": "step", "prompt_tokens": 2000, "completion_tokens": 70},
        {"kind": "tool"},
    ]
    write_events(st.path / "runs" / "run-build" / "events.jsonl", steps)
    write_events(
        st.path / "runs" / "run-pkg" / "events.jsonl",
        [{"kind": "step", "prompt_tokens": 500, "completion_tokens": 5}],
    )
    return st


def board(tmp_path: Path) -> tuple[BoardStore, str]:
    s = BoardStore(tmp_path / "board.sqlite")
    init = s.create(level="initiative", title="Profiles")
    epic = s.create(level="epic", title="Avatars", parent=init.key)
    story = s.create(level="story", title="Add avatar upload", parent=epic.key)
    return s, story.key


def gather(
    tmp_path: Path, st: StoryState, *, store: BoardStore | None = None, key: str = "AIR-9"
) -> Provenance:
    card = Card(key, key, key, "Avatars", "")
    return gather_provenance(
        st,
        card,
        STORY,
        LAYERS,
        0,
        prs=[(11, "u11"), (12, "u12")],
        cfg=cfg_for(tmp_path, roles=STORY_ROLES),
        store=store,
    )


def test_the_chain_reads_the_initiative_the_epic_and_its_criteria_from_the_board(tmp_path: Path) -> None:
    st = state(tmp_path)
    store, key = board(tmp_path)
    p = gather(tmp_path, st, store=store, key=key)
    assert p.chain.initiative == ("AIR-1", "Profiles")
    assert p.chain.epic == ("AIR-2", "Avatars", "")
    assert p.chain.story == (key, "Add avatar upload", "Users upload an avatar and see it.")
    assert p.chain.layer == ("01/02", "Storage")
    assert p.chain.epic_criteria == ()  # P3 and P4 define them; none exist yet


def test_a_story_without_an_epic_has_no_chain_above_it(tmp_path: Path) -> None:
    st = state(tmp_path)
    s = BoardStore(tmp_path / "b.sqlite")
    s.create(level="initiative", title="x")
    s.create(level="epic", title="y", parent="AIR-1")
    loose = s.create(level="story", title="z").key  # a story under no epic
    for store, key in ((None, "AIR-9"), (s, "AIR-77"), (s, loose)):
        p = gather(tmp_path, st, store=store, key=key)
        assert p.chain.initiative is None and p.chain.epic is None


def test_the_contract_names_the_packages_tests_under_their_criteria_with_the_red_and_green_shas(
    tmp_path: Path,
) -> None:
    p = gather(tmp_path, state(tmp_path))
    c = p.contract
    assert c.tests == (TEST_A,)
    assert dict(c.by_criterion) == {"AC1": (TEST_A,)}
    assert c.red_at == BASE[:SHORT] and c.green_at == SHA1[:SHORT]
    assert c.author_role == "architect" and c.author_model == "base"
    assert "AC1" in c.criteria and p.package == PACKAGE


def test_the_checks_carry_the_red_check_the_package_findings_the_gate_and_the_review(tmp_path: Path) -> None:
    p = gather(tmp_path, state(tmp_path))
    k = p.checks
    assert k.red_check is not None and BASE[:SHORT] in k.red_check
    assert k.package_findings == (f"assertion_free_test: {TEST_A}",)
    assert k.package_attempts == 2 and k.gate == "uv run pytest -q" and k.review == "approve"
    assert k.review_findings == ("minor store.py:3: name",) and k.supervisor_interventions == 1
    assert p.build_commit == "abc1234" and p.run_ids == ("run-pkg", "run-build")
    assert [b.model for b in p.builders] == ["base"]


def diagnosis(layer: str, cls: str, kind: str, sig: str) -> str:
    """A diagnosis file in P5a's shape: pack, classified, muse, next."""
    classified = {"class": cls, "kind": kind, "where": "w", "signature": sig, "evidence": [], "rule": "r"}
    return json.dumps({"pack": {"layer": layer, "n": 1}, "classified": classified, "muse": None, "next": ""})


def test_a_resumed_layer_lists_its_blocks_with_their_classes(tmp_path: Path) -> None:
    st = state(tmp_path)
    d = st.path / "diagnosis"
    d.mkdir()
    (d / "1.json").write_text(diagnosis("01", "process", "hold_trap", "sig-a"))
    (d / "2.json").write_text(diagnosis("01", "config", "max_steps", "sig-b"))
    (d / "3.json").write_text(diagnosis("01", "model", "gate_red", "sig-c"))
    (d / "4.json").write_text(diagnosis("02", "process", "other", "sig-d"))
    (d / "5.json").write_text("not json")
    (d / "6.json").write_text("[1]")
    with (st.path / "events.jsonl").open("a", encoding="utf-8") as f:
        for row in (
            {"ts": 300, "kind": "fix_filed", "story": "AIR-9", "fix": "AIR-12", "signature": "sig-a"},
            {"ts": 301, "kind": "fix_linked", "story": "AIR-9", "fix": "AIR-13", "signature": "sig-b"},
            {"ts": 400, "kind": "resumed_after_fix", "fix": "AIR-12"},
        ):
            f.write(json.dumps(row) + "\n")
    cost = layer_cost(st, "01")
    assert cost.blocks == (
        ("process", "hold_trap", "AIR-12"),
        ("config", "max_steps", "AIR-13 (not yet released)"),
        ("model", "gate_red", "a person"),
    )
    assert cost.minutes == 2.0 and cost.tokens_in == 3500 and cost.tokens_out == 125 and cost.attempts == 1


def test_a_dependent_storys_stack_base_is_the_story_it_stacks_on(tmp_path: Path) -> None:
    st = state(tmp_path)
    assert gather(tmp_path, st).stack.base == ("trunk", "main")
    st.base = {**(st.base or {}), "story": "AIR-39"}
    s = gather(tmp_path, st).stack
    assert s.base == ("story", "AIR-39") and s.position == 1 and s.total == 2
    assert s.prs == ((11, "u11", "Storage"), (12, "u12", "Docs"))


def test_a_state_from_before_the_package_renders_a_contract_from_the_tests_field_or_none(
    tmp_path: Path,
) -> None:
    st = state(tmp_path, package=False)
    assert gather(tmp_path, st).package is None
    st.built["01"]["tests"] = ["tests/a.py::t"]
    p = gather(tmp_path, st)
    assert p.contract.tests == ("tests/a.py::t",)
    assert dict(p.contract.by_criterion) == {"": ("tests/a.py::t",)}
    bare = state(tmp_path / "bare", package=False)
    (bare.path / "events.jsonl").unlink()
    q = gather(tmp_path, bare)
    assert q.contract.tests == () and dict(q.contract.by_criterion) == {} and q.checks.red_check is None
    assert q.cost.attempts == 1 and q.cost.minutes == 0.0 and q.build_commit is None


def test_a_package_that_does_not_read_back_is_left_out_not_raised(tmp_path: Path) -> None:
    st = state(tmp_path)
    st.built["01"]["package"] = {"layer": "x"}
    p = gather(tmp_path, st)
    assert p.package is None and p.contract.tests == (TEST_A,)
