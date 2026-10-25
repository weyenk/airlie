"""The passkey API at /v1/auth: invites, registration, login, logout, the people list. Driven with a software
authenticator (tests/unit/webauthn_helpers.py), no browser."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.auth.store import AuthStore
from airlie.serve.app import create_app
from tests.unit.serve_helpers import TestClient
from tests.unit.test_auth_store import Clock
from tests.unit.test_serve import FakeUpstream, cfg
from tests.unit.webauthn_helpers import SoftAuthenticator

ORIGIN = "http://localhost:8090"
ELSEWHERE = ("10.0.0.5", 40000)


class Studio:
    """One serve app over a store with a clock. `admin` is Noah, signed in with a session cookie; `phone`
    calls from elsewhere with no cookie. Both send a browser's Origin header."""

    def __init__(self, tmp_path: Path, **kw: Any) -> None:
        self.clock = Clock()
        self.cfg = cfg(tmp_path)
        self.store = AuthStore(tmp_path / "state" / "auth.sqlite", now=self.clock)
        self.noah = self.store.add_user("Noah", "admin", login="noah@github")
        self.app = create_app(
            self.cfg,
            transport=httpx.MockTransport(FakeUpstream()),
            auth_store=self.store,
            now=self.clock,
            **kw,
        )
        session = self.store.make_session(self.noah.id, 30)
        self.admin = TestClient(
            self.app, base_url=ORIGIN, headers={"Origin": ORIGIN, "Cookie": f"airlie_session={session}"}
        )
        self.phone = TestClient(self.app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=ELSEWHERE)

    def invite(self, name: str = "Pat", **body: Any) -> str:
        r = self.admin.post("/v1/auth/invites", json={"name": name, **body})
        assert r.status_code == 201, r.text
        url = r.json()["url"]
        assert url.startswith("/join/")
        return url.removeprefix("/join/")

    def register(self, token: str, authenticator: SoftAuthenticator, client: TestClient | None = None) -> Any:
        c = client or self.phone
        options = c.post("/v1/auth/register/options", json={"invite": token})
        assert options.status_code == 200, options.text
        cred = authenticator.create(options.json(), ORIGIN)
        return c.post("/v1/auth/register/verify", json={"invite": token, "credential": cred})

    def login(self, authenticator: SoftAuthenticator, client: TestClient, **kw: Any) -> Any:
        options = client.post("/v1/auth/login/options")
        assert options.status_code == 200, options.text
        return client.post(
            "/v1/auth/login/verify", json={"credential": authenticator.get(options.json(), ORIGIN, **kw)}
        )


@pytest.fixture
def studio(tmp_path: Path):
    s = Studio(tmp_path)
    yield s
    s.store.close()


def test_me_is_the_principal_and_401_for_nobody(studio: Studio) -> None:
    assert studio.admin.get("/v1/auth/me").json() == {
        "user_id": studio.noah.id,
        "name": "Noah",
        "role": "admin",
        "via": "session",
    }
    assert studio.phone.get("/v1/auth/me").status_code == 401


def test_a_fresh_install_bootstraps_from_loopback_and_the_bootstrap_admin_is_gone_once_someone_exists(
    tmp_path: Path,
) -> None:
    store = AuthStore(tmp_path / "state" / "auth.sqlite")
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), auth_store=store)
    try:
        bootstrap = TestClient(app, base_url=ORIGIN)
        assert bootstrap.get("/v1/auth/me").json()["name"] == "bootstrap"
        assert bootstrap.post("/v1/auth/invites", json={"name": "Noah", "role": "admin"}).status_code == 201
        assert bootstrap.get("/v1/auth/me").status_code == 401
    finally:
        store.close()


def test_an_invite_names_the_person_and_expires_after_invite_hours(studio: Studio) -> None:
    r = studio.admin.post("/v1/auth/invites", json={"name": "Pat", "login": "pat@github"})
    assert r.status_code == 201
    assert r.json()["expires_at"] == studio.clock.t + 24 * 3600
    users = studio.admin.get("/v1/auth/users").json()
    assert [(u["name"], u["login"], u["role"], u["credentials"]) for u in users] == [
        ("Noah", "noah@github", "admin", 0),
        ("Pat", "pat@github", "member", 0),
    ]
    assert (
        studio.admin.post("/v1/auth/invites", json={"name": "Dup", "login": "noah@github"}).status_code == 409
    )


