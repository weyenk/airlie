"""`/v1/auth/oidc/start` and `/v1/auth/oidc/callback`, and what each one refuses.

These two routes turn an IdP assertion into an airlie session, so most of what is tested here is refusal. The
callback is public and its query is attacker-writable; the whole difference between "the IdP vouched for this
person" and "someone posted a string that looks like a code" lives in what these routes decline to accept.

The IdP is a `MockTransport` on `create_app`'s existing `transport` seam, so the flow runs with no network:
discovery, the JWKS, the token exchange and the ID token are all built here, the token signed by a real
RSA key the way an IdP signs one. Nothing inside airlie is stubbed — `/start` redirects using endpoints it
actually discovered, and `/callback` verifies a signature it actually looked up by `kid`.

A successful callback ends in the same artifact a passkey login produces: a cookie minted by
`store.make_session`, so `Resolver.resolve()` downstream cannot tell the two sign-ins apart."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from airlie.auth.api import auth_router
from airlie.auth.oidc_flow import Flow, Outcome, code_challenge
from airlie.auth.principal import Resolver
from airlie.auth.store import AuthStore
from airlie.config import load_config
from airlie.config.schema import AuthSpec
from airlie.serve.app import PUBLIC_PATHS, create_app
from tests.support.auth_store import Clock
from tests.support.serve import FakeUpstream
from tests.support.serve import cfg as minimal_config

ISSUER = "https://id.acme.com"
EXTERNAL = "https://airlie.acme.com"
CALLBACK = f"{EXTERNAL}/v1/auth/oidc/callback"
CLIENT_ID = "airlie-client"
SECRET = "client-secret-value"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "v1"


class MovableClock(Clock):
    """The shared auth clock, plus the ability to move. Every TTL test needs it (`Attempts` compares a saved
    `expires_at` against this same object), and `tests.support.auth_store.Clock` is deliberately read-only."""

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _word(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def public_jwk(kid: str = KID) -> dict[str, str]:
    numbers = KEY.public_key().public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "alg": "RS256",
        "use": "sig",
        "n": _word(numbers.n),
        "e": _word(numbers.e),
    }


class IdPHost:
    """The three endpoints one sign-in touches, answering over a MockTransport.

    `tokens` is what the next code exchanges for; `reject` makes it answer the way a real IdP does when the
    request itself is wrong — a 400 carrying an `error` — which is the only reply airlie gets for a verifier
    that does not match the challenge it stored."""

    def __init__(self, clock: MovableClock) -> None:
        self.clock = clock
        self.document: Any = {
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/keys",
        }
        self.jwks: Any = {"keys": [public_jwk()]}
        self.tokens: dict[str, Any] | None = None
        self.reject: dict[str, Any] | None = None
        self.statuses = {"document": 200, "jwks": 200}
        self.token_requests: list[dict[str, str]] = []
        self.calls = {"document": 0, "jwks": 0, "token": 0}

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("openid-configuration"):
            self.calls["document"] += 1
            status = self.statuses["document"]
            return httpx.Response(status, json=self.document if status == 200 else {"error": "unavailable"})
        if path == "/keys":
            self.calls["jwks"] += 1
            status = self.statuses["jwks"]
            return httpx.Response(status, json=self.jwks if status == 200 else {"error": "unavailable"})
        if path == "/token":
            self.calls["token"] += 1
            self.token_requests.append(dict(parse_qsl(request.content.decode(), keep_blank_values=True)))
            if self.reject is not None:
                return httpx.Response(400, json=self.reject)
            if self.tokens is None:
                return httpx.Response(500, json={"error": "the test set no token reply"})
            return httpx.Response(200, json={**self.tokens, "token_type": "Bearer", "expires_in": 3600})
        return httpx.Response(404, json={"error": "the test IdP has no such endpoint"})

    def id_token(self, claims: dict[str, Any], *, kid: str = KID, algorithm: str = "RS256") -> str:
        return jwt.encode(claims, KEY, algorithm=algorithm, headers={"alg": algorithm, "kid": kid})


class Studio:
    """One serve app with OIDC enabled and the IdP mocked: a real AuthStore on a clock, `create_app` over the
    mock transport, and a client whose base URL is the configured `external_url`.

    The base config is `tests.support.serve.cfg`'s (which the loader requires: teams and lts name roles that
    have to exist), then the shared `airlie.yaml` is rewritten with an `auth.oidc` block and read again. Going
    through `load_config` rather than building the model by hand keeps the load-time rules in force —
    `enabled` without an issuer is a config error, not a route that silently does nothing.

    The clock is `tests.support.auth_store.Clock` plus `advance`: the TTL tests move it, and a shared clock is
    what lets an attempt saved before the move be judged expired by the store that saved it."""

    def __init__(self, tmp_path: Path, *, secret_file: Path | None = None, **oidc: Any) -> None:

        self.clock = MovableClock()
        self.idp = IdPHost(self.clock)
        minimal_config(tmp_path)  # writes tmp_path/airlie.yaml, which is the base merged into below
        block: dict[str, Any] = {
            "enabled": True,
            "issuer": ISSUER,
            "client_id": CLIENT_ID,
            "external_url": EXTERNAL,
            **oidc,
        }
        if secret_file is not None:
            block["client_secret_file"] = str(secret_file)
        elif "client_secret_file" not in oidc:
            block["client_secret_file"] = str(secret_file_for(tmp_path))
        auth: dict[str, Any] = {"oidc": block}
        if "session_days" in oidc:  # an auth-level key, not part of the oidc block
            auth["session_days"] = oidc.pop("session_days")
        merged = yaml.safe_load((tmp_path / "airlie.yaml").read_text())
        merged["auth"] = auth
        (tmp_path / "airlie.yaml").write_text(yaml.safe_dump(merged))
        self.cfg = load_config(tmp_path / "airlie.yaml")
        self.secret_path = secret_file if secret_file is not None else Path(block["client_secret_file"])
        self.store = AuthStore(tmp_path / "state" / "auth.sqlite", now=self.clock)
        self.app = self._build(self.idp.transport)
        self.client = client_for(self.app)

    def _build(self, transport: httpx.AsyncBaseTransport) -> Any:
        return create_app(self.cfg, transport=transport, auth_store=self.store, now=self.clock)

    def with_idp(self, transport: httpx.AsyncBaseTransport) -> None:
        """Swap the IdP out from under a second app, keeping the same store and clock — so a test can make the
        *second* half of a flow fail without losing the attempt the first half saved."""
        self.app = self._build(transport)
        self.client = client_for(self.app)

    def start(self, target: str | None = None) -> httpx.Response:
        url = "/v1/auth/oidc/start" + (f"?target={target}" if target is not None else "")
        return self.client.get(url, follow_redirects=False)

    def authorize_params(self, response: httpx.Response | None = None, *, target: str | None = None) -> dict:
        """The query the browser was redirected to the IdP with, as the IdP would read it."""
        response = response if response is not None else self.start(target)
        assert response.status_code == 302, response.text
        location = response.headers["location"]
        assert location.startswith(f"{ISSUER}/authorize?"), location
        return {key: values[0] for key, values in parse_qs(urlparse(location).query).items()}

    def callback(self, params: dict[str, str | None]) -> httpx.Response:
        query = "&".join(f"{key}={value}" for key, value in params.items() if value is not None)
        return self.client.get(f"/v1/auth/oidc/callback?{query}", follow_redirects=False)

    def token_for(self, nonce: str, *, kid: str = KID, **claims: Any) -> None:
        """Make the next exchange hand back a token signed by this IdP, carrying `nonce`."""
        body: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "user-123",
            "exp": int(self.clock.t) + 600,
            "iat": int(self.clock.t),
            "nonce": nonce,
            "email": "dana@acme.com",
        }
        body.update(claims)
        self.idp.tokens = {"access_token": "at-1", "id_token": self.idp.id_token(body, kid=kid)}

    def succeed(self, *, target: str | None = None, kid: str = KID, **claims: Any) -> httpx.Response:
        """A real `/start`, then the callback its browser would send: the token the IdP would mint for the
        saved nonce, and the code the IdP would have issued."""
        params = self.authorize_params(target=target)
        self.token_for(params["nonce"], kid=kid, **claims)
        return self.callback({"code": "the-code", "state": params["state"]})

    def bind(self, sub: str, login: str = "dana", role: str = "member") -> Any:
        """Bind a subject to a new person, through the same table the callback reads.

        Deliberately the harness's only binding helper, and deliberately writing the real store rather than
        a stand-in: Slice B faked this with a module dict patched into `oidc_flow`, which meant every green
        happy path here proved the callback asked *something* and not that it asked the table an admin fills.
        The issuer comes from the loaded config rather than the `ISSUER` constant so a test that runs against
        a different issuer cannot pass by coincidence."""
        user = self.store.add_user("Dana", role, login=login)  # type: ignore[arg-type]
        self.store.bind_subject(
            user_id=user.id, iss=str(self.cfg.auth.oidc.issuer), sub=sub, bound_by="admin"
        )
        return user


def _reached_the_binding_gate(response: httpx.Response) -> bool:
    """Whether the callback got all the way past verification to airlie's own binding table.

    Unbound used to answer 403 and now answers the sign-in redirect, and several tests here only need to know
    that the exchange and the verifier let them through to that table — not which of its two answers they got.
    Spelled as a helper so those tests stay one assertion and cannot drift into accepting a failure that
    stopped short of the gate."""
    return response.status_code == 302 and response.headers.get("location") == "/signin?oidc_unbound=1"


def _refused_without_a_session(response: httpx.Response) -> bool:
    """Whether this callback signed nobody in — the thing these admin-surface tests actually check when they
    ask whether a subject is still bound. A subject that is no longer bound is refused, and the unbound
    refusal is the sign-in redirect with no cookie on it, so "not signed in" is the assertion that outlives
    refusal changing shape. Checked together: a 302 that also minted a session would be a revoked person still
    working."""
    signed_in = session_cookie(response) is not None
    return not signed_in and response.status_code in (302, 403)


def secret_file_for(tmp_path: Path, text: str = SECRET) -> Path:
    path = tmp_path / "client-secret"
    path.write_text(text + "\n")
    path.chmod(0o600)
    return path


def client_for(app: Any, *, cookie: str | None = None) -> Any:
    from fastapi.testclient import TestClient

    headers = {"Origin": EXTERNAL}
    client = TestClient(app, base_url=EXTERNAL, headers=headers)
    if cookie:
        client.cookies.set("airlie_session", cookie)
    return client


@pytest.fixture
def studio(tmp_path: Path) -> Studio:
    built = Studio(tmp_path)
    yield built
    built.store.close()


# ---- wiring ----


def test_both_routes_are_public() -> None:
    """serve's middleware allows a public path by exact string. If either is missing, a person who has not
    signed in yet is told "not signed in" while trying to sign in, and the route body never runs."""
    assert "/v1/auth/oidc/start" in PUBLIC_PATHS
    assert "/v1/auth/oidc/callback" in PUBLIC_PATHS


def test_the_sign_in_options_route_is_public() -> None:
    """The sign-in screen asks whether this install has an IdP before it offers the button, and it asks while
    nobody is signed in. Left out of PUBLIC_PATHS the answer is a 401, the screen reads "no IdP", and an
    operator who configured everything correctly gets a sign-in screen with no SSO button and no error
    anywhere — the failure looks like the setting did nothing."""
    assert "/v1/auth/signin/options" in PUBLIC_PATHS


def test_the_options_endpoint_reports_no_idp_when_oidc_is_off(tmp_path: Path) -> None:
    """The shipped shape: a config that never mentions OIDC. The button is absent rather than greyed out, so a
    default install's sign-in screen is the screen it was before SSO existed."""
    store = AuthStore(tmp_path / "state" / "auth.sqlite")
    app = create_app(
        minimal_config(tmp_path), transport=httpx.MockTransport(FakeUpstream()), auth_store=store
    )
    r = client_for(app).get("/v1/auth/signin/options")
    assert r.status_code == 200
    assert r.json() == {"oidc": False}
    store.close()


