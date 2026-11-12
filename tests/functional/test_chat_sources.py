"""Linking a GitHub account from the chat panel through the real app: the device code shown to the person who
asked, the poll at GitHub's pace, the repositories each person's account reaches, unlinking, and the pins
that only a local checkout can be. GitHub is FakeGitHubUser over a MockTransport; two signed-in people share
one app. Another person's link id is a 404, as one that is not there."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from airlie.auth.principal import SESSION_COOKIE
from airlie.auth.store import AuthStore
from airlie.config import load_config
from airlie.serve.app import create_app
from airlie.sources.github import GitHubSource
from airlie.sources.protocol import SourceError
from tests.support.github_user import CODE_S, INTERVAL, Clock, FakeGitHubUser
from tests.support.serve_chat import ScriptedUpstream, chat_cfg
from tests.unit.github_helpers import APP_ID, FakeGitHub, app_spec
from tests.unit.serve_helpers import TestClient

ELSEWHERE = ("10.0.0.5", 40000)
CLIENT_ID, SECRET = "Iv1.testclient", "s3cr3t-client-secret"
LOCAL = ["weyenk/airlie", "o/r", "local/only"]
"""The checkouts airlie has; the App is installed on the first two."""
LINK = "/v1/airlie/chat/sources/github_app/link"
INSTALLATIONS = "/api/user/installations"
NO_CLIENT_ID = "set story.github_app.client_id in airlie.local.yaml to connect GitHub"


async def local_names() -> list[str]:
    return list(LOCAL)


@dataclass
class World:
    client: TestClient
    fake: FakeGitHubUser
    clock: Clock
    noah: dict[str, str]
    ashley: dict[str, str]
    hints: list[tuple[str, str | None]]

    def start(self, who: dict[str, str]) -> dict[str, Any]:
        r = self.client.post(LINK, headers=who)
        assert r.status_code == 201, r.text
        return r.json()  # type: ignore[no-any-return]

    def poll(self, who: dict[str, str], link_id: str) -> httpx.Response:
        return self.client.post(f"{LINK}/{link_id}", headers=who)

    def link(self, who: dict[str, str], login: str) -> None:
        """The person starts, approves on GitHub, and the next poll links them."""
        started = self.start(who)
        self.fake.approve(started["user_code"], login)
        self.clock.advance(INTERVAL)
        r = self.poll(who, started["link_id"])
        assert r.status_code == 200 and r.json() == {"state": "linked", "login": login}, r.text

    def listing(self, who: dict[str, str], refresh: bool = False) -> dict[str, Any]:
        r = self.client.get(f"/v1/airlie/chat/repositories?refresh={str(refresh).lower()}", headers=who)
        assert r.status_code == 200, r.text
        return r.json()  # type: ignore[no-any-return]

    def listings(self) -> int:
        return sum(1 for _, p in self.fake.calls if p == INSTALLATIONS)

    def polls(self) -> int:
        return sum(1 for f in self.fake.forms if "device_code" in f)


def config(tmp_path: Path, *, client_id: str | None = None):
    chat_cfg(tmp_path)
    path = tmp_path / "airlie.yaml"
    data = yaml.safe_load(path.read_text())
    app: dict[str, Any] = {"app_id": APP_ID, "token_key_file": str(tmp_path / "keys" / "tokens.key")}
    if client_id is not None:
        app["client_id"] = client_id
    data["story"] = {"github_app": app}
    data["chat"]["sources"] = ["github_app"]
    path.write_text(yaml.safe_dump(data))
    return load_config(path)


@pytest.fixture
def world(tmp_path: Path) -> World:
    clock = Clock()
    fake = FakeGitHubUser(
        FakeGitHub(installed=("o/r", "weyenk/airlie")), client_id=CLIENT_ID, client_secret=SECRET, clock=clock
    )
    fake.grant("weyenk", {1: [("weyenk/airlie", True), ("o/r", True)]})
    fake.grant("ashley", {2: [("o/r", False)]})
    secret = tmp_path / "client-secret"
    secret.write_text(SECRET + "\n")
    secret.chmod(0o600)
    spec = app_spec(
        tmp_path, client_id=CLIENT_ID, oauth_url="http://127.0.0.1:9/oauth", client_secret_file=secret
    )
    source = GitHubSource(
        spec, "https://github.com/o/r.git", transport=httpx.MockTransport(fake), clock=clock
    )
    store = AuthStore(tmp_path / "auth.sqlite", now=clock)
    noah, ashley = store.add_user("Noah", "member"), store.add_user("Ashley", "member")
    app = create_app(
        config(tmp_path, client_id=CLIENT_ID),
        transport=httpx.MockTransport(ScriptedUpstream()),
        auth_store=store,
        pinnable=local_names,
        repo_sources={"github_app": source},
        now=clock,
    )
    client = TestClient(app, client=ELSEWHERE)
    seen: list[tuple[str, str | None]] = []
    feed = app.state.feed
    publish = feed.publish

    def spy(topic: str, key: str | None = None) -> None:
        seen.append((topic, key))
        publish(topic, key)

    feed.publish = spy
    return World(
        client,
        fake,
        clock,
        {"Cookie": f"{SESSION_COOKIE}={store.make_session(noah.id, 30)}"},
        {"Cookie": f"{SESSION_COOKIE}={store.make_session(ashley.id, 30)}"},
        seen,
    )


# --- linking ------------------------------------------------------------------------------------------------


def test_linking_shows_a_code_then_the_persons_repositories(world: World) -> None:
    started = world.start(world.noah)

    assert started["user_code"] == "WDJB-MJHT"
    assert started["verification_uri"] == "https://github.com/login/device"
    assert started["interval"] == INTERVAL
    assert started["expires_at"] == world.clock.now + CODE_S
    assert set(started) == {"link_id", "user_code", "verification_uri", "expires_at", "interval"}
    secrets_seen = set(world.fake._codes)  # type: ignore[attr-defined]
    assert not any(s in json.dumps(started) for s in secrets_seen) and "device_code" not in started

    world.clock.advance(INTERVAL)
    pending = world.poll(world.noah, started["link_id"])
    assert (pending.status_code, pending.json()["state"]) == (202, "pending")

    world.fake.approve(started["user_code"], "weyenk")
    world.clock.advance(INTERVAL)
    linked = world.poll(world.noah, started["link_id"])
    assert (linked.status_code, linked.json()) == (200, {"state": "linked", "login": "weyenk"})

    (source,) = world.listing(world.noah)["sources"]
    assert (source["name"], source["title"], source["linked"], source["login"], source["error"]) == (
        "github_app",
        "GitHub",
        True,
        "weyenk",
        None,
    )
    assert source["repositories"] == [
        {"repo": "o/r", "private": True, "push": True, "url": "https://github.com/o/r"},
        {"repo": "weyenk/airlie", "private": True, "push": True, "url": "https://github.com/weyenk/airlie"},
    ]
    assert ("chat", None) in world.hints


def test_a_finished_link_cannot_be_polled_again(world: World) -> None:
    started = world.start(world.noah)
    world.fake.approve(started["user_code"], "weyenk")
    world.clock.advance(INTERVAL)
    assert world.poll(world.noah, started["link_id"]).status_code == 200

    assert world.poll(world.noah, started["link_id"]).status_code == 404


def test_noah_and_ashley_see_different_repositories(world: World) -> None:
    world.link(world.noah, "weyenk")
    world.link(world.ashley, "ashley")

    noah = world.listing(world.noah)
    ashley = world.listing(world.ashley)

    assert [r["repo"] for r in noah["sources"][0]["repositories"]] == ["o/r", "weyenk/airlie"]
    assert [(r["repo"], r["push"]) for r in ashley["sources"][0]["repositories"]] == [("o/r", False)]
    assert (noah["sources"][0]["login"], ashley["sources"][0]["login"]) == ("weyenk", "ashley")


def test_a_person_who_has_not_linked_sees_connect_and_the_local_extras(world: World) -> None:
    got = world.listing(world.noah)

    (source,) = got["sources"]
    assert (source["linked"], source["login"], source["repositories"], source["listed_at"]) == (
        False,
        None,
        [],
        None,
    )
    assert got["extra_candidates"] == ["local/only"] and got["extras"] == []


def test_another_persons_link_id_is_404(world: World) -> None:
    started = world.start(world.noah)

    stolen = world.poll(world.ashley, started["link_id"])

    assert stolen.status_code == 404
    assert world.polls() == 0  # GitHub was not asked on the stranger's behalf
    world.clock.advance(INTERVAL)
    assert world.poll(world.noah, started["link_id"]).status_code == 202  # still Noah's


def test_an_unknown_link_id_is_404(world: World) -> None:
    assert world.poll(world.noah, "nope").status_code == 404


def test_an_unknown_source_is_404(world: World) -> None:
    assert world.client.post("/v1/airlie/chat/sources/gitlab/link", headers=world.noah).status_code == 404
    assert world.client.post("/v1/airlie/chat/sources/gitlab/link/x", headers=world.noah).status_code == 404
    assert world.client.delete("/v1/airlie/chat/sources/gitlab/link", headers=world.noah).status_code == 404


def test_an_expired_code_is_410(world: World) -> None:
    started = world.start(world.noah)
    world.clock.advance(CODE_S + 1)

    r = world.poll(world.noah, started["link_id"])

    assert (r.status_code, r.json()) == (410, {"error": "the code expired: start again"})
    assert world.poll(world.noah, started["link_id"]).status_code == 404  # gone, not retried
    assert world.polls() == 0


def test_github_saying_the_code_expired_is_410(world: World) -> None:
    started = world.start(world.noah)
    code = next(iter(world.fake._codes))  # type: ignore[attr-defined]
    world.fake._codes[code]["expires"] = world.clock.now + 2 * INTERVAL  # type: ignore[attr-defined]
    world.clock.advance(2 * INTERVAL)  # GitHub's deadline passed; airlie's own is far off

    r = world.poll(world.noah, started["link_id"])

    assert (r.status_code, r.json()) == (410, {"error": "the code expired: start again"})


def test_a_declined_code_is_403(world: World) -> None:
    started = world.start(world.noah)
    world.fake.deny(started["user_code"])
    world.clock.advance(INTERVAL)

    r = world.poll(world.noah, started["link_id"])

    assert (r.status_code, r.json()) == (403, {"error": "you declined on GitHub"})
    assert world.poll(world.noah, started["link_id"]).status_code == 404


def test_without_a_client_id_linking_is_503_with_the_fix(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite")
    noah = {"Cookie": f"{SESSION_COOKIE}={store.make_session(store.add_user('Noah', 'member').id, 30)}"}
    app = create_app(
        config(tmp_path),
        transport=httpx.MockTransport(ScriptedUpstream()),
        auth_store=store,
        pinnable=local_names,
    )
    c = TestClient(app, client=ELSEWHERE)

    r = c.post(LINK, headers=noah)

    assert (r.status_code, r.json()) == (503, {"error": NO_CLIENT_ID})
    assert c.post(f"{LINK}/x", headers=noah).status_code == 503


def test_a_github_failure_on_start_is_502_without_secrets(world: World) -> None:
    world.fake.device_flow = False

    r = world.client.post(LINK, headers=world.noah)

    assert r.status_code == 502
    assert "Device Flow" in r.json()["error"]
    assert SECRET not in r.text and "device_code" not in r.text


def test_a_poll_before_the_interval_does_not_call_github(world: World) -> None:
    started = world.start(world.noah)

    early = world.poll(world.noah, started["link_id"])

    assert (early.status_code, early.json()) == (202, {"state": "pending", "interval": INTERVAL})
    assert world.polls() == 0
    world.clock.advance(INTERVAL)
    assert world.poll(world.noah, started["link_id"]).status_code == 202
    assert world.polls() == 1
    assert world.poll(world.noah, started["link_id"]).status_code == 202  # asked again at once: not forwarded
    assert world.polls() == 1


def test_starting_again_replaces_the_earlier_code(world: World) -> None:
    first = world.start(world.noah)
    second = world.start(world.noah)
    world.clock.advance(INTERVAL)

    assert world.poll(world.noah, first["link_id"]).status_code == 404
    assert world.poll(world.noah, second["link_id"]).status_code == 202


# --- the listing --------------------------------------------------------------------------------------------


def test_refresh_true_lists_again(world: World) -> None:
    world.link(world.noah, "weyenk")
    world.listing(world.noah)
    first = world.listings()

    world.listing(world.noah)
    assert world.listings() == first  # kept

    world.listing(world.noah, refresh=True)
    assert world.listings() == first + 1


def test_a_failing_listing_is_marked_so_the_panel_can_say_the_list_may_be_old(world: World) -> None:
    world.link(world.noah, "weyenk")
    world.listing(world.noah)
    world.fake.fail_listing = 503

    (source,) = world.listing(world.noah, refresh=True)["sources"]

    assert source["linked"] is True and source["error"]
    assert [r["repo"] for r in source["repositories"]] == ["o/r", "weyenk/airlie"]  # the last good list
    assert source["listed_at"] is not None


# --- unlinking ----------------------------------------------------------------------------------------------


def test_unlink_is_204_then_404_and_publishes_the_hint(world: World) -> None:
    world.link(world.noah, "weyenk")
    world.hints.clear()

    first = world.client.delete(LINK, headers=world.noah)
    second = world.client.delete(LINK, headers=world.noah)

    assert (first.status_code, first.content) == (204, b"")
    assert second.status_code == 404
    assert world.hints == [("chat", None)]
    assert len(world.fake.revoked_tokens) == 1  # GitHub ended the grant, not only airlie's copy
    assert world.listing(world.noah)["sources"][0]["linked"] is False


def test_unlinking_ends_a_code_still_in_flight(world: World) -> None:
    started = world.start(world.noah)
    world.client.delete(LINK, headers=world.noah)
    world.clock.advance(INTERVAL)

    assert world.poll(world.noah, started["link_id"]).status_code == 404


def test_one_persons_unlink_leaves_the_others_link(world: World) -> None:
    world.link(world.noah, "weyenk")
    world.link(world.ashley, "ashley")

    assert world.client.delete(LINK, headers=world.noah).status_code == 204

    assert world.listing(world.ashley)["sources"][0]["linked"] is True


# --- a person's own scope -----------------------------------------------------------------------------------

ROUTES = [
    ("GET", "/v1/airlie/chat/repositories"),
    ("POST", LINK),
    ("POST", f"{LINK}/abc"),
    ("DELETE", LINK),
    ("PUT", "/v1/airlie/chat/pins/o/r"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_route_is_401_without_a_principal(world: World, method: str, path: str) -> None:
    r = world.client.request(method, path)

    assert r.status_code == 401


# --- pins ---------------------------------------------------------------------------------------------------


def test_a_github_sourced_repository_cannot_be_pinned_as_an_extra(world: World) -> None:
    r = world.client.put("/v1/airlie/chat/pins/weyenk/airlie", headers=world.noah)

    assert r.status_code == 422
    assert r.json() == {
        "error": "weyenk/airlie comes from GitHub: it shows when your GitHub account can see it"
    }
    assert world.client.get("/v1/airlie/chat/pins", headers=world.noah).json() == {"pins": []}


def test_a_local_only_checkout_is_pinned_as_an_extra(world: World) -> None:
    assert world.client.put("/v1/airlie/chat/pins/local/only", headers=world.noah).status_code == 204

    assert world.listing(world.noah)["extras"] == ["local/only"]
    assert world.listing(world.ashley)["extras"] == []


def test_a_repository_airlie_does_not_have_still_cannot_be_pinned(world: World) -> None:
    r = world.client.put("/v1/airlie/chat/pins/nobody/nothing", headers=world.noah)

    assert r.status_code == 422 and "not a repository airlie knows" in r.json()["error"]


# --- races, retries and set-up ------------------------------------------------------------------------------


def asgi(world: World) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=world.client.app, client=ELSEWHERE), base_url="http://t"
    )


def source_of(world: World) -> Any:
    return world.client.app.state.repo_access._sources["github_app"]  # type: ignore[attr-defined]


def test_an_unlink_while_a_poll_is_with_github_leaves_the_person_not_linked(world: World) -> None:
    source = source_of(world)
    inside, release = asyncio.Event(), asyncio.Event()
    real = source.poll_link

    async def held(device_code: str) -> Any:
        got = await real(device_code)  # GitHub has issued the tokens
        inside.set()
        await release.wait()
        return got

    source.poll_link = held

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with asgi(world) as c:
            started = (await c.post(LINK, headers=world.noah)).json()
            world.fake.approve(started["user_code"], "weyenk")
            world.clock.advance(INTERVAL)
            polling = asyncio.create_task(c.post(f"{LINK}/{started['link_id']}", headers=world.noah))
            await inside.wait()
            unlinking = asyncio.create_task(c.delete(LINK, headers=world.noah))
            await asyncio.sleep(0.05)
            release.set()
            return await polling, await unlinking

    polled, unlinked = asyncio.run(scenario())

    assert polled.status_code == 404
    assert unlinked.status_code == 404  # there was nothing linked to drop
    assert world.listing(world.noah)["sources"][0]["linked"] is False
    assert len(world.fake.revoked_tokens) == 1  # the token GitHub issued to the ended attempt


def test_two_polls_at_once_ask_github_once(world: World) -> None:
    async def scenario() -> list[httpx.Response]:
        async with asgi(world) as c:
            started = (await c.post(LINK, headers=world.noah)).json()
            world.clock.advance(INTERVAL)
            url = f"{LINK}/{started['link_id']}"
            return list(await asyncio.gather(*(c.post(url, headers=world.noah) for _ in range(3))))

    got = asyncio.run(scenario())

    assert [r.status_code for r in got] == [202, 202, 202]
    assert world.polls() == 1


def test_after_a_github_failure_the_next_poll_waits_the_interval(world: World) -> None:
    source = source_of(world)
    asked: list[str] = []

    async def failing(device_code: str) -> Any:
        asked.append(device_code)
        raise SourceError("GitHub POST login/oauth/access_token: no reply")

    source.poll_link = failing
    started = world.start(world.noah)
    world.clock.advance(INTERVAL)

    first = world.poll(world.noah, started["link_id"])
    again = world.poll(world.noah, started["link_id"])

    assert first.status_code == 502
    assert (again.status_code, again.json()["state"]) == (202, "pending")
    assert len(asked) == 1


def test_a_link_that_cannot_be_saved_revokes_the_tokens_github_issued(world: World) -> None:
    source = source_of(world)

    async def failing(access_token: str) -> Any:
        raise SourceError("GitHub GET user: no reply")

    source.account = failing
    started = world.start(world.noah)
    world.fake.approve(started["user_code"], "weyenk")
    world.clock.advance(INTERVAL)

    r = world.poll(world.noah, started["link_id"])

    assert r.status_code == 502
    assert len(world.fake.revoked_tokens) == 1
    assert world.poll(world.noah, started["link_id"]).status_code == 404
    assert world.listing(world.noah)["sources"][0]["linked"] is False


def test_a_source_without_a_client_id_is_listed_as_needing_setup(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.sqlite")
    noah = {"Cookie": f"{SESSION_COOKIE}={store.make_session(store.add_user('Noah', 'member').id, 30)}"}
    app = create_app(
        config(tmp_path),
        transport=httpx.MockTransport(ScriptedUpstream()),
        auth_store=store,
        pinnable=local_names,
    )

    got = TestClient(app, client=ELSEWHERE).get("/v1/airlie/chat/repositories", headers=noah).json()

    assert got["sources"] == [
        {
            "name": "github_app",
            "source": "github_app",
            "title": "GitHub",
            "linked": False,
            "login": None,
            "repositories": [],
            "error": None,
            "listed_at": None,
            "status": "needs_setup",
            "fix": NO_CLIENT_ID,
        }
    ]
