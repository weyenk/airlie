"""A block's evidence pack and class, from disk alone (planning-line P5a, D4), on the recorded blocks of the
stories that stopped on this Mac (tests/unit/data/blocks, made by scripts/block_fixture.py) and on small
synthetic ones."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airlie.story.diagnosis import Pack, gather, render_pack
from airlie.story.model import StoryError
from tests.unit.git_helpers import git

BLOCKS = Path(__file__).parent / "data" / "blocks"
KEYS = {
    "hold_trap": "AIR-17",
    "precode": "AIR-9",
    "changed_nothing": "HDS-5.old-1792251374",
    "engine_stall": "AIR-31",
    "product_max_steps": "HDS-5.old-1792251374",
    "fetch_failed": "AIR-1",
    "gate_red": "AIR-9",
}


def recorded(case: str) -> Pack:
    return gather(BLOCKS / case, KEYS[case])


def write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps({"ts": 1.0 + i, "run_id": "S", **e}) + "\n" for i, e in enumerate(events))
    )


def test_the_pack_of_a_hold_trap_counts_the_refused_calls_and_names_the_hold() -> None:
    pack = recorded("hold_trap")
    assert (pack.stage, pack.layer) == ("layers", "02")
    assert (pack.subtask, pack.role, pack.stop_reason) == ("build", "coder", "repeated_action")
    assert pack.refused_calls == 12
    assert pack.holds == ("tests",)  # the coder missed the deadline to write tests, and the reads were held
    assert pack.run_id == "20261020-210203-3cdf"
    assert pack.paths.run_dir is not None and pack.paths.run_dir.name == pack.run_id
    assert 0 < len(pack.paths.steps) <= 5
    assert all(p.name.startswith("step-") for p in pack.paths.steps)
    assert pack.minutes > 0 and pack.steps > 0 and pack.tokens > 0
    assert "refused calls" in render_pack(pack)


def test_the_pack_of_a_gate_block_carries_the_gate_tail_and_the_diff_stat(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "layer-01")
    (repo / "b.py").write_text("y = 2\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "layer")
    state = tmp_path / "state"
    gate = "\n".join(f"line {i}" for i in range(100)) + "\nFAILED tests/test_b.py::test_y - assert 1 == 2"
    write_events(
        state / "stories" / "AIR-1" / "events.jsonl",
        [
            {
                "kind": "layer_start",
                "layer": "01",
                "branch": "layer-01",
                "base_sha": base,
                "studio_run": "r1",
            },
            {"kind": "gate_tail", "layer": "01", "output": gate},
            {"kind": "blocked", "stage": "layers", "reason": "layer 01 (B): `gate` fails on layer-01"},
        ],
    )
    (state / "stories" / "AIR-1" / "state.json").write_text(json.dumps({"repo": str(repo)}))
    pack = gather(state, "AIR-1")
    assert pack.layer == "01" and pack.run_id == "r1"
    tail = pack.gate_tail.splitlines()
    assert len(tail) == 60 and tail[-1].startswith("FAILED tests/test_b.py::test_y")
    assert "b.py" in pack.diff_stat
    assert "FAILED tests/test_b.py::test_y" in render_pack(pack)
    assert Pack.from_dict(json.loads(json.dumps(pack.to_dict()))) == pack


def test_a_story_with_no_block_is_refused_and_a_missing_trace_is_an_empty_field(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_events(state / "stories" / "AIR-2" / "events.jsonl", [{"kind": "story_start"}])
    with pytest.raises(StoryError, match="no blocked event"):
        gather(state, "AIR-2")
    with pytest.raises(StoryError):
        gather(state, "AIR-404")
    write_events(
        state / "stories" / "AIR-3" / "events.jsonl",
        [
            {"kind": "run", "stage": "refine", "studio_run": "gone"},
            {"kind": "blocked", "stage": "refine", "reason": "x"},
        ],
    )
    pack = gather(state, "AIR-3")  # no state.json, no run directory, no trace: empty fields, no raise
    assert pack.run_id == "gone" and pack.paths.run_dir is None and pack.paths.steps == ()
    assert (pack.stop_reason, pack.gate_tail, pack.diff_stat, pack.refused_calls) == (None, "", "", 0)
    assert pack.tracebacks == () and pack.base_gate_failed is None


def test_the_nth_block_is_gathered_when_asked(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_events(
        state / "stories" / "AIR-4" / "events.jsonl",
        [
            {"kind": "error", "traceback": "Traceback\nValueError: first"},
            {"kind": "blocked", "stage": "layers", "reason": "first"},
            {"kind": "blocked", "stage": "layers", "reason": "second"},
        ],
    )
    assert gather(state, "AIR-4").reason == "second"
    first = gather(state, "AIR-4", n=1)
    assert (first.n, first.reason) == (1, "first")
    assert (
        first.tracebacks == ("Traceback\nValueError: first",) and gather(state, "AIR-4", n=2).tracebacks == ()
    )
    with pytest.raises(StoryError, match="no block 3"):
        gather(state, "AIR-4", n=3)