def test_the_options_endpoint_reports_the_idp_it_would_redirect_to(studio: Studio) -> None:
    """Enabled and configured: the same condition `/start` tests, so the screen can never offer a button whose
    route answers 404."""
    r = studio.client.get("/v1/auth/signin/options")
    assert r.status_code == 200
    assert r.json() == {"oidc": True}


def test_the_reported_flag_is_the_flag_the_routes_answer_for(tmp_path: Path) -> None:
    """The two halves of one decision must not be able to drift: `/start` answers 404 exactly when the options
    endpoint says there is no IdP. An install advertising a button for a route that 404s is a sign-in screen
    that sends people to a dead link, and the drift is invisible from either side alone.

    `enabled: false` beside an issuer is the case that makes pinning the two together worth it: on it
    `build_oidc_flow` is non-None (its condition is `enabled and issuer`), and the flow's `enabled` is what
    the routes refuse on. So the flag reads the pair the routes read, not the builder alone — a build that
    answered from `oidc is not None` would offer a button for a 404."""
    built = Studio(tmp_path, enabled=False)
    try:
        assert built.client.get("/v1/auth/signin/options").json() == {"oidc": False}
        assert built.start().status_code == 404  # and it stayed 404, as the flag says
    finally:
        built.store.close()


def test_the_options_endpoint_names_nothing_about_the_idp(studio: Studio) -> None:
    """It is public, so it says only that an IdP exists. The issuer names the company behind the deployment to
    anyone who can reach it, and the rest of the block is policy an attacker would rather read than guess; all
    of it is one answer away the moment this route starts echoing its config."""
    body = studio.client.get("/v1/auth/signin/options")
    assert body.status_code == 200
    assert body.json() == {"oidc": True}
    for leak in (ISSUER, CLIENT_ID, EXTERNAL, SECRET):
        assert leak not in body.text


def test_the_options_endpoint_is_anonymous(tmp_path: Path) -> None:
    """No cookie: this is what the sign-in screen sends. A 401 here is the failure in
    `test_the_sign_in_options_route_is_public` arriving from the other side."""
    built = Studio(tmp_path)
    try:
        anonymous = client_for(built.app)
        assert "airlie_session" not in anonymous.cookies
        assert anonymous.get("/v1/auth/signin/options").status_code == 200
    finally:
        built.store.close()


def test_oidc_off_answers_404(tmp_path: Path) -> None:
    """The studio only shows the button when the options endpoint reports OIDC on, so a hand-typed URL gets
    nothing rather than a description of a feature this install never enabled. The shared minimal config has
    no oidc block, so this is the shipped shape."""
    store = AuthStore(tmp_path / "state" / "auth.sqlite")
    app = create_app(
        minimal_config(tmp_path), transport=httpx.MockTransport(FakeUpstream()), auth_store=store
    )
    client = client_for(app)
    assert client.get("/v1/auth/oidc/start", follow_redirects=False).status_code == 404
    assert client.get("/v1/auth/oidc/callback?code=x&state=y", follow_redirects=False).status_code == 404
    store.close()


# ---- /start ----


def test_start_redirects_to_the_idp_with_the_required_parameters(studio: Studio) -> None:
    params = studio.authorize_params()
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == CALLBACK
    assert params["scope"] == "openid profile email"
    assert params["code_challenge_method"] == "S256"
    assert params["state"] and params["nonce"] and params["code_challenge"]


def test_the_authorize_endpoint_came_from_discovery(studio: Studio) -> None:
    """airlie must not hardcode a vendor's authorize path; the document decides. Changing what it says proves
    the redirect was built from the fetched value."""
    studio.idp.document = {**studio.idp.document, "authorization_endpoint": f"{ISSUER}/oauth2/v1/authorize"}
    assert studio.start().headers["location"].startswith(f"{ISSUER}/oauth2/v1/authorize?")
    assert studio.idp.calls["document"] == 1


def test_discovery_is_fetched_once_not_per_sign_in(studio: Studio) -> None:
    for _ in range(4):
        studio.start()
    assert studio.idp.calls["document"] == 1


def test_the_verifier_is_never_sent_to_the_idp(studio: Studio) -> None:
    """The verifier is the secret proving the party at the token endpoint started the flow. Sending it on the
    front channel — where the browser, its history and any Referer can see it — is what PKCE exists to avoid,
    so only the challenge goes out."""
    assert "code_verifier" not in studio.start().headers["location"]


def test_the_challenge_is_the_s256_of_the_verifier_posted_later(studio: Studio) -> None:
    """The IdP compares the posted verifier against the challenge it stored. If airlie sent a challenge that
    did not derive from the verifier it would later post, every exchange would fail with an `invalid_request`
    that names nothing useful."""
    params = studio.authorize_params()
    studio.idp.tokens = {"id_token": "unused", "access_token": "a"}
    studio.callback({"code": "c", "state": params["state"]})
    assert code_challenge(studio.idp.token_requests[-1]["code_verifier"]) == params["code_challenge"]


def test_state_nonce_and_verifier_are_three_different_values(studio: Studio) -> None:
    """Three attacks, three secrets: state answers "did this browser ask?" (CSRF), nonce "was this token
    minted for this attempt?" (injection), the verifier "is this the browser that started?" (code
    interception). Two roles sharing a value hands an attacker both for the price of one leak."""
    params = studio.authorize_params()
    studio.idp.tokens = {"id_token": "unused", "access_token": "a"}
    studio.callback({"code": "c", "state": params["state"]})
    verifier = studio.idp.token_requests[-1]["code_verifier"]
    assert len({params["state"], params["nonce"], verifier}) == 3


def test_state_carries_48_bytes_of_entropy(studio: Studio) -> None:
    """`/start` is public and free, so state is both the unguessable key of a server-side record and the only
    thing separating a real callback from a forged one. Under 32 bytes it is guessable."""
    raw = base64.urlsafe_b64decode(studio.authorize_params()["state"] + "==")
    assert len(raw) == 48


def test_the_redirect_uri_is_the_configured_origin_not_the_host_header(studio: Studio) -> None:
    """serve runs uvicorn with proxy_headers off, so Host is whatever the caller sent. A redirect_uri built
    from it lets a caller have the IdP deliver the code — and then the exchange, carrying the client secret —
    at a host they own."""
    assert studio.authorize_params()["redirect_uri"] == CALLBACK
    spoofed = studio.client.get(
        "/v1/auth/oidc/start", follow_redirects=False, headers={"Host": "attacker.example"}
    )
    assert parse_qs(urlparse(spoofed.headers["location"]).query)["redirect_uri"] == [CALLBACK]


def test_scopes_come_from_the_config(tmp_path: Path) -> None:
    """A groups scope or offline_access has to be requestable without a code change. Whether `openid` is in
    the list is the operator's responsibility, and this pins that airlie asks for exactly what the config
    says rather than quietly adding or dropping anything."""
    studio = Studio(tmp_path, scopes=["openid", "email", "groups"])
    assert studio.authorize_params()["scope"] == "openid email groups"
    studio.store.close()


def test_a_public_client_exchanges_without_a_secret(tmp_path: Path) -> None:
    """A public client has no secret, so the form must omit the field: posting `client_secret=` (blank) is how
    some IdP libraries end up answering `invalid_client`."""
    studio = Studio(tmp_path, client_secret_file=str(tmp_path / "absent"))
    response = studio.succeed()
    assert "client_secret" not in studio.idp.token_requests[-1]
    assert _reached_the_binding_gate(response)  # it got to the binding gate, so the exchange was accepted
    studio.store.close()