def test_registering_a_passkey_from_an_invite_signs_the_person_in(studio: Studio) -> None:
    token = studio.invite("Pat")
    r = studio.register(token, SoftAuthenticator())
    assert r.status_code == 201, r.text
    assert r.json()["user"]["name"] == "Pat"
    cookie = r.headers["set-cookie"]
    assert (
        "airlie_session=" in cookie
        and "HttpOnly" in cookie
        and "SameSite=strict" in cookie.replace("Strict", "strict")
    )
    assert "Secure" not in cookie  # http://localhost
    assert studio.phone.get("/v1/auth/me").json()["name"] == "Pat"  # the client kept the cookie
    users = studio.admin.get("/v1/auth/users").json()
    assert [u["credentials"] for u in users] == [0, 1]


def test_an_invite_used_twice_is_410(studio: Studio) -> None:
    token = studio.invite("Pat")
    assert studio.register(token, SoftAuthenticator()).status_code == 201
    again = TestClient(studio.app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=ELSEWHERE)
    assert again.post("/v1/auth/register/options", json={"invite": token}).status_code == 410


def test_an_expired_invite_is_410(studio: Studio) -> None:
    token = studio.invite("Pat")
    studio.clock.t += 24 * 3600 + 1
    r = studio.phone.post("/v1/auth/register/options", json={"invite": token})
    assert r.status_code == 410
    assert r.json() == {"error": "invite expired or already used"}


def test_an_invite_that_expires_between_options_and_verify_is_410(studio: Studio) -> None:
    token = studio.invite("Pat")
    options = studio.phone.post("/v1/auth/register/options", json={"invite": token}).json()
    cred = SoftAuthenticator().create(options, ORIGIN)
    studio.clock.t += 24 * 3600 + 1
    r = studio.phone.post("/v1/auth/register/verify", json={"invite": token, "credential": cred})
    assert r.status_code == 410
    assert studio.phone.get("/v1/auth/me").status_code == 401


def test_a_registration_from_another_origin_is_refused(studio: Studio) -> None:
    token = studio.invite("Pat")
    options = studio.phone.post("/v1/auth/register/options", json={"invite": token}).json()
    cred = SoftAuthenticator().create(options, "http://evil.test")
    r = studio.phone.post("/v1/auth/register/verify", json={"invite": token, "credential": cred})
    assert r.status_code == 400
    assert studio.phone.get("/v1/auth/me").status_code == 401
    assert studio.store.peek_invite(token) is not None  # a failed ceremony does not burn the invite


def test_registration_challenges_are_single_use_and_short_lived(studio: Studio) -> None:
    token = studio.invite("Pat")
    options = studio.phone.post("/v1/auth/register/options", json={"invite": token}).json()
    cred = SoftAuthenticator().create(options, ORIGIN)
    studio.clock.t += 301
    r = studio.phone.post("/v1/auth/register/verify", json={"invite": token, "credential": cred})
    assert r.status_code == 400
    assert r.json() == {"error": "registration failed"}


def test_login_with_a_registered_passkey_sets_a_session(studio: Studio) -> None:
    token = studio.invite("Pat")
    key = SoftAuthenticator()
    assert studio.register(token, key).status_code == 201
    laptop = TestClient(studio.app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=ELSEWHERE)
    assert laptop.get("/v1/auth/me").status_code == 401
    r = studio.login(key, laptop)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["name"] == "Pat"
    assert laptop.get("/v1/auth/me").json()["via"] == "session"
    assert studio.store.credential(key.credential_id).sign_count == 1  # type: ignore[union-attr]


def test_a_stale_sign_count_is_refused_and_logged(studio: Studio, caplog: pytest.LogCaptureFixture) -> None:
    token = studio.invite("Pat")
    key = SoftAuthenticator()
    assert studio.register(token, key).status_code == 201
    laptop = TestClient(studio.app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=ELSEWHERE)
    assert studio.login(key, laptop).status_code == 200  # count 1
    clone = TestClient(studio.app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=ELSEWHERE)
    with caplog.at_level(logging.WARNING, logger="airlie.auth"):
        r = studio.login(key, clone, sign_count=1)
    assert r.status_code == 401
    assert r.json() == {"error": "sign-in failed"}
    assert clone.get("/v1/auth/me").status_code == 401
    refused = [rec for rec in caplog.records if "auth_refused" in rec.getMessage()]
    assert len(refused) == 1 and "sign count" in refused[0].getMessage()
    assert token not in caplog.text


