"""The golden layer build (generic nodes Part 2, Task 1): a characterization of the studio's layer build as
the base commit's driver runs it, recorded before the studio is split into steps. It is green on the base
commit by design and every later task keeps it green: the same events, state, prompts and ledger rows, so
a refactor cannot change what a layer build does without this test saying so. The script (SCRIPT_LAYER)
drives the layer through every phase, and the first test pins that it still does: a refactor that skipped
a phase would otherwise pass by recording less."""

from __future__ import annotations

from pathlib import Path

from tests.support.layer_golden import (
    GOLDEN_LEDGER,
    GOLDEN_NUDGES,
    GOLDEN_STATE,
    GOLDEN_STORY_KINDS,
    GOLDEN_STORY_LAYER,
    GOLDEN_STUDIO,
    GOLDEN_STUDIO_KINDS,
    golden_layer_events,
    golden_prompts,
    golden_state,
)


async def test_the_golden_script_exercises_every_phase_of_a_layer_build(tmp_path: Path) -> None:
    story, studio, ledger = await golden_layer_events(tmp_path)
    seen = [e["kind"] for e in studio]

    def one(kind: str, **fields: object) -> dict[str, object]:
        [found] = [e for e in studio if e["kind"] == kind and all(e.get(k) == v for k, v in fields.items())]
        return found

    # the coder turn, then a done-check that found a miss and changed the tree
    assert seen.index("subtask_start") < seen.index("done_check") < seen.index("done_check_done")
    assert one("done_check_done")["changed"] is True
    # one review round with a blocking finding, a fix, and the approving round after it
    blocked = one("review", round=0)
    assert (blocked["verdict"], blocked["blocking"]) == ("request_changes", 1)
    assert one("fix_round")["round"] == 1
    assert one("review", round=1)["verdict"] == "approve"
    assert seen.index("review") < seen.index("fix_round") < seen.index("gate_start")
    # a red gate with advice, a follow-up, a green regate
    gate_starts = [e for e in studio if e["kind"] == "gate_start"]
    assert [e["n"] for e in gate_starts] == [1, 2]
    assert one("gate_failed")["follow_up"] == "build"
    assert seen.index("gate_failed") < seen.index("follow_up") < seen.index("gate_passed")
    assert one("gate_passed")["n"] == 2 and one("run_done")["tests"] is True
    assert [(r.trigger, r.outcome) for r in ledger] == [("gate_retry", "helped")]  # the advice
    assert "supervisor_triage" in [e["kind"] for e in story] and "layer_built" in [e["kind"] for e in story]
    assert one("steer", id="gate-failed")["role"] == "coder"
    assert "Put add back as it was." in golden_prompts(tmp_path)["build/round-3"]  # the follow-up carries it


async def test_a_layer_build_emits_the_recorded_events_state_and_ledger_rows(tmp_path: Path) -> None:
    story, studio, ledger = await golden_layer_events(tmp_path)
    assert [e["kind"] for e in story] == GOLDEN_STORY_KINDS
    assert [e["kind"] for e in studio] == GOLDEN_STUDIO_KINDS
    # the fields of the layer's own events, not only their order
    layer = [
        e for e in story if e["kind"] in {"layer_start", "package_placed", "supervisor_triage", "layer_built"}
    ]
    assert layer == GOLDEN_STORY_LAYER
    assert studio == GOLDEN_STUDIO
    found = [
        (r.trigger, r.subtask, r.role, r.kinds, r.category, r.owner, r.message, r.outcome, r.layer)
        for r in ledger
    ]
    assert found == GOLDEN_LEDGER
    assert golden_state(tmp_path) == GOLDEN_STATE  # built.01's keys, no open attempt, the runs named


async def test_a_layer_builds_continuation_nudges_are_the_recorded_texts(tmp_path: Path) -> None:
    await golden_layer_events(tmp_path)
    nudges = golden_prompts(tmp_path)
    # the brief, then the done-check, the fix round and the follow-up, each in its trace folder
    assert list(nudges) == ["build", "build/round-1", "build/round-2", "build/round-3"]
    assert nudges == GOLDEN_NUDGES