def test_the_exchange_posts_the_confidential_client_credentials(studio: Studio) -> None:
    studio.succeed()
    posted = studio.idp.token_requests[-1]
    assert posted["grant_type"] == "authorization_code"
    assert posted["client_id"] == CLIENT_ID
    assert posted["client_secret"] == SECRET
    assert posted["code"] == "the-code"
    assert posted["redirect_uri"] == CALLBACK


def test_the_exchange_goes_to_the_discovered_token_endpoint(studio: Studio) -> None:
    """`Provider.from_document` refuses an endpoint off the issuer's origin; this confirms the exchange used
    the endpoint airlie vetted rather than one read straight off the wire."""
    studio.succeed()
    assert studio.idp.calls["token"] == 1


def test_a_signed_in_browser_can_still_start(studio: Studio) -> None:
    """Re-authenticating through the IdP while holding a session has to be possible (an expired corporate
    session, a second tenant), so a present cookie must not make the public route fail."""
    user = studio.store.add_user("Dana", "member", login="dana")
    client = client_for(studio.app, cookie=studio.store.make_session(user.id, 1))
    assert client.get("/v1/auth/oidc/start", follow_redirects=False).status_code == 302


def test_a_blank_or_odd_target_still_starts(studio: Studio) -> None:
    """`/start` is public, so odd input is ordinary traffic: the target is sanitised, not a 500."""
    for target in (" ", "", "//evil.example"):
        assert studio.start(target=target).status_code == 302


# ---- /callback: the attempt ----


def test_a_callback_that_answers_no_attempt_is_refused_before_the_exchange(studio: Studio) -> None:
    """A missing state is a forged callback; an unknown one is a forgery or an attempt that already ended.
    Both are refused without dialling the IdP, because the exchange posts the client secret and should only
    ever happen for a request this server actually made."""
    studio.idp.tokens = {"id_token": "x", "access_token": "a"}
    assert studio.callback({"code": "c"}).status_code == 400
    assert studio.callback({"code": "c", "state": "never-issued"}).status_code == 400
    assert studio.idp.calls["token"] == 0


def test_the_state_is_consumed_by_the_first_callback(studio: Studio) -> None:
    """A code is single-use at the IdP, so a second callback carrying the same state can only be a replay.
    Popping it also means the verifier is never offered twice."""
    params = studio.authorize_params()
    studio.token_for(params["nonce"])
    assert _reached_the_binding_gate(studio.callback({"code": "c", "state": params["state"]}))
    assert studio.callback({"code": "c", "state": params["state"]}).status_code == 400
    assert studio.idp.calls["token"] == 1


def test_an_expired_attempt_is_refused_without_an_exchange(studio: Studio) -> None:
    """An attempt outlives its usefulness long before its code expires at the IdP, so an old saved state
    plus a still-valid copied code is a working replay unless the TTL is enforced here."""
    params = studio.authorize_params()
    studio.idp.tokens = {"id_token": "x", "access_token": "a"}
    studio.clock.advance(301.0)
    assert studio.callback({"code": "c", "state": params["state"]}).status_code == 400
    assert studio.idp.calls["token"] == 0


def test_two_attempts_do_not_answer_each_other(studio: Studio) -> None:
    """Two people click Sign in with SSO at once. Each callback must land on its own attempt, so one person's
    leaked state cannot complete the other person's sign-in."""
    first = studio.authorize_params()
    second = studio.authorize_params()
    assert first["state"] != second["state"]
    studio.token_for(second["nonce"])
    assert studio.callback({"code": "c", "state": first["state"]}).status_code == 401
    assert studio.idp.calls["token"] == 1


def test_each_attempt_completes_on_its_own_nonce(studio: Studio) -> None:
    """The same flow twice, each with a token minted for its own nonce: both get as far as the gate airlie
    controls, which is as far as this goes without a binding."""
    assert _reached_the_binding_gate(studio.succeed())
    assert _reached_the_binding_gate(studio.succeed())


def test_a_url_from_the_history_cannot_sign_anyone_in(studio: Studio) -> None:
    """The realistic replay: someone copies a callback URL out of browser history an hour later. Its state is
    gone and its code is spent, so the callback refuses without contacting the IdP."""
    params = studio.authorize_params()
    studio.clock.advance(3600.0)
    studio.idp.tokens = {"id_token": "x", "access_token": "a"}
    assert studio.callback({"code": "spent-code", "state": params["state"]}).status_code == 400
    assert studio.idp.calls["token"] == 0


# ---- /callback: the token ----


def test_a_token_minted_for_another_attempt_is_refused(studio: Studio) -> None:
    """The nonce check through the real verify path: a valid signature, the right issuer and audience, and
    still not this attempt's token. Without the nonce, a token an attacker collected elsewhere would log in
    the victim — or the attacker."""
    minted_for = studio.authorize_params()
    studio.token_for(minted_for["nonce"])
    answering = studio.authorize_params()
    response = studio.callback({"code": "c", "state": answering["state"]})
    assert response.status_code == 401
    assert session_cookie(response) is None
    assert studio.idp.calls["token"] == 1


def test_a_token_from_another_issuer_is_refused(studio: Studio) -> None:
    """A token signed by our key asserting a different `iss` is the shape of a cross-tenant mix-up. The
    signature is real, so only the claim separates it from a valid token."""
    assert studio.succeed(iss="https://other.example").status_code == 401


def test_a_token_for_another_audience_is_refused(studio: Studio) -> None:
    """`aud` is what stops a token minted for another of this IdP's clients — a dashboard, a VPN portal — from
    working here, since the person and the signing key are identical in both."""
    assert studio.succeed(aud="some-other-client").status_code == 401


def test_an_expired_token_is_refused(studio: Studio) -> None:
    """airlie is the last line: accepting an expired token would mean a captured one never goes stale."""
    assert studio.succeed(exp=int(studio.clock.t) - 30).status_code == 401


def test_an_unsigned_none_alg_token_is_refused(studio: Studio) -> None:
    """The classic: strip the signature, set `alg: none`. Every check that runs after the signature would
    pass, so the algorithm allowlist is what stands between an unsigned JSON blob and a session."""
    params = studio.authorize_params()
    unsigned = jwt.encode(
        {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "user-123",
            "exp": int(studio.clock.t) + 600,
            "iat": int(studio.clock.t),
            "nonce": params["nonce"],
        },
        key="",
        algorithm="none",
    )
    studio.idp.tokens = {"id_token": unsigned, "access_token": "a"}
    response = studio.callback({"code": "c", "state": params["state"]})
    assert response.status_code == 401
    assert session_cookie(response) is None


def test_a_token_signed_with_an_untrusted_key_is_refused(studio: Studio) -> None:
    """An attacker's own RSA key with the right `alg` and `kid` would pass a signature check that trusted
    `kid` alone — so only the published JWKS is trusted, and `kid` only picks within it."""
    rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    params = studio.authorize_params()
    studio.idp.tokens = {
        "id_token": jwt.encode(
            {
                "iss": ISSUER,
                "aud": CLIENT_ID,
                "sub": "user-123",
                "exp": int(studio.clock.t) + 600,
                "iat": int(studio.clock.t),
                "nonce": params["nonce"],
            },
            rogue,
            algorithm="RS256",
            headers={"kid": KID},
        ),
        "access_token": "a",
    }
    assert studio.callback({"code": "c", "state": params["state"]}).status_code == 401


def test_an_unknown_kid_is_refused_after_one_refetch(studio: Studio) -> None:
    """A `kid` the IdP never published cannot be verified. One forced refetch is what lets a rotation land
    mid-sign-in; a still-unknown kid ends the attempt rather than looping."""
    assert studio.succeed(kid="never-published").status_code == 401


def test_a_rotated_key_is_picked_up(studio: Studio) -> None:
    """The reason for that one refetch: the IdP rotates, the first token names the new kid, the JWKS is read
    again and the same attempt succeeds."""
    params = studio.authorize_params()
    studio.idp.jwks = {"keys": [public_jwk("rotated")]}
    studio.token_for(params["nonce"], kid="rotated")
    assert _reached_the_binding_gate(studio.callback({"code": "c", "state": params["state"]}))


def test_the_jwks_is_fetched_once_across_sign_ins(studio: Studio) -> None:
    """With a `kid` the ring already knows, no sign-in should wait on the IdP's key endpoint."""
    studio.succeed()
    studio.succeed()
    assert studio.idp.calls["jwks"] == 1


def test_a_reply_without_an_id_token_is_refused(studio: Studio) -> None:
    """A 200 with no `id_token` is a real IdP reply (a scope mistake, a skipped consent screen). It must not
    reach the verifier as None, and must not be described in a way that dumps the reply."""
    params = studio.authorize_params()
    studio.idp.tokens = {"access_token": "at-1"}
    response = studio.callback({"code": "c", "state": params["state"]})
    assert response.status_code in (401, 502)
    assert session_cookie(response) is None


def test_an_idp_error_reply_does_not_quote_what_we_sent(studio: Studio) -> None:
    """On a failed exchange the IdP often echoes the request in `error_description`, which contains the client
    secret and the code. That text must not reach the browser."""
    studio.idp.reject = {
        "error": "invalid_client",
        "error_description": f"client_secret={SECRET} and code=the-code are not valid",
    }
    response = studio.succeed()
    assert response.status_code == 502
    assert SECRET not in response.text
    assert "the-code" not in response.text
    assert session_cookie(response) is None


# ---- /callback: airlie's own gates ----


