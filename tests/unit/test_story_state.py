"""Story state survives a restart, refuses a different repository, and the lock admits one story at a time."""

import errno
import json
import os
from pathlib import Path

import pytest

from airlie.story.board import text_card
from airlie.story.model import StorySetupError
from airlie.story.state import StoryState, story_lock


def test_state_round_trips_and_tracks_stages(tmp_path: Path) -> None:
    card = text_card("Let users upload an avatar")
    st = StoryState.open(tmp_path / card.key, card, tmp_path / "repo")
    assert st.stage == "new" and not st.done("intake")
    st.built["01"] = {"branch": "b1", "sha": "abc"}
    st.finish("intake")
    again = StoryState.open(tmp_path / card.key, card, tmp_path / "repo")
    assert again.stage == "intake" and again.done("intake") and not again.done("refine")
    assert again.built == {"01": {"branch": "b1", "sha": "abc"}} and again.card["title"] == card.title
    kinds = [json.loads(x)["kind"] for x in (tmp_path / card.key / "events.jsonl").read_text().splitlines()]
    assert kinds == ["stage"]


def test_a_story_resumes_only_in_its_own_repository(tmp_path: Path) -> None:
    card = text_card("x")
    StoryState.open(tmp_path / card.key, card, tmp_path / "a")
    with pytest.raises(StorySetupError, match="started in") as e:
        StoryState.open(tmp_path / card.key, card, tmp_path / "b")
    # the file to delete, not the directory, which also holds the story's events, notes and PR bodies
    assert str(e.value).endswith(f"or delete {tmp_path / card.key / 'state.json'} to start over")


def test_the_lock_admits_one_story_at_a_time(tmp_path: Path) -> None:
    with story_lock(tmp_path) as first:
        assert first
        with story_lock(tmp_path) as second:
            assert not second
    with story_lock(tmp_path) as again:
        assert again


def test_save_syncs_the_new_file_before_the_rename_and_the_directory_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = text_card("Let users upload an avatar")
    st = StoryState.open(tmp_path / card.key, card, tmp_path / "repo")
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def replace(src: Path, dst: Path) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    st.save()
    assert calls == ["fsync", "replace", "fsync"]  # sync the file, rename, sync the directory


def test_a_crash_between_the_sync_and_the_rename_leaves_the_old_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = text_card("Let users upload an avatar")
    story_dir = tmp_path / card.key
    st = StoryState.open(story_dir, card, tmp_path / "repo")
    st.finish("intake")
    old = (story_dir / "state.json").read_bytes()
    st.stage = "refine"  # in memory only: the rename that would publish it never happens

    def crash(src: Path, dst: Path) -> None:
        raise OSError("crashed between the sync and the rename")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="crashed between"):
        st.save()
    assert (story_dir / "state.json").read_bytes() == old
    assert StoryState.open(story_dir, card, tmp_path / "repo").stage == "intake"


@pytest.mark.parametrize(
    "damaged",
    [
        b"",  # emptied by a crash
        b'{"card": {"key": "text-',  # cut short mid-write
        b"\x00" * 64,  # NUL-filled, as some filesystems leave a file after a crash
        b'{"stage": "\xff\xfe"}',  # bytes that are not UTF-8 text
        b"[]",  # valid JSON, but not an object
        b'{"stage": "new"}',  # an object missing its required fields
        b'{"card": {}, "repo": "r", "stage": "new", "unknown": 1}',  # a key this version does not know
        b'{"card": {}, "repo": "r", "stage": "deploy"}',  # a stage this version does not know
    ],
    ids=[
        "empty",
        "truncated",
        "nul-filled",
        "not-text",
        "list",
        "missing-field",
        "unknown-key",
        "unknown-stage",
    ],
)
def test_a_damaged_state_file_is_a_readable_stop_that_names_the_story(tmp_path: Path, damaged: bytes) -> None:
    card = text_card("x")
    story_dir = tmp_path / card.key
    StoryState.open(story_dir, card, tmp_path / "repo")
    (story_dir / "state.json").write_bytes(damaged)
    with pytest.raises(StorySetupError, match="is damaged") as e:
        StoryState.open(story_dir, card, tmp_path / "repo")
    assert str(e.value).startswith(f"{story_dir / 'state.json'} is damaged (")  # the file, not the directory
    assert str(e.value).endswith("; delete it to start the story over")


