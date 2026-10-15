"""Publishing as a GitHub App's bot (story.github_app; docs/integrations/story.md, Publishing as a bot). The
app signs a short-lived JWT with its private key, finds its installation on the story's repository, and mints
an installation token scoped to that one repository: gh and git publish with it. A token is minted at the
start of each publish step and kept nowhere (not in the state, an event or a file). The bot's identity,
`<slug>[bot]` and its noreply address, authors the layer commits.

An error says what failed and how to fix it, and never holds the token, the JWT or the key: those travel only
in the Authorization header or stay in memory, GitHub's own message is cut short with them masked, and every
error is raised outside the handler that saw its cause, so none chains an httpx exception (whose request
carries the header)."""

from __future__ import annotations

import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import httpx
import jwt

from airlie.config import GitHubAppSpec

_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_API_EXAMPLE = "https://api.github.com"
_REMOTE = re.compile(
    r"(?i:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
    r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]+?)(?:\.git)?"
)
_FORMS = "git@github.com:O/R, ssh://git@github.com/O/R or https://github.com/O/R (.git optional)"
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*", re.ASCII)
_USERINFO = re.compile(r"(?<=://)[^/\s]*@")
_SAID = 200
"""How much of GitHub's own message an error keeps."""


class GitHubAppError(RuntimeError):
    """The app cannot publish: its settings, its key, its installation, or GitHub's answer. The message names
    the fix, never the token, the JWT or the key."""


@dataclass(frozen=True)
class InstallationToken:
    """A token of the app's installation, for one repository; it lasts an hour. Its repr leaves it out."""

    token: str = field(repr=False)
    expires_at: str
    permissions: Mapping[str, str]


class BotIdentity(NamedTuple):
    """Who the bot is in a commit: `<slug>[bot]` and `<id>+<slug>[bot]@users.noreply.github.com`, the address
    GitHub links to the bot's account."""

    name: str
    email: str


def repo_slug(remote_url: str) -> str:
    """The "owner/name" of a github.com remote: git@github.com:o/r, ssh://git@github.com/o/r or
    https://github.com/o/r, each with or without .git. Anything else is a GitHubAppError that shows the URL
    with any user and password masked."""
    m = _REMOTE.fullmatch(remote_url.strip())
    if m is None or m[2] in (".", ".."):
        shown = _USERINFO.sub("***@", remote_url)
        raise GitHubAppError(f"{shown!r} is not a github.com repository URL: the bot publishes to {_FORMS}")
    return f"{m[1]}/{m[2]}"


def api_base(url: str) -> str:
    """story.github_app.api_url as requests use it, or a GitHubAppError that never quotes it (it may carry a
    user and password). The JWT and the tokens go with every request, so it is https://, or http:// to this
    machine (tests), with no user or password of its own: httpx would send those instead."""
    try:
        parsed: httpx.URL | None = httpx.URL(url)  # parsed as a request will parse it
    except httpx.InvalidURL:
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.host:
        problem = f"must be a URL, like {_API_EXAMPLE}"
    elif parsed.scheme == "http" and parsed.host not in _LOOPBACK:
        problem = f"must be https:// (http:// is only for this machine), like {_API_EXAMPLE}"
    elif parsed.userinfo:
        problem = "must not carry a user or password; the app signs in with its key"
    else:
        return str(parsed).rstrip("/")
    raise GitHubAppError(f"story.github_app.api_url {problem}")  # outside the handler: nothing is chained


