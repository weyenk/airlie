"""GitHub as a repository source: the person links their account through the App's user authorization (device
flow: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app),
and what the App is installed on, as far as that person can see it, is their list of repositories
(https://docs.github.com/en/rest/apps/installations). The user's tokens expire after 8 hours and refresh with
a refresh token that works once (.../refreshing-user-access-tokens); revoking deletes the token with the App's
client id and secret (https://docs.github.com/en/rest/apps/oauth-applications).

Like github_app, no error holds a token or the client secret: the Authorization header and the form bodies
stay in memory, GitHub's own message is cut short with them masked, and every error is raised outside the
handler that saw its cause, so none chains an httpx exception."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from airlie.config import GitHubAppSpec
from airlie.sources.protocol import (
    DeviceCode,
    LinkDenied,
    LinkExpired,
    LinkPending,
    SourceError,
    SourceRepo,
    TokenRefused,
    UserTokens,
)
from airlie.story.github_app import REVOKE_S, GitHubApp, GitHubAppError, api_base, request
from airlie.story.publish import Publisher

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
PAGE = 100
"""per_page: GitHub's most."""
MAX_PAGES = 100
"""Pages read per listing: 10 000 repositories; more is cut, not looped on."""
log = logging.getLogger(__name__)
POLL_S = 5
SLOWED_S = 10
_SAID = 200


def _oauth_base(url: str) -> str:
    """story.github_app.oauth_url as requests use it: the same rules as the API's url."""
    problem = ""
    try:
        return api_base(url)
    except GitHubAppError as e:
        problem = str(e).replace("api_url", "oauth_url")
    raise SourceError(problem)


class _Unreachable(SourceError):
    """GitHub answered 403 or 404: this one thing cannot be read (an organization that enforces SAML SSO, an
    installation removed since it was listed)."""