def test_a_verified_but_unbound_subject_is_refused_and_no_user_appears(studio: Studio) -> None:
    """Slice C owns the bindings table and the admin surface that fills it. Until a person is bound, a
    perfectly good ID token is not permission to enter — and airlie must not create the user on the IdP's
    say-so. Auto-creating is the tempting version of this: it makes the first sign-in work, and it means
    anyone the IdP will assert (a former employee's still-live account, an IdP-wide group) can conjure an
    airlie identity and pick their own subject to be known by."""
    dana = studio.store.add_user("Dana", "member", login="dana")
    response = studio.succeed()
    assert response.status_code == 302  # the refusal is a redirect now; nothing about it mints a session
    assert session_cookie(response) is None
    assert "set-cookie" not in response.headers
    assert studio.store.users() == [dana]  # the sign-in created nobody


def test_a_binding_to_a_user_airlie_does_not_have_is_refused(tmp_path: Path, studio: Studio) -> None:
    """A binding whose user id matches no row must not mint anything.

    This is the guard against the tempting shortcut of using the IdP's `sub` as the user id. `sub` is the
    issuer's identifier and never chosen by airlie; `user_id` is airlie's own primary key, and every
    downstream row (sessions, chat pins, source links) is keyed on it. Letting the issuer's namespace occupy
    airlie's means a subject that happens to name an existing row signs in as that row. Here the shortcut
    would reach `make_session` for a user that does not exist.

    The store's foreign key refuses to create that row, and deleting a user cascades its bindings away, so
    neither path can produce it — an orphan binding is written here by hand, with enforcement off, the way an
    operator's own sqlite3 session would leave it (that client runs no `pragma foreign_keys`)."""
    studio.store.conn.execute("pragma foreign_keys=off")
    studio.store.conn.execute(
        "insert into oidc_bindings (iss, sub, user_id, bound_at, bound_by) values (?, ?, ?, ?, ?)",
        (ISSUER, "user-123", "no-such-user", studio.clock.t, "admin"),
    )
    response = studio.succeed()
    assert response.status_code == 403
    assert session_cookie(response) is None
    assert studio.store.users() == []


def test_the_subject_is_never_used_as_a_user_id(studio: Studio) -> None:
    """The same rule from the other side: with nothing bound, a verified `sub` shaped to match a real user's
    id still gets nothing. A session may only ever be keyed on a row airlie created."""
    dana = studio.store.add_user("Dana", "member", login="dana")
    response = studio.succeed(sub=dana.id)
    assert response.status_code == 302  # refused, and refused without a session
    assert session_cookie(response) is None
    assert "set-cookie" not in response.headers


def test_a_failure_never_redirects(studio: Studio) -> None:
    """A failure that did not verify answers a JSON body, never a redirect — so the target the person asked
    for cannot be used as a destination by a callback that failed. That single property is worth pinning
    across the failures rather than at one site, because the way to break it is to "help" the person back
    somewhere with a 302, which turns an attacker-chosen target into a live redirect and, on a guarded route,
    sends a session-less browser into a bounce loop.

    Exactly one refusal redirects, and it is not one of these: `unbound` — the only gate behind verification —
    is pinned separately at `test_an_unbound_subject_is_sent_to_the_sign_in_screen_with_one_flag`, and its
    destination is a constant rather than the attempt's target. A successful sign-in is the other place a
    Location is right, which is what the target tests above check."""
    studio.bind("user-123")  # otherwise the earliest failure is "unbound" and the interesting ones never run
    for params, expected in (
        ({"state": "never-issued", "code": "c"}, 400),
        ({"code": "c"}, 400),
    ):
        response = studio.callback(params)
        assert response.status_code == expected, params
        assert response.headers.get("content-type", "").startswith("application/json"), params

    studio.idp.tokens = {"id_token": "unsigned-junk", "access_token": "a"}
    forged = studio.authorize_params(target="/board/SECRET-1")
    bad_token = studio.callback({"code": "c", "state": forged["state"]})
    assert bad_token.status_code == 401
    assert bad_token.headers.get("content-type", "").startswith("application/json")
    assert session_cookie(bad_token) is None


def test_an_unbound_subject_is_sent_to_the_sign_in_screen_with_one_flag(tmp_path: Path) -> None:
    """The most common state of a real corporate rollout: SSO worked, nobody is linked yet. The old answer was
    a bare 403 JSON body, which left the browser sitting on airlie's JSON — the SPA never booted, so the
    person read nothing and the `reason` field could be read by nobody.

    So this one refusal answers 302 to airlie's own sign-in screen carrying the flag the screen already reads
    (`OIDC_UNBOUND_FLAG` in `web/src/lib/oidc.ts`, pinned by name at `web/src/lib/oidc.test.ts`). No user
    appears: the tempting version of this branch creates the person on the IdP's say-so, which makes the first
    sign-in work and means anyone the IdP will still assert — a departed account, an IdP-wide group — can
    conjure an airlie identity and choose the subject it is known by."""
    studio = Studio(tmp_path, client_secret_file=str(secret_file_for(tmp_path)))
    assert studio.store.users() == []
    response = studio.succeed(target="/board/SECRET-1")
    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/signin?oidc_unbound=1"
    assert response.text == ""  # a redirect carries no body for the SPA to miss
    assert session_cookie(response) is None
    assert studio.store.users() == []  # the sign-in created nobody
    studio.store.close()


def _domain_refused(tmp_path: Path) -> httpx.Response:
    """A token whose email domain the config refuses: 401, and the only gate that reaches this answer with a
    verified signature rather than a rejected one, so it cannot be produced from the shared studio (whose
    config allows `acme.com`)."""
    refused = Studio(tmp_path, allowed_domains=["other.example"])
    try:
        return refused.succeed(target="/board/SECRET-1")
    finally:
        refused.store.close()


def _bind_a_person_airlie_does_not_have(studio: Studio) -> None:
    """A binding row pointing at a user id with no row behind it — `binding_without_user`'s starting state.

    Written by hand with enforcement off because the store cannot produce it: its foreign key refuses the
    insert and deleting a person cascades their bindings away. An operator's own sqlite3 session is the only
    thing that leaves a row like this behind (that client runs no `pragma foreign_keys`), which is why the
    route has an answer for it. One helper because several tests need this exact row and the unique
    `(iss, sub)` means only one of them can create it."""
    studio.store.conn.execute("pragma foreign_keys=off")
    studio.store.conn.execute(
        "insert into oidc_bindings (iss, sub, user_id, bound_at, bound_by) values (?, ?, ?, ?, ?)",
        (ISSUER, "user-123", "no-such-user", studio.clock.t, "admin"),
    )
    studio.store.conn.commit()


def _refusal_shapes(studio: Studio, tmp_path: Path) -> list[tuple[str, httpx.Response]]:
    """One live HTTP response per way this route can refuse, keyed by the gate that closed.

    Built here rather than written as a table of literals because a refusal is defined by the gate that
    produced it, so each case has to run its own `/start`, read its own state off the authorize redirect, and
    set the IdP's reply just before the case that consumes it.

    `declined` and `no_code` are deliberately absent: no browser can make this route answer them. `create_app`
    calls `build_oidc_flow` once per app and `Flow.start`/`Flow.callback` share that one `Attempts`, so a
    state the browser read off `/start` is always live here — and a test's own second `build_oidc_flow` gets a
    second store, which is why an out-of-band state only ever answers `no_attempt`. Those two gates are
    pinned at the
    flow instead, in `test_no_other_failure_value_redirects_than_unbound`, so the whole failure set is still
    covered rather than only the half that happens to be reachable."""
    shapes: list[tuple[str, httpx.Response]] = []

    shapes.append(("no_attempt", studio.callback({"code": "c"})))
    shapes.append(("unknown_state", studio.callback({"code": "c", "state": "never-issued"})))

    # a claim the token itself fails to satisfy: refused at 401 by a TokenRejected
    expired = studio.authorize_params(target="/board/SECRET-1")
    studio.token_for(expired["nonce"], exp=int(studio.clock.t) - 30)
    shapes.append(("expired", studio.callback({"code": "c", "state": expired["state"]})))

    forged_nonce = studio.authorize_params(target="/board/SECRET-1")
    studio.token_for("a-nonce-minted-for-another-attempt")
    shapes.append(("nonce", studio.callback({"code": "c", "state": forged_nonce["state"]})))

    shapes.append(("domain", _domain_refused(tmp_path)))

    unsigned = studio.authorize_params(target="/board/SECRET-1")
    studio.idp.tokens = {"id_token": "unsigned-junk", "access_token": "a"}
    shapes.append(("unsigned", studio.callback({"code": "c", "state": unsigned["state"]})))

    no_id_token = studio.authorize_params(target="/board/SECRET-1")
    studio.idp.tokens = {"access_token": "at-1"}
    shapes.append(("no_id_token", studio.callback({"code": "c", "state": no_id_token["state"]})))

    refused_exchange = studio.authorize_params(target="/board/SECRET-1")
    studio.idp.tokens = {"id_token": "unused", "access_token": "a"}
    studio.idp.reject = {"error": "invalid_client", "error_description": f"secret {SECRET}"}
    shapes.append(("token_refused", studio.callback({"code": "c", "state": refused_exchange["state"]})))
    studio.idp.reject = None

    shapes.append(("idp_unreachable", _idp_unreachable(tmp_path)))
    return shapes


