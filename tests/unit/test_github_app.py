"""GitHubApp: the JWT, the installation, a token scoped to the one repository, the bot's identity, and
`story check`'s view of them, against a fake GitHub (httpx.MockTransport) and keys made here. No error ever
holds the token, the JWT or the key."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import jwt
import pytest

from airlie.story import github_app
from airlie.story.github_app import GitHubApp, GitHubAppError, InstallationToken, api_base, repo_slug
from tests.unit.github_helpers import (
    APP_ID,
    BOT,
    INSTALLATION,
    SLUG,
    WRITE,
    FakeGitHub,
    app_spec,
    fake_app,
    key_file,
    pem,
    rsa_key,
)


@pytest.fixture(autouse=True)
def _no_real_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every GitHubApp here runs on a MockTransport. Should one fall back to httpx's real transport, the
    request stops here instead of reaching GitHub."""

    async def refuse(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"a unit test sent a real request: {request.method} {request.url.host}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


def no_secret_in(e: BaseException, github: FakeGitHub, *extra: str) -> None:
    """Neither the message nor the repr of `e` holds a token GitHub minted, a JWT it was sent, a line of the
    key or any of `extra`; and nothing is chained to it (an httpx exception's request carries the header)."""
    text = f"{e} {e!r}"
    key_lines = [line for line in pem().decode().splitlines() if "-----" not in line]
    for secret in [*github.tokens, *github.jwts, *key_lines, *extra]:
        assert secret not in text
    assert e.__cause__ is None and e.__context__ is None


# ---- the remote's repository ----


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:weyenk/OpenHand.git",
        "git@github.com:weyenk/OpenHand",
        "ssh://git@github.com/weyenk/OpenHand.git",
        "ssh://git@github.com/weyenk/OpenHand",
        "https://github.com/weyenk/OpenHand.git",
        "https://github.com/weyenk/OpenHand",
        "https://GitHub.com/weyenk/OpenHand.git",  # a host is any case
    ],
)
def test_repo_slug_reads_the_three_github_forms(url: str) -> None:
    assert repo_slug(url) == "weyenk/OpenHand"


def test_repo_slug_keeps_a_name_with_dots_and_takes_one_git_off() -> None:
    assert repo_slug("git@github.com:o/my.repo.git") == "o/my.repo"
    assert repo_slug("https://github.com/o/a.git.git") == "o/a.git"
    assert repo_slug("https://github.com/o/.github") == "o/.github"


@pytest.mark.parametrize(
    "url",
    [
        "git@gitlab.com:o/r.git",  # another host
        "https://github.com.evil.example/o/r",
        "https://github.com/o/r/",  # not one of the forms
        "https://github.com/o/r/pulls",
        "https://github.com/o",
        "git@github.com:o/..",  # a path GitHub's API would resolve
        "git@github.com:o/.",
        "/srv/git/r.git",  # a local repository
        "file:///srv/git/r.git",
        "https://github.com:443/o/r",
        "ext::sh -c touch% /tmp/x",
        "",
    ],
)
def test_anything_else_is_a_clear_error(url: str) -> None:
    with pytest.raises(GitHubAppError, match="is not a github.com repository URL: the bot publishes to"):
        repo_slug(url)


def test_a_url_with_a_password_is_shown_masked() -> None:
    with pytest.raises(GitHubAppError) as e:
        repo_slug("https://me:hunter2@github.com/o/r.git")
    assert "'https://***@github.com/o/r.git'" in str(e.value) and "hunter2" not in str(e.value)


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("api.github.com", "must be a URL"),
        ("ftp://api.github.com", "must be a URL"),
        ("http://api.github.com", "must be https://"),
        ("https://me:s3cret-pw@api.github.com", "must not carry a user or password"),
        ("http://me:s3cret-pw@127.0.0.1:8080", "must not carry a user or password"),
    ],
)
def test_the_api_url_is_https_or_this_machine_and_carries_no_password(url: str, problem: str) -> None:
    with pytest.raises(GitHubAppError, match=f"^story.github_app.api_url {problem}") as e:
        api_base(url)
    assert "s3cret-pw" not in str(e.value) and e.value.__context__ is None


