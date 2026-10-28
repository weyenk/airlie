"""The ledger prices a story sketch (D6 of the specs phase, P4 of the planning line, 2026-10-25): published
stories' wall-clocks by size, medians with fallbacks that say which, a calibration from finished epics, and
the queue's hours."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from airlie.board import BoardStore
from airlie.story.costs import (
    Estimate,
    StoryCosts,
    StorySketch,
    queue_hours,
    size_of_story,
    story_wall_clock,
)

HOUR = 3600.0


def card_with_size(size: str) -> dict[str, Any]:
    block = {"type": "story_card", "size": size}
    return {"description": "x\n\n```json\n" + json.dumps(block) + "\n```\n"}


def story(
    root: Path,
    key: str,
    *,
    runs: list[tuple[float, float]] | None = None,
    stage: str = "review",
    layers: int = 0,
    size: str | None = None,
) -> Path:
    """stories/<key>/state.json and events.jsonl: each run a story_start at its first time and an event at
    its last."""
    d = root / "stories" / key
    d.mkdir(parents=True)
    state: dict[str, Any] = {"stage": stage, "built": {f"l{n}": {} for n in range(layers)}}
    if size:
        state["card"] = card_with_size(size)
    (d / "state.json").write_text(json.dumps(state))
    lines = []
    for first, last in runs or []:
        lines.append({"ts": first * HOUR, "kind": "story_start"})
        lines.append({"ts": last * HOUR, "kind": "stage"})
    (d / "events.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return d / "events.jsonl"


def costs(root: Path, *, min_samples: int = 3) -> StoryCosts:
    return StoryCosts(
        root,
        min_samples=min_samples,
        default_hours={"S": 1.0, "M": 5.0, "L": 8.0},
        size_scale={"S": 0.6, "M": 1.0, "L": 1.8},
        now=lambda: 1_700_000_000.0,
    )


def test_a_published_storys_wall_clock_sums_its_runs_and_an_unpublished_one_counts_for_nothing(
    tmp_path: Path,
) -> None:
    two_runs = story(tmp_path, "AIR-1", runs=[(0, 1.5), (10, 11)], layers=2)
    assert story_wall_clock(two_runs) == pytest.approx(2.5)  # the gap between the runs is nobody's work
    assert story_wall_clock(story(tmp_path, "AIR-2", runs=[(0, 3)], stage="build")) is None
    assert story_wall_clock(tmp_path / "stories" / "AIR-9" / "events.jsonl") is None  # no log at all
    torn = story(tmp_path, "AIR-3", runs=[(0, 2)])
    torn.write_text(torn.read_text() + '{"ts": 1\n')  # a torn last line is skipped
    assert story_wall_clock(torn) == pytest.approx(2.0)


def test_a_storys_size_is_its_cards_or_its_layer_count(tmp_path: Path) -> None:
    story(tmp_path, "AIR-1", layers=1)
    story(tmp_path, "AIR-2", layers=3)
    story(tmp_path, "AIR-3", layers=5)
    story(tmp_path, "AIR-4", layers=5, size="S")  # the card's size wins over the layer count
    story(tmp_path, "AIR-5")  # nothing built, no card size: left out
    assert [size_of_story(tmp_path, f"AIR-{n}") for n in range(1, 7)] == ["S", "M", "L", "S", None, None]


def test_the_median_by_size_falls_back_to_the_scaled_overall_median_then_to_the_defaults_and_says_which(
    tmp_path: Path,
) -> None:
    assert costs(tmp_path).median_hours("M") == (5.0, "default")  # an empty ledger
    for n, hours in enumerate((1.0, 2.0, 3.0), 1):
        story(tmp_path, f"AIR-{n}", runs=[(0, hours)], layers=1)  # three S stories
    story(tmp_path, "AIR-4", runs=[(0, 10.0)], layers=3)  # one M story: too few of its own
    c = costs(tmp_path)
    assert c.samples() == {"S": [1.0, 2.0, 3.0], "M": [10.0], "L": []}
    assert c.median_hours("S") == (2.0, "median of 3 S stories")
    hours, how = c.median_hours("L")  # the overall median (2.5) x the L scale (1.8)
    assert hours == pytest.approx(4.5) and how == "overall median x 1.8"
    assert costs(tmp_path, min_samples=1).median_hours("M") == (10.0, "median of 1 M stories")


def test_an_estimate_is_the_calibrated_sum_with_its_arithmetic_in_the_basis(tmp_path: Path) -> None:
    for n, hours in enumerate((1.0, 1.0, 1.2), 1):
        story(tmp_path, f"AIR-{n}", runs=[(0, hours)], layers=1)
    est = costs(tmp_path).estimate(
        [StorySketch("a", "S", "x"), StorySketch("b", "S", "y"), StorySketch("c", "M", "z")]
    )
    # 2 S at the median 1.0 h, 1 M at the overall median (1.0) x 1.0: 3.0 h, uncalibrated
    assert est.hours == pytest.approx(3.0)
    assert est.basis == (
        "2 S at 1.0 h (median of 3), 1 M at 1.0 h (overall median of 3 x 1), x1.0 calibration (uncalibrated)"
    )
    assert est.at == "2023-11-14T22:13:20Z" and est.revised is None and est.actual is None
    empty = costs(tmp_path).estimate([])
    assert empty.hours == 0.0 and empty.basis == "no stories sketched"
    assert Estimate.from_dict(est.to_dict()) == est
    closed = Estimate(3.0, "b", "t", revised=4.0, actual=5.5)
    assert Estimate.from_dict(closed.to_dict()) == closed and closed.latest == 4.0


def write_epic_estimate(root: Path, key: str, hours: float, actual: float | None) -> None:
    d = root / "epics" / key
    d.mkdir(parents=True)
    est = Estimate(hours, "b", "t", actual=actual).to_dict()
    (d / "state.json").write_text(json.dumps({"estimate": est}))


def test_calibration_is_one_until_three_epics_have_actuals(tmp_path: Path) -> None:
    write_epic_estimate(tmp_path, "AIR-1", 10.0, 15.0)
    write_epic_estimate(tmp_path, "AIR-2", 10.0, None)  # not finished
    write_epic_estimate(tmp_path, "AIR-3", 10.0, 15.0)
    assert costs(tmp_path).calibration() == (1.0, "uncalibrated")
    write_epic_estimate(tmp_path, "AIR-4", 20.0, 30.0)
    factor, how = costs(tmp_path).calibration()
    assert factor == pytest.approx(1.5) and how == "3 epics"
    est = costs(tmp_path).estimate([StorySketch("a", "M", "x")])
    assert est.hours == pytest.approx(7.5) and est.basis.endswith("x1.5 calibration (3 epics)")


def test_queue_hours_counts_the_undone_share_of_approved_and_doing_epics(tmp_path: Path) -> None:
    store = BoardStore(tmp_path / "board.sqlite")
    init = store.create(level="initiative", title="i", labels=["repo:o/r"])
    approved = store.create(level="epic", title="a", parent=init.key)
    doing = store.create(level="epic", title="b", parent=init.key)
    waiting = store.create(level="epic", title="c", parent=init.key)  # review: not in the queue yet
    store.update(approved.key, status="approved")
    store.update(doing.key, status="doing")
    store.update(waiting.key, status="review")
    for n, status in enumerate(("done", "done", "doing", "approved")):
        s = store.create(level="story", title=f"s{n}", parent=doing.key)
        store.update(s.key, status=status)  # type: ignore[arg-type]
    write_epic_estimate(tmp_path, approved.key, 6.0, None)  # no stories yet: counts whole
    write_epic_estimate(tmp_path, waiting.key, 99.0, None)
    d = tmp_path / "epics" / doing.key
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"estimate": Estimate(8.0, "b", "t", revised=10.0).to_dict()}))
    assert queue_hours(store, tmp_path) == pytest.approx(6.0 + 10.0 * 0.5)
    assert queue_hours(BoardStore(tmp_path / "empty.sqlite"), tmp_path) == 0.0