def _idp_unreachable(tmp_path: Path) -> httpx.Response:
    """A callback whose token endpoint cannot be reached at all — the `Unsigned` half of the failure set.

    Built as an app that opens every discovery/JWKS call and then a token exchange over a transport that
    answers nothing, because the seam cannot be swapped mid-flow here: `create_app` calls `build_oidc_flow`,
    which constructs a fresh `Attempts`, so `Studio.with_idp` would throw away the attempt `/start` just saved
    and this case would answer `no_attempt` instead of the gate it is meant to pin."""
    home = _unbound_home(tmp_path, "unreachable")

    class HalfDead(httpx.AsyncBaseTransport):
        """Discovery and the JWKS work; the token endpoint is gone."""

        def __init__(self, host: IdPHost) -> None:
            self.host = host

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                raise httpx.ConnectError("connection refused", request=request)
            return self.host.handle(request)

    studio = Studio(home)
    try:
        studio.idp.tokens = {"id_token": "unused", "access_token": "a"}
        studio.app = create_app(
            studio.cfg, transport=HalfDead(studio.idp), auth_store=studio.store, now=studio.clock
        )
        studio.client = client_for(studio.app)
        params = studio.authorize_params(target="/board/SECRET-1")
        return studio.callback({"code": "c", "state": params["state"]})
    finally:
        studio.store.close()


def test_only_the_unbound_refusal_redirects(studio: Studio, tmp_path: Path) -> None:
    """REQ 1 — every refusal this route can actually answer keeps its status, keeps a JSON body, and keeps no
    Location.

    Enumerated rather than exemplified: the way to break this is a second `if` that "helps" one more failure
    along, and one example cannot see that. `/signin` is public, so redirecting there is safe in a way
    redirecting a failure to its target never is — which is precisely the reason the exception has to stay
    confined to the one gate behind verification. Anything else that grew a Location would be reading a
    destination from a request that just failed."""
    unbound = studio.succeed(target="/board/SECRET-1")
    assert unbound.status_code == 302  # the one exception, so the loop below is about everything else

    # The enumeration must cover the branch that exception lives in, not merely the gate that got the new
    # answer. `unbound` above is one half of `if outcome.failure:`; the other half is a binding whose person
    # is
    # gone. It is seeded on its own store and last, because a connection's `pragma foreign_keys` is persistent
    # and the shipped default (`pragma foreign_keys=on`, `auth/store.py`) would cascade a hand-written orphan
    # row away before the callback read it — leaving the case to degrade silently into the unbound refusal it
    # exists to distinguish itself from, which is exactly the hole that let the mutation through.
    seeded = Studio(_unbound_home(tmp_path, "enumerated"))
    try:
        _bind_a_person_airlie_does_not_have(seeded)
        orphan = seeded.authorize_params(target="/board/SECRET-1")
        seeded.token_for(orphan["nonce"])
        shapes = _refusal_shapes(studio, tmp_path) + [
            ("binding_without_user", seeded.callback({"code": "c", "state": orphan["state"]}))
        ]
    finally:
        seeded.store.close()

    seen = {gate for gate, _ in shapes}
    # the enumeration is only worth anything if it still covers the gates it claims to
    assert {"no_attempt", "expired", "nonce", "domain", "unsigned", "no_id_token", "token_refused"} <= seen, (
        seen
    )

    for gate, response in shapes:
        assert "location" not in response.headers, f"{gate} answered a redirect: {response.headers}"
        assert response.headers.get("content-type", "").startswith("application/json"), gate
        if gate == "binding_without_user":
            assert response.status_code == 403, gate
            assert response.json()["reason"] == "binding_without_user", gate
        elif gate in ("expired", "nonce", "domain", "unsigned"):
            assert response.status_code == 401, gate
        elif gate in ("no_id_token", "token_refused", "idp_unreachable"):
            assert response.status_code == 502, gate
        else:
            assert response.status_code == 400, gate
        assert session_cookie(response) is None, gate
        assert "set-cookie" not in response.headers, gate


def test_no_other_failure_value_redirects_than_unbound(tmp_path: Path) -> None:
    """REQ 1, at the seam the route actually branches on: `Outcome.failure`.

    The route's rule is one string comparison, so the honest version of "only unbound redirects" is: hand the
    route every failure value the flow can produce and check what it answers. Two of them — the declined
    consent screen and a callback with no code — cannot be produced over HTTP at all (see `_refusal_shapes`),
    so they are manufactured here at the flow. Without this half the enumeration would cover only what
    is reachable today, which is exactly how `declined` could one day start redirecting unnoticed. The last
    entry is a gate nobody has written yet: an unknown failure must still be refused, not waved at a page."""
    store = AuthStore(tmp_path / "auth.sqlite")
    try:
        outcomes = [
            Outcome(failure="declined", target="/board/SECRET-1"),
            Outcome(failure="no_code", target="/board/SECRET-1"),
            Outcome(failure="binding_without_user", target="/board/SECRET-1"),
            Outcome(failure="some_future_gate", target="/board/SECRET-1"),
        ]
        refused = _route_refusals(store, outcomes)
        assert len(refused) == len(outcomes), "the harness produced nothing to check"
        for failure, response in refused:
            assert response.status_code == 403, failure
            assert "location" not in response.headers, f"{failure} redirected: {response.headers}"
            assert response.headers.get("content-type", "").startswith("application/json"), failure
            assert response.json()["reason"] == failure, failure
            assert session_cookie(response) is None, failure
            assert "set-cookie" not in response.headers, failure
    finally:
        store.close()


def _route_refusals(store: AuthStore, outcomes: list[Outcome]) -> list[tuple[str, Any]]:
    """Each `Outcome` above, answered by the real route, with only the flow's decision stubbed.

    The double stands in for the IdP-facing half only: `callback` returns the outcome, everything else — the
    route, its branches, its response — is airlie's own. So a change in how a failure becomes HTTP shows up
    here, and a failure that started redirecting could not hide behind a mocked response."""
    results: list[tuple[str, Any]] = []

    class _Decided(Flow):
        def __init__(self, outcome: Outcome) -> None:
            self._outcome = outcome

        @property
        def enabled(self) -> bool:
            return True

        @property
        def issuer(self) -> str:
            return ISSUER

        async def callback(self, query: Mapping[str, str]) -> Outcome:
            return self._outcome

    router = auth_router(store, Resolver(store, AuthSpec(), None), AuthSpec(), oidc=_Decided(Outcome()))
    assert router.routes  # the shipped factory built the route, so a 404 below means a real mistake
    for outcome in outcomes:
        app = FastAPI()
        decided = _Decided(outcome)
        app.include_router(auth_router(store, Resolver(store, AuthSpec(), None), AuthSpec(), oidc=decided))
        response = TestClient(app).get("/v1/auth/oidc/callback?code=c&state=s", follow_redirects=False)
        assert response.status_code != 404, "the route did not use the stubbed flow"
        results.append((str(outcome.failure), response))
    return results


def test_the_unbound_redirect_carries_nothing_the_flow_used(studio: Studio) -> None:
    """REQ 2 — the destination is the sign-in route plus one flag, byte for byte, and nothing else.

    Each value below is on the request that just reached this response, and none of them may appear in the
    Location: the code and the client secret were in the exchange form, the state was live until a moment ago,
    the nonce and verifier are what made the token count, and the `sub` is the key an admin binds a person on.
    An address is kept in browser history and can leave as a Referer, so a subject in a URL is a subject
    handed onward — which is why the pair lives in the log line and not here."""
    params = studio.authorize_params(target="/board/SECRET-1")
    studio.token_for(params["nonce"])
    response = studio.callback({"code": "the-code", "state": params["state"]})
    assert response.status_code == 302
    location = response.headers["location"]
    assert location == "/signin?oidc_unbound=1"
    assert "?" in location  # the flag is a query parameter, not a path airlie has no route for
    for leak in (
        "user-123",  # sub
        ISSUER,  # iss
        "dana@acme.com",  # email claim
        params["state"],
        params["nonce"],
        "the-code",
        SECRET,
        "SECRET-1",  # the target the attempt carried
        "board",
        "&",  # a second parameter would mean the flag is no longer alone
    ):
        assert leak not in location, f"{leak!r} reached the Location: {location}"
    # the verifier went to the token endpoint and must not have surfaced anywhere
    assert "code_verifier" not in location
    assert studio.idp.token_requests[-1]["code_verifier"] not in location


def test_the_unbound_destination_ignores_the_target_the_attempt_carried(studio: Studio) -> None:
    """REQ 3 — the Location is one constant, whatever the attempt was aiming at.

    On a failure the stored target is meaningless: it arrived on the request that just failed, and
    `Outcome.target` collapses to `/signin` for exactly that reason. Asserted byte-for-byte across a whole set
    of targets — including the ones an implementation built from `request.url`, `Host`, `X-Forwarded-Host` or
    `outcome.target` would answer differently — so no such derivation can slip in under the cover of "the
    redirect is only a flag". The Host and forwarded-header variants are sent here because the risk is a
    future implementation reading them; a constant survives them by construction."""
    targets = ["/", "/board/SECRET-1", "/chat", "/metrics", "/admin", "/workflows/x", "//evil.example"]
    seen = set()
    for target in targets:
        response = studio.succeed(target=target)
        assert response.status_code == 302, response.text
        seen.add(response.headers["location"])
        assert response.headers["location"] == "/signin?oidc_unbound=1", target

    weird = studio.authorize_params(target="/board/SECRET-1")
    studio.token_for(weird["nonce"])
    headers = {
        "Host": "attacker.example",
        "X-Forwarded-Host": "attacker.example",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Prefix": "/airlie",
    }
    spoofed = studio.client.get(
        f"/v1/auth/oidc/callback?code=the-code&state={weird['state']}",
        follow_redirects=False,
        headers=headers,
    )
    assert spoofed.status_code == 302
    seen.add(spoofed.headers["location"])
    assert spoofed.headers["location"] == "/signin?oidc_unbound=1"
    assert "attacker" not in spoofed.headers["location"]
    assert seen == {"/signin?oidc_unbound=1"}  # one answer, for seven targets and a spoofed host