class GitHubSource:
    name = "github_app"
    title = "GitHub"

    def __init__(
        self,
        spec: GitHubAppSpec,
        remote: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if spec.app_id is None:
            raise ValueError("story.github_app.app_id is not set: there is no app to link accounts to")
        if spec.client_id is None:
            raise ValueError("story.github_app.client_id is not set: nobody can link a GitHub account")
        self._spec = spec
        self._client_id = spec.client_id
        self._remote = remote
        self._transport = transport
        self._clock = clock
        self._app = GitHubApp(spec, transport=transport, clock=clock)

    # --- the OAuth host ------------------------------------------------------------------------------

    async def _form(self, path: str, form: dict[str, str]) -> dict[str, Any]:
        """POST `form` to the OAuth host (form-encoded, JSON back). GitHub answers an error with 200 and the
        error in the body; that body is returned for the caller to read. A reply that is not JSON, or no
        reply, is a SourceError."""
        base = _oauth_base(self._spec.oauth_url)
        failure, body = "", None
        try:
            async with httpx.AsyncClient(
                base_url=base + "/",
                headers={"Accept": "application/json"},
                transport=self._transport,
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=False,
            ) as client:
                r = await client.post(path, data=form)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            failure = f"{type(e).__name__}, no reply"
        else:
            try:
                body = r.json()
            except (ValueError, RecursionError):
                body = None
            if not isinstance(body, dict):
                failure = f"{r.status_code}, but the reply is not a JSON object"
        if failure:
            raise SourceError(f"GitHub POST {path}: {failure}")
        assert isinstance(body, dict)
        said = body.get("error_description")
        if isinstance(said, str):  # GitHub's own words, with what we sent masked, are all that may be shown
            for name in ("client_secret", "refresh_token", "device_code"):
                if form.get(name):
                    said = said.replace(form[name], "***")
            body["error_description"] = said
        return body

    @staticmethod
    def _described(body: dict[str, Any]) -> str:
        text = body.get("error_description") or body.get("error") or "no reason given"
        return str(text)[:_SAID]

    def _secret(self) -> str | None:
        """The client secret from its file, or None when none is set or it is empty or missing. A file that is
        a symlink, belongs to someone else or can be read by group or others is a SourceError: it is opened
        once, without following a link, and checked on that descriptor (as the token key is)."""
        file = self._spec.client_secret_file
        if file is None:
            return None
        path = file.expanduser()
        name = "story.github_app.client_secret_file"
        failure = ""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as e:
            if e.errno != errno.ELOOP:
                return None
            failure = f"{name} {path} is a symlink: point the setting at the file"
        else:
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if st.st_mode & 0o077:
                    failure = f"{name} {path} can be read by others: chmod 600 {path}"
                elif st.st_uid != os.geteuid():
                    failure = f"{name} {path} belongs to another user"
                else:
                    return f.read().decode("utf-8", "replace").strip() or None
        raise SourceError(failure)

    def _tokens(self, body: dict[str, Any]) -> UserTokens:
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise SourceError("GitHub's token reply has no access_token")
        refresh = body.get("refresh_token")
        now = self._clock()
        return UserTokens(
            access_token=access,
            access_expires_at=self._expiry(now, body.get("expires_in")),
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            refresh_expires_at=self._expiry(now, body.get("refresh_token_expires_in")),
        )

    @staticmethod
    def _expiry(now: float, seconds: object) -> float | None:
        ok = isinstance(seconds, int) and not isinstance(seconds, bool)
        return now + seconds if ok and isinstance(seconds, int) else None

    async def start_link(self) -> DeviceCode:
        body = await self._form("login/device/code", {"client_id": self._client_id})
        if "error" in body:
            code = body["error"]
            if code == "device_flow_disabled":
                raise SourceError(
                    "Device Flow is off for the GitHub App: enable it under the App's settings "
                    "(Optional features, or General, 'Enable Device Flow')"
                )
            raise SourceError(f"GitHub refused the device code ({code}): {self._described(body)}")
        try:
            return DeviceCode(
                device_code=str(body["device_code"]),
                user_code=str(body["user_code"]),
                verification_uri=str(body["verification_uri"]),
                expires_in=int(body["expires_in"]),
                interval=int(body.get("interval", POLL_S)),
            )
        except (KeyError, TypeError, ValueError):
            raise SourceError("GitHub's device code reply is missing a field") from None

    async def poll_link(self, device_code: str) -> UserTokens | LinkPending:
        body = await self._form(
            "login/oauth/access_token",
            {"client_id": self._client_id, "device_code": device_code, "grant_type": DEVICE_GRANT},
        )
        code = body.get("error")
        if code is None:
            return self._tokens(body)
        interval = body.get("interval")
        known = isinstance(interval, int) and not isinstance(interval, bool)
        if code == "authorization_pending":
            return LinkPending(interval if known and isinstance(interval, int) else POLL_S)
        if code == "slow_down":
            return LinkPending(interval if known and isinstance(interval, int) else SLOWED_S)
        if code in ("expired_token", "incorrect_device_code"):
            raise LinkExpired("the GitHub code ran out or is not one: start linking again")
        if code == "access_denied":
            raise LinkDenied("the GitHub authorization was declined")
        if code == "device_flow_disabled":
            raise SourceError("Device Flow is off for the GitHub App: enable it under the App's settings")
        raise SourceError(f"GitHub refused the authorization ({code}): {self._described(body)}")

    async def refresh(self, refresh_token: str) -> UserTokens:
        """A device-flow token refreshes without the client secret (GitHub's refreshing-user-access-tokens
        docs); it is sent whenever one is configured."""
        secret = self._secret()
        form = {"client_id": self._client_id, "grant_type": "refresh_token", "refresh_token": refresh_token}
        if secret is not None:
            form["client_secret"] = secret
        body = await self._form("login/oauth/access_token", form)
        code = body.get("error")
        if code is None:
            return self._tokens(body)
        if code == "bad_refresh_token":
            raise TokenRefused("GitHub no longer accepts the refresh token: link the account again")
        if code == "incorrect_client_credentials":
            raise SourceError(
                "GitHub refused the client id or secret: check story.github_app.client_secret_file"
            )
        raise SourceError(f"GitHub refused the refresh ({code}): {self._described(body)}")

    # --- the API -------------------------------------------------------------------------------------

    async def _get(self, path: str, token: str, **params: Any) -> httpx.Response:
        """GET `path` as the person (their token in the Authorization header). 401 is TokenRefused, any other
        failure a SourceError with GitHub's message, the token masked."""
        failure = ""
        r: httpx.Response | None = None
        try:
            r = await request(self._spec.api_url, self._transport, "GET", path, token, params=params)
        except GitHubAppError as e:
            failure = str(e)
        if r is None:
            raise SourceError(failure)
        if r.status_code == 401:
            raise TokenRefused("GitHub refused the person's token: link the account again")
        if r.status_code in (403, 404):
            raise _Unreachable(f"GitHub GET {path}: {GitHubApp.said(r, token)}")
        if not r.is_success:
            raise SourceError(f"GitHub GET {path}: {GitHubApp.said(r, token)}")
        return r

    @staticmethod
    def _object(r: httpx.Response, path: str) -> dict[str, Any]:
        try:
            body = r.json()
        except (ValueError, RecursionError):
            body = None
        if not isinstance(body, dict):
            raise SourceError(f"GitHub GET {path}: {r.status_code}, but the reply is not a JSON object")
        return body

    async def _pages(self, path: str, key: str, token: str) -> list[dict[str, Any]]:
        """Every item of `key` on every page of `path`: pages are asked for by number while the Link header
        names a next one (its URL is never followed, so the token goes only to the API's own host)."""
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            r = await self._get(path, token, per_page=PAGE, page=page)
            found = self._object(r, path).get(key)
            if not isinstance(found, list):
                raise SourceError(f"GitHub GET {path}: {r.status_code}, but the reply has no {key}")
            items.extend(i for i in found if isinstance(i, dict))
            if 'rel="next"' not in r.headers.get("link", ""):
                break
        return items

    async def account(self, access_token: str) -> tuple[str, int]:
        r = await self._get("user", access_token)
        body = self._object(r, "user")
        login, uid = body.get("login"), body.get("id")
        if not isinstance(login, str) or not isinstance(uid, int) or isinstance(uid, bool):
            raise SourceError("GitHub GET user: the reply has no login and id")
        return login, uid

    async def repositories(self, access_token: str) -> list[SourceRepo]:
        found: dict[str, SourceRepo] = {}
        for installation in await self._pages("user/installations", "installations", access_token):
            iid = installation.get("id")
            if not isinstance(iid, int) or isinstance(iid, bool):
                continue
            try:
                items = await self._pages(
                    f"user/installations/{iid}/repositories", "repositories", access_token
                )
            except _Unreachable as e:  # one installation the person cannot read does not hide the others
                log.warning("GitHub installation %s skipped: %s", iid, e)
                continue
            for item in items:
                name = item.get("full_name")
                if not isinstance(name, str) or not name:
                    continue
                perms = item.get("permissions")
                push = bool(perms.get("push")) if isinstance(perms, dict) else False
                seen = found.get(name)
                if seen is not None:
                    push = push or seen.push  # reachable through two installations: the better of them
                found[name] = SourceRepo(
                    name, bool(item.get("private")), push, str(item.get("html_url") or "")
                )
        return sorted(found.values(), key=lambda r: r.repo.lower())

    async def revoke(self, access_token: str) -> None:
        """DELETE /applications/{client_id}/grant with Basic client_id:client_secret: the grant ends, every
        token of it with it, the refresh token too. Best effort: nothing is raised when there is no usable
        secret, GitHub refuses or does not answer, or it takes more than REVOKE_S; the tokens are then left to
        expire (8 hours, 6 months)."""
        with contextlib.suppress(SourceError, GitHubAppError, TimeoutError):
            secret = self._secret()
            if secret is None:
                return
            async with asyncio.timeout(REVOKE_S):
                await request(
                    self._spec.api_url,
                    self._transport,
                    "DELETE",
                    f"applications/{self._client_id}/grant",
                    None,
                    auth=(self._client_id, secret),
                    json={"access_token": access_token},
                )

    async def reaches(self, repo: str) -> bool:
        """The App is installed on `repo`. A yes is kept until serve restarts (an uninstall reads yes until
        then); a no is asked again each time, so installing the App takes effect at once."""
        failure = ""
        try:
            return await self._app.installation(repo) is not None
        except GitHubAppError as e:
            failure = str(e)
        raise SourceError(failure)

    def publisher(self) -> Publisher:
        return Publisher(remote=self._remote, app=self._app, publish="single")
