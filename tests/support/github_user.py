"""GitHub's user side for tests, as GitHubSource uses it: the device flow, expiring user tokens, the user's
installations and their repositories, and revocation, over an httpx MockTransport handler. Anything else goes
to the FakeGitHub of the app (tests/unit/github_helpers.py). Shapes follow GitHub's docs:

- device flow and refresh:
  https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app
  https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens
  POST /login/device/code and POST /login/oauth/access_token; GitHub answers 200 with {"error": ...} for
  authorization_pending, slow_down (adds 5 s to the interval), expired_token, access_denied,
  device_flow_disabled, incorrect_device_code, bad_refresh_token; an access token lasts 28800 s, a refresh
  token 15897600 s and works once (using it ends it and the access token it came with)
- installations: https://docs.github.com/en/rest/apps/installations
  GET /user/installations and GET /user/installations/{id}/repositories: per_page up to 100, page, a Link
  header with rel="next", total_count, repositories[].permissions.push
- revocation: https://docs.github.com/en/rest/apps/oauth-applications
  DELETE /applications/{client_id}/token with Basic client_id:client_secret, body access_token, 204"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx

from tests.unit.github_helpers import FakeGitHub

ACCESS_S = 28800
REFRESH_S = 15897600
CODE_S = 900
INTERVAL = 5
USER_CODE = "WDJB-MJHT"
VERIFICATION_URI = "https://github.com/login/device"
PAGE_MAX = 100
BAD_CLIENT = "The client_id and/or client_secret passed are incorrect."


class Clock:
    """A clock a test moves."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeGitHubUser:
    """The routes under /oauth (the OAuth host) and /api/user, /api/applications (the API); the rest of /api
    goes to `app`. `calls` keeps (method, path) of every request this fake answered itself, `forms` the form
    bodies sent to the OAuth host, `requests` every request."""

    def __init__(
        self, app: FakeGitHub, *, client_id: str, client_secret: str | None, clock: Callable[[], float]
    ) -> None:
        self.app, self.client_id, self.client_secret, self.clock = app, client_id, client_secret, clock
        self.device_flow = True  # False: the App's settings leave Device Flow off
        self.fail_listing: int | None = None  # a status every GET /user/installations answers with
        self.calls: list[tuple[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.revoked_tokens: list[str] = []
        self._grants: dict[str, dict[int, list[tuple[str, bool]]]] = {}
        self._codes: dict[str, dict[str, Any]] = {}
        self._by_user_code: dict[str, str] = {}
        self._access: dict[str, tuple[str, float]] = {}  # token: (login, expires_at)
        self._refresh: dict[str, tuple[str, float, str]] = {}  # token: (login, expires_at, its access token)
        self._issued = 0

    # --- arrangement ---------------------------------------------------------------------------------

    def grant(self, login: str, installations: dict[int, list[tuple[str, bool]]]) -> None:
        """`login` has these installations, each with (owner/name, push) repositories."""
        self._grants[login] = installations

    def approve(self, user_code: str, login: str) -> None:
        self._codes[self._by_user_code[user_code]]["state"] = login

    def deny(self, user_code: str) -> None:
        self._codes[self._by_user_code[user_code]]["state"] = "denied"

    def revoke_all(self, login: str) -> None:
        """The person removed the App on GitHub: every token of theirs stops working."""
        self._access = {t: v for t, v in self._access.items() if v[0] != login}
        self._refresh = {t: v for t, v in self._refresh.items() if v[0] != login}
        self._grants.pop(login, None)

    def has_access_token(self, token: str) -> bool:
        return self._live(token) is not None

    # --- routing -------------------------------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/oauth/"):
            self.calls.append((request.method, path))
            return self._oauth(request, path.removeprefix("/oauth"))
        api = path.removeprefix("/api")
        if api == "/user" or api.startswith(("/user/", "/applications/")):
            self.calls.append((request.method, path))
            return self._api(request, api)
        return self.app(request)

    # --- the OAuth host ------------------------------------------------------------------------------

    @staticmethod
    def _error(code: str, description: str = "", **more: Any) -> httpx.Response:
        """GitHub answers an OAuth error with 200 and the error in the body."""
        return httpx.Response(200, json={"error": code, "error_description": description, **more})

    def _oauth(self, request: httpx.Request, path: str) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        if form.get("client_id") != self.client_id:
            return self._error("incorrect_client_credentials", BAD_CLIENT)
        if path == "/login/device/code":
            return self._device_code()
        if path == "/login/oauth/access_token":
            grant = form.get("grant_type")
            if grant == "urn:ietf:params:oauth:grant-type:device_code":
                return self._poll(form.get("device_code", ""))
            if grant == "refresh_token":
                return self._refresh_grant(form)
            return self._error(
                "unsupported_grant_type", "The grant type must be a device_code or a refresh_token."
            )
        return httpx.Response(404, json={"message": "Not Found"})

    def _device_code(self) -> httpx.Response:
        if not self.device_flow:
            return self._error("device_flow_disabled", "Device Flow must be explicitly enabled for this App")
        self._issued += 1
        code = f"{self._issued:02d}" + "d3" * 19  # 40 characters
        user_code = USER_CODE if not self._codes else f"{USER_CODE[:5]}{self._issued:03d}"[:9]
        self._codes[code] = {
            "expires": self.clock() + CODE_S,
            "state": "pending",
            "polled": None,
            "interval": INTERVAL,
        }
        self._by_user_code[user_code] = code
        body = {
            "device_code": code,
            "user_code": user_code,
            "verification_uri": VERIFICATION_URI,
            "expires_in": CODE_S,
            "interval": INTERVAL,
        }
        return httpx.Response(200, json=body)

    def _poll(self, device_code: str) -> httpx.Response:
        found = self._codes.get(device_code)
        if found is None:
            return self._error("incorrect_device_code", "The device_code provided is not valid.")
        now = self.clock()
        if now >= found["expires"]:
            return self._error("expired_token", "The device_code has expired.")
        last, found["polled"] = found["polled"], now
        if last is not None and now - last < found["interval"]:  # polled too soon: 5 s more each time
            found["interval"] += 5
            return self._error(
                "slow_down",
                "Too many requests have been made in the same timeframe.",
                interval=found["interval"],
            )
        state = found["state"]
        if state == "pending":
            return self._error("authorization_pending", "The authorization request is still pending.")
        del self._codes[device_code]  # an answered code is spent
        if state == "denied":
            return self._error("access_denied", "The user has denied the request.")
        return httpx.Response(200, json=self._tokens(state))

    def _refresh_grant(self, form: dict[str, str]) -> httpx.Response:
        if self.client_secret is not None and form.get("client_secret") != self.client_secret:
            return self._error("incorrect_client_credentials", BAD_CLIENT)
        found = self._refresh.pop(form.get("refresh_token", ""), None)  # a refresh token works once
        if found is None or self.clock() >= found[1]:
            return self._error("bad_refresh_token", "The refresh token passed is incorrect or expired.")
        self._access.pop(found[2], None)
        return httpx.Response(200, json=self._tokens(found[0]))

    def _tokens(self, login: str) -> dict[str, Any]:
        self._issued += 1
        n, now = self._issued, self.clock()
        access, refresh = f"ghu_{n:04d}" + "A1b2" * 8, f"ghr_{n:04d}" + "C3d4" * 15
        self._access[access] = (login, now + ACCESS_S)
        self._refresh[refresh] = (login, now + REFRESH_S, access)
        return {
            "access_token": access,
            "expires_in": ACCESS_S,
            "refresh_token": refresh,
            "refresh_token_expires_in": REFRESH_S,
            "scope": "",
            "token_type": "bearer",
        }

    # --- the API -------------------------------------------------------------------------------------

    def _live(self, token: str) -> str | None:
        found = self._access.get(token)
        return found[0] if found is not None and self.clock() < found[1] else None

    @staticmethod
    def _bad() -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    def _api(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "DELETE" and path == f"/applications/{self.client_id}/token":
            return self._revoke(request)
        login = self._live(request.headers.get("authorization", "").removeprefix("Bearer "))
        if login is None:
            return self._bad()
        installs = self._grants.get(login, {})
        if request.method != "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        if path == "/user":
            return httpx.Response(200, json={"login": login, "id": 1000 + sum(map(ord, login))})
        if path == "/user/installations":
            if self.fail_listing is not None:
                return httpx.Response(self.fail_listing, json={"message": "Server Error"})
            rows = [
                {"id": i, "account": {"login": login}, "repository_selection": "selected"} for i in installs
            ]
            return self._page(request, rows, "installations")
        found = re.fullmatch(r"/user/installations/(\d+)/repositories", path)
        if found and int(found[1]) in installs:
            rows = [
                {
                    "full_name": repo,
                    "name": repo.split("/")[1],
                    "private": True,
                    "html_url": f"https://github.com/{repo}",
                    "permissions": {"admin": False, "pull": True, "push": push},
                }
                for repo, push in installs[int(found[1])]
            ]
            return self._page(request, rows, "repositories", repository_selection="selected")
        return httpx.Response(404, json={"message": "Not Found"})

    @staticmethod
    def _page(request: httpx.Request, rows: list[dict[str, Any]], key: str, **more: Any) -> httpx.Response:
        """One page of `rows` (per_page up to 100, default 30; page from 1), total_count, and a Link header
        while there is a next page."""
        q = request.url.params
        per, page = min(int(q.get("per_page", 30)), PAGE_MAX), int(q.get("page", 1))
        headers = {}
        if page * per < len(rows):
            nxt = request.url.copy_merge_params({"page": str(page + 1), "per_page": str(per)})
            last = request.url.copy_merge_params({"page": str(-(-len(rows) // per)), "per_page": str(per)})
            headers["Link"] = f'<{nxt}>; rel="next", <{last}>; rel="last"'
        chunk = rows[(page - 1) * per : page * per]
        return httpx.Response(200, headers=headers, json={"total_count": len(rows), **more, key: chunk})

    def _revoke(self, request: httpx.Request) -> httpx.Response:
        want = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        if request.headers.get("authorization", "") != f"Basic {want}":
            return self._bad()
        token = json.loads(request.content or b"{}").get("access_token", "")
        if token not in self._access:
            return httpx.Response(404, json={"message": "Not Found"})
        del self._access[token]
        self.revoked_tokens.append(token)
        return httpx.Response(204)