def test_the_unbound_redirect_mints_no_session(studio: Studio) -> None:
    """REQ 4 — a refusal mints nothing. Not a cookie, not a row.

    The row is the part a `Set-Cookie` check cannot see: an implementation that called `make_session` and then
    redirected without the cookie would pass a header-only test and leave a live session in the store for a
    person airlie refused."""
    assert _session_rows(studio.store) == 0
    response = studio.succeed(target="/board/SECRET-1")
    assert response.status_code == 302
    assert "set-cookie" not in response.headers, response.headers
    assert session_cookie(response) is None
    assert _session_rows(studio.store) == 0


def test_two_unbound_people_get_the_same_answer(tmp_path: Path) -> None:
    """REQ 5 — one person's unbound redirect is indistinguishable from another's.

    Two separate deployments' worth of flow, two subjects, two targets: same status, same Location, same empty
    body, same header names. Anything that varied here — the subject, the issuer, a per-attempt value, a
    timestamp — would let a caller compare two redirects and read a binding table they have no access to."""
    one = Studio(_unbound_home(tmp_path, "one"))
    two = Studio(_unbound_home(tmp_path, "two"))
    try:
        first_params = one.authorize_params(target="/board/SECRET-1")
        one.token_for(first_params["nonce"], sub="subject-one", email="one@acme.com")
        first = one.callback({"code": "code-one", "state": first_params["state"]})

        second_params = two.authorize_params(target="/metrics")
        two.token_for(second_params["nonce"], sub="subject-two", email="two@acme.com")
        second = two.callback({"code": "code-two", "state": second_params["state"]})

        assert first.status_code == second.status_code == 302
        assert first.headers["location"] == second.headers["location"] == "/signin?oidc_unbound=1"
        assert first.text == second.text == ""
        assert sorted(first.headers.keys()) == sorted(second.headers.keys())
        for value in ("subject-one", "subject-two", "one@acme.com", "two@acme.com", ISSUER, "SECRET-1"):
            assert value not in first.text
            assert value not in second.text
            assert value not in first.headers["location"]
            assert value not in second.headers["location"]
    finally:
        one.store.close()
        two.store.close()


def _unbound_home(tmp_path: Path, name: str) -> Path:
    """A directory `Studio` can be built in. It inherits `Studio`'s creation of the base config, so all it has
    to supply is a place for that config to live."""
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    return home


def _session_rows(store: AuthStore) -> int:
    return int(store.conn.execute("select count(*) from sessions").fetchone()[0])


def test_an_unbound_refusal_is_still_logged_with_the_pair_the_admin_needs(studio: Studio, caplog) -> None:
    """The redirect carries no `iss`/`sub`, so the log line is the whole handoff: it must survive the change
    unchanged, naming both halves, while nothing sensitive joins it or the redirect."""
    with caplog.at_level("WARNING"):
        response = studio.succeed(target="/board/SECRET-1")
    lines = [r.getMessage() for r in caplog.records if "auth_refused" in r.getMessage()]
    joined = " ".join(lines)
    assert f"issuer={ISSUER}" in joined, joined
    assert "sub=user-123" in joined, joined
    assert "unbound" in joined, joined
    assert response.status_code == 302
    assert "user-123" not in response.headers["location"]
    assert ISSUER not in response.headers["location"]


def test_an_unbound_subject_gets_no_session_and_no_user_and_no_target(tmp_path: Path) -> None:
    """The unbound path with nothing bound, from the side that must never change: no session, no new person,
    and no trace of the target the attempt carried.

    The redirect goes to one constant, so honouring the target here is impossible by construction — and this
    is
    the assertion that stays true whichever way the status is spelled, which is why it is kept beside (not in
    place of) the test that pins the 302 itself.

    And no user appears: the tempting version of this branch creates the person on the IdP's say-so, which
    makes the first sign-in work and means anyone the IdP will still assert — a departed account, an IdP-wide
    group — can conjure an airlie identity and choose the subject it is known by."""
    studio = Studio(tmp_path, client_secret_file=str(secret_file_for(tmp_path)))
    assert studio.store.users() == []
    response = studio.succeed(target="/board/SECRET-1")
    assert response.headers["location"] == "/signin?oidc_unbound=1"
    assert "SECRET-1" not in response.headers["location"]
    assert session_cookie(response) is None
    assert "set-cookie" not in response.headers
    assert studio.store.users() == []  # the sign-in created nobody
    studio.store.close()


def test_a_domain_outside_the_allowlist_is_refused(tmp_path: Path) -> None:
    """The gate reads the verified token. A profile with no email at all is refused rather than waved through
    a domain rule it cannot satisfy."""
    studio = Studio(tmp_path, allowed_domains=["other.example"])
    assert studio.succeed().status_code == 401
    studio.store.close()


def test_an_allowed_domain_still_needs_a_binding(tmp_path: Path) -> None:
    studio = Studio(tmp_path, allowed_domains=["acme.com"])
    assert _reached_the_binding_gate(studio.succeed())  # the gate passed; the missing binding is what refuses
    studio.store.close()


def test_a_group_outside_the_allowlist_is_refused(tmp_path: Path) -> None:
    studio = Studio(tmp_path, groups_claim="groups", allowed_groups=["engineering"])
    assert studio.succeed(groups=["marketing"]).status_code == 401
    assert _reached_the_binding_gate(studio.succeed(groups=["engineering", "marketing"]))
    studio.store.close()


# ---- outcomes ----


def test_a_binding_mints_the_session_a_passkey_login_would(studio: Studio) -> None:
    """The end of Slice B as the spec draws it: the callback finishes at the existing `store.make_session()`,
    so the cookie is the same artifact a passkey login sets, with the same attributes, and `/v1/auth/me`
    answers for it."""
    user = studio.bind("user-123")
    response = studio.succeed()
    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/"
    raw = session_cookie(response)
    assert raw is not None
    assert studio.store.session_user(raw) is not None
    assert "httponly" in cookie_attributes(response)
    assert "samesite=strict" in cookie_attributes(response)
    assert "secure" in cookie_attributes(response)
    assert client_for(studio.app, cookie=raw).get("/v1/auth/me").json()["user_id"] == user.id


@pytest.mark.parametrize("target", ["/", "/chat", "/board/ABC-1", "/metrics", "/admin", "/workflows/x"])
def test_the_target_survives_the_round_trip(studio: Studio, target: str) -> None:
    """The destination rides in the saved attempt, never in the callback query. A `target` read from the
    callback's own query is an open redirect chosen by whoever wrote the link."""
    studio.bind("user-123")
    assert studio.succeed(target=target).headers["location"] == target


def test_a_target_that_is_not_a_studio_route_does_not_survive(studio: Studio) -> None:
    studio.bind("user-123")
    assert studio.succeed(target="//evil.example").headers["location"] == "/"


def test_a_302_without_a_session_goes_only_to_the_public_sign_in_screen(studio: Studio) -> None:
    """The invariant the route keeps, restated for the one refusal that is allowed to redirect.

    It used to read "a 302 out of /callback always arrives with the session cookie". That held only because no
    failure could redirect at all, and it was never the property worth having — the property is that a
    session-less browser is never sent somewhere it cannot stand. So the rule is now about the destination: a
    302 with a cookie may name the person's target, and the one 302 without a cookie must name `/signin`
    (`meta.public` in `web/src/router.ts`, so it cannot bounce), and never the target the attempt carried.

    The failure paths carry a real target because /start sanitises and saves it before anything is known about
    the subject, so a refactor that answered "go to your target, you'll get bounced" would produce an
    attacker-chosen redirect, and a guarded page reads to the person as the IdP being broken."""
    unbound = studio.succeed(target="/board/SECRET-1")  # nothing bound: the one refusal that redirects
    assert unbound.status_code == 302
    assert session_cookie(unbound) is None
    assert unbound.headers["location"] == "/signin?oidc_unbound=1"

    studio.idp.tokens = {"id_token": "unsigned-junk", "access_token": "a"}
    unverified = studio.authorize_params(target="/board/SECRET-1")
    refused_unverified = studio.callback({"code": "c", "state": unverified["state"]})
    assert refused_unverified.status_code == 401
    assert "location" not in refused_unverified.headers

    for response in (
        unbound,
        refused_unverified,
        studio.callback({"code": "c"}),  # no state
        studio.callback({"code": "c", "state": "never-issued"}),  # unknown state
    ):
        location = response.headers.get("location")
        if session_cookie(response) is None:
            assert location is None or location == "/signin?oidc_unbound=1", response.status_code
        assert location is None or "SECRET-1" not in location

    studio.bind("user-123")
    signed_in = studio.succeed(target="/board/SECRET-1")
    assert signed_in.status_code == 302
    assert signed_in.headers["location"] == "/board/SECRET-1"
    assert session_cookie(signed_in) is not None


def test_the_failure_body_is_a_fixed_sentence_not_the_idps_words(studio: Studio) -> None:
    """The IdP's text describes airlie to a third party — our client_id, our endpoints, sometimes the secret.
    The browser gets a fixed sentence; the detail belongs in the log.

    The `error` carries `outcome.failure` (`unbound`, `declined`): airlie's own word for which of its gates
    stopped this, never the IdP's. It is what the sign-in screen's note is built on, and it stays one of a
    small set of strings — a body that quoted the IdP would be a body describing airlie to a stranger."""
    studio.idp.statuses["document"] = 503
    studio.idp.document = {"error": f"upstream: {CLIENT_ID} authorize_endpoint {ISSUER}/authorize"}
    response = studio.start()
    assert response.status_code == 502
    for leak in (CLIENT_ID, "authorize", ISSUER):
        assert leak not in response.text


