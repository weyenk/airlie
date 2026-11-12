"""The chat screen's session routes through the real app: making a chat before its first message (so the panel
lists it at once), the list with each chat's repository and session branch, a person's pinned repositories
(kept on the server, so a phone and a laptop see the same panel), and the live feed's `chat` hint each change
publishes. Every row is its owner's: another person's chat is 404, as one that is not there."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.auth.principal import SESSION_COOKIE
from airlie.auth.store import AuthStore
from airlie.chat.repo_session import slug_of
from airlie.config import load_config
from airlie.manager.core import Manager
from airlie.serve.app import create_app
from airlie.story.publish import PublishError
from tests.support.repo_chat import RepoFakes
from tests.support.serve import FakeEngine, StuckClock
from tests.support.serve_chat import ScriptedUpstream, Text, chat_cfg
from tests.unit.serve_helpers import TestClient

ELSEWHERE = ("10.0.0.5", 40000)


KNOWN = ["weyenk/airlie", "o/r"] + [f"o/r{n}" for n in range(60)]
"""The repositories a pin may name (GET /v1/airlie/repos/names, faked: the real one reads checkouts)."""


async def known() -> list[str]:
    return list(KNOWN)


def client_for(tmp_path: Path, up: ScriptedUpstream | None = None, **kw: Any) -> TestClient:
    up = up or ScriptedUpstream()
    kw.setdefault("pinnable", known)
    return TestClient(create_app(chat_cfg(tmp_path), transport=httpx.MockTransport(up), **kw))


def people(tmp_path: Path) -> tuple[TestClient, dict[str, str], dict[str, str]]:
    """A client off the Mac and two signed-in people, Pat and Sam."""
    store = AuthStore(tmp_path / "auth.sqlite")
    pat, sam = store.add_user("Pat", "member"), store.add_user("Sam", "member")
    as_pat = {"Cookie": f"{SESSION_COOKIE}={store.make_session(pat.id, 30)}"}
    as_sam = {"Cookie": f"{SESSION_COOKIE}={store.make_session(sam.id, 30)}"}
    up = httpx.MockTransport(ScriptedUpstream())
    app = create_app(chat_cfg(tmp_path), transport=up, auth_store=store, pinnable=known)
    return TestClient(app, client=ELSEWHERE), as_pat, as_sam


def hints(c: TestClient) -> list[tuple[str, str | None]]:
    """Record what the app publishes to the live feed from now on."""
    seen: list[tuple[str, str | None]] = []
    feed = c.app.state.feed  # type: ignore[attr-defined]
    publish = feed.publish

    def spy(topic: str, key: str | None = None) -> None:
        seen.append((topic, key))
        publish(topic, key)

    feed.publish = spy
    return seen


def turn(thread_id: str, text: str = "hello", **extra: Any) -> dict[str, Any]:
    msg = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": text}]}
    return {"id": thread_id, "kind": "plain", "messages": [msg], **extra}


# --- POST /v1/airlie/chat/threads ---------------------------------------------------------------------------


def test_post_threads_makes_a_plain_chat_and_the_list_shows_it_first(tmp_path: Path) -> None:
    c = client_for(tmp_path)

    r = c.post("/v1/airlie/chat/threads", json={"kind": "plain"})

    assert r.status_code == 201
    made = r.json()
    assert made["id"] and made["kind"] == "plain" and made["title"] == ""
    assert (made["repo"], made["base_ref"], made["branch"], made["running"]) == (None, None, None, False)
    assert made["created"] == made["updated"]
    assert c.get("/v1/airlie/chat/threads").json()[0] == made


def test_post_threads_makes_a_repository_chat_named_with_its_session_branch(tmp_path: Path) -> None:
    c = client_for(tmp_path)

    r = c.post("/v1/airlie/chat/threads", json={"kind": "repository", "repo": "o/r", "base_ref": "dev"})

    assert r.status_code == 201
    made = r.json()
    assert (made["kind"], made["repo"], made["base_ref"]) == ("repository", "o/r", "dev")
    assert made["branch"] == f"airlie/chat-{slug_of(made['id'])}"
    got = c.get(f"/v1/airlie/chat/threads/{made['id']}").json()
    assert (got["repo"], got["branch"], got["messages"]) == ("o/r", made["branch"], [])


def test_post_threads_without_a_session_is_401(tmp_path: Path) -> None:
    c = TestClient(client_for(tmp_path, api_key="k").app, client=ELSEWHERE)

    r = c.post("/v1/airlie/chat/threads", json={"kind": "plain"})

    assert r.status_code == 401 and r.json() == {"error": "not signed in"}


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "repository"},
        {"kind": "repository", "repo": "nope"},
        {"kind": "repository", "repo": "o/r", "base_ref": "-x"},
        {"kind": "plain", "repo": "o/r"},
        {"kind": "voice"},
        {},
    ],
)
def test_post_threads_with_a_bad_body_is_422_and_makes_nothing(tmp_path: Path, body: dict[str, Any]) -> None:
    c = client_for(tmp_path)

    r = c.post("/v1/airlie/chat/threads", json=body)

    assert r.status_code == 422
    assert c.get("/v1/airlie/chat/threads").json() == []


def test_a_made_chat_stays_listed_when_its_first_turn_is_refused(tmp_path: Path) -> None:
    c = client_for(tmp_path)
    made = c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).json()

    r = c.post("/v1/airlie/chat/turn", json=turn(made["id"], skill="nope"))

    assert r.status_code == 404
    assert [t["id"] for t in c.get("/v1/airlie/chat/threads").json()] == [made["id"]]


def test_the_first_turn_of_a_made_chat_runs_on_it_and_titles_it(tmp_path: Path) -> None:
    c = client_for(tmp_path, ScriptedUpstream(Text("hi there")))
    made = c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).json()

    r = c.post("/v1/airlie/chat/turn", json=turn(made["id"], "what is up"))

    assert r.status_code == 200
    listed = c.get("/v1/airlie/chat/threads").json()
    assert [(t["id"], t["title"]) for t in listed] == [(made["id"], "what is up")]


# --- GET /v1/airlie/chat/threads ----------------------------------------------------------------------------


def test_get_threads_lists_only_the_callers_own_chats(tmp_path: Path) -> None:
    c, as_pat, as_sam = people(tmp_path)
    made = c.post("/v1/airlie/chat/threads", json={"kind": "repository", "repo": "o/r"}, headers=as_pat)

    assert made.status_code == 201
    assert c.get("/v1/airlie/chat/threads", headers=as_sam).json() == []
    assert [t["repo"] for t in c.get("/v1/airlie/chat/threads", headers=as_pat).json()] == ["o/r"]
    assert c.get(f"/v1/airlie/chat/threads/{made.json()['id']}", headers=as_sam).status_code == 404


# --- the live feed's chat hint ------------------------------------------------------------------------------


def test_making_running_and_deleting_a_chat_each_publish_a_chat_hint_with_no_id(tmp_path: Path) -> None:
    """The feed goes to every signed-in client: the hint names no chat, and each refetches its own."""
    c = client_for(tmp_path, ScriptedUpstream(Text("hi")))
    seen = hints(c)

    made = c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).json()["id"]
    assert seen == [("chat", None)]
    c.post("/v1/airlie/chat/turn", json=turn(made))
    assert seen[-1] == ("chat", None)
    seen.clear()
    assert c.delete(f"/v1/airlie/chat/threads/{made}").status_code == 204
    assert seen == [("chat", None)]


# --- /v1/airlie/chat/pins -----------------------------------------------------------------------------------


def test_put_pins_a_repository_once_and_get_lists_pins_in_the_order_they_were_made(tmp_path: Path) -> None:
    c = client_for(tmp_path)

    assert c.put("/v1/airlie/chat/pins/weyenk/airlie").status_code == 204
    assert c.put("/v1/airlie/chat/pins/o/r").status_code == 204
    assert c.put("/v1/airlie/chat/pins/Weyenk/Airlie").status_code == 204  # GitHub's names are any case

    assert c.get("/v1/airlie/chat/pins").json() == {"pins": ["weyenk/airlie", "o/r"]}


def test_pins_are_each_persons_own(tmp_path: Path) -> None:
    c, as_pat, as_sam = people(tmp_path)

    c.put("/v1/airlie/chat/pins/o/r", headers=as_pat)

    assert c.get("/v1/airlie/chat/pins", headers=as_pat).json() == {"pins": ["o/r"]}
    assert c.get("/v1/airlie/chat/pins", headers=as_sam).json() == {"pins": []}
    assert c.delete("/v1/airlie/chat/pins/o/r", headers=as_sam).status_code == 404  # not Sam's to unpin
    assert c.get("/v1/airlie/chat/pins", headers=as_pat).json() == {"pins": ["o/r"]}


def test_pins_outlast_a_restart_of_serve(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite")
    first = TestClient(create_app(chat_cfg(tmp_path), auth_store=store, pinnable=known))
    first.put("/v1/airlie/chat/pins/o/r")

    cfg = load_config(tmp_path / "airlie.yaml")  # chat_cfg writes its files once
    again = TestClient(create_app(cfg, auth_store=AuthStore(tmp_path / "auth.sqlite"), pinnable=known))

    assert again.get("/v1/airlie/chat/pins").json() == {"pins": ["o/r"]}


def test_delete_pins_unpins_and_a_second_delete_is_404(tmp_path: Path) -> None:
    c = client_for(tmp_path)
    c.put("/v1/airlie/chat/pins/o/r")

    assert c.delete("/v1/airlie/chat/pins/O/R").status_code == 204
    second = c.delete("/v1/airlie/chat/pins/o/r")

    assert second.status_code == 404 and second.json() == {"error": "o/r is not pinned"}
    assert c.get("/v1/airlie/chat/pins").json() == {"pins": []}


def test_unpinning_keeps_the_repositorys_chats(tmp_path: Path) -> None:
    c = client_for(tmp_path)
    c.put("/v1/airlie/chat/pins/o/r")
    made = c.post("/v1/airlie/chat/threads", json={"kind": "repository", "repo": "o/r"}).json()

    c.delete("/v1/airlie/chat/pins/o/r")

    assert [t["id"] for t in c.get("/v1/airlie/chat/threads").json()] == [made["id"]]


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_the_pins_routes_without_a_session_are_401(tmp_path: Path, method: str) -> None:
    c = TestClient(client_for(tmp_path, api_key="k").app, client=ELSEWHERE)
    path = "/v1/airlie/chat/pins" if method == "get" else "/v1/airlie/chat/pins/o/r"

    r = getattr(c, method)(path)

    assert r.status_code == 401 and r.json() == {"error": "not signed in"}


@pytest.mark.parametrize("path", ["-o/r", "o/-r", "o/a..b", "o/r.."])
def test_pinning_a_name_that_is_not_owner_name_is_422(tmp_path: Path, path: str) -> None:
    c = client_for(tmp_path)

    r = c.put(f"/v1/airlie/chat/pins/{path}")

    assert r.status_code == 422 and "owner/name" in r.json()["error"]
    assert c.get("/v1/airlie/chat/pins").json() == {"pins": []}


def test_pinning_and_unpinning_publish_the_chat_hint(tmp_path: Path) -> None:
    c = client_for(tmp_path)
    seen = hints(c)

    c.put("/v1/airlie/chat/pins/o/r")
    c.delete("/v1/airlie/chat/pins/o/r")

    assert seen == [("chat", None), ("chat", None)]


def test_pinning_a_repository_airlie_does_not_know_is_422(tmp_path: Path) -> None:
    c = client_for(tmp_path)

    r = c.put("/v1/airlie/chat/pins/someone/else")

    assert r.status_code == 422
    assert r.json() == {"error": "someone/else is not a repository airlie knows (GET /v1/airlie/repos/names)"}
    assert c.get("/v1/airlie/chat/pins").json() == {"pins": []}


def test_a_person_pins_at_most_fifty_repositories(tmp_path: Path) -> None:
    c = client_for(tmp_path)
    for n in range(50):
        assert c.put(f"/v1/airlie/chat/pins/o/r{n}").status_code == 204

    over = c.put("/v1/airlie/chat/pins/o/r50")

    assert over.status_code == 422 and over.json() == {
        "error": "at most 50 pinned repositories: unpin one first"
    }
    assert c.put("/v1/airlie/chat/pins/o/r0").status_code == 204  # pinning one again is no new pin
    assert len(c.get("/v1/airlie/chat/pins").json()["pins"]) == 50


def test_a_path_trick_cannot_make_a_pin(tmp_path: Path) -> None:
    c = client_for(tmp_path)

    for path in ("a%2Fb/c", "o/%2E%2E", "../o/r"):
        assert c.put(f"/v1/airlie/chat/pins/{path}").status_code in (404, 405, 422)

    assert c.get("/v1/airlie/chat/pins").json() == {"pins": []}


# --- GET /v1/airlie/chat/threads/{id}/status ----------------------------------------------------------------


def status_rig(tmp_path: Path, *, unpushed: bool = False) -> tuple[TestClient, RepoFakes, Manager]:
    """The real app over a manager with one fake engine (`main`, 10 GB of a 100 GB budget) and the fakes a
    repository chat runs on (a FakeBox VM, a host mirror over a recording runner)."""
    fakes = RepoFakes(tmp_path)
    if unpushed:
        answer = fakes.runner.__call__

        async def refuse_the_remote(args: list[str], cwd: Path, env: Any) -> str:
            if "refs/remotes/origin/" + f"airlie/chat-{slug_of('t1')}" in args:
                raise PublishError("not a valid ref")
            return await answer(args, cwd, env)

        fakes.mirror.run = refuse_the_remote  # type: ignore[assignment]
    manager = Manager([FakeEngine("main")], budget_gb=100.0, clock=StuckClock())
    app = create_app(
        chat_cfg(tmp_path),
        transport=httpx.MockTransport(ScriptedUpstream(Text("hi"))),
        manager=manager,
        repo_sessions=fakes.build,
        pinnable=known,
    )
    return TestClient(app), fakes, manager


def repo_turn(thread_id: str = "t1") -> dict[str, Any]:
    msg = {"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}
    return {"id": thread_id, "kind": "repository", "repo": "o/r", "messages": [msg]}


def test_status_without_a_session_is_401(tmp_path: Path) -> None:
    c = TestClient(client_for(tmp_path, api_key="k").app, client=ELSEWHERE)

    r = c.get("/v1/airlie/chat/threads/t1/status")

    assert r.status_code == 401 and r.json() == {"error": "not signed in"}


def test_status_of_a_chat_that_is_not_there_is_404(tmp_path: Path) -> None:
    c = client_for(tmp_path)

    r = c.get("/v1/airlie/chat/threads/nope/status")

    assert r.status_code == 404 and r.json() == {"error": "no such thread"}


def test_status_of_another_persons_chat_is_404(tmp_path: Path) -> None:
    c, as_pat, as_sam = people(tmp_path)
    made = c.post("/v1/airlie/chat/threads", json={"kind": "plain"}, headers=as_pat).json()["id"]

    assert c.get(f"/v1/airlie/chat/threads/{made}/status", headers=as_pat).status_code == 200
    assert c.get(f"/v1/airlie/chat/threads/{made}/status", headers=as_sam).status_code == 404


def test_a_plain_chats_status_has_no_vm_and_no_branch_but_the_engine_and_memory(tmp_path: Path) -> None:
    c, _, _ = status_rig(tmp_path)
    made = c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).json()["id"]

    r = c.get(f"/v1/airlie/chat/threads/{made}/status")

    assert r.status_code == 200
    assert r.json() == {
        "vm": None,
        "engine": {"name": "main", "model": "base", "state": "stopped", "footprint_gb": 10.0},
        "memory": {"used_gb": 0.0, "budget_gb": 100.0, "waiting": None},
        "branch": None,
    }


def test_a_repository_chats_status_names_its_vm_the_loaded_engine_and_its_commits_ahead(
    tmp_path: Path,
) -> None:
    c, _, _ = status_rig(tmp_path)
    assert c.post("/v1/airlie/chat/turn", json=repo_turn()).status_code == 200

    got = c.get("/v1/airlie/chat/threads/t1/status").json()

    assert got["vm"] == {
        "name": "vm:t1",
        "state": "ready",
        "detail": "",
        "since": got["vm"]["since"],
        "memory_gb": 8.0,
    }
    assert got["engine"]["state"] == "ready"
    assert got["memory"] == {"used_gb": 18.0, "budget_gb": 100.0, "waiting": None}
    assert got["branch"] == {
        "name": f"airlie/chat-{slug_of('t1')}",
        "base": "main",
        "ahead": 2,
        "pushed": True,
    }


def test_a_branch_the_remote_does_not_have_is_not_pushed(tmp_path: Path) -> None:
    c, _, _ = status_rig(tmp_path, unpushed=True)
    assert c.post("/v1/airlie/chat/turn", json=repo_turn()).status_code == 200

    got = c.get("/v1/airlie/chat/threads/t1/status").json()

    assert got["branch"]["ahead"] == 2 and got["branch"]["pushed"] is False


def test_a_chat_whose_session_has_not_opened_has_no_vm_row_and_no_commit_count(
    tmp_path: Path,
) -> None:
    c, fakes, _ = status_rig(tmp_path)
    made = c.post("/v1/airlie/chat/threads", json={"kind": "repository", "repo": "x/y"}).json()["id"]

    got = c.get(f"/v1/airlie/chat/threads/{made}/status").json()

    assert got["vm"] is None  # (an unknown repository is not pre-warmed)
    assert got["branch"] == {
        "name": f"airlie/chat-{slug_of(made)}",
        "base": "main",
        "ahead": None,
        "pushed": None,
    }
    assert fakes.runner.calls == []  # the mirror was not asked


def test_a_mirror_that_cannot_say_leaves_the_count_unknown_and_the_status_whole(tmp_path: Path) -> None:
    c, fakes, _ = status_rig(tmp_path)
    assert c.post("/v1/airlie/chat/turn", json=repo_turn()).status_code == 200
    fakes.runner.failures["rev-list"] = "fatal: bad revision"

    r = c.get("/v1/airlie/chat/threads/t1/status")

    assert r.status_code == 200
    assert r.json()["branch"]["ahead"] is None and r.json()["vm"]["state"] == "ready"


def test_status_names_what_a_waiting_engine_waits_for(tmp_path: Path) -> None:
    c, _, manager = status_rig(tmp_path)
    made = c.post("/v1/airlie/chat/threads", json={"kind": "plain"}).json()["id"]
    slot = manager._slots["main"]
    slot.state, slot.detail = "waiting", "waiting for memory: held by reviewer"

    got = c.get(f"/v1/airlie/chat/threads/{made}/status").json()

    assert got["engine"]["state"] == "waiting"
    assert got["memory"]["waiting"] == "waiting for memory: held by reviewer"