@pytest.mark.parametrize(
    ("how", "said"),
    [
        ("no read permission", "Permission denied"),
        ("no search permission on its directory", "Permission denied"),
        ("an I/O error", os.strerror(errno.EIO)),
        ("an error without a description", "OSError"),
    ],
)
def test_a_state_file_that_cannot_be_read_is_a_readable_stop_that_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str, said: str
) -> None:
    """Not a raw OSError, which would escape a tick at every beat: the tick sets the issue aside, as it does
    one whose state is damaged."""
    card = text_card("x")
    story_dir = tmp_path / card.key
    StoryState.open(story_dir, card, tmp_path / "repo")
    f = story_dir / "state.json"
    read_text = Path.read_text

    def fails(error: OSError) -> None:
        def read(self: Path, *args: object, **kwargs: object) -> str:
            if self == f:
                raise error
            return read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", read)

    if how == "no read permission":
        f.chmod(0)
    elif how == "no search permission on its directory":
        story_dir.chmod(0o600)
    elif how == "an I/O error":
        fails(OSError(errno.EIO, os.strerror(errno.EIO)))
    else:
        fails(OSError("disk on fire"))
    try:
        with pytest.raises(StorySetupError) as e:
            StoryState.open(story_dir, card, tmp_path / "repo")
    finally:
        story_dir.chmod(0o755)
        f.chmod(0o644)
    assert str(e.value) == f"{f} cannot be read ({said}); fix that, or delete it to start the story over"
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_a_torn_event_line_is_closed_before_the_next_event_on_resume(tmp_path: Path) -> None:
    card = text_card("x")
    story_dir = tmp_path / card.key
    StoryState.open(story_dir, card, tmp_path / "repo").finish("intake")
    fragment = '{"ts": 1.0, "run_id": "text-x", "kind": "sta'  # a write cut off before its newline
    with (story_dir / "events.jsonl").open("a") as f:
        f.write(fragment)
    StoryState.open(story_dir, card, tmp_path / "repo").finish("refine")
    lines = (story_dir / "events.jsonl").read_text().splitlines()
    assert lines[1] == fragment  # the fragment keeps a line of its own
    after = [json.loads(x) for x in lines[2:]]  # and every line after it is an event
    assert [(e["kind"], e["stage"]) for e in after] == [("stage", "refine")]


def test_a_torn_event_line_is_closed_when_a_story_starts_again_from_scratch(tmp_path: Path) -> None:
    card = text_card("x")
    story_dir = tmp_path / card.key
    StoryState.open(story_dir, card, tmp_path / "repo").finish("intake")
    (story_dir / "state.json").unlink()  # deleting the state to start over keeps the event log
    with (story_dir / "events.jsonl").open("a") as f:
        f.write('{"ts": 1.0, "run_id": "text-x", "kind": "sta')
    StoryState.open(story_dir, card, tmp_path / "repo").finish("intake")
    lines = (story_dir / "events.jsonl").read_text().splitlines()
    assert len(lines) == 3  # the first event, the fragment on its own line, the new event
    after = [json.loads(x) for x in lines[2:]]
    assert [(e["kind"], e["stage"]) for e in after] == [("stage", "intake")]


def test_an_error_in_the_contended_branch_is_not_chained_to_the_lock(tmp_path: Path) -> None:
    with story_lock(tmp_path) as first:
        assert first
        with pytest.raises(RuntimeError, match="caller failed") as e, story_lock(tmp_path) as second:
            assert not second
            raise RuntimeError("caller failed")
        assert e.value.__context__ is None