class GitHubApp:
    """The app of story.github_app, over GitHub's REST API (`transport` stands in for GitHub in tests). It
    keeps what is not secret (the app's slug, its installation per repository, the bot's identity) for the
    run; never a token, and never the key, which is read from its file for every JWT."""

    def __init__(
        self,
        spec: GitHubAppSpec,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if spec.app_id is None:
            raise ValueError("story.github_app.app_id is not set: there is no app to publish as")
        self.app_id = spec.app_id
        self.key_file = spec.key_file.expanduser()
        self.api_url = spec.api_url
        self._transport = transport
        self._clock = clock
        self._slug: str | None = None
        self._installations: dict[str, int] = {}
        self._identity: BotIdentity | None = None

    def _jwt(self) -> str:
        """The app's JWT, signed with the key read from its file now: issued a minute ago (clocks drift) and
        valid for ten minutes in all, as GitHub allows."""
        try:
            key = self.key_file.read_bytes()
        except OSError as e:
            failure = f"cannot be read ({e.strerror or type(e).__name__}): put the app's private key there"
        else:
            now = int(self._clock())
            claims = {"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}
            try:
                return jwt.encode(claims, key, algorithm="RS256")
            except Exception as e:  # noqa: BLE001 - whatever the library says about the key: it is not one
                failure = f"is not an RSA private key in PEM form ({type(e).__name__})"
        raise GitHubAppError(f"story.github_app.key_file {self.key_file} {failure}")

    async def _call(self, method: str, path: str, bearer: str, **kw: Any) -> httpx.Response:
        """One request on a short-lived client, with `bearer` (the JWT, or a token) in the Authorization
        header. Redirects are not followed: the header stays with api_url. GitHubAppError when GitHub cannot
        be reached; it names the method and path, never a header."""
        base = api_base(self.api_url)
        failure = ""
        try:
            async with httpx.AsyncClient(
                base_url=base + "/",
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                transport=self._transport,
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=False,
            ) as client:
                r = await client.request(method, path, **kw)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            failure = f"{type(e).__name__}, no reply"
        if failure:
            raise GitHubAppError(f"GitHub {method} {path}: {failure}")
        return r

    @staticmethod
    def _said(r: httpx.Response, bearer: str) -> str:
        """The status and GitHub's message (cut short, with `bearer` masked), else the reason phrase."""
        try:
            body = r.json()
        except (ValueError, RecursionError):  # not JSON, or nested deeper than the parser reads
            body = None
        said = body.get("message") if isinstance(body, dict) else None
        text = str(said).replace(bearer, "***")[:_SAID] if said else r.reason_phrase
        return f"{r.status_code} {text}"

    def _refused(self, r: httpx.Response, method: str, path: str, bearer: str) -> GitHubAppError:
        """A reply to the app's JWT that is not a success: a 401 is the app's id or its key."""
        if r.status_code == 401:
            return GitHubAppError(
                f"GitHub refused the app's JWT ({self._said(r, bearer)}): check that story.github_app.app_id "
                f"({self.app_id}) is the app and story.github_app.key_file ({self.key_file}) its private key"
            )
        return GitHubAppError(f"GitHub {method} {path}: {self._said(r, bearer)}")

    @staticmethod
    def _json(r: httpx.Response, method: str, path: str) -> dict[str, Any]:
        """The reply's JSON object, or a GitHubAppError."""
        try:
            body = r.json()
        except (ValueError, RecursionError):
            body = None
        if isinstance(body, dict):
            return body
        raise GitHubAppError(f"GitHub {method} {path}: {r.status_code}, but the reply is not a JSON object")

    @staticmethod
    def _id(body: dict[str, Any]) -> int | None:
        found = body.get("id")
        return found if isinstance(found, int) and not isinstance(found, bool) else None

    async def slug(self) -> str:
        """The app's slug (GET /app, as the app): its bot is `<slug>[bot]`, and its install link names it."""
        if self._slug is None:
            bearer = self._jwt()
            r = await self._call("GET", "app", bearer)
            if not r.is_success:
                raise self._refused(r, "GET", "app", bearer)
            slug = self._json(r, "GET", "app").get("slug")
            if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
                raise GitHubAppError(f"GitHub GET app: {r.status_code}, but the reply has no usable slug")
            self._slug = slug
        return self._slug

    async def _installation(self, repo: str) -> int:
        """The id of the app's installation on `repo` (GET /repos/{owner}/{name}/installation, as the app)."""
        if repo not in self._installations:
            slug = await self.slug()
            path, bearer = f"repos/{repo}/installation", self._jwt()
            r = await self._call("GET", path, bearer)
            if r.status_code == 404:
                raise GitHubAppError(
                    f"the GitHub App {slug} is not installed on {repo}: install it on that repository "
                    f"(only that one) at https://github.com/apps/{slug}/installations/new"
                )
            if not r.is_success:
                raise self._refused(r, "GET", path, bearer)
            found = self._id(self._json(r, "GET", path))
            if found is None:
                raise GitHubAppError(f"GitHub GET {path}: {r.status_code}, but the reply has no id")
            self._installations[repo] = found
        return self._installations[repo]

    async def token(self, repo: str) -> InstallationToken:
        """A token of the app's installation for `repo` ("owner/name") and no other repository (POST
        /app/installations/{id}/access_tokens with that one repository, as the app). It lasts an hour."""
        installation = await self._installation(repo)
        slug = await self.slug()
        path, bearer = f"app/installations/{installation}/access_tokens", self._jwt()
        r = await self._call("POST", path, bearer, json={"repositories": [repo.split("/", 1)[1]]})
        if r.status_code == 422:
            raise GitHubAppError(
                f"the installation of the GitHub App {slug} does not cover {repo} ({self._said(r, bearer)}): "
                f"add {repo} to it at https://github.com/apps/{slug}/installations/new"
            )
        if not r.is_success:
            raise self._refused(r, "POST", path, bearer)
        body = self._json(r, "POST", path)
        token, permissions = body.get("token"), body.get("permissions")
        if not isinstance(token, str) or not token:
            raise GitHubAppError(f"GitHub POST {path}: {r.status_code}, but the reply has no token")
        granted = {str(k): str(v) for k, v in permissions.items()} if isinstance(permissions, dict) else {}
        return InstallationToken(token, str(body.get("expires_at") or ""), granted)

    async def identity(self, repo: str) -> BotIdentity:
        """Who the bot is (GET /users/<slug>[bot], with a token for `repo`): its name and its noreply address,
        which GitHub links to the bot's account, so a commit with them shows as the bot's."""
        if self._identity is None:
            slug = await self.slug()
            path, bearer = f"users/{slug}%5Bbot%5D", (await self.token(repo)).token
            r = await self._call("GET", path, bearer)
            if not r.is_success:
                raise GitHubAppError(f"GitHub GET {path}: {self._said(r, bearer)}")
            uid = self._id(self._json(r, "GET", path))
            if uid is None:
                raise GitHubAppError(f"GitHub GET {path}: {r.status_code}, but the reply has no id")
            name = f"{slug}[bot]"
            self._identity = BotIdentity(name, f"{uid}+{name}@users.noreply.github.com")
        return self._identity

    async def check(self, repo: str) -> tuple[list[str], list[str]]:
        """For `task story:check`: the problems that would stop the bot publishing to `repo` (the key file,
        the installation, a token with contents and pull_requests write) and the notes (a key file that group
        or others can read; who publishes)."""
        notes: list[str] = []
        try:
            mode = self.key_file.stat().st_mode
        except OSError as e:
            why = e.strerror or type(e).__name__
            return [f"story.github_app.key_file {self.key_file} cannot be read ({why})"], notes
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            notes.append(f"warning: {self.key_file} is readable by group or others: chmod 600 it")
        try:
            granted = (await self.token(repo)).permissions
            bot = await self.identity(repo)
        except GitHubAppError as e:
            return [str(e)], notes
        lacking = [p for p in ("contents", "pull_requests") if granted.get(p) != "write"]
        if lacking:
            have = ", ".join(f"{k}: {v}" for k, v in sorted(granted.items())) or "none"
            return [
                f"the app's token for {repo} lacks write on {' and '.join(lacking)} (it has {have}): give "
                "the app Contents and Pull requests write, then approve that on its installation"
            ], notes
        notes.append(f"publishing to {repo} as {bot.name}")
        return [], notes
