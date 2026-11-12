"""GitHubSource: the device flow, refreshing a user token, listing what the App reaches for the person,
revoking the grant and asking whether the App is installed on a repository, against a fake GitHub
(httpx.MockTransport). No error ever holds a token or the client secret."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from airlie.sources.github import GitHubSource
from airlie.sources.protocol import (
    LinkDenied,
    LinkExpired,
    LinkPending,
    SourceError,
    SourceRepo,
    TokenRefused,
    UserTokens,
)
from tests.support.github_user import ACCESS_S, CODE_S, REFRESH_S, Clock, FakeGitHubUser
from tests.unit.github_helpers import FakeGitHub, app_spec

CLIENT_ID, SECRET = "Iv1.testclient", "s3cr3t-client-secret"


@pytest.fixture(autouse=True)
def _no_real_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a unit test sent a real request: {request.method} {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


Made = Callable[..., tuple[GitHubSource, FakeGitHubUser, Clock]]


@pytest.fixture
def made(tmp_path: Path) -> Made:
    def make(
        *, secret: str | None = SECRET, installed: tuple[str, ...] = ("o/r",)
    ) -> tuple[GitHubSource, FakeGitHubUser, Clock]:
        clock = Clock()
        fake = FakeGitHubUser(
            FakeGitHub(installed=installed), client_id=CLIENT_ID, client_secret=secret, clock=clock
        )
        extra: dict[str, object] = {"client_id": CLIENT_ID, "oauth_url": "http://127.0.0.1:9/oauth"}
        if secret is not None:
            file = tmp_path / "client-secret"
            file.write_text(secret + "\n")
            file.chmod(0o600)
            extra["client_secret_file"] = file
        spec = app_spec(tmp_path, **extra)
        source = GitHubSource(
            spec, "https://github.com/o/r.git", transport=httpx.MockTransport(fake), clock=clock
        )
        return source, fake, clock

    return make


async def tokens_for(
    source: GitHubSource, fake: FakeGitHubUser, clock: Clock, login: str = "ada"
) -> UserTokens:
    code = await source.start_link()
    fake.approve(code.user_code, login)
    got = await source.poll_link(code.device_code)
    assert isinstance(got, UserTokens)
    return got


async def test_start_link_asks_for_a_device_code_with_the_client_id(made: Made) -> None:
    source, fake, _ = made()
    code = await source.start_link()
    assert fake.forms == [{"client_id": CLIENT_ID}]
    assert fake.calls == [("POST", "/oauth/login/device/code")]
    assert (code.user_code, code.verification_uri, code.interval, code.expires_in) == (
        "WDJB-MJHT",
        "https://github.com/login/device",
        5,
        CODE_S,
    )
    assert code.device_code not in repr(code)


async def test_poll_is_pending_until_approved_then_gives_both_tokens(made: Made) -> None:
    source, fake, clock = made()
    code = await source.start_link()
    assert await source.poll_link(code.device_code) == LinkPending(5)
    clock.advance(5)
    fake.approve(code.user_code, "ada")
    got = await source.poll_link(code.device_code)
    assert isinstance(got, UserTokens)
    assert got.access_expires_at == clock.now + ACCESS_S
    assert got.refresh_expires_at == clock.now + REFRESH_S
    assert got.refresh_token is not None
    assert fake.forms[-1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert "client_secret" not in fake.forms[-1]
    assert got.access_token not in repr(got) and got.refresh_token not in repr(got)


async def test_slow_down_raises_the_interval(made: Made) -> None:
    source, _, _ = made()
    code = await source.start_link()
    assert await source.poll_link(code.device_code) == LinkPending(5)
    assert await source.poll_link(code.device_code) == LinkPending(10)  # asked again at once: slow_down


async def test_an_expired_code_is_link_expired(made: Made) -> None:
    source, _, clock = made()
    code = await source.start_link()
    clock.advance(CODE_S)
    with pytest.raises(LinkExpired):
        await source.poll_link(code.device_code)
    with pytest.raises(LinkExpired):  # a code GitHub never issued
        await source.poll_link("nope")


async def test_a_declined_code_is_link_denied(made: Made) -> None:
    source, fake, _ = made()
    code = await source.start_link()
    fake.deny(code.user_code)
    with pytest.raises(LinkDenied):
        await source.poll_link(code.device_code)


async def test_device_flow_disabled_names_the_app_setting(made: Made) -> None:
    source, fake, _ = made()
    fake.device_flow = False
    with pytest.raises(SourceError, match="Device Flow") as e:
        await source.start_link()
    assert "settings" in str(e.value)


async def test_refresh_sends_the_client_secret_and_gets_a_new_pair(made: Made) -> None:
    source, fake, clock = made()
    first = await tokens_for(source, fake, clock)
    assert first.refresh_token is not None
    clock.advance(ACCESS_S + 1)
    second = await source.refresh(first.refresh_token)
    assert fake.forms[-1] == {
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "grant_type": "refresh_token",
        "refresh_token": first.refresh_token,
    }
    assert second.access_token != first.access_token
    assert second.access_expires_at == clock.now + ACCESS_S
    assert await source.account(second.access_token) == ("ada", 1000 + sum(map(ord, "ada")))
    with pytest.raises(TokenRefused) as e:  # a refresh token works once
        await source.refresh(first.refresh_token)
    assert SECRET not in str(e.value) and first.refresh_token not in str(e.value)


async def test_refresh_without_a_client_secret_still_gets_a_new_pair(made: Made) -> None:
    source, fake, clock = made(secret=None)
    first = await tokens_for(source, fake, clock)
    assert first.refresh_token is not None
    second = await source.refresh(first.refresh_token)
    assert "client_secret" not in fake.forms[-1]  # a device-flow token needs none (GitHub's refresh docs)
    assert second.access_token != first.access_token


async def test_a_refused_client_secret_names_the_setting_and_not_the_secret(made: Made) -> None:
    source, fake, clock = made()
    first = await tokens_for(source, fake, clock)
    fake.client_secret = "another-secret"
    assert first.refresh_token is not None
    with pytest.raises(SourceError, match="client_secret_file") as e:
        await source.refresh(first.refresh_token)
    assert SECRET not in str(e.value)


async def test_a_client_secret_file_others_can_read_is_refused(made: Made, tmp_path: Path) -> None:
    source, fake, clock = made()
    first = await tokens_for(source, fake, clock)
    (tmp_path / "client-secret").chmod(0o644)
    assert first.refresh_token is not None
    with pytest.raises(SourceError, match="chmod 600") as e:
        await source.refresh(first.refresh_token)
    assert SECRET not in str(e.value)
    before = len(fake.calls)
    await source.revoke(first.access_token)  # best effort: not even tried with a secret others can read
    assert len(fake.calls) == before


async def test_an_error_description_is_masked_and_cut(made: Made) -> None:
    source, fake, _ = made()
    code = await source.start_link()
    fake.describe = f"bad {code.device_code} " + "x" * 500
    fake.client_id = "another-client"  # GitHub then says incorrect_client_credentials, in its own words
    with pytest.raises(SourceError, match="incorrect_client_credentials") as e:
        await source.poll_link(code.device_code)
    assert code.device_code not in str(e.value) and "***" in str(e.value) and len(str(e.value)) < 400


async def test_listing_follows_every_page_of_every_installation(made: Made) -> None:
    source, fake, clock = made()
    first = [(f"o/Repo{i:03d}", i % 2 == 0) for i in range(150)]
    second = [("o/Repo007", True), ("z/last", False), ("a/First", True), ("m/mid", False)]
    fake.grant("ada", {1: first, 2: second})
    got = await tokens_for(source, fake, clock)
    repos = await source.repositories(got.access_token)
    assert len(repos) == 153  # 150 + 4, one of them in both
    assert [r.repo.lower() for r in repos] == sorted(r.repo.lower() for r in repos)
    assert repos[0] == SourceRepo("a/First", True, True, "https://github.com/a/First")
    by_name = {r.repo: r for r in repos}
    assert by_name["z/last"].push is False and by_name["o/Repo001"].push is False
    assert by_name["o/Repo007"].push is True  # pushes through the second installation
    paths = [c[1] for c in fake.calls]
    assert paths.count("/api/user/installations/1/repositories") == 2  # 100 and 50


async def test_a_401_on_listing_is_token_refused(made: Made) -> None:
    source, fake, clock = made()
    got = await tokens_for(source, fake, clock)
    fake.revoke_all("ada")
    with pytest.raises(TokenRefused):
        await source.repositories(got.access_token)
    with pytest.raises(TokenRefused):
        await source.account(got.access_token)


async def test_an_installation_that_answers_403_is_skipped(made: Made) -> None:
    source, fake, clock = made()
    fake.grant("ada", {1: [("o/a", True)], 2: [("o/b", True)], 3: [("o/c", False)]})
    fake.forbidden_installations = {2}
    got = await tokens_for(source, fake, clock)
    assert [r.repo for r in await source.repositories(got.access_token)] == ["o/a", "o/c"]


async def test_a_500_on_listing_is_source_error_without_the_token(made: Made) -> None:
    source, fake, clock = made()
    got = await tokens_for(source, fake, clock)
    fake.fail_listing = 500
    with pytest.raises(SourceError) as e:
        await source.repositories(got.access_token)
    assert not isinstance(e.value, TokenRefused)
    assert "ghu_" not in f"{e.value} {e.value!r}" and e.value.__cause__ is None


async def test_revoke_deletes_the_grant_with_basic_auth_and_never_raises(made: Made) -> None:
    source, fake, clock = made()
    got = await tokens_for(source, fake, clock)
    assert got.refresh_token is not None
    await source.revoke(got.access_token)
    assert fake.revoked_tokens == [got.access_token]
    assert not fake.has_access_token(got.access_token)
    assert fake.calls[-1] == ("DELETE", f"/api/applications/{CLIENT_ID}/grant")
    with pytest.raises(TokenRefused):  # the grant is gone: its refresh token no longer mints
        await source.refresh(got.refresh_token)
    await source.revoke(got.access_token)  # already gone: 422, nothing raised


async def test_revoke_survives_a_500_and_a_missing_secret(made: Made, tmp_path: Path) -> None:
    source, fake, clock = made()
    got = await tokens_for(source, fake, clock)
    broken = GitHubSource(
        app_spec(
            tmp_path,
            client_id=CLIENT_ID,
            oauth_url="http://127.0.0.1:9/oauth",
            client_secret_file=tmp_path / "client-secret",
        ),
        "o/r",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        clock=clock,
    )
    await broken.revoke(got.access_token)
    nosecret, nofake, _ = made(secret=None)
    await nosecret.revoke("ghu_x")
    assert nofake.calls == []


async def test_reaches_is_false_for_a_repository_the_app_is_not_installed_on(made: Made) -> None:
    source, fake, _ = made(installed=("o/r",))
    assert await source.reaches("o/r") is True
    assert await source.reaches("o/r") is True
    assert [r.url.path for r in fake.app.requests].count("/api/repos/o/r/installation") == 1  # cached
    assert await source.reaches("o/other") is False
    assert await source.reaches("o/other") is False  # a "no" is asked again: it may be installed since
    assert [r.url.path for r in fake.app.requests].count("/api/repos/o/other/installation") == 2


async def test_the_publisher_pushes_as_the_apps_bot(made: Made) -> None:
    source, _, _ = made()
    publisher = source.publisher()
    assert publisher.remote == "https://github.com/o/r.git"
    assert publisher.publish == "single" and publisher.app is not None


def test_a_source_needs_an_app_and_a_client_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="client_id"):
        GitHubSource(app_spec(tmp_path), "o/r")
    with pytest.raises(ValueError, match="app_id"):
        GitHubSource(app_spec(tmp_path, app_id=None, client_id=CLIENT_ID), "o/r")


async def test_an_oauth_url_with_http_to_another_host_is_refused(tmp_path: Path) -> None:
    spec = app_spec(tmp_path, client_id=CLIENT_ID, oauth_url="http://github.example.com")
    with pytest.raises(SourceError, match="oauth_url"):
        await GitHubSource(spec, "o/r").start_link()
