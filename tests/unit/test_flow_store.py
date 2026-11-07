import json

import pytest

from airlie.flow.store import MemoryStore, Sessions, StateStore
from airlie.story.state import StoryState
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_story_pipeline import text_card


def test_memory_store_treats_empty_values_as_absent():
    s = MemoryStore()
    for v in (None, {}, [], False, ""):
        s.put("x", v)
        assert not s.has("x")
    s.put("x", {"a": 1})
    assert s.has("x")


def test_memory_store_serves_loose_names_and_records_events():
    s = MemoryStore({"card": "c"})
    assert s.has("card") and s.get("card") == "c"
    s.emit("stage", stage="x")
    assert s.events == [("stage", {"stage": "x"})]


def test_state_store_reads_and_writes_story_fields_and_dotted_layers(tmp_path):
    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    s = StateStore(st, loose={"card": "c"})
    assert not s.has("story") and s.get("card") == "c"
    s.put("story", {"title": "t"})
    s.put("built.01", {"sha": "a"})
    s.save()
    again = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    assert again.story == {"title": "t"} and again.built["01"] == {"sha": "a"}
    assert s.has("built.01") and not s.has("built.02")
    with pytest.raises(KeyError):
        s.get("built.02")


@pytest.mark.parametrize("head", ["feedback", "package"])
def test_feedback_and_packages_live_beside_the_state_and_are_never_saved(tmp_path, head):
    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    s = StateStore(st, loose={})
    assert not s.has(f"{head}.01")
    s.put(f"{head}.01", "red")
    s.save()
    assert s.has(f"{head}.01") and s.get(f"{head}.01") == "red"
    assert head not in json.loads((tmp_path / "AIR-1" / "state.json").read_text())


def test_put_on_an_unknown_field_is_refused(tmp_path):
    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    with pytest.raises(KeyError):
        StateStore(st, loose={}).put("ghost", 1)
    with pytest.raises(KeyError):
        StateStore(st, loose={}).put("path", 1)


def test_put_on_a_loose_name_is_refused(tmp_path):
    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    card = dict(st.card)
    s = StateStore(st, loose={"card": "c", "repo": "r"})
    for name in ("card", "repo"):
        with pytest.raises(KeyError):
            s.put(name, "other")
    assert s.get("card") == "c" and st.card == card and st.repo == str(repo)


def test_sessions_hold_objects_in_memory_only():
    ss = Sessions()
    obj = object()
    ss.put("drafts", obj)
    assert ss.get("drafts") is obj
    assert ss.pop("drafts") is obj and ss.get("drafts") is None
    ss.put("a", 1)
    ss.clear()
    assert ss.get("a") is None