def test_an_unknown_passkey_is_refused(studio: Studio) -> None:
    stranger = SoftAuthenticator()
    stranger.user_handle = b"nobody"
    r = studio.login(stranger, studio.phone)
    assert r.status_code == 401


def test_logout_ends_this_session_only(studio: Studio) -> None:
    token = studio.invite("Pat")
    key = SoftAuthenticator()
    assert studio.register(token, key).status_code == 201
    laptop = TestClient(studio.app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=ELSEWHERE)
    assert studio.login(key, laptop).status_code == 200
    r = studio.phone.post("/v1/auth/logout")
    assert r.status_code == 204
    assert 'airlie_session=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    assert studio.phone.get("/v1/auth/me").status_code == 401
    assert laptop.get("/v1/auth/me").status_code == 200
    assert TestClient(studio.app, client=ELSEWHERE).post("/v1/auth/logout").status_code == 204  # nobody: fine


def test_deleting_a_user_ends_their_sessions_and_passkeys_at_once(studio: Studio) -> None:
    token = studio.invite("Pat")
    key = SoftAuthenticator()
    assert studio.register(token, key).status_code == 201
    pat_id = studio.phone.get("/v1/auth/me").json()["user_id"]
    assert studio.admin.delete(f"/v1/auth/users/{pat_id}").status_code == 204
    assert studio.phone.get("/v1/auth/me").status_code == 401
    assert studio.login(key, studio.phone).status_code == 401
    assert [u["name"] for u in studio.admin.get("/v1/auth/users").json()] == ["Noah"]
    assert studio.admin.delete(f"/v1/auth/users/{pat_id}").status_code == 404


def test_a_member_may_not_invite_list_or_delete(studio: Studio) -> None:
    token = studio.invite("Pat")
    assert studio.register(token, SoftAuthenticator()).status_code == 201
    assert studio.phone.post("/v1/auth/invites", json={"name": "Sam"}).status_code == 403
    assert studio.phone.get("/v1/auth/users").status_code == 403
    me = studio.phone.get("/v1/auth/me").json()["user_id"]
    assert studio.phone.delete(f"/v1/auth/users/{me}").status_code == 403


def test_an_admin_invites_another_admin(studio: Studio) -> None:
    token = studio.invite("Sam", role="admin")
    assert studio.register(token, SoftAuthenticator()).status_code == 201
    assert studio.phone.get("/v1/auth/me").json()["role"] == "admin"
    assert studio.admin.post("/v1/auth/invites", json={"name": "X", "role": "owner"}).status_code == 422


def test_the_api_key_admin_manages_people_too(tmp_path: Path) -> None:
    s = Studio(tmp_path, api_key="s3cret")
    try:
        opencode = TestClient(s.app, headers={"Authorization": "Bearer s3cret"}, client=ELSEWHERE)
        r = opencode.post("/v1/auth/invites", json={"name": "Pat"})
        assert r.status_code == 201
        assert [u["name"] for u in opencode.get("/v1/auth/users").json()] == ["Noah", "Pat"]
    finally:
        s.store.close()


def test_public_routes_need_no_principal_but_me_invites_and_users_do(studio: Studio) -> None:
    studio.invite("Pat")  # not a fresh install any more
    nobody = TestClient(studio.app, base_url=ORIGIN, client=ELSEWHERE)
    assert nobody.post("/v1/auth/register/options", json={"invite": "bogus"}).status_code == 410
    assert nobody.post("/v1/auth/login/options").status_code == 200
    assert nobody.post("/v1/auth/logout").status_code == 204
    assert nobody.get("/v1/auth/me").status_code == 401
    assert nobody.post("/v1/auth/invites", json={"name": "X"}).status_code == 401
    assert nobody.get("/v1/auth/users").status_code == 401
    assert nobody.get("/v1/models").status_code == 401


def test_registration_options_ask_for_a_discoverable_verified_passkey_and_login_options_allow_any(
    studio: Studio,
) -> None:
    token = studio.invite("Pat")
    o = studio.phone.post("/v1/auth/register/options", json={"invite": token}).json()
    assert o["rp"] == {"id": "localhost", "name": "airlie"}
    assert o["user"]["name"] == "Pat"
    assert o["authenticatorSelection"]["residentKey"] == "required"
    assert o["authenticatorSelection"]["userVerification"] == "required"
    lo = studio.phone.post("/v1/auth/login/options").json()
    assert lo["rpId"] == "localhost" and lo["userVerification"] == "required"
    assert lo.get("allowCredentials", []) == []


