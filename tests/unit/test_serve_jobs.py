"""Serve jobs: plan → edit → run the edited plan in the background, follow it, cancel it. So are stories."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.engine import FakeEngine, FakeReply
from airlie.obs import EventSink
from airlie.router import ChiefRouter
from airlie.serve.app import create_app
from airlie.serve.jobs import Jobs, read_events
from airlie.story import factory
from airlie.story.board import BoardError, text_card
from airlie.story.model import StorySetupError
from airlie.story.state import StoryState, story_lock
from airlie.topology.studio import StudioOrchestrator
from tests.unit.serve_helpers import TestClient
from tests.unit.test_fork_orchestrator import repo_with_marker_tests
from tests.unit.test_serve import FakeUpstream
from tests.unit.test_story_pipeline import STORY_ROLES
from tests.unit.test_studio_orchestrator import APPROVE, DONE, PLAN, RUN, WRITE_PASS, cfg_for


def finish(client: TestClient, job: str) -> dict[str, Any]:
    """Follow a job to its end with long polls; the last status, with every event."""
    body = client.get(f"/v1/airlie/jobs/{job}", params={"wait": 10}).json()
    events = list(body["events"])
    while body["state"] == "running":
        body = client.get(f"/v1/airlie/jobs/{job}", params={"since": body["next"], "wait": 10}).json()
        events += body["events"]
    return {**body, "events": events}


def app_for(tmp_path: Path, engine: FakeEngine, **kw: Any) -> Any:
    def factory(cfg, k, topology=None):  # type: ignore[no-untyped-def]
        assert topology == "studio"
        return StudioOrchestrator(cfg, engine, router=ChiefRouter())

    return create_app(
        cfg_for(tmp_path), transport=httpx.MockTransport(FakeUpstream()), orchestrator_factory=factory, **kw
    )


def test_a_plan_job_then_the_edited_plan_runs_as_a_job(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    engine = FakeEngine({"chief": [PLAN], "coder": [WRITE_PASS, RUN, DONE], "reviewer": [APPROVE]})
    with TestClient(app_for(tmp_path, engine)) as client:
        r = client.post("/v1/airlie/plans", json={"goal": "fix the build", "repo": str(repo)})
        assert r.status_code == 202 and r.json()["kind"] == "plan"
        planned = finish(client, r.json()["job_id"])
        assert planned["state"] == "done" and planned["result"]["type"] == "plan"
        assert planned["result"]["run_id"] == r.json()["job_id"] and not planned["result"]["failed"]
        plan = planned["result"]["plan"]
        plan["subtasks"] = plan["subtasks"][:1]  # the person drops the docs subtask
        body = {"goal": "fix the build", "repo": str(repo), "plan": plan, "test_cmd": "./check.sh"}
        r = client.post("/v1/airlie/runs", json={**body, "base": "HEAD", "synthesise": False})
        assert r.status_code == 202 and r.json()["kind"] == "run"  # a base that is a commit is accepted
        done = finish(client, r.json()["job_id"])
        assert client.delete(f"/v1/airlie/jobs/{r.json()['job_id']}").json()["cancelled"] is False  # it ended
    assert done["state"] == "done" and done["result"]["tests"] == {"studio": True}
    assert done["result"]["branch"].startswith("airlie/") and done["result"]["ok"] is True
    assert Path(done["result"]["report"]).is_file()
    routes = [e for e in done["events"] if e["kind"] == "route"]
    assert [(e["chosen"], e["decision"]) for e in routes] == [("coder", "pinned")]
    assert [q.role for q in engine.requests].count("chief") == 1  # the plan call only: no synthesis


def test_a_plan_job_whose_chief_answers_or_fails_says_so_in_its_result(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    body = {"goal": "what is 6 x 7?", "repo": str(repo)}
    answer = FakeReply(text=json.dumps({"type": "answer", "answer": "it is 42"}))
    with TestClient(app_for(tmp_path, FakeEngine({"chief": [answer]}))) as client:
        job = client.post("/v1/airlie/plans", json=body).json()["job_id"]
        done = finish(client, job)
    assert done["state"] == "done"
    assert done["result"] == {"run_id": job, "failed": False, "type": "answer", "answer": "it is 42"}
    with TestClient(app_for(tmp_path, FakeEngine({}))) as client:  # no chief script: the engine errors
        job = client.post("/v1/airlie/plans", json=body).json()["job_id"]
        done = finish(client, job)
    assert done["state"] == "done" and done["result"]["failed"] is True  # a finished job, a failed plan
    assert done["result"]["type"] == "answer" and "engine error" in done["result"]["answer"]


def test_a_plan_or_base_that_cannot_run_is_a_400_and_unknown_jobs_are_404(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    one = {"subtasks": [{"id": "b", "role": "coder", "brief": "write PASS"}]}
    runs = tmp_path / "state" / "runs"

    def left_behind() -> list[Path]:  # the orchestrator makes a run directory when it is built
        return list(runs.iterdir()) if runs.exists() else []

    with TestClient(app_for(tmp_path, FakeEngine({}))) as client:
        bad = {"subtasks": [{"id": "a", "role": "chief", "brief": "x"}]}
        r = client.post("/v1/airlie/runs", json={"goal": "g", "repo": str(repo), "plan": bad})
        assert r.status_code == 400 and r.json()["detail"].startswith("plan rejected: ")
        assert "role 'chief'" in r.json()["detail"]
        r = client.post("/v1/airlie/runs", json={"goal": "g", "repo": str(repo), "plan": {"subtasks": []}})
        assert r.status_code == 400 and "subtasks" in r.json()["detail"]
        edited = {**one, "assumptions": 5}  # a hand-edited field of the wrong type: a 400, not a 500
        r = client.post("/v1/airlie/runs", json={"goal": "g", "repo": str(repo), "plan": edited})
        assert r.status_code == 400 and r.json()["detail"].startswith("plan rejected: ")
        assert left_behind() == []  # a rejected plan makes no run directory
        for base in ("--orphan", "no-such-branch"):
            body = {"goal": "g", "repo": str(repo), "plan": one, "base": base}
            r = client.post("/v1/airlie/runs", json=body)
            assert r.status_code == 400 and "base" in r.json()["detail"]
        assert left_behind() == []  # nor does a rejected base
        r = client.post("/v1/airlie/plans", json={"goal": "g", "repo": str(tmp_path / "nowhere")})
        assert r.status_code == 400
        body = {"goal": "g", "repo": str(repo), "images": [str(tmp_path / "missing.png")]}
        r = client.post("/v1/airlie/plans", json=body)
        assert r.status_code == 400 and "image not found" in r.json()["detail"]
        assert client.get("/v1/airlie/jobs/ghost").status_code == 404
        assert client.delete("/v1/airlie/jobs/ghost").status_code == 404
    assert left_behind() == []


def test_a_negative_since_is_rejected_instead_of_reading_the_tail(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    with TestClient(app_for(tmp_path, FakeEngine({"chief": [PLAN]}))) as client:
        job = client.post("/v1/airlie/plans", json={"goal": "g", "repo": str(repo)}).json()["job_id"]
        done = finish(client, job)
        assert [e["kind"] for e in done["events"]] == ["run_start", "plan", "run_done"]
        assert client.get(f"/v1/airlie/jobs/{job}", params={"since": -1}).status_code == 422
        tail = client.get(f"/v1/airlie/jobs/{job}", params={"since": 2}).json()  # `since` counts events seen
    assert [e["kind"] for e in tail["events"]] == ["run_done"] and tail["next"] == 3


def test_a_long_poll_is_capped_at_five_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def status(self, job_id, *, since=0, wait=0.0):  # type: ignore[no-untyped-def]
        waits.append(wait)
        return {"job_id": job_id, "kind": "run", "state": "done", "events": [], "next": since}

    monkeypatch.setattr(Jobs, "status", status)
    with TestClient(app_for(tmp_path, FakeEngine({}))) as client:
        assert client.get("/v1/airlie/jobs/j1", params={"wait": 99999}).status_code == 200
        assert client.get("/v1/airlie/jobs/j1", params={"wait": 2.5}).status_code == 200
    assert waits == [300.0, 2.5]


def test_a_running_job_can_be_cancelled(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)

    class Hang(FakeEngine):
        async def generate(self, req, on_token=None):  # type: ignore[no-untyped-def]
            await asyncio.sleep(3600)

    one = {"subtasks": [{"id": "b", "role": "coder", "brief": "write PASS"}]}
    with TestClient(app_for(tmp_path, Hang({}))) as client:
        body = {"goal": "g", "repo": str(repo), "plan": one}
        job = client.post("/v1/airlie/runs", json=body).json()["job_id"]
        assert client.get(f"/v1/airlie/jobs/{job}").json()["state"] == "running"
        assert client.delete(f"/v1/airlie/jobs/{job}").json() == {"job_id": job, "cancelled": True}
        body = client.get(f"/v1/airlie/jobs/{job}", params={"since": 10_000, "wait": 5}).json()
    assert body["state"] == "cancelled"


# ---- the registry itself ----


def test_read_events_follows_the_log_and_stops_at_a_line_still_being_written(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    assert read_events(log, 0) == ([], 0)  # the run has not written anything yet
    log.write_text('{"n": 1}\n{"n": 2}\n{"n": 3')  # the writer is part-way through the third line
    assert read_events(log, 0) == ([{"n": 1}, {"n": 2}], 2)  # the cursor stops before that line too
    assert read_events(log, 1) == ([{"n": 2}], 2) and read_events(log, 9) == ([], 9)
    log.write_text('{"n": 1}\n{"n": 2}\n{"n": 3}\n')
    assert read_events(log, 2) == ([{"n": 3}], 3)


async def test_status_holds_a_long_poll_until_an_event_or_the_end_or_the_wait_is_up(tmp_path: Path) -> None:
    sink = EventSink(tmp_path / "events.jsonl", "j1")
    release = asyncio.Event()

    async def work() -> dict[str, Any]:
        await asyncio.sleep(0.1)
        sink.emit("a")
        await release.wait()
        return {"done": True}

    jobs = Jobs()
    jobs.start("run", "j1", sink.path, work())
    out = await jobs.status("j1", wait=5)  # held until the event
    assert out is not None and [e["kind"] for e in out["events"]] == ["a"] and out["state"] == "running"
    assert out["next"] == 1
    t0 = time.monotonic()
    out = await jobs.status("j1", since=1, wait=0.3)  # nothing new: held until the wait is up
    assert out is not None and out["events"] == [] and out["next"] == 1 and out["state"] == "running"
    assert time.monotonic() - t0 >= 0.25
    release.set()
    out = await jobs.status("j1", since=1, wait=5)  # held until the job ends
    assert out is not None and out["state"] == "done" and out["result"] == {"done": True}


async def test_a_job_that_raises_ends_failed_with_the_error(tmp_path: Path) -> None:
    async def boom() -> dict[str, Any]:
        raise RuntimeError("engine fell over")

    jobs = Jobs()
    jobs.start("run", "j1", tmp_path / "events.jsonl", boom())
    out = await jobs.status("j1", wait=5)
    assert out is not None and out["state"] == "failed" and out["error"] == "RuntimeError: engine fell over"
    assert "result" not in out and await jobs.status("ghost") is None


async def test_a_running_job_id_cannot_start_twice_but_an_ended_one_can(tmp_path: Path) -> None:
    async def hang() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    async def quick() -> dict[str, Any]:
        return {"ok": True}

    jobs = Jobs()
    jobs.start("run", "j1", tmp_path / "events.jsonl", hang())
    with pytest.raises(ValueError, match="already running"):
        jobs.start("run", "j1", tmp_path / "events.jsonl", hang())
    assert jobs.cancel("j1") is True and jobs.cancel("ghost") is False
    out = await jobs.status("j1", wait=5)
    assert out is not None and out["state"] == "cancelled" and jobs.cancel("j1") is False  # it has ended
    jobs.start("run", "j1", tmp_path / "events.jsonl", quick())
    out = await jobs.status("j1", wait=5)
    assert out is not None and out["state"] == "done" and out["result"] == {"ok": True}


def test_a_closed_fragment_in_the_middle_of_the_log_is_skipped_and_the_cursor_moves_past_it(
    tmp_path: Path,
) -> None:
    # a story that was resumed after a crash: the half line the crash left was closed (state.py), and the run
    # went on writing after it. Stopping at that line would freeze every follower there.
    log = tmp_path / "events.jsonl"
    # a fragment, and a line that is no event
    log.write_text('{"n": 1}\n{"n": 2, "kin\n{"n": 3}\n12\n{"n": 4}\n')
    assert read_events(log, 0) == ([{"n": 1}, {"n": 3}, {"n": 4}], 5)
    assert read_events(log, 2) == ([{"n": 3}, {"n": 4}], 5) and read_events(log, 5) == ([], 5)
    log.write_text('{"n": 1}\n{"n": 2, "kin\n{"n": 3}\n{"n": 4')  # the last line is still being written
    assert read_events(log, 0) == ([{"n": 1}, {"n": 3}], 3)  # held back, not skipped, and not counted
    log.write_text('{"n": 1}\n{"n": 2, "kin\n{"n": 3}\n{"n": 4}\n')
    assert read_events(log, 3) == ([{"n": 4}], 4)


def test_the_log_of_a_resumed_story_is_read_past_the_line_its_crash_tore(tmp_path: Path) -> None:
    story_dir = tmp_path / "text-0123456789"
    story_dir.mkdir()
    (story_dir / "events.jsonl").write_text('{"kind": "story_start"}\n{"kind": "sta')  # a crash mid-append
    st = StoryState.open(story_dir, text_card("avatars"), tmp_path)  # the resume closes the torn line
    st.emit("stage", stage="intake")
    events, upto = read_events(story_dir / "events.jsonl", 0)
    assert [e["kind"] for e in events] == ["story_start", "stage"] and upto == 3


async def test_a_follower_gets_the_events_after_a_fragment_and_a_cursor_past_it(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text('{"kind": "a"}\n{"kind": "b", "ti\n{"kind": "c"}\n')

    async def work() -> dict[str, Any]:
        return {}

    jobs = Jobs()
    jobs.start("story", "j1", log, work())
    out = await jobs.status("j1", wait=5)
    assert out is not None and [e["kind"] for e in out["events"]] == ["a", "c"] and out["next"] == 3
    out = await jobs.status("j1", since=1)  # a follower that had seen "a" only
    assert out is not None and [e["kind"] for e in out["events"]] == ["c"] and out["next"] == 3
    out = await jobs.status("j1", since=3)
    assert out is not None and out["events"] == [] and out["next"] == 3


async def test_a_long_poll_that_finds_only_a_fragment_still_moves_the_cursor(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text('{"kind": "a"}\n{"kind": "b", "ti\n')
    release = asyncio.Event()

    async def work() -> dict[str, Any]:
        await release.wait()
        return {}

    jobs = Jobs()
    jobs.start("story", "j1", log, work())
    out = await jobs.status("j1", since=1, wait=0.3)  # nothing but the fragment after the cursor
    assert out is not None and out["events"] == [] and out["next"] == 2 and out["state"] == "running"
    release.set()
    await jobs.status("j1", since=2, wait=5)


# ---- POST /v1/airlie/stories ----


def test_the_story_endpoint_starts_a_job_and_rejects_bad_requests(tmp_path: Path) -> None:
    events = tmp_path / "story-events.jsonl"

    async def fake_story_job(cfg, *, card, text, repo, running):  # type: ignore[no-untyped-def]
        if text == "broken":
            raise StorySetupError("no repository")

        async def work() -> dict[str, Any]:
            EventSink(events, "s").emit("stage", stage="intake")
            return {"stage": "review", "blocked": None, "prs": [], "dir": "x"}

        return "story-text-abc", events, work()

    with TestClient(app_for(tmp_path, FakeEngine({}), story_job=fake_story_job)) as client:
        assert client.post("/v1/airlie/stories", json={}).status_code == 400
        assert client.post("/v1/airlie/stories", json={"card": "a", "text": "b"}).status_code == 400
        r = client.post("/v1/airlie/stories", json={"text": "broken"})
        assert r.status_code == 400 and "no repository" in r.json()["detail"]
        r = client.post("/v1/airlie/stories", json={"text": "avatars", "repo": "/x"})
        assert r.status_code == 202 and r.json() == {"job_id": "story-text-abc", "kind": "story"}
        done = finish(client, "story-text-abc")
    assert done["state"] == "done" and done["result"]["stage"] == "review"
    assert [e["kind"] for e in done["events"]] == ["stage"]


def test_the_story_endpoint_passes_the_request_to_the_job_factory(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_story_job(cfg, **kw):  # type: ignore[no-untyped-def]
        seen.append(kw)

        async def work() -> dict[str, Any]:
            return {}

        return "story-AIR-7", tmp_path / "events.jsonl", work()

    body = {"text": "avatars", "repo": "~/code/app"}  # a written request's repository
    with TestClient(app_for(tmp_path, FakeEngine({}), story_job=fake_story_job)) as client:
        assert client.post("/v1/airlie/stories", json=body).status_code == 202
        finish(client, "story-AIR-7")
        assert client.post("/v1/airlie/stories", json={"card": "AIR-7", "repo": " "}).status_code == 202
    for kw in seen:
        assert callable(kw.pop("running"))  # what tells it a job of the same story is running
    assert seen == [
        {"card": None, "text": "avatars", "repo": "~/code/app"},
        {"card": "AIR-7", "text": None, "repo": None},  # a blank repo is none
    ]


@pytest.mark.parametrize("body", [{"card": "AIR-7", "repo": " "}, {"text": "avatars", "repo": "\t "}])
def test_a_blank_repo_is_no_repository_all_the_way_to_the_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    """Not a path made of blanks (no git repository there: a 400). An issue's story is placed by its label
    and a written request's in story.repo, as when the request names no repo."""
    from tests.unit.test_story_board import FakeJira
    from tests.unit.test_story_board import board as jira_board_for
    from tests.unit.test_story_pipeline import checkout_of

    repo = checkout_of(tmp_path / "git", "o/repo")  # FakeJira's AIR-7 is labelled repo:o/repo
    story = {"repos_root": str(tmp_path / "git"), "repo": str(repo)}
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": story})
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(FakeJira()))
    given: list[Path | None] = []

    async def builds(c: Any, *, board: Any, card: Any, repo: Path | None = None) -> StoryState:
        given.append(repo)
        return StoryState(path=tmp_path / "s", card=card.to_dict(), repo=str(repo), stage="review")

    monkeypatch.setattr(factory, "run_one", builds)
    with TestClient(create_app(cfg, transport=httpx.MockTransport(FakeUpstream()))) as client:
        r = client.post("/v1/airlie/stories", json=body)
        assert r.status_code == 202, r.json()
        assert finish(client, r.json()["job_id"])["state"] == "done"
    assert given == [None]


