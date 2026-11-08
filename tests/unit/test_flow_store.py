import json

import pytest

from airlie.flow.store import BundleStore, MemoryStore, Sessions, StateStore
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
    names = ("path", "path.x", "feedback", "package", "artifacts", "workflow", "workflow.name", "nodes_done")
    for name in names:
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


def test_the_done_set_is_kept_in_memory_and_on_the_state(tmp_path):
    m = MemoryStore()
    m.mark_done("a")
    m.mark_done("a")
    m.mark_done("b@01")
    assert m.done("a") and m.done("b@01") and not m.done("b")
    m.clear_done("a")
    assert not m.done("a")
    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    s = StateStore(st, loose={})
    s.mark_done("mark")
    s.mark_done("mark")
    s.save()
    assert json.loads((tmp_path / "AIR-1" / "state.json").read_text())["nodes_done"] == ["mark"]
    s.clear_done("mark")
    assert not s.done("mark") and st.nodes_done == []


def test_put_none_clears_a_field_to_its_default_an_artifact_and_a_key(tmp_path):
    """What a loop re-run does to the outputs of the nodes it sends back."""
    repo = repo_with_marker_tests(tmp_path)
    st = StoryState.open(tmp_path / "AIR-1", text_card("x"), repo)
    s = StateStore(st, loose={"card": "c"})
    s.put("story", {"t": 1})
    s.put("built.01", {"sha": "a"})
    s.put("repro", "steps")
    s.put("found.01", {"f": 1})
    s.put("feedback.a", "red")
    for name in ("story", "built.01", "repro", "found.01", "feedback.a", "drafts", "nope"):
        s.put(name, None)
    assert st.story is None and st.built == {} and st.drafts == {}
    assert st.artifacts == {"found": {}} and not s.has("feedback.a")
    with pytest.raises(KeyError):
        s.get("repro")
    with pytest.raises(KeyError):
        s.put("card", None)
    m = MemoryStore({"x": 1})
    m.put("x", None)
    with pytest.raises(KeyError):
        m.get("x")


def test_a_bundle_store_reads_the_parent_and_writes_only_its_outputs_and_memory():
    parent = MemoryStore({"layers": [1], "card": "c"})
    b = BundleStore(parent, workflow="layer", outputs=["built.{item}"], item="02")
    assert b.get("layers") == [1] and b.has("card")
    b.put("built.02", {"sha": "s"})
    b.put("feedback.build.02", "red")
    b.mark_done("build@02")
    b.emit("node_started", node="build")
    assert parent.get("built.02") == {"sha": "s"} and parent.get("feedback.build.02") == "red"
    assert parent.done("build@02") and parent.events == [("node_started", {"node": "build"})]
    for name in ("built.01", "story", "drafted"):
        with pytest.raises(KeyError, match="bundle layer: '.+' is not one of its outputs"):
            b.put(name, 1)
