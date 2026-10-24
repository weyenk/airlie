"""The per-repository lesson store: a size-blocked layer's lesson is recorded to disk under the
observability state dir and survives a process restart, so the architect's brief (a later layer) can
carry the most relevant recent lessons. AC5: a lesson recorded for repository R in one process run is
still present when a new process opens the same state dir and plans a story on R — read from disk,
never from memory."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.story.board import NullBoard
from airlie.story.lessons import Lesson, LessonsStore
from airlie.story.pipeline import StoryPipeline
from airlie.story.publish import Publisher
from tests.unit.test_studio_orchestrator import cfg_for

# The AC1 fields, exactly: what a size-blocked layer's lesson records.
AC1_FIELDS = ("title", "acceptance", "source_files", "test_files", "steps", "context_tokens", "ending")


def make_lesson(title: str, ending: str = "out_of_steps", **overrides: object) -> Lesson:
    """A lesson with the AC1 fields; `ending` is one of the three size-block endings."""
    base: dict[str, object] = {
        "title": title,
        "acceptance": ["AC1: the avatar is stored", "AC2: the profile shows it"],
        "source_files": 12,
        "test_files": 3,
        "steps": 40,
        "context_tokens": 111_000,
        "ending": ending,
    }
    base.update(overrides)
    return Lesson(**base)  # type: ignore[arg-type]


def lessons_dir(state_dir: Path) -> list[Path]:
    """The lesson JSON files the store has written under state_dir/lessons."""
    d = state_dir / "lessons"
    return sorted(d.glob("*.json")) if d.is_dir() else []


def test_record_then_recent_returns_it(tmp_path: Path) -> None:
    """record(lesson) appends and persists; recent() returns it, with the AC1 fields intact."""
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    store = LessonsStore(state, repo)
    lesson = make_lesson("Layer 01 storage", ending="out_of_context")
    store.record(lesson)
    got = store.recent()
    assert len(got) == 1
    back = got[0]
    assert back.title == "Layer 01 storage"
    assert back.acceptance == ["AC1: the avatar is stored", "AC2: the profile shows it"]
    assert back.source_files == 12
    assert back.test_files == 3
    assert back.steps == 40
    assert back.context_tokens == 111_000
    assert back.ending == "out_of_context"


def test_a_recorded_lesson_survives_a_process_restart(tmp_path: Path) -> None:
    """AC5: a lesson recorded for repository R in one process run is still present when a new
    process opens the same state dir and plans a story on R. Two stores constructed independently
    over the same state_dir stand in for the two processes; the second must read the lesson from
    disk, not from memory."""
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    first = LessonsStore(state, repo)
    lesson = make_lesson("Layer 01 storage", ending="gate_red_at_step_end")
    first.record(lesson)
    # A new process: a fresh store over the same state dir, no shared memory with `first`.
    second = LessonsStore(state, repo)
    got = second.recent()
    assert [les.title for les in got] == ["Layer 01 storage"]
    assert got[0].ending == "gate_red_at_step_end"
    assert got[0].context_tokens == 111_000


def test_recent_caps_at_n_and_orders_newest_first(tmp_path: Path) -> None:
    """recent(n=5) returns the most recent n lessons for that repo, newest first."""
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    store = LessonsStore(state, repo)
    for i in range(1, 8):  # seven lessons, recorded oldest to newest
        store.record(make_lesson(f"layer {i}"))
    got = store.recent(5)
    assert [les.title for les in got] == ["layer 7", "layer 6", "layer 5", "layer 4", "layer 3"]
    assert len(store.recent()) == 5  # the default is n=5


def test_lessons_are_kept_per_repository(tmp_path: Path) -> None:
    """Lessons are keyed by the repository: a lesson for R does not appear when planning on another
    repository, and two repositories recorded in one process keep their lessons separate."""
    state = tmp_path / "state"
    r1 = tmp_path / "repo-one"
    r2 = tmp_path / "repo-two"
    s1 = LessonsStore(state, r1)
    s2 = LessonsStore(state, r2)
    s1.record(make_lesson("one"))
    s2.record(make_lesson("two"))
    assert [les.title for les in s1.recent()] == ["one"]
    assert [les.title for les in s2.recent()] == ["two"]


def test_the_on_disk_file_is_valid_json_with_the_ac1_field_names(tmp_path: Path) -> None:
    """The lesson is persisted as valid JSON under state_dir/lessons, with the AC1 field names and
    values, plus a timestamp for recency ordering."""
    state = tmp_path / "state"
    repo = tmp_path / "repo"
    store = LessonsStore(state, repo)
    lesson = make_lesson("Layer 01 storage", ending="out_of_steps")
    store.record(lesson)
    files = lessons_dir(state)
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    records = data if isinstance(data, list) else [data]
    assert len(records) == 1
    rec = records[0]
    for field in AC1_FIELDS:
        assert field in rec, f"the on-disk lesson is missing the AC1 field {field!r}"
    assert rec["title"] == "Layer 01 storage"
    assert rec["acceptance"] == ["AC1: the avatar is stored", "AC2: the profile shows it"]
    assert rec["source_files"] == 12
    assert rec["test_files"] == 3
    assert rec["steps"] == 40
    assert rec["context_tokens"] == 111_000
    assert rec["ending"] == "out_of_steps"
    assert any("time" in k for k in rec), "the on-disk lesson needs a timestamp for recency ordering"


def test_the_pipeline_carries_a_lessons_store_for_its_repository(tmp_path: Path) -> None:
    """The pipeline constructs self.lessons = LessonsStore(cfg.observability.state_dir, repo) beside
    self.stories_dir: the record site (a later layer) and the brief feeder share one store."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = cfg_for(tmp_path)
    p = StoryPipeline(
        cfg,
        board=NullBoard(cfg.observability.state_dir / "stories"),
        studio=lambda: None,  # type: ignore[return-value]
        publisher=Publisher(run=None),
        repo=repo,
    )
    assert isinstance(p.lessons, LessonsStore)
