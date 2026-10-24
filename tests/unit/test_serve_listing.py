"""The lists the web app reads: GET /v1/airlie/jobs, /runs, /stories and /repos."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from airlie.config import load_config
from airlie.serve.app import create_app
from airlie.serve.jobs import Jobs
from airlie.serve.listing import recent_runs, repos, stories, story_page
from tests.unit.test_serve import FakeUpstream
from tests.unit.test_serve import cfg as base_cfg


def cfg(tmp_path: Path, **story: Any):  # type: ignore[no-untyped-def]
    base_cfg(tmp_path)  # writes airlie.yaml and the role prompts
    data = yaml.safe_load((tmp_path / "airlie.yaml").read_text())
    data["story"] = {"repos_root": str(tmp_path / "git"), **story}
    (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(data))
    return load_config(tmp_path / "airlie.yaml")


def write_run(state: Path, run_id: str, lines: list[str] | None) -> None:
    d = state / "runs" / run_id
    d.mkdir(parents=True)
    if lines is not None:
        (d / "events.jsonl").write_text("".join(lines))


def event(kind: str, ts: float, **fields: Any) -> str:
    return json.dumps({"ts": ts, "run_id": "r", "kind": kind, **fields}) + "\n"


def test_recent_runs_reads_each_log_and_tolerates_a_torn_or_missing_one(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_run(
        state,
        "20261017-010000-aaaa",
        [
            event("run_start", 1.0, goal="fix the build", repo="/r", orchestrator="studio"),
            event("run_done", 9.0, ok=True, wall_ms=8000.0),
        ],
    )
    torn = event("run_start", 2.0, goal="add docs", repo="/r", mode="plan")
    write_run(
        state, "20261017-020000-bbbb", [torn, '{"kind": "step", "tool": "read_fi']
    )  # still being written
    write_run(state, "20261017-030000-cccc", None)  # no events.jsonl at all
    (state / "runs" / "not-a-run.txt").write_text("x")
    out = recent_runs(state)
    assert [r["id"] for r in out] == ["20261017-030000-cccc", "20261017-020000-bbbb", "20261017-010000-aaaa"]
    assert out[2] == {
        "id": "20261017-010000-aaaa",
        "goal": "fix the build",
        "repo": "/r",
        "kind": "run",
        "started": 1.0,
        "ok": True,
        "wall_ms": 8000.0,
    }
    assert out[1]["goal"] == "add docs" and out[1]["kind"] == "plan" and out[1]["ok"] is None
    assert out[0] == {
        "id": "20261017-030000-cccc",
        "goal": None,
        "repo": None,
        "kind": "run",
        "started": None,
        "ok": None,
        "wall_ms": None,
    }
    assert recent_runs(state, limit=1) == out[:1]
    assert recent_runs(tmp_path / "nowhere") == []


def test_stories_come_from_each_state_json(tmp_path: Path) -> None:
    state = tmp_path / "state"
    d = state / "stories" / "HDS-5"
    d.mkdir(parents=True)
    (d / "state.json").write_text(
        json.dumps(
            {
                "card": {"key": "HDS-5", "title": "Web search tools"},
                "repo": "/Users/n/git/airlie",
                "stage": "built",
                "blocked": "layer 01 failed its test gate",
            }
        )
    )
    (state / "stories" / "HDS-9").mkdir()  # started, no state yet
    (state / "stories" / "HDS-bad").mkdir()
    (state / "stories" / "HDS-bad" / "state.json").write_text("{not json")
    out = stories(state)
    for row in out:  # the mtimes are the clock's; test_last_activity_* pins them
        assert isinstance(row.pop("last_activity"), float)
    assert out == [
        {
            "key": "HDS-5",
            "title": "Web search tools",
            "repo": "/Users/n/git/airlie",
            "stage": "built",
            "blocked": "layer 01 failed its test gate",
        },
        {"key": "HDS-9", "title": None, "repo": None, "stage": None, "blocked": None},
        {"key": "HDS-bad", "title": None, "repo": None, "stage": None, "blocked": "state.json is damaged"},
    ]
    assert stories(tmp_path / "nowhere") == []


def keys(page: dict[str, Any]) -> list[str]:
    return [r["key"] for r in page["stories"]]


def story_row(key: str, stage: str | None, activity: float, **fields: Any) -> dict[str, Any]:
    return {
        "key": key,
        "title": None,
        "repo": None,
        "stage": stage,
        "blocked": None,
        "last_activity": activity,
        **fields,
    }


ROWS = [
    story_row("AIR-9", "refine", 30.0, title="Add the board", repo="a"),
    story_row("AIR-17", "build", 20.0, title="Fix login", repo="b", blocked="x"),
    story_row("AIR-2", "review", 10.0, title="Docs pass", repo="a"),
]


def test_story_page_sorts_filters_and_pages() -> None:
    assert keys(story_page(ROWS)) == ["AIR-9", "AIR-17", "AIR-2"]
    assert keys(story_page(ROWS, sort="key")) == ["AIR-2", "AIR-9", "AIR-17"]
    assert keys(story_page(ROWS, repo="a")) == ["AIR-9", "AIR-2"]
    assert keys(story_page(ROWS, blocked=True)) == ["AIR-17"]
    assert keys(story_page(ROWS, q="air-1")) == ["AIR-17"]
    p = story_page(ROWS, page=2, size=2)
    assert keys(p) == ["AIR-2"] and p["total"] == 3


def test_story_page_says_what_it_returned_and_counts_after_filtering() -> None:
    assert story_page(ROWS) == {"stories": ROWS, "total": 3, "page": 1, "size": None}
    p = story_page(ROWS, page=1, size=1, repo="a")
    assert keys(p) == ["AIR-9"] and (p["total"], p["page"], p["size"]) == (2, 1, 1)
    past_the_end = story_page(ROWS, page=3, size=2)
    assert past_the_end == {"stories": [], "total": 3, "page": 3, "size": 2}
    assert story_page([]) == {"stories": [], "total": 0, "page": 1, "size": None}
    assert story_page(ROWS, size=2, sort="key")["total"] == 3
    # without a size there is one page of everything, whatever page asks for
    assert keys(story_page(ROWS, page=5)) == ["AIR-9", "AIR-17", "AIR-2"]


def test_story_page_filters_combine_and_blocked_false_keeps_the_unblocked() -> None:
    assert keys(story_page(ROWS, blocked=False)) == ["AIR-9", "AIR-2"]
    assert keys(story_page(ROWS, stage="build")) == ["AIR-17"]
    assert keys(story_page(ROWS, stage="build", repo="a")) == []
    assert keys(story_page(ROWS, repo="a", blocked=False, q="docs")) == ["AIR-2"]
    assert keys(story_page(ROWS, repo="no such repo")) == []
    rows = [
        story_row("AIR-1", "new", 1.0, blocked=""),
        story_row("AIR-3", "new", 2.0, blocked="state.json is damaged"),
    ]
    assert keys(story_page(rows, blocked=True)) == ["AIR-3"]  # an empty reason is not a block
    assert keys(story_page(rows, blocked=False)) == ["AIR-1"]


def test_story_page_search_matches_key_or_title_ignoring_case_and_a_missing_title() -> None:
    assert keys(story_page(ROWS, q="LOGIN")) == ["AIR-17"]  # the title, any case
    assert keys(story_page(ROWS, q="Air-9")) == ["AIR-9"]  # the key, any case
    assert keys(story_page(ROWS, q="the b")) == ["AIR-9"]  # a substring of the title, not a word
    assert keys(story_page(ROWS, q="nothing like it")) == []
    assert keys(story_page(ROWS, q="")) == ["AIR-9", "AIR-17", "AIR-2"]
    untitled = [story_row("AIR-4", None, 5.0), story_row("AIR-5", None, 6.0, title="Four")]
    assert keys(story_page(untitled, q="air-4")) == ["AIR-4"]  # title None does not break the search
    assert keys(story_page(untitled, q="four")) == ["AIR-5"]
    odd = [story_row("AIR-6", None, 1.0, title=404)]  # a state.json whose title is not text
    assert keys(story_page(odd, q="air")) == ["AIR-6"] and keys(story_page(odd, q="nope")) == []


def test_story_page_key_sort_is_natural_across_prefixes() -> None:
    rows = [
        story_row(k, "new", 1.0) for k in ("AIR-17", "HDS-5", "AIR-9", "AIR-100", "hds-10", "AIR-2", "AIR")
    ]
    assert keys(story_page(rows, sort="key")) == [
        "AIR",
        "AIR-2",
        "AIR-9",
        "AIR-17",
        "AIR-100",
        "HDS-5",
        "hds-10",
    ]


def test_story_page_stage_sort_is_pipeline_order_none_first_then_newest() -> None:
    rows = [
        story_row("AIR-1", "review", 5.0),
        story_row("AIR-2", "build", 1.0),
        story_row("AIR-3", None, 2.0),
        story_row("AIR-4", "new", 3.0),
        story_row("AIR-5", "build", 9.0),
        story_row("AIR-6", "intake", 4.0),
        story_row("AIR-7", None, 8.0),
        story_row("AIR-8", "layers", 4.0),
        story_row("AIR-9", "publish", 4.0),
        story_row("AIR-10", "refine", 4.0),
        story_row("AIR-11", "built", 6.0),  # a stage this airlie does not know sorts last
    ]
    assert keys(story_page(rows, sort="stage")) == [
        "AIR-7",
        "AIR-3",  # no stage yet, newest first
        "AIR-4",
        "AIR-6",
        "AIR-10",
        "AIR-8",
        "AIR-5",
        "AIR-2",  # build: newest first
        "AIR-9",
        "AIR-1",
        "AIR-11",
    ]


def test_story_page_activity_ties_fall_back_to_the_key() -> None:
    rows = [story_row(k, "new", 7.0) for k in ("AIR-10", "AIR-9", "AIR-1")]
    assert keys(story_page(rows)) == ["AIR-1", "AIR-9", "AIR-10"]


def test_story_page_refuses_a_page_or_size_below_one() -> None:
    for kw in ({"page": 0}, {"page": -1}, {"size": 0}, {"size": -3}):
        with pytest.raises(ValueError, match="at least 1"):
            story_page(ROWS, **kw)


def story_dir(state: Path, key: str, **state_json: Any) -> Path:
    d = state / "stories" / key
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"card": {"key": key}, **state_json}))
    return d


def touch(path: Path, mtime: float) -> Path:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    os.utime(path, (mtime, mtime))
    return path


def activity(state: Path, key: str) -> float:
    return next(r["last_activity"] for r in stories(state) if r["key"] == key)


def test_last_activity_is_the_newest_of_the_story_and_its_last_run(tmp_path: Path) -> None:
    state = tmp_path / "state"
    d = story_dir(state, "AIR-1", runs=["r-early", "r-last"])
    touch(d / "state.json", 100)
    touch(d / "events.jsonl", 200)
    touch(state / "runs" / "r-early" / "events.jsonl", 999)  # not the last run: it is not read
    last_log = touch(state / "runs" / "r-last" / "events.jsonl", 300)
    assert activity(state, "AIR-1") == 300.0
    touch(last_log, 150)  # the story's own log is newer
    assert activity(state, "AIR-1") == 200.0
    touch(d / "state.json", 250)  # now state.json is
    assert activity(state, "AIR-1") == 250.0


def test_last_activity_skips_what_is_missing_and_is_zero_with_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    only_state = story_dir(state, "AIR-1")
    touch(only_state / "state.json", 40)
    assert activity(state, "AIR-1") == 40.0
    named_run_without_a_log = story_dir(state, "AIR-2", runs=["r-gone"])
    touch(named_run_without_a_log / "state.json", 50)
    (state / "runs" / "r-gone").mkdir(parents=True)  # the run directory is there, its log is not
    assert activity(state, "AIR-2") == 50.0
    damaged = state / "stories" / "AIR-3"
    damaged.mkdir()
    (damaged / "state.json").write_text("{not json")
    touch(damaged / "state.json", 10)
    touch(damaged / "events.jsonl", 60)
    assert activity(state, "AIR-3") == 60.0  # a damaged state names no runs, its files still count
    (state / "stories" / "AIR-4").mkdir()
    assert activity(state, "AIR-4") == 0.0  # nothing to date it by
    junk_runs = story_dir(state, "AIR-5", runs=[7, None])
    touch(junk_runs / "state.json", 70)
    assert activity(state, "AIR-5") == 70.0  # a run id that is not a string is not a run
    no_runs = story_dir(state, "AIR-6", runs="not a list")
    touch(no_runs / "state.json", 80)
    assert activity(state, "AIR-6") == 80.0
    empty_runs = story_dir(state, "AIR-7", runs=[])
    touch(empty_runs / "state.json", 90)
    assert activity(state, "AIR-7") == 90.0


def test_last_activity_ignores_a_run_id_that_cannot_name_a_run_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    touch(state / "events.jsonl", 999)  # what the id ".." would reach
    touch(state / "elsewhere" / "events.jsonl", 999)  # what "../elsewhere" would reach
    touch(state / "runs" / "events.jsonl", 999)  # what "" and "." would reach
    for i, run in enumerate(["a\0b", "..", ".", "", "../elsewhere", "a/b", "r r"]):
        d = story_dir(state, f"AIR-{i}", runs=["r-earlier", run])
        touch(d / "state.json", 50)
        assert activity(state, f"AIR-{i}") == 50.0, repr(run)  # no error, and the story's own file dates it


def test_the_stories_route_takes_the_options_and_rejects_an_unknown_stage(tmp_path: Path) -> None:
    state = tmp_path / "state"
    for key, stage, repo, blocked, title, mtime in (
        ("AIR-9", "refine", "/r/a", None, "Add the board", 30),
        ("AIR-17", "build", "/r/b", "gate failed", "Fix login", 20),
        ("AIR-2", "review", "/r/a", None, "Docs pass", 10),
    ):
        d = story_dir(state, key, card={"key": key, "title": title}, stage=stage, repo=repo, blocked=blocked)
        touch(d / "state.json", mtime)
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    with TestClient(app) as c:
        auth = {"Authorization": "Bearer k"}

        def get(query: str = "") -> Any:
            return c.get(f"/v1/airlie/stories{query}", headers=auth)

        assert c.get("/v1/airlie/stories?size=1").status_code == 401
        everything = get().json()
        assert [s["key"] for s in everything["stories"]] == ["AIR-9", "AIR-17", "AIR-2"]  # newest first
        assert (everything["total"], everything["page"], everything["size"]) == (3, 1, None)
        assert everything["stories"][1] == {
            "key": "AIR-17",
            "title": "Fix login",
            "repo": "/r/b",
            "stage": "build",
            "blocked": "gate failed",
            "last_activity": 20.0,
        }
        one = get("?size=1&sort=key").json()
        assert [s["key"] for s in one["stories"]] == ["AIR-2"] and one["total"] == 3 and one["size"] == 1
        second = get("?size=1&page=2&sort=stage").json()
        assert [s["key"] for s in second["stories"]] == [
            "AIR-17"
        ]  # refine, build, review: the second is build
        assert second["page"] == 2
        assert [s["key"] for s in get("?stage=build").json()["stories"]] == ["AIR-17"]
        assert [s["key"] for s in get("?repo=/r/a").json()["stories"]] == ["AIR-9", "AIR-2"]
        assert [s["key"] for s in get("?blocked=true").json()["stories"]] == ["AIR-17"]
        assert [s["key"] for s in get("?blocked=false").json()["stories"]] == ["AIR-9", "AIR-2"]
        assert [s["key"] for s in get("?q=LOGIN").json()["stories"]] == ["AIR-17"]
        assert get("?stage=nope").status_code == 422
        assert "nope" in get("?stage=nope").json()["detail"]
        for bad in ("size=0", "size=101", "page=0", "sort=newest", "blocked=maybe"):
            assert get(f"?{bad}").status_code == 422, bad
        assert get("?size=100").status_code == 200


def test_repos_are_story_repo_and_the_git_checkouts_under_repos_root(tmp_path: Path) -> None:
    root = tmp_path / "git"
    (root / "plain").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root / "airlie")], check=True)
    subprocess.run(["git", "init", "-q", str(root / "OpenHand")], check=True)
    (root / "a-file").write_text("x")
    assert repos(cfg(tmp_path)) == [str(root / "airlie"), str(root / "OpenHand")]  # by name, any case
    mine = tmp_path / "mine"
    subprocess.run(["git", "init", "-q", str(mine)], check=True)
    assert repos(cfg(tmp_path, repo=str(mine))) == [str(mine), str(root / "airlie"), str(root / "OpenHand")]
    assert repos(cfg(tmp_path, repo=str(root / "airlie"))) == [str(root / "airlie"), str(root / "OpenHand")]
    assert repos(cfg(tmp_path, repos_root=str(tmp_path / "nowhere"))) == []


async def test_jobs_list_includes_failed_state_and_error(tmp_path: Path) -> None:
    async def boom() -> dict[str, Any]:
        raise RuntimeError("engine fell over")

    async def hang() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    jobs = Jobs()
    assert jobs.list() == []
    first = jobs.start("plan", "p1", tmp_path / "e1", boom())
    first.started -= 10
    jobs.start("run", "r1", tmp_path / "e2", hang())
    await jobs.status("p1", wait=5)
    out = jobs.list()
    assert [j["id"] for j in out] == ["r1", "p1"]  # newest first
    assert out[0] == {"id": "r1", "kind": "run", "state": "running", "started": out[0]["started"]}
    assert out[1]["state"] == "failed" and out[1]["error"] == "RuntimeError: engine fell over"
    jobs.cancel("r1")


def test_the_listing_routes_answer_with_the_key(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write_run(state, "20261017-010000-aaaa", [event("run_start", 1.0, goal="g", repo="/r")])
    (state / "stories" / "HDS-1").mkdir(parents=True)
    (state / "stories" / "HDS-1" / "state.json").write_text(
        json.dumps({"card": {"key": "HDS-1"}, "stage": "new"})
    )
    subprocess.run(["git", "init", "-q", str(tmp_path / "git" / "airlie")], check=True)
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    with TestClient(app) as c:
        for path in ("jobs", "runs", "stories", "repos"):
            assert c.get(f"/v1/airlie/{path}").status_code == 401
        auth = {"Authorization": "Bearer k"}
        assert c.get("/v1/airlie/jobs", headers=auth).json() == {"jobs": []}
        runs = c.get("/v1/airlie/runs", headers=auth).json()["runs"]
        assert [r["id"] for r in runs] == ["20261017-010000-aaaa"] and runs[0]["goal"] == "g"
        assert c.get("/v1/airlie/stories", headers=auth).json()["stories"][0]["key"] == "HDS-1"
        assert c.get("/v1/airlie/repos", headers=auth).json() == {"repos": [str(tmp_path / "git" / "airlie")]}


def test_the_flow_routes_serve_a_story_and_its_jobs_and_404_for_what_is_not_there(tmp_path: Path) -> None:
    state = tmp_path / "state"
    d = state / "stories" / "HDS-1"
    d.mkdir(parents=True)
    (d / "state.json").write_text(
        json.dumps({"card": {"key": "HDS-1", "title": "T"}, "stage": "refine", "layers": None})
    )
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    with TestClient(app) as c:
        auth = {"Authorization": "Bearer k"}
        assert c.get("/v1/airlie/stories/HDS-1/flow").status_code == 401
        flow = c.get("/v1/airlie/stories/HDS-1/flow", headers=auth).json()
        assert flow["key"] == "HDS-1" and flow["nodes"][0]["id"] == "intake"
        job = c.get("/v1/airlie/stories/HDS-1/flow/refine/refine", headers=auth).json()
        assert job["job"]["id"] == "refine" and job["input"] is None
        assert c.get("/v1/airlie/stories/NOPE-1/flow", headers=auth).status_code == 404
        assert c.get("/v1/airlie/stories/HDS-1/flow/layer-09/build", headers=auth).status_code == 404
