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


def test_a_name_that_is_no_field_is_a_workflow_artifact_saved_with_the_state(tmp_path):
    """D15: a workflow's own output (`repro`) lives in `st.artifacts`; `<name>.<key>` in the dict there."""
    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    s = StateStore(st, loose={"card": "c"})
    assert not s.has("repro") and not s.has("found.01")
    with pytest.raises(KeyError):
        s.get("repro")
    s.put("repro", "steps to see it")
    s.put("found.01", {"file": "a.py"})
    assert s.has("repro") and s.get("repro") == "steps to see it"
    assert s.has("found.01") and s.get("found.01") == {"file": "a.py"} and not s.has("found.02")
    s.save()
    saved = json.loads((tmp_path / "AIR-1" / "state.json").read_text())
    assert saved["artifacts"] == {"repro": "steps to see it", "found": {"01": {"file": "a.py"}}}
    again = StateStore(StoryState.open(tmp_path / "AIR-1", text_card("x"), repo), loose={})
    assert again.get("repro") == "steps to see it" and again.has("found.01")


def test_a_dotted_name_under_a_plain_artifact_is_refused(tmp_path):
    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    s = StateStore(st, loose={})
    s.put("repro", "text")
    with pytest.raises(KeyError):
        s.put("repro.01", 1)
    assert not s.has("repro.01")


def test_put_on_a_reserved_or_memory_head_is_refused(tmp_path):
    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    for name in ("path", "path.x", "feedback", "package"):
        with pytest.raises(KeyError):
            StateStore(st, loose={}).put(name, 1)
    assert st.artifacts == {}


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


def test_undeclared_outputs_land_in_artifacts(tmp_path):
    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    s = StateStore(st, loose={})
    s.put("artifacts.repro", {"steps": 3})
    s.save()
    again = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    assert again.artifacts["repro"] == {"steps": 3} and again.workflow is None
