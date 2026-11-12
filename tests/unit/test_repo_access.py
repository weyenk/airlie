"""RepoAccess: a person's repositories from their linked sources over FakeGitHubUser, an AuthStore, a real
vault in tmp_path and a fake clock."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from airlie.auth.store import AuthStore
from airlie.sources.access import ENDED, REFRESH_MARGIN_S, RepoAccess
from airlie.sources.github import GitHubSource
from airlie.sources.protocol import UserTokens
from airlie.sources.vault import TokenVault
from tests.support.github_user import ACCESS_S, Clock, FakeGitHubUser
from tests.unit.github_helpers import FakeGitHub, app_spec

CLIENT_ID, SECRET = "Iv1.testclient", "s3cr3t-client-secret"
LOCAL = ["o/r", "local/only", "Local/Two"]
INSTALLATIONS = "/api/user/installations"


async def local_names() -> list[str]:
    return list(LOCAL)


@pytest.fixture(autouse=True)
def _no_real_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a unit test sent a real request: {request.method} {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@dataclass
class World:
    access: RepoAccess
    fake: FakeGitHubUser
    clock: Clock
    store: AuthStore
    source: GitHubSource
    tmp: Path

    def listings(self) -> int:
        return sum(1 for _, p in self.fake.calls if p == INSTALLATIONS)

    def refreshes(self) -> int:
        return sum(1 for f in self.fake.forms if f.get("grant_type") == "refresh_token")

    async def link(self, login: str, user_id: str = "u-noah") -> UserTokens:
        code = await self.source.start_link()
        self.fake.approve(code.user_code, login)
        self.clock.advance(5)
        got = await self.source.poll_link(code.device_code)
        assert isinstance(got, UserTokens)
        await self.access.save(user_id, "github_app", got)
        return got


@pytest.fixture
def world(tmp_path: Path) -> Callable[..., World]:
    def make(*, sources: bool = True, cache_s: float = 300) -> World:
        clock = Clock()
        fake = FakeGitHubUser(
            FakeGitHub(installed=("o/r", "weyenk/airlie")),
            client_id=CLIENT_ID,
            client_secret=SECRET,
            clock=clock,
        )
        fake.grant("noah", {1: [("weyenk/airlie", True), ("o/r", True)]})
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
        store = AuthStore(tmp_path / "state" / "auth.sqlite", now=clock)
        vault = TokenVault(tmp_path / "keys" / "tokens.key")

        access = RepoAccess(
            store, vault, {"github_app": source} if sources else {}, local_names, cache_s=cache_s, clock=clock
        )
        return World(access, fake, clock, store, source, tmp_path)

    return make


async def test_an_unlinked_person_sees_connect_and_their_extras(world: Callable[..., World]) -> None:
    w = world()
    w.store.pin("u-noah", "local/only")
    w.store.pin("u-noah", "o/r")  # the App reaches it: not an extra
    got = await w.access.view("u-noah")
    (src,) = got.sources
    assert (src.name, src.title, src.linked, src.login, src.repositories, src.error) == (
        "github_app",
        "GitHub",
        False,
        None,
        (),
        None,
    )
    assert got.extras == ("local/only",)
    assert got.extra_candidates == ("local/only", "Local/Two")


async def test_two_people_see_their_own_repositories(world: Callable[..., World]) -> None:
    w = world()
    await w.link("noah")
    await w.link("ashley", "u-ashley")
    noah = await w.access.view("u-noah")
    assert [r.repo for r in noah.sources[0].repositories] == ["o/r", "weyenk/airlie"]
    assert noah.sources[0].login == "noah"
    assert await w.access.access("u-noah", "weyenk/airlie") == "source"
    assert await w.access.access("u-ashley", "o/r") == "source"
    assert await w.access.access("u-ashley", "weyenk/airlie") is None


async def test_the_listing_is_cached_until_refresh(world: Callable[..., World]) -> None:
    w = world()
    await w.link("noah")
    await w.access.view("u-noah")
    await w.access.view("u-noah")
    assert w.listings() == 1
    await w.access.view("u-noah", refresh=True)
    assert w.listings() == 2
    w.clock.advance(301)
    await w.access.view("u-noah")
    assert w.listings() == 3


async def test_a_token_near_expiry_is_refreshed_first(world: Callable[..., World]) -> None:
    w = world()
    first = await w.link("noah")
    assert first.access_expires_at is not None
    w.clock.now = first.access_expires_at - (REFRESH_MARGIN_S - 1)
    got = await w.access.view("u-noah")
    assert w.refreshes() == 1
    assert got.sources[0].linked
    link = w.store.link("u-noah", "github_app")
    assert link is not None and link.access_expires_at == w.clock.now + ACCESS_S
    await w.access.view("u-noah", refresh=True)
    assert w.refreshes() == 1  # the new token is good for hours


async def test_a_token_with_margin_left_is_not_refreshed(world: Callable[..., World]) -> None:
    w = world()
    first = await w.link("noah")
    assert first.access_expires_at is not None
    w.clock.now = first.access_expires_at - REFRESH_MARGIN_S - 1
    await w.access.view("u-noah")
    assert w.refreshes() == 0


async def test_concurrent_views_refresh_the_token_once(world: Callable[..., World]) -> None:
    w = world()
    first = await w.link("noah")
    assert first.access_expires_at is not None
    w.clock.now = first.access_expires_at - 10
    real = w.source.refresh

    async def slow(token: str) -> UserTokens:
        await asyncio.sleep(0)  # the network: other tasks run while the refresh is out
        return await real(token)

    w.source.refresh = slow  # type: ignore[method-assign]
    one, two, three = await asyncio.gather(
        w.access.view("u-noah"), w.access.access("u-noah", "o/r", fresh=True), w.access.view("u-noah")
    )
    assert w.refreshes() == 1
    assert two == "source" and one.sources[0].linked and three.sources[0].linked


async def test_a_refused_token_drops_the_link_with_the_reason(world: Callable[..., World]) -> None:
    w = world()
    await w.link("noah")
    w.fake.revoke_all("noah")
    got = await w.access.view("u-noah", refresh=True)
    assert got.sources[0].linked is False
    assert got.sources[0].error == ENDED == "GitHub ended airlie's access: connect again"
    assert w.store.link("u-noah", "github_app") is None
    again = await w.access.view("u-noah")
    assert again.sources[0].error == ENDED  # the reason stays until they connect again


async def test_a_vault_that_cannot_open_the_tokens_reads_as_unlinked(
    world: Callable[..., World], caplog: pytest.LogCaptureFixture
) -> None:
    w = world()
    first = await w.link("noah")
    key = w.tmp / "keys" / "tokens.key"
    key.write_bytes(b"k" * 32)  # the key file was replaced; a restarted serve reads the new one
    restarted = RepoAccess(w.store, TokenVault(key), {"github_app": w.source}, local_names, cache_s=300)
    with caplog.at_level(logging.WARNING, logger="airlie.sources"):
        got = await restarted.view("u-noah")
    assert got.sources[0].linked is False and got.sources[0].error == ENDED
    assert w.store.link("u-noah", "github_app") is None
    assert "dropped" in caplog.text and first.access_token not in caplog.text


async def test_a_listing_failure_keeps_the_last_good_list(world: Callable[..., World]) -> None:
    w = world()
    await w.link("noah")
    await w.access.view("u-noah")
    w.fake.fail_listing = 500
    got = await w.access.view("u-noah", refresh=True)
    src = got.sources[0]
    assert src.linked and src.error and [r.repo for r in src.repositories] == ["o/r", "weyenk/airlie"]
    assert w.store.link("u-noah", "github_app") is not None


async def test_a_repository_the_app_reaches_is_never_an_extra_candidate(world: Callable[..., World]) -> None:
    w = world()
    got = await w.access.view("u-noah")
    assert "o/r" not in got.extra_candidates
    assert await w.access.access("u-noah", "o/r") is None  # unlinked: no access, not even as an extra


async def test_an_old_pin_of_a_github_repository_is_not_an_extra(world: Callable[..., World]) -> None:
    w = world()
    w.store.pin("u-noah", "O/R")
    w.store.pin("u-noah", "Local/Only")
    assert (await w.access.view("u-noah")).extras == ("Local/Only",)
    assert await w.access.access("u-noah", "o/r") is None
    assert await w.access.access("u-noah", "local/ONLY") == "extra"


async def test_may_push_needs_the_persons_push_permission(world: Callable[..., World]) -> None:
    w = world()
    await w.link("noah")
    await w.link("ashley", "u-ashley")
    assert await w.access.may_push("u-noah", "o/r") is None
    assert (
        await w.access.may_push("u-ashley", "o/r")
        == "you cannot push to o/r on GitHub, so airlie will not push for you"
    )
    w.store.pin("u-noah", "local/only")
    assert (
        await w.access.may_push("u-noah", "local/only")
        == "local/only is a local checkout: airlie does not push it"
    )
    assert (await w.access.may_push("u-ashley", "weyenk/airlie") or "").startswith(
        "you have no access to weyenk/airlie: connect GitHub"
    )


async def test_forget_revokes_drops_and_clears_the_cache(world: Callable[..., World]) -> None:
    w = world()
    got = await w.link("noah")
    await w.access.view("u-noah")
    assert await w.access.forget("u-noah", "github_app") is True
    assert w.fake.revoked_tokens == [got.access_token]
    assert w.store.link("u-noah", "github_app") is None
    after = await w.access.view("u-noah")
    assert after.sources[0].linked is False and after.sources[0].error is None
    assert await w.access.forget("u-noah", "github_app") is False


async def test_the_stored_tokens_are_sealed(world: Callable[..., World]) -> None:
    w = world()
    got = await w.link("noah")
    link = w.store.link("u-noah", "github_app")
    assert link is not None and got.access_token.encode() not in link.access_token


async def test_with_no_source_every_local_name_is_source(world: Callable[..., World]) -> None:
    w = world(sources=False)
    assert await w.access.access("u-noah", "LOCAL/only") == "source"
    assert await w.access.access("u-noah", "elsewhere/x") is None
    assert await w.access.may_push("u-noah", "local/only") is None
    got = await w.access.view("u-noah")
    assert got.sources == () and got.extra_candidates == tuple(LOCAL)


async def test_a_failed_live_listing_never_grants_push_or_fresh_access(world: Callable[..., World]) -> None:
    w = world()
    await w.link("noah")
    await w.access.view("u-noah")
    w.fake.fail_listing = 500
    refusal = await w.access.may_push("u-noah", "o/r")
    assert refusal == "airlie could not check your access to o/r on GitHub: try again"
    assert await w.access.access("u-noah", "o/r", fresh=True) is None
    assert await w.access.access("u-noah", "o/r") == "source"  # a turn may use the last good list for now


async def test_a_list_kept_through_failures_ages_out(world: Callable[..., World]) -> None:
    w = world()
    await w.link("noah")
    await w.access.view("u-noah")
    w.fake.fail_listing = 500
    w.clock.advance(301)
    assert await w.access.access("u-noah", "o/r") == "source"
    w.clock.advance(3 * 300)
    got = await w.access.view("u-noah")
    assert got.sources[0].repositories == () and got.sources[0].error
    assert await w.access.access("u-noah", "o/r") is None


async def test_forget_during_a_slow_refresh_leaves_no_link(world: Callable[..., World]) -> None:
    w = world()
    first = await w.link("noah")
    assert first.access_expires_at is not None
    w.clock.now = first.access_expires_at - 10
    real, entered, release = w.source.refresh, asyncio.Event(), asyncio.Event()

    async def slow(token: str) -> UserTokens:
        entered.set()
        await release.wait()
        return await real(token)

    async def no_revoke(token: str) -> None:  # an App with no client secret cannot revoke: the tokens live on
        return None

    w.source.refresh = slow  # type: ignore[method-assign]
    w.source.revoke = no_revoke  # type: ignore[method-assign]
    viewing = asyncio.create_task(w.access.view("u-noah"))
    await entered.wait()
    forgetting = asyncio.create_task(w.access.forget("u-noah", "github_app"))
    await asyncio.sleep(0.05)  # an unlocked forget would be done by now
    release.set()
    await asyncio.gather(viewing, forgetting)
    assert w.store.link("u-noah", "github_app") is None
    assert await w.access.access("u-noah", "o/r") is None