def test_finish_refuses_a_stage_that_is_not_one(tmp_path: Path) -> None:
    card = text_card("x")
    story_dir = tmp_path / card.key
    st = StoryState.open(story_dir, card, tmp_path / "repo")
    before = (story_dir / "state.json").read_bytes()
    with pytest.raises(ValueError, match="'deploy' is not a stage"):
        st.finish("deploy")
    assert st.stage == "new" and (story_dir / "state.json").read_bytes() == before
    assert (story_dir / "events.jsonl").read_bytes() == b""  # made when the story was opened; no event


def test_the_base_a_story_builds_on_is_kept_and_a_state_file_from_before_it_has_none(tmp_path: Path) -> None:
    card = text_card("Let users upload an avatar")
    where = tmp_path / card.key
    st = StoryState.open(where, card, tmp_path / "repo")
    assert st.base is None
    st.base = {"ref": "origin/main", "sha": "abc123"}
    st.save()
    assert StoryState.open(where, card, tmp_path / "repo").base == {"ref": "origin/main", "sha": "abc123"}
    old = json.loads((where / "state.json").read_text())
    del old["base"]  # a state file as an earlier airlie wrote it
    (where / "state.json").write_text(json.dumps(old))
    assert StoryState.open(where, card, tmp_path / "repo").base is None


@pytest.mark.parametrize(
    "how",
    ["mode 000", "mode 444, a torn last line", "mode 444, intact", "a directory"],
)
def test_an_event_log_that_cannot_be_read_and_appended_to_is_a_readable_stop_that_names_it(
    tmp_path: Path, how: str
) -> None:
    """Not a raw OSError at open, nor at the first event after the story started: a tick sets the issue
    aside, as it does one whose state cannot be read."""
    card = text_card("x")
    story_dir = tmp_path / card.key
    StoryState.open(story_dir, card, tmp_path / "repo").finish("intake")
    events = story_dir / "events.jsonl"
    if how == "a directory":
        events.unlink()
        events.mkdir()
    elif how == "mode 000":
        events.chmod(0)
    else:
        if "torn" in how:
            with events.open("a") as f:
                f.write('{"ts": 1.0, "kind": "sta')
        events.chmod(0o444)
    try:
        with pytest.raises(StorySetupError) as e:
            StoryState.open(story_dir, card, tmp_path / "repo")
    finally:
        if events.is_file():
            events.chmod(0o644)
    said = "Is a directory" if how == "a directory" else "Permission denied"
    assert str(e.value) == (
        f"{events} cannot be read and appended to ({said}); fix that, or move it away to start a new "
        "event log"
    )
    assert e.value.__cause__ is None and e.value.__suppress_context__ is False and e.value.__context__ is None


def test_an_event_log_left_without_its_state_is_checked_too(tmp_path: Path) -> None:
    card = text_card("x")
    story_dir = tmp_path / card.key
    StoryState.open(story_dir, card, tmp_path / "repo").finish("intake")
    (story_dir / "state.json").unlink()  # starting over keeps the event log
    (story_dir / "events.jsonl").chmod(0o444)
    try:
        with pytest.raises(StorySetupError, match="events.jsonl cannot be read and appended to"):
            StoryState.open(story_dir, card, tmp_path / "repo")
    finally:
        (story_dir / "events.jsonl").chmod(0o644)


def test_an_event_log_is_made_when_a_story_is_opened_and_an_intact_one_is_left_as_it_is(
    tmp_path: Path,
) -> None:
    card = text_card("x")
    story_dir = tmp_path / card.key
    st = StoryState.open(story_dir, card, tmp_path / "repo")
    assert (story_dir / "events.jsonl").read_bytes() == b""
    st.finish("intake")
    before = (story_dir / "events.jsonl").read_bytes()
    StoryState.open(story_dir, card, tmp_path / "repo")
    assert (story_dir / "events.jsonl").read_bytes() == before and before.endswith(b"\n")