def test_an_api_url_that_can_be_used_is_kept_as_requests_use_it() -> None:
    assert api_base("https://api.github.com/") == "https://api.github.com"
    assert api_base("HTTPS://API.GITHUB.COM") == "https://api.github.com"
    assert api_base("http://127.0.0.1:8080/api") == "http://127.0.0.1:8080/api"
    assert api_base("http://localhost:9") == "http://localhost:9"


# ---- the JWT ----


def test_the_jwt_is_rs256_signed_with_the_key_for_the_app_for_ten_minutes(tmp_path: Path) -> None:
    app = GitHubApp(app_spec(tmp_path), clock=lambda: 1_800_000_000.7)
    token = app._jwt()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(
        token,
        rsa_key().public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": str(APP_ID)}
    with pytest.raises(jwt.InvalidSignatureError):  # signed by this key and no other
        jwt.decode(token, rsa_key("other").public_key(), algorithms=["RS256"], options={"verify_exp": False})


def test_the_key_is_read_from_its_file_for_every_jwt(tmp_path: Path) -> None:
    app = GitHubApp(app_spec(tmp_path))
    app._jwt()
    app.key_file.write_bytes(pem("other"))  # a key replaced on disk is the one the next JWT uses
    jwt.decode(app._jwt(), rsa_key("other").public_key(), algorithms=["RS256"])


def test_the_key_file_is_expanded_at_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    key_file(tmp_path, "app")
    app = GitHubApp(app_spec(tmp_path, key_file="~/app-key.pem"))
    assert app.key_file == tmp_path / "app-key.pem"
    jwt.decode(app._jwt(), rsa_key().public_key(), algorithms=["RS256"])


@pytest.mark.parametrize(
    ("content", "said"),
    [
        (None, "cannot be read (No such file or directory): put the app's private key there"),
        (
            b"-----BEGIN PRIVATE KEY-----\nc2VjcmV0LWtleS1ieXRlcw==\n-----END PRIVATE KEY-----\n",
            "is not an RSA",
        ),
        (b"secret-key-bytes but not a key at all", "is not an RSA private key in PEM form ("),
    ],
    ids=["missing", "broken pem", "not a key"],
)
def test_a_key_file_that_is_missing_or_not_a_key_is_named_but_not_shown(
    tmp_path: Path, content: bytes | None, said: str
) -> None:
    path = tmp_path / "key.pem"
    if content is not None:
        path.write_bytes(content)
    app = GitHubApp(app_spec(tmp_path, key_file=path))
    with pytest.raises(GitHubAppError) as e:
        app._jwt()
    assert str(e.value).startswith(f"story.github_app.key_file {path} ") and said in str(e.value)
    assert "c2VjcmV0" not in str(e.value) and "secret-key-bytes" not in str(e.value)
    assert e.value.__cause__ is None and e.value.__context__ is None


def test_an_app_without_an_id_cannot_be_made(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="app_id is not set"):
        GitHubApp(app_spec(tmp_path, app_id=None))


# ---- the token flow ----


async def test_a_token_covers_the_one_repository_and_the_bot_is_named(tmp_path: Path) -> None:
    github = FakeGitHub()
    app = fake_app(tmp_path, github)
    token = await app.token("o/r")
    assert token.token == github.tokens[0] and token.permissions == github.permissions
    assert token.expires_at == "2026-10-15T13:00:00Z"
    assert await app.identity("o/r") == BOT
    # as the app: who it is, its installation on o/r, a token for r alone; then the bot, with a token that is
    # revoked once the lookup is done
    assert [(r.method, r.url.path) for r in github.requests] == [
        ("GET", "/api/app"),
        ("GET", "/api/repos/o/r/installation"),
        ("POST", f"/api/app/installations/{INSTALLATION}/access_tokens"),
        ("POST", f"/api/app/installations/{INSTALLATION}/access_tokens"),
        ("GET", f"/api/users/{SLUG}[bot]"),
        ("DELETE", "/api/installation/token"),
    ]
    assert github.minted_for() == [["r"], ["r"]]
    auth = [r.headers["authorization"] for r in github.requests]
    assert auth[:4] == [f"Bearer {j}" for j in github.jwts]
    assert auth[4:] == [f"Bearer {github.tokens[1]}"] * 2 and github.revoked == [github.tokens[1]]
    assert all(r.headers["accept"] == "application/vnd.github+json" for r in github.requests)


async def test_each_token_is_new_and_what_is_not_secret_is_asked_once(tmp_path: Path) -> None:
    github = FakeGitHub()
    app = fake_app(tmp_path, github)
    first, second = await app.token("o/r"), await app.token("o/r")
    assert first.token != second.token and [first.token, second.token] == github.tokens
    await app.identity("o/r")
    await app.identity("o/r")
    paths = github.asked("GET")
    assert paths.count("/api/app") == 1 and paths.count("/api/repos/o/r/installation") == 1
    assert paths.count(f"/api/users/{SLUG}[bot]") == 1


def test_a_token_does_not_show_in_its_repr() -> None:
    token = InstallationToken("ghs_PLANTEDrepr", "2026-10-15T13:00:00Z", {"contents": "write"})
    assert "ghs_PLANTEDrepr" not in repr(token) and "contents" in repr(token)


async def test_an_app_that_is_not_installed_on_the_repository_says_so_with_the_install_link(
    tmp_path: Path,
) -> None:
    github = FakeGitHub(installed=("o/elsewhere",))
    with pytest.raises(GitHubAppError) as e:
        await fake_app(tmp_path, github).token("o/r")
    assert str(e.value) == (
        f"the GitHub App {SLUG} is not installed on o/r: install it on o/r (or on all your repositories) at "
        f"https://github.com/apps/{SLUG}/installations/new"
    )
    no_secret_in(e.value, github)
    assert github.tokens == []


async def test_a_key_that_is_not_the_apps_is_a_401_that_names_the_settings(tmp_path: Path) -> None:
    github = FakeGitHub()
    other = key_file(tmp_path, "other")
    with pytest.raises(GitHubAppError) as e:
        await fake_app(tmp_path, github, key_file=other).token("o/r")
    assert str(e.value) == (
        "GitHub refused the app's JWT (401 A JSON web token could not be decoded): check that "
        f"story.github_app.app_id ({APP_ID}) is the app and story.github_app.key_file ({other}) its "
        "private key"
    )
    sent = [r.headers["authorization"].removeprefix("Bearer ") for r in github.requests]
    no_secret_in(e.value, github, *sent, *pem("other").decode().splitlines()[1:-2])


async def test_an_app_id_that_is_not_the_keys_is_a_401_too(tmp_path: Path) -> None:
    github = FakeGitHub()
    with pytest.raises(GitHubAppError, match=r"^GitHub refused the app's JWT \(401 Integration not found\)"):
        await fake_app(tmp_path, github, app_id=APP_ID + 1).token("o/r")


async def test_a_repository_the_installation_does_not_cover_is_a_422_that_says_so(tmp_path: Path) -> None:
    github = FakeGitHub(installed=("o/r",), covered=("other",))
    with pytest.raises(GitHubAppError) as e:
        await fake_app(tmp_path, github).token("o/r")
    assert str(e.value) == (
        f"the GitHub App {SLUG} cannot get a token for o/r with contents and pull_requests write (422 There "
        "is at least one repository that does not exist or is not accessible to the parent installation.): "
        f"give the app those permissions, and add o/r to its installation at "
        f"https://github.com/apps/{SLUG}/installations/new"
    )
    no_secret_in(e.value, github)


async def test_a_token_is_asked_for_with_contents_and_pull_requests_write_alone(tmp_path: Path) -> None:
    """Whatever more the app holds, its tokens carry only what publishing uses."""
    github = FakeGitHub(permissions={**WRITE, "workflows": "write", "administration": "write"})
    token = await fake_app(tmp_path, github).token("o/r")
    assert github.minted_with() == [{"contents": "write", "pull_requests": "write"}]
    assert dict(token.permissions) == {"contents": "write", "pull_requests": "write", "metadata": "read"}


async def test_an_app_without_a_permission_publishing_uses_cannot_get_a_token(tmp_path: Path) -> None:
    github = FakeGitHub(permissions={"contents": "write", "metadata": "read"})
    with pytest.raises(GitHubAppError) as e:
        await fake_app(tmp_path, github).token("o/r")
    assert "(422 The permissions requested are not granted to this installation.): give the app" in str(
        e.value
    )
    no_secret_in(e.value, github)


async def test_githubs_own_message_is_cut_short_and_never_echoes_the_jwt(tmp_path: Path) -> None:
    def echoes(request: httpx.Request) -> httpx.Response:
        sent = request.headers["authorization"].removeprefix("Bearer ")
        return httpx.Response(403, json={"message": f"no {sent} " + "x" * 500})

    with pytest.raises(GitHubAppError) as e:
        await GitHubApp(app_spec(tmp_path), transport=httpx.MockTransport(echoes)).slug()
    said = str(e.value).removeprefix("GitHub GET app: 403 ")
    assert said.startswith("no *** x") and len(said) == 200 and "eyJ" not in str(e.value)


@pytest.mark.parametrize(
    ("reply", "said"),
    [
        (httpx.Response(200, text="<html>"), "GitHub GET app: 200, but the reply is not a JSON object"),
        (httpx.Response(200, json=["app"]), "GitHub GET app: 200, but the reply is not a JSON object"),
        (
            httpx.Response(200, json={"slug": "No Such/App"}),
            "GitHub GET app: 200, but the reply has no usable slug",
        ),
        (httpx.Response(200, json={}), "GitHub GET app: 200, but the reply has no usable slug"),
        (httpx.Response(502, text="bad gateway"), "GitHub GET app: 502 Bad Gateway"),
    ],
)
async def test_a_reply_that_cannot_be_used_is_named(tmp_path: Path, reply: httpx.Response, said: str) -> None:
    app = GitHubApp(app_spec(tmp_path), transport=httpx.MockTransport(lambda request: reply))
    with pytest.raises(GitHubAppError) as e:
        await app.slug()
    assert str(e.value) == said


async def test_a_token_reply_without_a_token_or_an_installation_without_an_id_is_named(
    tmp_path: Path,
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/app"):
            return httpx.Response(200, json={"slug": SLUG})
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": True} if "/o/bool/" in request.url.path else {"id": 7})
        return httpx.Response(201, json={"permissions": {}})

    app = GitHubApp(app_spec(tmp_path), transport=httpx.MockTransport(answer))
    with pytest.raises(
        GitHubAppError, match="^GitHub GET repos/o/bool/installation: 200, but the reply has no id$"
    ):
        await app.token("o/bool")
    with pytest.raises(
        GitHubAppError, match="^GitHub POST app/installations/7/access_tokens: 201, but .* no token$"
    ):
        await app.token("o/r")


async def test_github_that_cannot_be_reached_is_named_without_a_header(tmp_path: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app = GitHubApp(app_spec(tmp_path), transport=httpx.MockTransport(down))
    with pytest.raises(GitHubAppError) as e:
        await app.token("o/r")
    assert str(e.value) == "GitHub GET app: ConnectError, no reply"
    assert e.value.__cause__ is None and e.value.__context__ is None


async def test_redirects_are_not_followed(tmp_path: Path) -> None:
    hosts: list[str] = []

    def moved(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(301, headers={"location": "https://elsewhere.invalid/app"})

    with pytest.raises(GitHubAppError, match="^GitHub GET app: 301 Moved Permanently$"):
        await GitHubApp(app_spec(tmp_path), transport=httpx.MockTransport(moved)).slug()
    assert hosts == ["127.0.0.1"]  # the JWT went to api_url and nowhere else


async def test_a_bot_user_that_cannot_be_found_is_named(tmp_path: Path) -> None:
    github = FakeGitHub()

    def no_bot(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/users/"):
            return httpx.Response(404, json={"message": "Not Found"})
        return github(request)

    app = GitHubApp(app_spec(tmp_path), transport=httpx.MockTransport(no_bot))
    with pytest.raises(GitHubAppError) as e:
        await app.identity("o/r")
    assert str(e.value) == f"GitHub GET users/{SLUG}%5Bbot%5D: 404 Not Found"
    no_secret_in(e.value, github)
    assert github.revoked == github.tokens and len(github.tokens) == 1  # a lookup that fails revokes it too


async def test_a_lookup_that_gets_no_reply_still_tries_to_revoke_its_token(tmp_path: Path) -> None:
    github = FakeGitHub()
    sent: list[tuple[str, str]] = []

    def lookup_down(request: httpx.Request) -> httpx.Response:
        sent.append((request.method, request.url.path))
        if request.url.path.startswith("/api/users/"):
            raise httpx.ConnectError("connection reset", request=request)
        return github(request)

    app = GitHubApp(app_spec(tmp_path), transport=httpx.MockTransport(lookup_down))
    with pytest.raises(GitHubAppError, match=r"^GitHub GET users/.*: ConnectError, no reply$") as e:
        await app.identity("o/r")
    no_secret_in(e.value, github)
    assert sent[-2:] == [("GET", f"/api/users/{SLUG}[bot]"), ("DELETE", "/api/installation/token")]
    assert github.revoked == github.tokens


async def test_revoking_a_token_sends_the_token_itself_and_nothing_else(tmp_path: Path) -> None:
    github = FakeGitHub()
    app = fake_app(tmp_path, github)
    token = (await app.token("o/r")).token
    before = len(github.requests)
    await app.revoke(token)
    [sent] = github.requests[before:]
    assert (sent.method, sent.url.path) == ("DELETE", "/api/installation/token")
    assert sent.headers["authorization"] == f"Bearer {token}" and github.revoked == [token]


def no_reply(kind: type[httpx.TransportError]) -> Callable[[httpx.Request], httpx.Response]:
    """A GitHub that does not answer: every request fails with `kind`."""

    def fail(request: httpx.Request) -> httpx.Response:
        raise kind("no reply", request=request)

    return fail


@pytest.mark.parametrize(
    "answer",
    [
        lambda request: httpx.Response(401, json={"message": "Bad credentials"}),  # revoked, or expired
        lambda request: httpx.Response(503, text="unavailable"),
        no_reply(httpx.ConnectError),
        no_reply(httpx.ReadTimeout),
    ],
    ids=["401", "503", "no-reply", "timeout"],
)
async def test_a_revocation_github_refuses_or_does_not_answer_is_let_go(
    tmp_path: Path, answer: Callable[[httpx.Request], httpx.Response]
) -> None:
    """The token then lasts out its hour; nothing is raised (a publish step ends with its revocation)."""
    app = GitHubApp(app_spec(tmp_path), transport=httpx.MockTransport(answer))
    assert await app.revoke("ghs_PLANTEDrevoke") is None


async def test_a_revocation_github_holds_on_to_is_let_go_after_revoke_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step revokes its token even while the story is being cancelled (launchd's SIGTERM), and the story
    must still post its cancel note before launchd kills it: a GitHub that never answers is not waited for."""

    async def hangs(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError("not reached")

    assert github_app.REVOKE_S <= 3  # with kill_group's drain and the cancel note, inside launchd's 20 s
    monkeypatch.setattr(github_app, "REVOKE_S", 0.05)
    app = GitHubApp(app_spec(tmp_path), transport=httpx.MockTransport(hangs))
    started = time.monotonic()
    assert await app.revoke("ghs_PLANTEDrevoke") is None
    assert time.monotonic() - started < 2


async def test_revoking_with_an_api_url_that_cannot_be_used_is_let_go_too(tmp_path: Path) -> None:
    app = GitHubApp(app_spec(tmp_path, api_url="http://api.github.com"))  # http:// that is not this machine
    assert await app.revoke("ghs_PLANTEDrevoke") is None


# ---- whether GitHub answers at all (a tick that publishes as you asks before it takes an issue) ----


async def test_ping_asks_githubs_api_for_its_rate_limit_and_sends_no_credential() -> None:
    """GET /rate_limit costs no quota: a beat every few minutes never runs the unauthenticated limit down."""
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"resources": {"core": {"limit": 60, "remaining": 60}}})

    assert await github_app.ping("http://127.0.0.1:9/api", transport=httpx.MockTransport(answer)) is None
    [request] = sent
    assert (request.method, str(request.url)) == ("GET", "http://127.0.0.1:9/api/rate_limit")
    assert "authorization" not in request.headers  # nothing of yours, and nothing of the app's
    assert request.headers["accept"] == "application/vnd.github+json"


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (lambda request: httpx.Response(503, text="unicorn"), "503 Service Unavailable"),
        (lambda request: httpx.Response(500, json={"message": "Server Error"}), "500 Internal Server Error"),
        (
            lambda request: httpx.Response(302, headers={"location": "https://portal.invalid/login"}),
            "302 Found",  # a captive portal, say: not followed, and not GitHub
        ),
        (lambda request: httpx.Response(404, json={"message": "Not Found"}), "404 Not Found"),
        (no_reply(httpx.ConnectError), "ConnectError, no reply"),
        (no_reply(httpx.ConnectTimeout), "ConnectTimeout, no reply"),
    ],
    ids=["503", "500", "redirect", "404", "no-reply", "timeout"],
)
async def test_ping_names_what_github_said_when_it_does_not_answer_as_it_should(
    answer: Callable[[httpx.Request], httpx.Response], said: str
) -> None:
    with pytest.raises(GitHubAppError) as e:
        await github_app.ping("http://127.0.0.1:9/api", transport=httpx.MockTransport(answer))
    assert str(e.value) == f"GitHub GET rate_limit: {said}"
    assert e.value.__cause__ is None and e.value.__context__ is None


async def test_ping_refuses_an_api_url_that_cannot_be_used_before_any_request() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    with pytest.raises(GitHubAppError, match="^story.github_app.api_url must be https://"):
        await github_app.ping("http://api.github.com", transport=httpx.MockTransport(answer))
    assert sent == []


# ---- story check ----


async def test_check_says_who_publishes_when_the_app_can(tmp_path: Path) -> None:
    app = fake_app(tmp_path, FakeGitHub())
    assert await app.check("o/r") == ([], [f"publishing to o/r as {SLUG}[bot]"])


async def test_check_revokes_every_token_it_mints(tmp_path: Path) -> None:
    """One to read what a token is granted, one to look the bot up: neither outlives the check."""
    github = FakeGitHub()
    problems, _ = await fake_app(tmp_path, github).check("o/r")
    assert problems == [] and len(github.tokens) == 2 and github.revoked == github.tokens
    lacking = FakeGitHub(granted={"contents": "read"})  # a check that finds a problem revokes it as well
    problems, _ = await fake_app(tmp_path, lacking).check("o/r")
    assert problems and lacking.tokens and lacking.revoked == lacking.tokens


async def test_check_warns_of_a_key_that_group_or_others_can_read(tmp_path: Path) -> None:
    for mode in (0o640, 0o604):
        app = fake_app(tmp_path, FakeGitHub(), key_file=key_file(tmp_path, mode=mode))
        problems, notes = await app.check("o/r")
        assert problems == [] and notes == [
            f"warning: {app.key_file} is readable by group or others: chmod 600 it",
            f"publishing to o/r as {SLUG}[bot]",
        ]


async def test_check_names_a_missing_key_file_without_asking_github(tmp_path: Path) -> None:
    github = FakeGitHub()
    app = fake_app(tmp_path, github, key_file=tmp_path / "nowhere.pem")
    problems, notes = await app.check("o/r")
    assert problems == [
        f"story.github_app.key_file {tmp_path / 'nowhere.pem'} cannot be read (No such file or directory)"
    ]
    assert notes == [] and github.requests == []


async def test_check_names_what_the_installation_lacks(tmp_path: Path) -> None:
    app = fake_app(tmp_path, FakeGitHub(installed=("o/other",)))
    problems, notes = await app.check("o/r")
    assert len(problems) == 1 and problems[0].startswith(f"the GitHub App {SLUG} is not installed on o/r")
    assert notes == []


@pytest.mark.parametrize(
    ("granted", "lacking"),
    [
        ({"contents": "read", "pull_requests": "write", "metadata": "read"}, "contents"),
        ({"contents": "write", "metadata": "read"}, "pull_requests"),
        ({}, "contents and pull_requests"),
    ],
)
async def test_check_names_the_write_permissions_a_token_came_without(
    tmp_path: Path, granted: dict[str, str], lacking: str
) -> None:
    """Should GitHub mint a token with less than it was asked for, check says what is missing."""
    app = fake_app(tmp_path, FakeGitHub(granted=granted))
    problems, notes = await app.check("o/r")
    have = ", ".join(f"{k}: {v}" for k, v in sorted(granted.items())) or "none"
    assert problems == [
        f"the app's token for o/r lacks write on {lacking} (it has {have}): give the app Contents and Pull "
        "requests write, then approve that on its installation"
    ]
    assert notes == []


async def test_check_says_when_the_app_cannot_get_the_permissions_publishing_uses(tmp_path: Path) -> None:
    app = fake_app(tmp_path, FakeGitHub(permissions={"contents": "write", "metadata": "read"}))
    problems, notes = await app.check("o/r")
    assert len(problems) == 1 and "The permissions requested are not granted" in problems[0] and notes == []


async def test_check_warns_of_what_the_app_holds_beyond_what_publishing_uses(tmp_path: Path) -> None:
    held = {**WRITE, "workflows": "write", "administration": "read"}
    problems, notes = await fake_app(tmp_path, FakeGitHub(permissions=held)).check("o/r")
    assert problems == [] and notes == [
        "warning: the app holds permissions publishing does not use (administration: read, workflows: "
        "write); its tokens are minted with contents and pull_requests write alone, but its key could mint "
        "more",
        f"publishing to o/r as {SLUG}[bot]",
    ]


async def test_check_warns_when_the_installation_reaches_every_repository(tmp_path: Path) -> None:
    problems, notes = await fake_app(tmp_path, FakeGitHub(selection="all")).check("o/r")
    assert problems == [] and notes == [
        "warning: the bot can reach all your repositories; publishing is pinned to o/r",
        f"publishing to o/r as {SLUG}[bot]",
    ]


def test_nothing_in_the_app_merges() -> None:
    names = [*dir(github_app), *dir(GitHubApp)]
    assert not [n for n in names if "merge" in n.lower()]  # only a human merges


def test_the_fake_github_is_what_the_tests_think_it_is() -> None:
    """The fake refuses a JWT signed by another key, so the tests above show the real one is checked."""
    github = FakeGitHub()
    forged = jwt.encode({"iss": str(APP_ID)}, pem("other"), algorithm="RS256")
    r = github(
        httpx.Request("GET", "http://127.0.0.1:9/api/app", headers={"authorization": f"Bearer {forged}"})
    )
    assert r.status_code == 401 and json.loads(r.content)["message"].startswith("A JSON web token")