def test_an_issues_story_with_a_repo_is_refused_only_a_person_on_the_cli_may_move_it(tmp_path: Path) -> None:
    called: list[dict[str, Any]] = []

    async def fake_story_job(cfg, **kw):  # type: ignore[no-untyped-def]
        called.append(kw)
        raise AssertionError("never reached")

    with TestClient(app_for(tmp_path, FakeEngine({}), story_job=fake_story_job)) as client:
        r = client.post("/v1/airlie/stories", json={"card": "AIR-7", "repo": "/somewhere/else"})
    assert r.status_code == 422 and called == []
    assert r.json()["detail"] == (
        "repo is for a written request (text) only: an issue's repository is its repo:<owner>/<name> label, "
        "and only a person's `task story --repo` builds an issue elsewhere"
    )


@pytest.mark.parametrize("extra", [{"test_cmd": "true"}, {"test_cmd": None}, {"gate": "true"}])
def test_a_story_request_that_names_a_test_command_is_refused_not_ignored(
    tmp_path: Path, extra: dict[str, Any]
) -> None:
    """A story's gate is its repository's AGENTS.md: a request cannot name one, and one that tries learns so
    rather than having its command silently dropped."""
    called: list[dict[str, Any]] = []

    async def fake_story_job(cfg, **kw):  # type: ignore[no-untyped-def]
        called.append(kw)
        raise AssertionError("never reached")

    with TestClient(app_for(tmp_path, FakeEngine({}), story_job=fake_story_job)) as client:
        r = client.post("/v1/airlie/stories", json={"card": "AIR-7", **extra})
    assert r.status_code == 422 and called == []
    [problem] = r.json()["detail"]
    assert problem["loc"] == ["body", next(iter(extra))] and problem["type"] == "extra_forbidden"