def test_the_signin_screen_reads_the_flag_the_failure_leaves_behind() -> None:
    """The unbound person is told to ask an admin by the web app, and that half of the fix lives on the other
    side of a language boundary: `web/src/lib/oidc.ts` names the query flag `SignInScreen.vue` reads, and
    `web:gate` would happily stay green if it were renamed there while the screen kept comparing against the
    old spelling — the person would get the plain sign-in screen and go bother nobody, the one failure the
    whole unbound path exists to prevent.

    Read the constant, not a sentence: the wording may be reworded, the name may not. (The `iss`/`sub` half of
    the same refusal is pinned by `test_a_refusal_is_logged_with_the_subject_but_never_a_token`, and the bind
    route that consumes that pair by `test_an_admin_binds_a_subject_and_the_person_can_then_sign_in` — this
    check is only the seam.)"""
    web = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "oidc.ts"
    assert re.search(r'OIDC_UNBOUND_FLAG\s*=\s*"oidc_unbound"', web.read_text()), (
        "the sign-in screen's unbound flag was renamed on one side only"
    )


def test_the_refusal_body_names_the_gate_and_nothing_else(tmp_path: Path) -> None:
    """A refusal body the UI can branch on — `reason` — and nothing from the flow.

    It is the *binding-without-a-user* refusal that answers with a body here: `unbound` redirects now, so no
    unbound response carries JSON, and that is pinned by
    `test_an_unbound_subject_is_sent_to_the_sign_in_screen_with_one_flag` rather than weakened into this one.
    The two refusals share one rule, which is what this test still checks: `reason` is airlie's own word
    for the gate, and every other value is either something the caller already held (the subject it just
    presented, its own target) or a secret that must stay server-side. The state was live until this moment,
    and the address the person was aiming at belongs to a redirect that a refused sign-in must not be."""
    studio = Studio(tmp_path)
    try:
        studio.store.conn.execute("pragma foreign_keys=off")
        studio.store.conn.execute(
            "insert into oidc_bindings (iss, sub, user_id, bound_at, bound_by) values (?, ?, ?, ?, ?)",
            (ISSUER, "user-123", "no-such-user", studio.clock.t, "admin"),
        )
        response = studio.succeed(target="/board/SECRET-1")
        assert response.status_code == 403
        body = response.json()
        assert body["reason"] == "binding_without_user"
        assert body["error"] == "sign-in failed"  # the fixed sentence stays
        for leak in ("user-123", "SECRET-1", "board", "the-code", SECRET, ISSUER, CLIENT_ID):
            assert leak not in response.text
    finally:
        studio.store.close()


def test_a_failure_never_echoes_the_target_it_was_handed(studio: Studio) -> None:
    """The target is the one value on this request that a refusal must not repeat.

    `/start` sanitises and saves it before anything is known about the subject, so every failure path is
    holding a destination that arrived on the request which just failed. Repeating it — as a `Location`, or as
    a `next` field a client would obediently open — is the open redirect coming back through the JSON door,
    and on the unbound path it also means a session-less browser sent to a guarded page and bounced into a
    loop. The person's destination is used by exactly one answer: the 302 that arrives with a session."""
    studio.bind("user-123")  # so the failures below are not merely the unbound one
    forged = studio.authorize_params(target="/board/SECRET-1")
    studio.idp.tokens = {"id_token": "unsigned-junk", "access_token": "a"}
    for response in (
        studio.callback({"code": "c", "state": forged["state"]}),  # token did not verify
        studio.callback({"code": "c", "state": "never-issued"}),  # answers no attempt of ours
        studio.callback({"code": "c"}),  # no state
    ):
        assert response.status_code in (400, 401)
        assert "SECRET-1" not in response.text
        assert "board" not in response.text


