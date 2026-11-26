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
from tests.support.serve import FakeUpstream, cfg

LOOPBACK = ("127.0.0.1", 40000)
ELSEWHERE = ("10.0.0.5", 40000)
DECLARED_PROXY = ("172.17.0.2", 40000)
OTHER_PROXY = ("172.18.0.2", 40000)
"""A reverse proxy on the docker bridge (declared in these tests) and one on another bridge the operator
did not name."""
PROXIED = {
    "Tailscale-User-Login": "noah@github",
    "X-Forwarded-For": "100.64.0.7",
    "X-Forwarded-Proto": "https",
}
"""What a request looks like after `tailscale serve` proxied it from a tailnet device: it arrives from
loopback with the device's tailnet address forwarded."""


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


def test_dual_serve_api_keys_both_authorize(store: AuthStore) -> None:
    r = Resolver(
        store,
        AuthSpec(),
        api_key="bridge-key",
        accepted_api_keys=("bridge-key", "legacy-key"),
    )
    for key in ("bridge-key", "legacy-key"):
        assert probe(r, ELSEWHERE, headers={"Authorization": f"Bearer {key}"}).json()["via"] == "api-key"
    assert probe(r, ELSEWHERE, headers={"Authorization": "Bearer other"}).json() is None


def test_a_tailscale_login_on_loopback_is_its_user(store: AuthStore) -> None:
    u = store.add_user("Noah", "admin", login="noah@github")
    r = Resolver(store, AuthSpec(tailscale=True), api_key=None)
    got = probe(r, LOOPBACK, headers=PROXIED).json()
    assert got == {"user_id": u.id, "name": "Noah", "role": "admin", "via": "tailscale"}


def test_tailscale_header_is_ignored_off_loopback(store: AuthStore) -> None:
    store.add_user("Noah", "admin", login="noah@github")
    r = Resolver(store, AuthSpec(tailscale=True), api_key=None)
    assert probe(r, ELSEWHERE, headers=PROXIED).json() is None


def test_tailscale_header_needs_a_forwarded_tailnet_address(store: AuthStore) -> None:
    """Loopback alone is any local proxy (task tunnel, a reverse proxy): only `tailscale serve`, which
    forwards the device's 100.64/10 or fd7a:115c:a1e0::/48 address, vouches for the header."""
    store.add_user("Noah", "admin", login="noah@github")
    r = Resolver(store, AuthSpec(tailscale=True), api_key=None)
    assert probe(r, LOOPBACK, headers={"Tailscale-User-Login": "noah@github"}).json() is None
    internet = {**PROXIED, "X-Forwarded-For": "203.0.113.9"}
    assert probe(r, LOOPBACK, headers=internet).json() is None
    v6 = {**PROXIED, "X-Forwarded-For": "fd7a:115c:a1e0::1234"}
    assert probe(r, LOOPBACK, headers=v6).json()["via"] == "tailscale"
    chain = {**PROXIED, "X-Forwarded-For": "100.64.0.7, 10.0.0.1"}
    assert probe(r, LOOPBACK, headers=chain).json()["via"] == "tailscale"
    bogus = {**PROXIED, "X-Forwarded-For": "not-an-address"}
    assert probe(r, LOOPBACK, headers=bogus).json() is None


def test_tailscale_header_is_ignored_when_auth_tailscale_is_off(store: AuthStore) -> None:
    store.add_user("Noah", "admin", login="noah@github")
    r = Resolver(store, AuthSpec(tailscale=False), api_key=None)
    assert probe(r, LOOPBACK, headers=PROXIED).json() is None


# ---- who may speak for a client: auth.trusted_proxies (empty by default) ----