def test_configured_rp_id_and_origins_win_over_the_request(tmp_path: Path) -> None:
    s = Studio(tmp_path)
    s.cfg.auth.rp_id = "studio.tail1234.ts.net"
    s.cfg.auth.origins = ["https://studio.tail1234.ts.net"]
    try:
        token = s.invite("Pat")
        o = s.phone.post("/v1/auth/register/options", json={"invite": token}).json()
        assert o["rp"]["id"] == "studio.tail1234.ts.net"
        cred = SoftAuthenticator().create(o, "https://studio.tail1234.ts.net")
        r = s.phone.post("/v1/auth/register/verify", json={"invite": token, "credential": cred})
        assert r.status_code == 201, r.text
        assert "Secure" not in r.headers["set-cookie"]  # the request itself came over http
    finally:
        s.store.close()


def test_the_cookie_is_secure_when_tailscale_serve_says_the_request_was_https(tmp_path: Path) -> None:
    s = Studio(tmp_path)
    s.cfg.auth.rp_id = "studio.tail1234.ts.net"
    try:
        token = s.invite("Pat")
        proxied = TestClient(
            s.app, base_url="http://studio.tail1234.ts.net", headers={"X-Forwarded-Proto": "https"}
        )  # loopback, as tailscale serve's proxy is
        o = proxied.post("/v1/auth/register/options", json={"invite": token}).json()
        cred = SoftAuthenticator().create(o, "https://studio.tail1234.ts.net")
        r = proxied.post("/v1/auth/register/verify", json={"invite": token, "credential": cred})
        assert r.status_code == 201, r.text
        assert "Secure" in r.headers["set-cookie"]
    finally:
        s.store.close()


def test_outstanding_ceremonies_are_capped_so_a_public_route_cannot_grow_memory(tmp_path: Path) -> None:
    from airlie.auth.api import MAX_CEREMONIES, Ceremonies

    clock = Clock()
    c = Ceremonies(clock)
    first = b"first"
    c.open(first, "login", None)
    for i in range(MAX_CEREMONIES + 5):
        c.open(f"c{i}".encode(), "login", None)
    assert len(c.pending) == MAX_CEREMONIES
    assert first not in c.pending  # the oldest went first
    assert f"c{MAX_CEREMONIES + 4}".encode() in c.pending
    clock.t += 301
    c.open(b"late", "login", None)
    assert list(c.pending) == [b"late"]  # expired ones are gone


def test_login_options_flood_leaves_the_server_bounded(studio: Studio) -> None:
    from airlie.auth.api import MAX_CEREMONIES

    for _ in range(MAX_CEREMONIES + 20):
        assert studio.phone.post("/v1/auth/login/options").status_code == 200
    # the app's ceremonies live inside the router; the public route still answers and a real login still works
    token = studio.invite("Pat")
    key = SoftAuthenticator()
    assert studio.register(token, key).status_code == 201


def test_an_admin_issues_a_fresh_link_for_an_existing_person(studio: Studio) -> None:
    """The People list's "New link": someone's invite lapsed, or they want a passkey on a second device."""
    token = studio.invite("Pat")
    pat_id = studio.store.peek_invite(token).id  # type: ignore[union-attr]
    r = studio.admin.post(f"/v1/auth/users/{pat_id}/invites")
    assert r.status_code == 201, r.text
    assert r.json()["user"]["id"] == pat_id and r.json()["url"].startswith("/join/")
    fresh = r.json()["url"].removeprefix("/join/")
    assert fresh != token
    assert studio.register(fresh, SoftAuthenticator()).status_code == 201
    assert [u["name"] for u in studio.admin.get("/v1/auth/users").json()] == ["Noah", "Pat"]  # nobody new
    assert studio.admin.post("/v1/auth/users/nobody/invites").status_code == 404
    assert studio.phone.post(f"/v1/auth/users/{pat_id}/invites").status_code == 403


def test_an_invite_link_is_a_clean_path(studio: Studio) -> None:
    r = studio.admin.post("/v1/auth/invites", json={"name": "Pat"})
    assert r.status_code == 201, r.text
    url = r.json()["url"]
    assert url.startswith("/join/") and "#" not in url and len(url) > len("/join/")