def test_an_unreachable_idp_is_a_bad_gateway(studio: Studio) -> None:
    """A transport failure is an operational fact about the IdP. A 500 would be both useless to the person and
    a chance to leak a URL or a form; 502 says retry, and the log says why."""

    class Failing(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

    studio.with_idp(Failing())
    response = studio.start()
    assert response.status_code == 502
    assert "connection refused" not in response.text


def test_a_refusal_is_logged_with_the_subject_but_never_a_token(studio: Studio, caplog) -> None:
    """The reason an unbound subject can be bound later is that `iss` and `sub` were logged. Nothing else from
    the flow may be: a token in a log file is a session in a log file."""
    with caplog.at_level("WARNING"):
        studio.succeed()
    lines = [record.getMessage() for record in caplog.records if "auth_refused" in record.getMessage()]
    assert lines, [record.getMessage() for record in caplog.records]
    joined = " ".join(lines)
    assert "user-123" in joined  # what an admin needs in order to bind this person
    assert SECRET not in joined
    assert "at-1" not in joined
    assert (studio.idp.tokens or {}).get("id_token", "no-token") not in joined


# ---- Slice C: the session the callback mints ----


def _max_age(response: httpx.Response) -> int | None:
    for name, value in response.headers.multi_items():
        if name.lower() == "set-cookie" and value.startswith("airlie_session="):
            for part in value.split(";")[1:]:
                key, _, got = part.strip().partition("=")
                if key.lower() == "max-age":
                    return int(got)
    return None


def test_an_oidc_session_is_minted_with_its_own_lifetime_not_session_days(studio: Studio) -> None:
    """The callback's cookie says `session_hours` in the browser, not the 30-day `session_days` a passkey
    login gets. The two have to agree in both places: a store row that outlives the cookie is a session the
    browser threw away, and a cookie outliving the row is a browser that keeps sending a token nothing will
    honour."""
    studio.bind("user-123")
    response = studio.succeed()
    raw = session_cookie(response)
    assert raw is not None
    assert _max_age(response) == 12 * 3600
    assert studio.store.session_expires_at(raw) == studio.clock.t + 12 * 3600


def test_an_oidc_session_is_not_renewed_past_its_cap_by_ordinary_use(tmp_path: Path) -> None:
    """The whole point of the cap, stated where the renewal happens.

    A passkey session gets a fresh 30 days the moment it drops under half its life (`test_auth_api.py` pins
    that), and the same middleware sees an IdP cookie too. Without the ceiling, a person the IdP had
    deactivated would hold a self-renewing airlie session for another month after their first request — and
    because the
    renewal happens on a route that never spoke to the IdP, nothing else in the build would notice.

    The session is walked out in ten-minute steps of real traffic, which is the shape of somebody actually
    working: each call is a request that could have renewed it, and the walk runs right up to the moment the
    passkey rule would have renewed. At every step it survives, the row is still exactly as long as the day it
    was minted and the browser is told the same shortening number the store holds. A fresh client per request,
    because a TestClient keeps the cookies a response sets and would otherwise hide a Set-Cookie that
    mattered."""
    studio = Studio(tmp_path, session_hours=1)
    studio.bind("user-123")
    token = session_cookie(studio.succeed())
    assert token is not None
    minted = studio.store.session_expires_at(token)
    assert minted == studio.clock.t + 3600

    steps = int((minted - studio.clock.t) // 600) - 1
    for _ in range(steps):  # ten-minute steps up to the cap — past the half-life the passkey rule renews at
        studio.clock.t += 600
        response = client_for(studio.app, cookie=token).get("/v1/auth/me")
        assert response.status_code == 200
        assert studio.store.session_expires_at(token) == minted  # never pushed out, not by one second
        if "set-cookie" in response.headers:  # the browser's copy shrinks toward the cap; it never grows
            assert _max_age(response) == int(minted - studio.clock.t)

    studio.clock.t += 3600  # one hour after minting: the cap, and the cookie is on its own way out
    assert client_for(studio.app, cookie=token).get("/v1/auth/me").status_code == 401
    studio.store.close()


def test_a_passkey_session_is_still_renewed_where_an_oidc_one_is_not(tmp_path: Path) -> None:
    """The cap must be about IdP sessions and nothing else. Same app, same middleware, same clock: a session
    minted by a passkey login is still extended on use exactly as it was before this slice, or the change
    would have quietly signed every passkey user out earlier instead of bounding the federation path."""
    studio = Studio(tmp_path, session_hours=1)
    dana = studio.store.add_user("Dana", "member", login="dana")
    token = studio.store.make_session(dana.id, studio.cfg.auth.session_days)
    studio.clock.t += 16 * 86400  # under half of 30 days, which is the renewal trigger
    before = studio.store.session_expires_at(token)
    assert client_for(studio.app, cookie=token).get("/v1/auth/me").status_code == 200
    after = studio.store.session_expires_at(token)
    assert after is not None and before is not None and after > before
    assert after == studio.clock.t + studio.cfg.auth.session_days * 86400
    studio.store.close()


def test_a_refused_oidc_config_still_leaves_passkey_renewal_alone(tmp_path: Path) -> None:
    """`session_hours` is only read through the OIDC block, so an install that has none — the shipped
    default — must not acquire a ceiling it never asked for."""
    studio = Studio(tmp_path)
    dana = studio.store.add_user("Dana", "member", login="dana")
    token = studio.store.make_session(dana.id, 30)
    studio.clock.t += 16 * 86400
    assert client_for(studio.app, cookie=token).get("/v1/auth/me").status_code == 200
    assert studio.store.session_expires_at(token) == studio.clock.t + 30 * 86400
    studio.store.close()


# ---- Slice C: the admin's bind surface ----


def _signed_in(studio: Studio, role: str = "admin") -> tuple[Any, Any]:
    """A client holding a session for a person of this role, and that person. Most of these routes are about
    who is asking, so the id is part of what a test has to keep rather than re-derive from the store."""
    person = studio.store.add_user("Noah" if role == "admin" else "Pat", role, login=role)
    return client_for(studio.app, cookie=studio.store.make_session(person.id, 30)), person


def test_an_admin_binds_a_subject_and_the_person_can_then_sign_in(studio: Studio) -> None:
    """The path an operator actually walks: a person is refused, the log names `iss` and `sub`, the admin
    posts that pair at a person airlie already has, and the next sign-in works. Nothing about the callback
    changed to make that happen — the admin surface writes the table the callback already read."""
    admin = _signed_in(studio)[0]
    dana = studio.store.add_user("Dana", "member", login="dana")
    body = {"user_id": dana.id, "iss": ISSUER, "sub": "user-123", "email_display": "dana@acme.com"}
    r = admin.post("/v1/auth/oidc/bindings", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["user_id"] == dana.id and r.json()["sub"] == "user-123"
    assert studio.store.lookup_binding(ISSUER, "user-123") == dana.id

    signed_in = studio.succeed()
    assert signed_in.status_code == 302
    assert client_for(studio.app, cookie=session_cookie(signed_in)).get("/v1/auth/me").json()["user_id"] == (
        dana.id
    )


def test_the_bind_surface_is_admin_only_and_anonymous_is_401(studio: Studio) -> None:
    """`/oidc/start` and `/callback` are public because a stranger has to reach them; these are not, because
    what they do is grant a person. A member is refused for the same reason they cannot invite or delete
    anyone, and a stranger never gets as far as the role check — serve's middleware stops it first, which is
    only true because the route is not on `PUBLIC_PATHS` (pinned by the test at the end of this group)."""
    member = _signed_in(studio, role="member")[0]
    body = {"user_id": "x", "iss": ISSUER, "sub": "s"}
    assert member.get("/v1/auth/oidc/bindings").status_code == 403
    assert member.post("/v1/auth/oidc/bindings", json=body).status_code == 403
    assert member.delete(f"/v1/auth/oidc/bindings?iss={ISSUER}&sub=s").status_code == 403
    anonymous = client_for(studio.app)
    assert anonymous.get("/v1/auth/oidc/bindings").status_code == 401
    assert anonymous.post("/v1/auth/oidc/bindings", json=body).status_code == 401
    assert anonymous.delete(f"/v1/auth/oidc/bindings?iss={ISSUER}&sub=s").status_code == 401


def test_binding_an_unknown_person_is_404_and_binds_nobody(studio: Studio) -> None:
    """The subject in the log and the person in the People list are two strings an admin has to line up by
    eye, so a mistyped id is the ordinary mistake. It must not create a row that points at nothing."""
    admin = _signed_in(studio)[0]
    r = admin.post(
        "/v1/auth/oidc/bindings", json={"user_id": "no-such-user", "iss": ISSUER, "sub": "user-123"}
    )
    assert r.status_code == 404
    assert studio.store.bindings() == []


def test_an_empty_or_absent_field_is_refused_before_it_reaches_the_table(studio: Studio) -> None:
    """Each of these would otherwise write a row that matches nothing (an empty `sub`) or everything
    (no `user_id`), and a binding is the one row in this deployment that decides who may enter."""
    admin = _signed_in(studio)[0]
    dana = studio.store.add_user("Dana", "member", login="dana")
    for body in (
        {"user_id": dana.id, "iss": ISSUER, "sub": ""},
        {"user_id": dana.id, "iss": "", "sub": "user-123"},
        {"user_id": "", "iss": ISSUER, "sub": "user-123"},
        {"user_id": dana.id, "iss": ISSUER},
        {"iss": ISSUER, "sub": "user-123"},
    ):
        assert admin.post("/v1/auth/oidc/bindings", json=body).status_code == 422, body
    assert studio.store.bindings() == []


def test_the_list_names_who_is_bound_and_rebinding_moves_a_subject_to_one_person(studio: Studio) -> None:
    """Two things the list has to get right: it is the audit trail, so it names the subject, the person and
    the admin who linked them; and a subject corrected to a different person must appear once, because the
    callback's answer has to be single-valued."""
    admin, noah = _signed_in(studio)
    dana = studio.store.add_user("Dana", "member", login="dana")
    sam = studio.store.add_user("Sam", "member", login="sam")
    admin.post("/v1/auth/oidc/bindings", json={"user_id": dana.id, "iss": ISSUER, "sub": "user-123"})
    again = admin.post(
        "/v1/auth/oidc/bindings",
        json={"user_id": sam.id, "iss": ISSUER, "sub": "user-123", "email_display": "s@acme.com"},
    )
    assert again.status_code == 201
    listed = admin.get("/v1/auth/oidc/bindings").json()
    assert len(listed) == 1
    assert listed[0]["user_id"] == sam.id and listed[0]["email_display"] == "s@acme.com"
    assert listed[0]["bound_by"] == noah.id  # the audit trail names who clicked, not merely "an admin"
    assert studio.store.lookup_binding(ISSUER, "user-123") == sam.id


def test_deleting_a_bound_person_signs_them_out_and_is_not_inherited_by_a_namesake(studio: Studio) -> None:
    """The whole hazard in one walk. The store test proves the cascade removes the row; this proves what the
    deployment then does: the cookie the person was holding stops working, the subject that used to be them is
    refused, and a *different* person created afterwards under the same login does not become the person the
    IdP can sign in. Ids are uuid4, so inheritance would take a reused login rather than a reused id — the
    ordinary thing that happens when somebody leaves and is replaced."""
    admin = _signed_in(studio)[0]
    dana = studio.bind("user-123")
    raw = session_cookie(studio.succeed())
    assert raw is not None
    assert client_for(studio.app, cookie=raw).get("/v1/auth/me").status_code == 200

    assert admin.delete(f"/v1/auth/users/{dana.id}").status_code == 204
    assert client_for(studio.app, cookie=raw).get("/v1/auth/me").status_code == 401
    # the subject is nobody now, not a ghost of Dana
    assert _refused_without_a_session(studio.succeed())

    studio.store.add_user("Dana", "admin", login="dana")  # a new person, the same login
    assert _refused_without_a_session(studio.succeed())
    # and only a deliberate admin act gives the newcomer that subject
    made = admin.post("/v1/auth/oidc/bindings", json={"user_id": dana.id, "iss": ISSUER, "sub": "user-123"})
    assert made.status_code == 404  # the deleted row is gone, so binding to it is refused
    newcomer = studio.store.user_by_login("dana")
    assert newcomer is not None and newcomer.id != dana.id
    again = admin.post(
        "/v1/auth/oidc/bindings", json={"user_id": newcomer.id, "iss": ISSUER, "sub": "user-123"}
    )
    assert again.status_code == 201
    assert studio.succeed().status_code == 302


def test_unbinding_stops_the_next_sign_in_and_404s_a_subject_that_was_never_bound(studio: Studio) -> None:
    """Unbind is the revoke an operator can actually perform, so it has to bite on the *next* attempt: the
    subject that signed in a moment ago is refused once its row is gone. And a typo in the pair must answer
    404 rather than 204, or the operator believes a revocation happened that did not."""
    admin = _signed_in(studio)[0]
    studio.bind("user-123")
    assert studio.succeed().status_code == 302

    r = admin.delete(f"/v1/auth/oidc/bindings?iss={ISSUER}&sub=user-123")
    assert r.status_code == 204
    assert _refused_without_a_session(studio.succeed())
    assert admin.delete(f"/v1/auth/oidc/bindings?iss={ISSUER}&sub=typo").status_code == 404


def test_unbinding_leaves_existing_sessions_until_an_admin_ends_them(studio: Studio) -> None:
    """Unbinding removes the route from an IdP subject to a person; it does not end what was already minted,
    and this pins that split rather than leaving it to be assumed. A cookie belongs to a person, not to the
    subject that happened to mint it, so ending it is the explicit per-person call — which is what makes the
    pair of routes a complete revocation: unbind stops the next sign-in, the sessions call ends this one."""
    admin = _signed_in(studio)[0]
    dana = studio.bind("user-123")
    raw = session_cookie(studio.succeed())
    assert raw is not None
    admin.delete(f"/v1/auth/oidc/bindings?iss={ISSUER}&sub=user-123")
    assert client_for(studio.app, cookie=raw).get("/v1/auth/me").json()["user_id"] == dana.id

    ended = admin.delete(f"/v1/auth/users/{dana.id}/sessions")
    assert ended.status_code == 204
    assert client_for(studio.app, cookie=raw).get("/v1/auth/me").status_code == 401


def test_ending_sessions_keeps_the_person_and_their_passkeys(studio: Studio) -> None:
    """The difference between this and `DELETE /v1/auth/users/{id}`, which is the reason both exist: a lost
    laptop or a corporate session that must die now should not cost the person their passkey and an invite."""
    admin = _signed_in(studio)[0]
    dana = studio.bind("user-123")
    raw = session_cookie(studio.succeed())
    assert admin.delete(f"/v1/auth/users/{dana.id}/sessions").status_code == 204
    assert studio.store.user(dana.id) is not None
    assert studio.store.session_user(str(raw)) is None
    assert admin.delete("/v1/auth/users/nobody/sessions").status_code == 404


def test_the_admin_bind_routes_are_not_public(studio: Studio) -> None:
    """serve allows a public path by exact string before resolving anyone. These three must not be on that
    list: a public bind route would let any browser grant itself a person."""
    assert "/v1/auth/oidc/bindings" not in PUBLIC_PATHS
    assert "/v1/auth/oidc/start" in PUBLIC_PATHS  # the two that must stay public
    assert "/v1/auth/oidc/callback" in PUBLIC_PATHS


def session_cookie(response: httpx.Response) -> str | None:
    for name, value in response.headers.multi_items():
        if name.lower() == "set-cookie" and value.startswith("airlie_session="):
            return value.split(";", 1)[0].split("=", 1)[1]
    return None


def cookie_attributes(response: httpx.Response) -> str:
    for name, value in response.headers.multi_items():
        if name.lower() == "set-cookie" and value.startswith("airlie_session="):
            return value.lower()
    return ""