def test_a_declared_proxy_vouches_for_the_forwarded_tailnet_address(store: AuthStore) -> None:
    """`tailscale serve` in its own container reaches serve over the bridge, not loopback; naming that
    address (or its CIDR) reads its forwarded headers, and a proxy on another bridge stays nobody."""
    u = store.add_user("Noah", "admin", login="noah@github")
    by_address = Resolver(store, AuthSpec(tailscale=True, trusted_proxies=["172.17.0.2"]), api_key=None)
    assert probe(by_address, DECLARED_PROXY, headers=PROXIED).json() == {
        "user_id": u.id,
        "name": "Noah",
        "role": "admin",
        "via": "tailscale",
    }
    assert probe(by_address, OTHER_PROXY, headers=PROXIED).json() is None
    by_cidr = Resolver(store, AuthSpec(tailscale=True, trusted_proxies=["172.17.0.0/24"]), api_key=None)
    assert probe(by_cidr, DECLARED_PROXY, headers=PROXIED).json()["via"] == "tailscale"
    assert probe(by_cidr, OTHER_PROXY, headers=PROXIED).json() is None
    # and tailscale serve on loopback is unchanged: the device's tailnet address, gated on auth.tailscale
    assert probe(by_cidr, LOOPBACK, headers=PROXIED).json()["via"] == "tailscale"


def test_a_declared_proxy_does_not_open_the_tailscale_header_by_itself(store: AuthStore) -> None:
    """Declaring a proxy says where forwarded headers come from; `auth.tailscale` still says to read an
    identity header at all."""
    store.add_user("Noah", "admin", login="noah@github")
    r = Resolver(store, AuthSpec(tailscale=False, trusted_proxies=["172.17.0.2"]), api_key=None)
    assert probe(r, DECLARED_PROXY, headers=PROXIED).json() is None


def test_an_undeclared_peer_forges_no_identity(store: AuthStore) -> None:
    """With the peer guard removed these become admins, which is why they are here: a routable peer is not a
    proxy until declared, so `auth.trusted_proxies=[]` must make X-Forwarded-For and Tailscale-User-Login
    inert no matter how plausible the tailnet address in them looks."""
    store.add_user("Noah", "admin", login="noah@github")
    declared_everywhere_else = Resolver(
        store, AuthSpec(tailscale=True, trusted_proxies=["172.17.0.0/24"]), api_key=None
    )
    for client in (ELSEWHERE, OTHER_PROXY):
        assert probe(declared_everywhere_else, client, headers=PROXIED).json() is None, client


def test_a_routable_peer_cannot_bootstrap_a_fresh_install(store: AuthStore) -> None:
    """No users and no API key plus `--host 0.0.0.0`: the bootstrap admin is the machine's own direct
    connection only, so no routable peer takes a fresh install — not an undeclared one, not a declared
    proxy, whatever it forwards."""
    r = Resolver(store, AuthSpec(tailscale=True, trusted_proxies=["172.17.0.0/24"]), api_key=None)
    for client in (ELSEWHERE, OTHER_PROXY, DECLARED_PROXY):
        for headers in ({}, {"X-Forwarded-For": "100.64.0.7"}, PROXIED):
            body = probe(r, client, headers=headers).json() or {}
            # nobody, or the tailscale "not invited" refusal — never the bootstrap admin
            assert body.get("name") != "bootstrap" and body.get("role") != "admin", (client, headers)
    assert probe(r, LOOPBACK).json()["name"] == "bootstrap"  # the Mac's own onboarding path still works


def test_a_declared_loopback_proxy_is_not_the_machine_itself(store: AuthStore) -> None:
    """One pod or container: the proxy shares the network namespace, so it arrives on loopback and may
    forward nothing at all. Declaring it is what stops a fresh install bootstrapping a remote client behind
    it; undeclared, loopback is still the Mac and the web app works."""
    plain = Resolver(store, AuthSpec(), api_key=None)
    assert probe(plain, LOOPBACK).json()["name"] == "bootstrap"
    declared = Resolver(store, AuthSpec(trusted_proxies=["127.0.0.1"]), api_key=None)
    assert probe(declared, LOOPBACK).json() is None
    assert probe(declared, LOOPBACK, headers=PROXIED).json() is None


def test_an_unknown_tailscale_login_is_not_invited(store: AuthStore) -> None:
    store.add_user("Noah", "admin", login="noah@github")  # so the install is not fresh
    r = Resolver(store, AuthSpec(tailscale=True), api_key=None)
    got = probe(r, LOOPBACK, headers={**PROXIED, "Tailscale-User-Login": "stranger@github"}).json()
    assert got == {"not_invited": "stranger@github"}


