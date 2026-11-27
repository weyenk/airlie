"""`/v1/auth`: who am I, invites and the people list (admins), passkey registration from an invite, passkey
login, logout. The WebAuthn ceremonies are py_webauthn's; the challenge of each outstanding ceremony is kept
here for five minutes and found again from the credential's clientDataJSON, so a client sends nothing but the
credential back. Refusals are logged as `auth_refused` with a reason and never a token or credential."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, options_to_json_dict, parse_client_data_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from airlie.auth.oidc_flow import Flow, Refused, Unsigned
from airlie.auth.principal import SESSION_COOKIE, Principal, Resolver, speaks_for_clients
from airlie.auth.store import VIA_OIDC, AuthStore, Credential, OIDCBinding, User
from airlie.config.schema import AuthSpec

log = logging.getLogger("airlie.auth")

PREFIX = "/v1/auth"
PUBLIC_PATHS = frozenset(
    f"{PREFIX}/{p}"
    for p in (
        "register/options",
        "register/verify",
        "login/options",
        "login/verify",
        "logout",
        "oidc/start",
        "oidc/callback",
    )
)
"""The routes a person reaches before they are anyone; serve's middleware lets them through. The two OIDC
routes are here because a sign-in through the IdP starts, by definition, from someone airlie does not know
yet — and `/callback` arrives on the IdP's redirect, which carries no cookie airlie would recognize."""

CHALLENGE_SECONDS = 300
MAX_CEREMONIES = 1000
"""Outstanding challenges kept at once; login/options is public, so the oldest go when a flood opens more."""
LOCAL_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")
INVITE_GONE = "invite expired or already used"


class InviteRequest(BaseModel):
    name: str = Field(min_length=1)
    role: Literal["admin", "member"] = "member"
    login: str | None = None


class RegisterOptions(BaseModel):
    invite: str


class RegisterVerify(BaseModel):
    invite: str
    credential: dict[str, Any]


class LoginVerify(BaseModel):
    credential: dict[str, Any]


class BindSubject(BaseModel):
    """Who to link, and the pair an admin read out of the log (`iss=`/`sub=` from the refusal).

    `iss` is not typed `pattern="^https://"`: `OIDCSpec.issuer` is a plain `str`, so a deployment whose
    configured issuer is somehow not https would be bindable today and unbindable if this route refused it.
    The row is the configured issuer either way — this value is only ever copied from a refusal the server
    logged for a token that already verified against `OIDCSpec.issuer`.

    `sub` is `min_length=1` because an empty subject would bind the *absence* of a subject: a token whose
    `sub` claim is empty is already refused in `verify_id_token`, and the table must not hold a row that
    matches nothing at all.

    `email_display` is what the admin saw when they made the link, and is optional for a reason: the IdP's
    groups and profile are not what decides anything here, so a deployment that reads no email must still be
    able to bind. It is stored and listed, never matched.
    """

    user_id: str = Field(min_length=1)
    iss: str = Field(min_length=1)
    sub: str = Field(min_length=1)
    email_display: str | None = None


def _binding_dict(b: OIDCBinding) -> dict[str, Any]:
    """One binding, shaped for the admin list. `email_display` stays display-only here, as it is in the
    table — the pair is the identity, and nothing downstream should read this field as one."""
    return {
        "iss": b.iss,
        "sub": b.sub,
        "user_id": b.user_id,
        "email_display": b.email_display,
        "groups_cached_at": b.groups_cached_at,
        "bound_at": b.bound_at,
        "bound_by": b.bound_by,
    }


def _user_dict(u: User, credentials: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": u.id,
        "name": u.name,
        "login": u.login,
        "role": u.role,
        "created_at": u.created_at,
    }
    if credentials is not None:
        out["credentials"] = credentials
    return out


def _principal(request: Request) -> Principal:
    p = getattr(request.state, "principal", None)
    if p is None:
        raise HTTPException(401, "not signed in")
    return p  # type: ignore[no-any-return]


def _admin(request: Request) -> Principal:
    p = _principal(request)
    if p.role != "admin":
        raise HTTPException(403, "admins only")
    return p


