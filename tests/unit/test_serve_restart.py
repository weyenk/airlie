"""POST /v1/airlie/stories/{key}/restart: a story started over from a step, as the person signed in (the
restart itself is airlie.story.restart's, tested in test_story_restart). 409 while it runs, 422 for what it
refuses with nothing changed, 502 for gh failing part-way, 401 without a sign-in."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from airlie.board import BoardStore
from airlie.serve.app import create_app
from airlie.story.board import BoardError
from airlie.story.model import StoryRunning
from airlie.story.publish import PublishError
from airlie.story.restart import Restarted, RestartError, RestartIncomplete
from tests.support.serve import FakeUpstream
from tests.support.studio import cfg_for
from tests.unit.serve_helpers import TestClient

DONE = Restarted("AIR-7", "layer-02", [11, 12], ["AIR-7-x-02-y"], [], "/s/archive/stories/AIR-7-from-x", "n")


def app_with(tmp_path: Path, restart: Any = None, **kw: Any) -> Any:
    return create_app(
        cfg_for(tmp_path), transport=httpx.MockTransport(FakeUpstream()), story_restart=restart, **kw
    )


def test_a_restart_runs_as_the_person_signed_in_and_returns_what_it_did(tmp_path: Path) -> None:
    asked: list[tuple[str, str, str, bool]] = []

    async def restart(cfg: Any, key: str, point: str, *, by: str, running: Any) -> Restarted:
        asked.append((key, point, by, running(f"story-{key}")))
        return DONE

    with TestClient(app_with(tmp_path, restart)) as client:
        r = client.post("/v1/airlie/stories/AIR-7/restart", json={"from": "layer-02"})
        assert r.status_code == 200 and r.json() == DONE.to_dict()
        assert client.post("/v1/airlie/stories/AIR-7/restart").status_code == 200  # no body: from scratch
        assert client.post("/v1/airlie/stories/AIR-7/restart", json={"step": "x"}).status_code == 422
    [(key, point, by, running), (_, fresh, _, _)] = asked
    assert (key, point, running) == ("AIR-7", "layer-02", False) and by and fresh == "refine"


def test_what_a_restart_refuses_is_a_409_or_a_422_and_gh_failing_part_way_a_502(tmp_path: Path) -> None:
    failures: list[Exception] = [
        StoryRunning("story AIR-7 is running now"),
        RestartError("story AIR-7's PR #12 is merged"),
        BoardError("AIR-3 is an epic; only stories are built"),
        RestartError("layer 01 conflicts with the trunk; start it over from an earlier step"),
        PublishError("`gh pr view 12` exited 1: HTTP 502"),  # reading the PRs: nothing changed yet
        # part-way: a board or git failing after the PRs were closed is no refusal, whatever it raised
        RestartIncomplete("the board is down; the restart stopped part-way ...: run it again to finish"),
        RestartIncomplete("`git update-ref` exited 128; ...: run it again to finish"),
    ]

    async def restart(cfg: Any, key: str, point: str, *, by: str, running: Any) -> Restarted:
        raise failures.pop(0)

    with TestClient(app_with(tmp_path, restart)) as client:
        got = [client.post("/v1/airlie/stories/AIR-7/restart", json={"from": "refine"}) for _ in range(7)]
    assert [r.status_code for r in got] == [409, 422, 422, 422, 502, 502, 502]
    assert "is merged" in got[1].json()["detail"] and "earlier step" in got[3].json()["detail"]
    assert "nothing was changed" in got[4].json()["detail"]
    assert all("run it again to finish" in r.json()["detail"] for r in got[5:])


def test_a_restart_needs_a_sign_in(tmp_path: Path) -> None:
    async def restart(cfg: Any, key: str, point: str, *, by: str, running: Any) -> Restarted:
        raise AssertionError("not signed in: never asked")

    app = app_with(tmp_path, restart, api_key="k")
    remote = TestClient(app, client=("10.0.0.5", 1))
    assert remote.post("/v1/airlie/stories/AIR-7/restart", json={"from": "refine"}).status_code == 401


def test_the_default_restart_moves_a_story_with_no_record_to_approved(tmp_path: Path) -> None:
    app = app_with(tmp_path)
    cfg = cfg_for(tmp_path)
    store = BoardStore(cfg.observability.state_dir / "board.sqlite", prefix=cfg.board.prefix)
    story = store.create(level="story", title="Avatars")
    epic = store.create(level="epic", title="Profiles")
    with TestClient(app) as client:
        r = client.post(f"/v1/airlie/stories/{story.key}/restart", json={"from": "refine"})
        assert r.status_code == 200 and r.json()["archive"] is None
        assert client.post(f"/v1/airlie/stories/{epic.key}/restart").status_code == 422
        late = client.post(f"/v1/airlie/stories/{story.key}/restart", json={"from": "packages"})
        assert late.status_code == 422 and "has not started" in late.json()["detail"]
    assert store.get(story.key).status == "approved"