def test_an_unknown_tailscale_login_with_a_passkey_session_is_the_sessions_user(store: AuthStore) -> None:
    """A person invited without a login comes through tailscale serve too; their cookie must count."""
    pat = store.add_user("Pat", "member")
    token = store.make_session(pat.id, days=30)
    r = Resolver(store, AuthSpec(tailscale=True), api_key=None)
    headers = {**PROXIED, "Tailscale-User-Login": "pat@github", "Cookie": f"airlie_session={token}"}
    got = probe(r, LOOPBACK, headers=headers).json()
    assert got == {"user_id": pat.id, "name": "Pat", "role": "member", "via": "session"}
    stale = {**PROXIED, "Tailscale-User-Login": "pat@github", "Cookie": "airlie_session=forged"}
    assert probe(r, LOOPBACK, headers=stale).json() == {"not_invited": "pat@github"}


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


def test_a_wrong_bearer_does_not_block_a_passkey_session(store: AuthStore) -> None:
    """Stale admin Bearer in the SPA must not beat a valid passkey session cookie."""
    u = store.add_user("Pat", "member")
    token = store.make_session(u.id, days=30)
    r = Resolver(store, AuthSpec(), api_key="s3cret")
    got = probe(
        r,
        ELSEWHERE,
        headers={"Authorization": "Bearer stale-or-wrong", "Cookie": f"airlie_session={token}"},
    )
    assert got.json() == {"user_id": u.id, "name": "Pat", "role": "member", "via": "session"}


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


def test_fresh_install_bootstrap_is_for_direct_loopback_requests_only(store: AuthStore) -> None:
    """A request a local proxy forwarded (task tunnel, tailscale serve, Funnel) is not the Mac itself."""
    r = Resolver(store, AuthSpec(), api_key=None)
    for proxied in (
        {"X-Forwarded-For": "100.64.0.7"},
        {"X-Forwarded-Host": "studio.ts.net"},
        {"X-Forwarded-Proto": "https"},
        {"Forwarded": "for=203.0.113.9"},
        {"Tailscale-User-Login": "x@y"},
    ):
        assert probe(r, LOOPBACK, headers=proxied).json() is None, proxied
    assert probe(r, LOOPBACK).json()["name"] == "bootstrap"


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


def test_serve_accepts_either_key_during_a_dual_key_transition(tmp_path: Path) -> None:
    app = create_app(
        cfg(tmp_path),
        transport=httpx.MockTransport(FakeUpstream()),
        api_key="bridge-key",
        accepted_api_keys=("bridge-key", "legacy-key"),
    )
    with TestClient(app, client=ELSEWHERE) as c:
        for key in ("bridge-key", "legacy-key"):
            assert c.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).status_code == 200
        assert c.get("/v1/models", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_serve_answers_an_unknown_tailscale_login_with_401_not_invited(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    c.auth.tailscale = True
    store = AuthStore(tmp_path / "state" / "auth.sqlite")
    store.add_user("Noah", "admin", login="noah@github")
    store.close()
    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()))
    with TestClient(app, client=LOOPBACK) as tc:
        r = tc.get("/v1/models", headers={**PROXIED, "Tailscale-User-Login": "stranger@github"})
        assert r.status_code == 401
        assert r.json() == {"error": "not invited"}
        assert tc.get("/v1/models", headers=PROXIED).status_code == 200


def test_serve_accepts_a_declared_proxy_and_nobody_off_it(tmp_path: Path) -> None:
    """The middleware sees the same peer rule: `tailscale serve` in another container works once declared,
    and a proxy off that network gets 401 with the very same headers."""
    c = cfg(tmp_path)
    c.auth.tailscale = True
    c.auth.trusted_proxies = ["172.17.0.0/24"]
    store = AuthStore(tmp_path / "state" / "auth.sqlite")
    store.add_user("Noah", "admin", login="noah@github")
    store.close()
    app = create_app(c, transport=httpx.MockTransport(FakeUpstream()))
    with TestClient(app, client=DECLARED_PROXY) as tc:
        assert tc.get("/v1/models", headers=PROXIED).status_code == 200
    with TestClient(app, client=OTHER_PROXY) as tc:
        assert tc.get("/v1/models", headers=PROXIED).status_code == 401


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
