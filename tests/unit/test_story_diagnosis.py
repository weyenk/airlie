"""A block's evidence pack and class, from disk alone (planning-line P5a, D4), on the recorded blocks of the
stories that stopped on this Mac (tests/unit/data/blocks, made by scripts/block_fixture.py) and on small
synthetic ones."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airlie.story.diagnosis import Pack, Paths, classify, gather, render_pack
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


# ---- the class ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "class_", "kind"),
    [
        ("hold_trap", "process", "hold_trap"),
        ("precode", "content", "precode_failed"),
        ("changed_nothing", "content", "changed_nothing"),
        ("engine_stall", "transient", "engine_error"),
        ("product_max_steps", "config", "max_steps"),
        ("fetch_failed", "transient", "fetch_failed"),
        ("gate_red", "content", "gate_failed"),
    ],
)
def test_each_fixture_classes_as_expected(case: str, class_: str, kind: str) -> None:
    got = classify(recorded(case))
    assert (got.class_, got.kind) == (class_, kind)
    assert got.signature == f"{class_}:{kind}:{got.where}" and got.evidence and got.rule


def test_the_where_of_the_recorded_signatures_is_a_hold_a_role_or_the_stage() -> None:
    assert classify(recorded("hold_trap")).where == "write_deadline:tests"
    assert classify(recorded("product_max_steps")).where == "product"
    assert classify(recorded("changed_nothing")).where == "layers"


def traceback(line: int, *, exception: str = "KeyError: 'x'") -> str:
    return (
        "Traceback (most recent call last):\n"
        f'  File "/r/src/airlie/story/pipeline.py", line {line}, in _build_once\n    x = 1\n'
        '  File "/r/src/airlie/story/state.py", line 9, in save\n    y = 2\n'
        f"{exception}"
    )


def blocked_pack(**fields: Any) -> Pack:
    base: dict[str, Any] = {
        "key": "AIR-1", "n": 1, "reason": "layer 01 (B) changed nothing", "stage": "layers", "layer": "01",
        "attempt": 1, "run_id": "r", "subtask": "build", "role": "coder", "stop_reason": "done",
        "refused_calls": 0, "holds": (), "repeats": 0, "continuations": 0, "steps": 5, "tokens": 100,
        "minutes": 1.0, "gate_tail": "", "tracebacks": (), "failures": (), "base_gate_failed": None,
        "base_failing": (), "diff_stat": "", "paths": Paths(Path("e"), None, None, ()),
    }  # fmt: skip
    return Pack(**{**base, **fields})


def test_a_traceback_in_airlie_outranks_muses_category() -> None:
    pack = blocked_pack(tracebacks=(traceback(120),), stop_reason="done")
    got = classify(pack, muse_category="not_this_roles_test")
    assert got.class_ == "process" and got.kind == "KeyError"
    assert got.where == "airlie.story.state:save"  # the innermost airlie frame
    assert got.rule == "traceback_in_airlie"


def test_a_signature_does_not_change_when_lines_move() -> None:
    a = classify(blocked_pack(tracebacks=(traceback(120),)))
    b = classify(blocked_pack(tracebacks=(traceback(187, exception="KeyError: 'y'"),)))
    assert a.signature == b.signature == "process:KeyError:airlie.story.state:save"
    other = classify(blocked_pack(tracebacks=(traceback(1, exception="ValueError: z"),)))
    assert other.signature != a.signature


def test_an_unclassifiable_block_is_content_with_the_stage_as_where() -> None:
    got = classify(blocked_pack(reason="something nobody has seen", stage="publish", layer=None))
    assert (got.class_, got.kind, got.where) == ("content", "blocked", "publish")
    assert got.signature == "content:blocked:publish"


def test_a_planning_role_out_of_steps_is_config_but_a_coder_out_of_steps_is_content() -> None:
    planning = blocked_pack(stage="refine", layer=None, role="architect", stop_reason="max_steps")
    assert classify(planning).signature == "config:max_steps:architect"
    coder = blocked_pack(stop_reason="max_steps", reason="layer 01 (B): `gate` fails on b")
    assert classify(coder).class_ == "content"


def test_a_gate_that_was_red_on_the_base_is_the_environments() -> None:
    pack = blocked_pack(
        reason="layer 01 (B): `gate` fails on b",
        base_gate_failed=True,
        base_failing=("tests/test_a.py::test_a",),
    )
    got = classify(pack)
    assert (got.class_, got.kind, got.where) == ("environment", "gate_on_base", "tests/test_a.py::test_a")


def test_muses_category_names_a_content_kind_and_the_failing_test_is_the_where() -> None:
    pack = blocked_pack(
        reason="layer 01 (B): `gate` fails on b",
        gate_tail="FAILED tests/unit/test_x.py::test_y - assert 0",
    )
    got = classify(pack, muse_category="wrong_acceptance_test")
    assert got.signature == "content:wrong_acceptance_test:tests/unit/test_x.py::test_y"
    assert classify(pack).kind == "gate_failed"