def test_a_board_that_cannot_give_the_issue_is_a_400(tmp_path: Path) -> None:
    async def fake_story_job(cfg, **kw):  # type: ignore[no-untyped-def]
        raise BoardError("not a Jira issue key (AIR-7) or issue link (https://SITE/browse/AIR-7): 'banana'")

    with TestClient(app_for(tmp_path, FakeEngine({}), story_job=fake_story_job)) as client:
        r = client.post("/v1/airlie/stories", json={"card": "banana"})
    assert r.status_code == 400 and "not a Jira issue key" in r.json()["detail"]


def test_a_story_that_is_already_running_is_a_409_and_an_ended_one_can_start_again(tmp_path: Path) -> None:
    async def fake_story_job(cfg, **kw):  # type: ignore[no-untyped-def]
        async def work() -> dict[str, Any]:
            await asyncio.sleep(3600)
            return {}

        return "story-AIR-7", tmp_path / "events.jsonl", work()

    with TestClient(app_for(tmp_path, FakeEngine({}), story_job=fake_story_job)) as client:
        assert client.post("/v1/airlie/stories", json={"card": "AIR-7"}).status_code == 202
        r = client.post("/v1/airlie/stories", json={"card": "AIR-7"})
        assert r.status_code == 409 and "already running" in r.json()["detail"]
        assert (
            client.get("/v1/airlie/jobs/story-AIR-7").json()["state"] == "running"
        )  # the first is untouched
        assert client.delete("/v1/airlie/jobs/story-AIR-7").json()["cancelled"] is True
        ended = client.get("/v1/airlie/jobs/story-AIR-7", params={"since": 10_000, "wait": 5}).json()
        assert ended["state"] == "cancelled"
        again = client.post("/v1/airlie/stories", json={"card": "AIR-7"})  # the same key: the story resumes
        assert again.status_code == 202
        assert client.delete("/v1/airlie/jobs/story-AIR-7").json()["cancelled"] is True