def set_session_cookie(
    response: Response, request: Request, cfg: AuthSpec, token: str, lifetime_seconds: float
) -> None:
    """Give the browser the cookie for a session the store already minted.

    Takes the lifetime in seconds rather than the `days` it was minted for, because the two are not the same
    number for the session this matters most for: an IdP-minted one lasts `auth.oidc.session_hours`. A `days`
    parameter would round 12 hours up to a day and hand the browser a Max-Age the store does not agree with —
    and Max-Age is the one lifetime the browser enforces on its own, without asking airlie."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(lifetime_seconds),
        httponly=True,
        samesite="strict",
        secure=request_is_https(request, cfg),
        path="/",
    )


def request_is_https(request: Request, cfg: AuthSpec) -> bool:
    """https itself, or a peer that may speak for a client — `tailscale serve` on loopback (`auth.tailscale`)
    or one declared in `auth.trusted_proxies` — saying the request it proxied was https. From any other peer
    `X-Forwarded-Proto` is a stranger asserting a protocol, and marking the cookie Secure on a plain-http
    origin would just break the sign-in it belongs to."""
    if request.url.scheme == "https":
        return True
    claimed = request.headers.get("x-forwarded-proto", "").lower()
    return speaks_for_clients(request, cfg) and claimed == "https"


class Ceremonies:
    """Outstanding WebAuthn challenges: challenge → (kind, user id or None, expiry)."""

    def __init__(self, now: Callable[[], float]) -> None:
        self.now = now
        self.pending: dict[bytes, tuple[str, str | None, float]] = {}

    def open(self, challenge: bytes, kind: str, user_id: str | None) -> None:
        t = self.now()
        # insertion order is expiry order (one TTL), so expired ones are at the front
        while self.pending and next(iter(self.pending.values()))[2] <= t:
            del self.pending[next(iter(self.pending))]
        while len(self.pending) >= MAX_CEREMONIES:
            del self.pending[next(iter(self.pending))]
        self.pending[challenge] = (kind, user_id, t + CHALLENGE_SECONDS)

    def take(self, credential: dict[str, Any], kind: str) -> str | None:
        """Pop the ceremony this credential answers; None when there is none, it expired or is the wrong kind.
        Returns the user id the ceremony was opened for ("" for none)."""
        try:
            data = parse_client_data_json(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        except (KeyError, TypeError, ValueError, WebAuthnException):
            return None
        found = self.pending.pop(data.challenge, None)
        if found is None or found[0] != kind or found[2] < self.now():
            return None
        return found[1] or ""


def auth_router(
    store: AuthStore,
    resolver: Resolver,
    cfg: AuthSpec,
    *,
    now: Callable[[], float] = time.time,
    update_panel: bool = False,
    oidc: Flow | None = None,
) -> APIRouter:
    router = APIRouter(prefix=PREFIX)
    ceremonies = Ceremonies(now)

    def rp_id(request: Request) -> str:
        if cfg.rp_id:
            return cfg.rp_id
        host = request.headers.get("host", "localhost")
        return host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0].lstrip("[")

    def origins(request: Request) -> list[str]:
        if cfg.origins:
            return list(cfg.origins)
        out = [f"https://{rp_id(request)}"]
        origin = request.headers.get("origin", "")
        if LOCAL_ORIGIN.match(origin):
            out.append(origin)
        return out

    def refused(reason: str, **fields: Any) -> None:
        log.warning("auth_refused %s %s", reason, " ".join(f"{k}={v}" for k, v in fields.items()))

    def sign_in(response: Response, request: Request, user: User) -> None:
        set_session_cookie(
            response, request, cfg, store.make_session(user.id, cfg.session_days), cfg.session_days * 86400
        )

    # ---- sign in with the company IdP (docs/superpowers/specs/2026-11-26-oidc-sign-in-design.md) ----

    _OIDC_FAILED = "sign-in failed"

    def oidc_denied(code: str, status: int = 400, **fields: Any) -> JSONResponse:
        """One log line naming the check that stopped this sign-in, and a fixed sentence to the browser.

        The IdP's own words never appear in the response: they describe airlie to a third party (client_id,
        endpoints) and on a failed exchange they quote the request, client secret included."""
        refused(code, **fields)
        return JSONResponse({"error": _OIDC_FAILED}, status_code=status)

    @router.get("/oidc/start")
    async def oidc_start(request: Request, target: str | None = None) -> Response:
        if oidc is None or not oidc.enabled:
            # Not enabled here: nothing to explain to someone who typed the URL. The studio only asks the
            # options endpoint whether to show the button, so nobody clicks their way to this.
            raise HTTPException(404, "no such route")
        try:
            location = await oidc.start(target)
        except Unsigned as e:
            refused(f"oidc start: {e.code}")
            return JSONResponse({"error": _OIDC_FAILED}, status_code=502)
        return RedirectResponse(location, status_code=302)

    @router.get("/oidc/callback")
    async def oidc_callback(request: Request) -> Response:
        if oidc is None or not oidc.enabled:
            raise HTTPException(404, "no such route")
        query = {key: str(value) for key, value in request.query_params.items()}
        try:
            outcome = await oidc.callback(query)
        except Refused as e:
            # e.status is the route's answer: 401 for a token that did not verify (Slice D's UI distinguishes
            # "your IdP said no" from "nobody has linked you yet"), 400 for a callback answering no attempt.
            return oidc_denied(f"oidc callback: {e.code}", status=e.status)
        except Unsigned as e:
            refused(f"oidc callback: {e.code}")
            return JSONResponse({"error": _OIDC_FAILED}, status_code=502)
        if outcome.failure:
            # The verified subject is the one thing that makes this fixable: an admin binds exactly that. The
            # state is deliberately not logged — until this moment it was a live secret, and a log line is
            # how a session ends up in a log file. 403, not 400: the request was well-formed and the token
            # verified, airlie just has no permission for this person, which is what tells Slice D's UI that
            # this is a "ask an admin to link you" state rather than a retry.
            refused(f"oidc callback: {outcome.failure}", issuer=oidc.issuer, sub=outcome.subject or "?")
            return oidc_denied(f"oidc callback: {outcome.failure}", status=403)
        user = store.user(str(outcome.user_id))
        if user is None:
            # A binding to a person airlie no longer has: Slice C drops those with the user. Refusing is the
            # only safe reading, and re-linking is an admin action rather than a fresh sign-in.
            return oidc_denied("oidc callback: binding without a user", status=403)
        redirect = RedirectResponse(outcome.target, status_code=302)
        sign_in_oidc(redirect, request, user)
        return redirect

    def sign_in_oidc(response: Response, request: Request, user: User) -> None:
        """Mint the cookie a passkey login would mint, capped by `auth.oidc.session_hours`, and marked as
        coming from the IdP so the request middleware can never renew it past that.

        Two things carry the cap, and both are needed. The shorter lifetime is what mints it; `via=VIA_OIDC`
        plus the ceiling is what keeps it, because the middleware renews a cookie it finds half-spent, and
        without the mark an IdP-minted session would be back to `auth.session_days` after two thirds of a
        day's ordinary use — the drift the spec's "a session outlives the reason it was granted" is about.
        Here rather than in the middleware, because a session has to record its own origin at the moment it is
        created: the request that renews it is a different request, on a different route, with no idea what
        minted the cookie.

        The Max-Age is read back off the row rather than rebuilt from `session_hours`, so the browser's copy
        of the lifetime and the store's copy cannot disagree.
        """
        hours = cfg.oidc.session_hours
        token = store.make_session(user.id, hours / 24, via=VIA_OIDC, ceiling_hours=hours)
        _, ceiling = store.session_renewal(token)
        set_session_cookie(response, request, cfg, token, max(0.0, (ceiling or 0.0) - now()))

    @router.get("/oidc/bindings")
    async def list_bindings(request: Request) -> list[dict[str, Any]]:
        """Every subject airlie lets in, and who it lets in as.

        Admin-only, and not public like the two routes beside it: this is the list an operator reads to answer
        the refusal in the log, and it names which people hold a corporate identity."""
        _admin(request)
        return [_binding_dict(b) for b in store.bindings()]

    @router.post("/oidc/bindings", status_code=201)
    async def bind_subject(request: Request, req: BindSubject) -> dict[str, Any]:
        """Link a verified subject to a person. The one way an airlie user ever gains an IdP identity — the
        callback never creates or adopts anybody, whatever its `email` says.

        The admin supplies the `iss`/`sub` rather than selecting them from a list of attempted sign-ins on
        purpose: `Attempts` is in-memory and single-use, so a subject that was refused five minutes ago is
        already gone from it. The refusal logged `iss=` and `sub=` precisely so this call is possible.

        Re-binding a subject replaces its person (see `AuthStore.bind_subject`), which is how a link made to
        the wrong row is corrected.
        """
        admin = _admin(request)
        if store.user(req.user_id) is None:
            raise HTTPException(404, "no such user")
        binding = store.bind_subject(
            user_id=req.user_id,
            iss=req.iss,
            sub=req.sub,
            bound_by=admin.user_id,
            email_display=req.email_display,
        )
        # The subject is the thing an admin is granting, so the grant is logged. No token and no session
        # belong in a log line; this names who can now sign in, which is what an audit of this table wants.
        log.warning(
            "oidc_bound iss=%s sub=%s user=%s bound_by=%s",
            binding.iss,
            binding.sub,
            binding.user_id,
            binding.bound_by,
        )
        return _binding_dict(binding)

    @router.delete("/oidc/bindings", status_code=204)
    async def unbind_subject(request: Request, iss: str, sub: str) -> Response:
        """Cut a subject off. 404 when nothing was bound, so an admin cannot believe a typo revoked something.

        A person's existing sessions are left alone: this removes the route from an IdP subject to a row, it
        does not end what was already minted, and signing someone out of every device is the explicit
        `DELETE /v1/auth/users/{user_id}/sessions` rather than a side effect here.
        """
        admin = _admin(request)
        if not store.unbind_subject(iss, sub):
            raise HTTPException(404, "no such binding")
        log.warning("oidc_unbound iss=%s sub=%s by=%s", iss, sub, admin.user_id)
        return Response(status_code=204)

    @router.get("/me")
    async def me(request: Request) -> dict[str, Any]:
        p = _principal(request)
        return {
            "user_id": p.user_id,
            "name": p.name,
            "role": p.role,
            "via": p.via,
            "update_panel": update_panel,
        }

    @router.post("/invites", status_code=201)
    async def invite(request: Request, req: InviteRequest) -> dict[str, Any]:
        _admin(request)
        if req.login and store.user_by_login(req.login) is not None:
            raise HTTPException(409, f"a user with login {req.login!r} already exists")
        user = store.add_user(req.name, req.role, login=req.login)
        token = store.make_invite(user.id, cfg.invite_hours)
        return {
            "url": f"/join/{token}",
            "expires_at": now() + cfg.invite_hours * 3600,
            "user": _user_dict(user),
        }

    @router.post("/users/{user_id}/invites", status_code=201)
    async def reinvite(request: Request, user_id: str) -> dict[str, Any]:
        """A fresh link for an existing person: a lapsed invite, or a passkey on another device."""
        _admin(request)
        user = store.user(user_id)
        if user is None:
            raise HTTPException(404, "no such user")
        token = store.make_invite(user.id, cfg.invite_hours)
        return {
            "url": f"/join/{token}",
            "expires_at": now() + cfg.invite_hours * 3600,
            "user": _user_dict(user),
        }

    @router.get("/users")
    async def users(request: Request) -> list[dict[str, Any]]:
        _admin(request)
        return [_user_dict(u, len(store.credentials(u.id))) for u in store.users()]

    @router.delete("/users/{user_id}/sessions", status_code=204)
    async def revoke_sessions(request: Request, user_id: str) -> Response:
        """End every device a person is signed in on, and keep their passkeys.

        Separate from `DELETE /v1/auth/users/{user_id}` because the reason is usually not "remove this
        person": their laptop was left on, they are leaving the company, a corporate session needs to die now
        rather than when it next goes stale. Deleting the user would take the passkeys with it and mean
        re-inviting them to get their own device back.

        It is also the far half of revoking an IdP identity: unbinding a subject stops that subject minting
        new sessions at the callback, and this ends the ones the callback already handed out. The row cannot
        be signed out from the binding — a session belongs to a person, not to the subject that happened to
        mint it.

        404 on an unknown person, so a typo in an id cannot look like it signed someone out. `end_sessions`
        removes rows only (`store.py`), and a row whose person is already gone cannot resolve to anybody.
        """
        admin = _admin(request)
        if store.user(user_id) is None:
            raise HTTPException(404, "no such user")
        store.end_sessions(user_id)
        # `auth_refused` is this router's audit prefix; nothing is being refused here, but a revocation wants
        # to be greppable beside the rest, and no session token belongs in the line either way.
        refused("sessions revoked", user=user_id, by=admin.user_id)
        return Response(status_code=204)

    @router.delete("/users/{user_id}", status_code=204)
    async def delete_user(request: Request, user_id: str) -> Response:
        _admin(request)
        if store.user(user_id) is None:
            raise HTTPException(404, "no such user")
        store.delete_user(user_id)  # credentials, sessions and invites go with them
        return Response(status_code=204)

    @router.post("/register/options")
    async def register_options(request: Request, req: RegisterOptions) -> JSONResponse:
        user = store.peek_invite(req.invite)
        if user is None:
            return JSONResponse({"error": INVITE_GONE}, status_code=410)
        options = generate_registration_options(
            rp_id=rp_id(request),
            rp_name="airlie",
            user_id=user.id.encode(),
            user_name=user.name,
            user_display_name=user.name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[],
        )
        ceremonies.open(options.challenge, "register", user.id)
        return JSONResponse(options_to_json_dict(options))

    @router.post("/register/verify", status_code=201)
    async def register_verify(request: Request, req: RegisterVerify) -> Response:
        user = store.peek_invite(req.invite)
        if user is None:
            return JSONResponse({"error": INVITE_GONE}, status_code=410)
        opened_for = ceremonies.take(req.credential, "register")
        if opened_for != user.id:
            refused("no registration in progress for this invite")
            return JSONResponse({"error": "registration failed"}, status_code=400)
        try:
            verified = verify_registration_response(
                credential=req.credential,
                expected_challenge=parse_client_data_json(
                    base64url_to_bytes(req.credential["response"]["clientDataJSON"])
                ).challenge,
                expected_rp_id=rp_id(request),
                expected_origin=origins(request),
                require_user_verification=True,
            )
        except (WebAuthnException, KeyError, TypeError, ValueError) as e:
            refused(f"registration: {e}", user=user.id)
            return JSONResponse({"error": "registration failed"}, status_code=400)
        if store.take_invite(req.invite) is None:  # consumed by a parallel registration in the meantime
            return JSONResponse({"error": INVITE_GONE}, status_code=410)
        transports = tuple(req.credential.get("response", {}).get("transports") or ())
        store.add_credential(
            Credential(
                verified.credential_id,
                user.id,
                verified.credential_public_key,
                verified.sign_count,
                tuple(str(t) for t in transports),
                now(),
            )
        )
        response = JSONResponse({"user": _user_dict(user)}, status_code=201)
        sign_in(response, request, user)
        return response

    @router.post("/login/options")
    async def login_options(request: Request) -> JSONResponse:
        options = generate_authentication_options(
            rp_id=rp_id(request), user_verification=UserVerificationRequirement.REQUIRED
        )
        ceremonies.open(options.challenge, "login", None)
        return JSONResponse(options_to_json_dict(options))

    @router.post("/login/verify")
    async def login_verify(request: Request, req: LoginVerify) -> Response:
        failed = JSONResponse({"error": "sign-in failed"}, status_code=401)
        if ceremonies.take(req.credential, "login") is None:
            refused("login: no ceremony in progress")
            return failed
        try:
            cred_id = base64url_to_bytes(str(req.credential["rawId"]))
        except (KeyError, ValueError):
            refused("login: malformed credential")
            return failed
        cred = store.credential(cred_id)
        if cred is None:
            refused("login: unknown passkey")
            return failed
        user = store.user(cred.user_id)
        if user is None:
            refused("login: passkey without a user", user=cred.user_id)
            return failed
        try:
            verified = verify_authentication_response(
                credential=req.credential,
                expected_challenge=parse_client_data_json(
                    base64url_to_bytes(req.credential["response"]["clientDataJSON"])
                ).challenge,
                expected_rp_id=rp_id(request),
                expected_origin=origins(request),
                credential_public_key=cred.public_key,
                credential_current_sign_count=cred.sign_count,
                require_user_verification=True,
            )
        except (WebAuthnException, KeyError, TypeError, ValueError) as e:
            refused(f"login: {e}", user=user.id)
            return failed
        store.bump_sign_count(cred.id, verified.new_sign_count)
        response = JSONResponse({"user": _user_dict(user)})
        sign_in(response, request, user)
        return response

    @router.post("/logout", status_code=204)
    async def logout(request: Request) -> Response:
        """End this device's session row and drop the cookie.

        This is the whole of the spec's "revoke on logout" for airlie: the row goes, so the token is dead
        server-side and not merely forgotten by one browser. What it deliberately does *not* do is end the
        person's other sessions, or call the IdP's `end_session_endpoint` — the browser that signed out is not
        the browser that was logged in elsewhere, and single logout across devices is the IdP's own
        front-channel logout, which needs a route of its own and an IdP that calls it (`oidc_http` reads no
        `end_session_endpoint` today). `end_sessions` — all of a person's devices — is the admin route
        `DELETE /v1/auth/users/{user_id}/sessions`, and `web/src/stores/session.ts` posts exactly this one.
        """
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            store.end_session(token)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router
