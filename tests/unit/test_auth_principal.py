"""Who a request is: the Bearer key, then the Tailscale identity header (only on requests `tailscale serve`
proxies from loopback), then the session cookie. serve's /v1 routes refuse a request that is nobody."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from airlie.auth.principal import NotInvited, Principal, Resolver
from airlie.auth.store import AuthStore
from airlie.config.schema import AuthSpec
from airlie.serve.app import create_app
from tests.unit.test_serve import FakeUpstream, cfg

LOOPBACK = ("127.0.0.1", 40000)
ELSEWHERE = ("10.0.0.5", 40000)


@pytest.fixture
def store(tmp_path: Path):
    s = AuthStore(tmp_path / "auth.sqlite")
    yield s
    s.close()


def probe(resolver: Resolver, client: tuple[str, int], **kw: object) -> httpx.Response:
    """One request through `resolver` from `client`; the body is the principal (or null)."""
    app = FastAPI()

    @app.get("/who")
    async def who(request: Request) -> dict[str, object] | None:
        try:
            p = await resolver.resolve(request)
        except NotInvited as e:
            return {"not_invited": e.login}
        return None if p is None else {"user_id": p.user_id, "name": p.name, "role": p.role, "via": p.via}

    return TestClient(app, client=client).get("/who", **kw)  # type: ignore[arg-type]


def test_the_bearer_key_is_the_admin_principal_named_api_key(store: AuthStore) -> None:
    r = Resolver(store, AuthSpec(), api_key="s3cret")
    assert probe(r, ELSEWHERE, headers={"Authorization": "Bearer s3cret"}).json() == {
        "user_id": "api-key",
        "name": "api-key",
        "role": "admin",
        "via": "api-key",
    }
    assert probe(r, ELSEWHERE, headers={"Authorization": "Bearer wrong"}).json() is None
    assert probe(r, ELSEWHERE).json() is None


def test_a_tailscale_login_on_loopback_is_its_user(store: AuthStore) -> None:
    u = store.add_user("Noah", "admin", login="noah@github")
    r = Resolver(store, AuthSpec(tailscale=True), api_key=None)
    got = probe(r, LOOPBACK, headers={"Tailscale-User-Login": "noah@github"}).json()
    assert got == {"user_id": u.id, "name": "Noah", "role": "admin", "via": "tailscale"}


def test_tailscale_header_is_ignored_off_loopback(store: AuthStore) -> None:
    store.add_user("Noah", "admin", login="noah@github")
    r = Resolver(store, AuthSpec(tailscale=True), api_key=None)
    assert probe(r, ELSEWHERE, headers={"Tailscale-User-Login": "noah@github"}).json() is None


def test_tailscale_header_is_ignored_when_auth_tailscale_is_off(store: AuthStore) -> None:
    store.add_user("Noah", "admin", login="noah@github")
    r = Resolver(store, AuthSpec(tailscale=False), api_key=None)
    assert probe(r, LOOPBACK, headers={"Tailscale-User-Login": "noah@github"}).json() is None


def test_an_unknown_tailscale_login_is_not_invited(store: AuthStore) -> None:
    store.add_user("Noah", "admin", login="noah@github")  # so the install is not fresh
    r = Resolver(store, AuthSpec(tailscale=True), api_key=None)
    got = probe(r, LOOPBACK, headers={"Tailscale-User-Login": "stranger@github"}).json()
    assert got == {"not_invited": "stranger@github"}


def test_a_session_cookie_is_its_user_until_the_user_is_deleted(store: AuthStore) -> None:
    u = store.add_user("Pat", "member")
    token = store.make_session(u.id, days=30)
    r = Resolver(store, AuthSpec(), api_key=None)
    got = probe(r, ELSEWHERE, headers={"Cookie": f"airlie_session={token}"}).json()
    assert got == {"user_id": u.id, "name": "Pat", "role": "member", "via": "session"}
    assert probe(r, ELSEWHERE, headers={"Cookie": "airlie_session=forged"}).json() is None
    store.delete_user(u.id)
    assert probe(r, ELSEWHERE, headers={"Cookie": f"airlie_session={token}"}).json() is None


def test_the_bearer_key_wins_over_a_cookie(store: AuthStore) -> None:
    u = store.add_user("Pat", "member")
    token = store.make_session(u.id, days=30)
    r = Resolver(store, AuthSpec(), api_key="s3cret")
    got = probe(r, ELSEWHERE, headers={"Authorization": "Bearer s3cret", "Cookie": f"airlie_session={token}"})
    assert got.json()["via"] == "api-key"


def test_fresh_install_is_loopback_only(store: AuthStore) -> None:
    r = Resolver(store, AuthSpec(), api_key=None)
    assert probe(r, LOOPBACK).json() == {
        "user_id": "bootstrap",
        "name": "bootstrap",
        "role": "admin",
        "via": "api-key",
    }
    assert probe(r, ELSEWHERE).json() is None
    store.add_user("Noah", "admin")  # once anyone exists, loopback is nobody too
    assert probe(r, LOOPBACK).json() is None


def test_fresh_install_with_an_api_key_set_needs_the_key_even_on_loopback(store: AuthStore) -> None:
    r = Resolver(store, AuthSpec(), api_key="s3cret")
    assert probe(r, LOOPBACK).json() is None


def test_principal_is_frozen() -> None:
    p = Principal("u", "n", "admin", "session")
    with pytest.raises(AttributeError):
        p.role = "member"  # type: ignore[misc]


# ---- serve wires the resolver in front of /v1 ----


def test_serve_refuses_v1_to_nobody_with_a_json_401_and_leaves_health_open(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="s3cret")
    with TestClient(app, client=ELSEWHERE) as c:
        assert c.get("/health").status_code == 200
        r = c.get("/v1/models")
        assert r.status_code == 401
        assert r.json() == {"error": "not signed in"}
        assert c.get("/v1/models", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_serve_answers_an_unknown_tailscale_login_with_401_not_invited(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    c.auth.tailscale = True
    store = AuthStore(tmp_path / "state" / "auth.sqlite")
    store.add_user("Noah", "admin", login="noah@github")
    store.close()
    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()))
    with TestClient(app, client=LOOPBACK) as tc:
        r = tc.get("/v1/models", headers={"Tailscale-User-Login": "stranger@github"})
        assert r.status_code == 401
        assert r.json() == {"error": "not invited"}
        assert tc.get("/v1/models", headers={"Tailscale-User-Login": "noah@github"}).status_code == 200


def test_serve_lets_a_fresh_install_in_from_loopback_only(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()))
    with TestClient(app, client=LOOPBACK) as c:
        assert c.get("/v1/models").status_code == 200
    with TestClient(app, client=ELSEWHERE) as c:
        assert c.get("/v1/models").status_code == 401


def test_serve_opens_the_user_store_under_state_dir_and_routes_see_the_principal(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    store = AuthStore(tmp_path / "state" / "auth.sqlite")
    u = store.add_user("Pat", "member")
    token = store.make_session(u.id, days=30)
    store.close()
    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()))

    @app.get("/v1/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        p: Principal = request.state.principal
        return {"name": p.name, "via": p.via}

    with TestClient(app, client=ELSEWHERE) as tc:
        assert tc.get("/v1/whoami").status_code == 401
        assert tc.get("/v1/whoami", headers={"Cookie": f"airlie_session={token}"}).json() == {
            "name": "Pat",
            "via": "session",
        }
