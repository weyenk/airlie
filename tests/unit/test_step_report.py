"""The step report: per conversation, what a story role spent before its first write (synthetic data)."""

import json
from pathlib import Path

from airlie.obs.step_report import Segment, render, story_segments
from tests.unit.git_helpers import git

STORY = "AIR-1"
RUN = "run-1"


def _call(name: str, **arguments: object) -> dict[str, object]:
    return {"u_t": repr({"name": name, "arguments": json.dumps(arguments)}), "o_t": "", "ok": True}


def _trace(state: Path, folder: str, calls: list[dict[str, object]]) -> None:
    d = state / "runs" / RUN / "trace" / folder
    d.mkdir(parents=True)
    for n, c in enumerate(calls, 1):
        (d / f"step-{n:03d}.json").write_text(json.dumps(c))


def _lines(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


def _fixture(tmp_path: Path, brief: str = "", extra: tuple[str, ...] = ()) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    for name in ("src/a.py", *extra):
        (repo / name).write_text("x = 1\n")
    git(repo, "init", "-q")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    sha = git(repo, "rev-parse", "HEAD")
    state = tmp_path / "state"
    story = state / "stories" / STORY
    story.mkdir(parents=True)
    layer = {"slug": "one", "title": "One", "scope": "", "brief": brief, "acceptance": []}
    (story / "state.json").write_text(json.dumps({"runs": [RUN, "missing-run"], "layers": [layer]}))
    _lines(
        story / "events.jsonl", [{"kind": "layer_start", "layer": "01", "studio_run": RUN, "base_sha": sha}]
    )
    _lines(
        state / "runs" / RUN / "events.jsonl",
        [
            {"kind": "subtask_start", "subtask": "tests", "role": "tester"},
            {"kind": "subtask_start", "subtask": "build", "role": "coder"},
        ],
    )
    _trace(
        state,
        "tests",
        [
            _call("read_file", path="src/a.py"),
            _call("read_file", path="src/a.py", offset=400),
            _call("read_file", path="src/a.py"),
            _call("write_file", path="t.py", content=""),
        ],
    )
    _trace(state, "build", [_call("edit_file", path="x.py")])
    return state, repo


def _by_seg(segments: list[Segment]) -> dict[str, Segment]:
    return {s.seg: s for s in segments}


def test_segment_counts_reads_pages_and_repeats_before_the_first_write(tmp_path: Path) -> None:
    state, repo = _fixture(tmp_path)
    seg = _by_seg(story_segments(state, STORY, repo, {"tester": 4}))["tests"]
    assert (seg.role, seg.layer, seg.steps) == ("tester", "01", 4)
    assert seg.first_write == 4
    assert seg.reads_before_write == 3
    assert seg.pages == 1
    assert seg.repeats == 1
    assert seg.max_steps_hit


def test_brief_file_reads_count_named_files_present_at_the_base(tmp_path: Path) -> None:
    state, repo = _fixture(tmp_path, brief="Change src/a.py and add src/new.py.")
    _trace(
        state,
        "more",
        [
            _call("read_file", path="src/a.py"),
            _call("read_file", path="src/new.py"),
            _call("edit_file", path="q.py"),
        ],
    )
    _lines(
        state / "runs" / RUN / "events.jsonl",
        [{"kind": "subtask_start", "subtask": "more", "role": "tester"}],
    )
    seg = _by_seg(story_segments(state, STORY, repo, {}))["more"]
    assert seg.brief_file_reads == 1
    assert not seg.max_steps_hit


def test_a_role_that_writes_first_has_no_reads_before_it(tmp_path: Path) -> None:
    state, repo = _fixture(tmp_path)
    seg = _by_seg(story_segments(state, STORY, repo, {}))["build"]
    assert seg.first_write == 1
    assert seg.reads_before_write == 0


def test_render_has_one_row_per_role(tmp_path: Path) -> None:
    state, repo = _fixture(tmp_path)
    text = render(story_segments(state, STORY, repo, {}))
    assert "| tester |" in text
    assert "| coder |" in text


def test_a_multi_path_read_counts_each_path(tmp_path: Path) -> None:
    """read_file(paths=[...]) reads each path once, from line 1: two batched reads of three different brief
    files are three brief-file reads and no repeat (a list, and a JSON-list string as some models send)."""
    brief = "Change src/a.py, src/b.py and src/c.py."
    state, repo = _fixture(tmp_path, brief=brief, extra=("src/b.py", "src/c.py"))
    _trace(
        state,
        "batched",
        [
            _call("read_file", paths=["src/a.py", "./src/b.py"]),
            _call("read_file", paths=json.dumps(["src/c.py"])),
            _call("write_file", path="t.py", content=""),
        ],
    )
    _lines(
        state / "runs" / RUN / "events.jsonl", [{"kind": "subtask_start", "subtask": "batched", "role": "t"}]
    )
    seg = _by_seg(story_segments(state, STORY, repo, {}))["batched"]
    assert (seg.brief_file_reads, seg.repeats, seg.pages, seg.reads_before_write) == (3, 0, 0, 2)


def test_a_multi_path_read_then_the_same_file_from_the_start_is_a_repeat(tmp_path: Path) -> None:
    state, repo = _fixture(tmp_path)
    _trace(
        state,
        "again",
        [_call("read_file", paths=["src/a.py"]), _call("read_file", path="src/a.py"), _call("edit_file")],
    )
    _lines(
        state / "runs" / RUN / "events.jsonl", [{"kind": "subtask_start", "subtask": "again", "role": "t"}]
    )
    seg = _by_seg(story_segments(state, STORY, repo, {}))["again"]
    assert (seg.repeats, seg.pages) == (1, 0)


def test_a_killed_run_s_partial_lines_and_step_files_are_skipped(tmp_path: Path) -> None:
    """A run killed mid-write leaves a cut last line in events.jsonl and a step file that does not parse:
    those are left out and the rest of the report is built."""
    state, repo = _fixture(tmp_path)
    events = state / "runs" / RUN / "events.jsonl"
    events.write_text(events.read_text() + '{"kind": "subtask_st')
    story_events = state / "stories" / STORY / "events.jsonl"
    story_events.write_text(story_events.read_text() + "{not json\n")
    (state / "runs" / RUN / "trace" / "tests" / "step-005.json").write_text('{"u_t": "{\'name')
    seg = _by_seg(story_segments(state, STORY, repo, {}))["tests"]
    assert (seg.role, seg.layer, seg.steps, seg.first_write) == ("tester", "01", 4, 4)