def test_a_resumed_story_is_followed_past_the_fragment_its_crash_left(tmp_path: Path) -> None:
    events = tmp_path / "story-events.jsonl"
    # what a crash left, closed by the resume
    events.write_text('{"kind": "story_start", "run_id": "s"}\n{"kind": "sta\n')

    async def fake_story_job(cfg, **kw):  # type: ignore[no-untyped-def]
        async def work() -> dict[str, Any]:
            EventSink(events, "s").emit("stage", stage="intake")
            return {"stage": "intake"}

        return "story-text-abc", events, work()

    with TestClient(app_for(tmp_path, FakeEngine({}), story_job=fake_story_job)) as client:
        client.post("/v1/airlie/stories", json={"text": "avatars"})
        done = finish(client, "story-text-abc")
    assert [e["kind"] for e in done["events"]] == ["story_start", "stage"] and done["next"] == 3


def test_the_default_story_job_refuses_a_config_without_the_story_roles(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    # app_for's config has the studio roles, but no product or architect
    with TestClient(app_for(tmp_path, FakeEngine({}))) as client:
        r = client.post("/v1/airlie/stories", json={"text": "avatars", "repo": str(repo)})
        assert r.status_code == 400 and "story.product_role" in r.json()["detail"]
        assert client.get(f"/v1/airlie/jobs/story-{text_card('avatars').key}").status_code == 404  # no job


def test_the_default_story_job_builds_the_story_and_reports_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    prs = [{"number": 11, "branch": "b1", "url": "https://github.com/o/r/pull/11"}]

    async def fake_run_one(c, *, board, card, repo=None, test_cmd=None):  # type: ignore[no-untyped-def]
        assert test_cmd is None  # the story's gate is its AGENTS.md's
        path = factory.stories_dir(c) / card.key
        EventSink(path / "events.jsonl", card.key).emit("stage", stage="intake")
        return StoryState(path=path, card=card.to_dict(), repo=str(repo), stage="review", prs=prs)

    monkeypatch.setattr(factory, "run_one", fake_run_one)
    key = text_card("avatars").key
    with TestClient(create_app(cfg, transport=httpx.MockTransport(FakeUpstream()))) as client:
        r = client.post("/v1/airlie/stories", json={"text": "avatars", "repo": str(repo)})
        assert r.status_code == 202 and r.json() == {"job_id": f"story-{key}", "kind": "story"}
        done = finish(client, f"story-{key}")
    assert done["state"] == "done" and [e["kind"] for e in done["events"]] == ["stage"]
    assert done["result"] == {
        "stage": "review",
        "blocked": None,
        "prs": prs,
        "dir": str(factory.stories_dir(cfg) / key),
    }


def test_a_second_story_while_one_is_in_flight_ends_as_a_failed_job_that_says_so(tmp_path: Path) -> None:
    repo = repo_with_marker_tests(tmp_path)
    cfg = cfg_for(tmp_path, roles=STORY_ROLES)
    app = create_app(cfg, transport=httpx.MockTransport(FakeUpstream()))
    with (
        story_lock(factory.stories_dir(cfg)) as held,
        TestClient(app) as client,
    ):  # another story holds the lock
        assert held
        r = client.post("/v1/airlie/stories", json={"text": "avatars", "repo": str(repo)})
        assert r.status_code == 202  # a different story id: nothing is running under it
        done = finish(client, r.json()["job_id"])
    assert done["state"] == "failed" and "StoryBusy: another story is in flight" in done["error"]


def test_a_story_already_running_is_a_409_before_its_issue_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second POST for a story whose job runs: refused as the job's duplicate, before `placed` can note on
    the issue or move it (here its checkout has gone since, which would have the issue told and moved to
    Blocked while the first job builds it)."""
    from tests.unit.test_story_board import FakeJira
    from tests.unit.test_story_board import board as jira_board_for
    from tests.unit.test_story_pipeline import checkout_of

    repo = checkout_of(tmp_path / "git", "o/repo")  # FakeJira's AIR-7 is labelled repo:o/repo
    cfg = cfg_for(tmp_path, roles=STORY_ROLES, extra={"story": {"repos_root": str(tmp_path / "git")}})
    jira = FakeJira()
    monkeypatch.setattr(factory, "make_board", lambda cfg: jira_board_for(jira))
    release = asyncio.Event()

    async def builds(c: Any, *, board: Any, card: Any, repo: Path | None = None) -> StoryState:
        await release.wait()
        return StoryState(path=tmp_path / "s", card=card.to_dict(), repo=str(repo), stage="review")

    monkeypatch.setattr(factory, "run_one", builds)
    with TestClient(create_app(cfg, transport=httpx.MockTransport(FakeUpstream()))) as client:
        assert client.post("/v1/airlie/stories", json={"card": "AIR-7"}).status_code == 202
        shutil.rmtree(repo)  # the checkout goes while the first job runs
        posted = len([r for r in jira.requests if r.method == "POST"])
        r = client.post("/v1/airlie/stories", json={"card": "AIR-7"})
        assert r.status_code == 409 and "already running" in r.json()["detail"]
        assert len([r for r in jira.requests if r.method == "POST"]) == posted  # no note, no move
        assert client.delete("/v1/airlie/jobs/story-AIR-7").json()["cancelled"] is True
