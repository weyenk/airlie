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
from fastapi.responses import JSONResponse
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

from airlie.auth.principal import SESSION_COOKIE, Principal, Resolver, is_loopback
from airlie.auth.store import AuthStore, Credential, User
from airlie.config.schema import AuthSpec

log = logging.getLogger("airlie.auth")

PREFIX = "/v1/auth"
PUBLIC_PATHS = frozenset(
    f"{PREFIX}/{p}"
    for p in ("register/options", "register/verify", "login/options", "login/verify", "logout")
)
"""The routes a person reaches before they are anyone; serve's middleware lets them through."""

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


def request_is_https(request: Request) -> bool:
    """https itself, or `tailscale serve` (loopback) saying the request it proxied was https."""
    if request.url.scheme == "https":
        return True
    return is_loopback(request) and request.headers.get("x-forwarded-proto", "").lower() == "https"


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
    store: AuthStore, resolver: Resolver, cfg: AuthSpec, *, now: Callable[[], float] = time.time
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
        token = store.make_session(user.id, cfg.session_days)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=cfg.session_days * 86400,
            httponly=True,
            samesite="strict",
            secure=request_is_https(request),
            path="/",
        )

    @router.get("/me")
    async def me(request: Request) -> dict[str, Any]:
        p = _principal(request)
        return {"user_id": p.user_id, "name": p.name, "role": p.role, "via": p.via}

    @router.post("/invites", status_code=201)
    async def invite(request: Request, req: InviteRequest) -> dict[str, Any]:
        _admin(request)
        if req.login and store.user_by_login(req.login) is not None:
            raise HTTPException(409, f"a user with login {req.login!r} already exists")
        user = store.add_user(req.name, req.role, login=req.login)
        token = store.make_invite(user.id, cfg.invite_hours)
        return {
            "url": f"/#/join/{token}",
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
            "url": f"/#/join/{token}",
            "expires_at": now() + cfg.invite_hours * 3600,
            "user": _user_dict(user),
        }

    @router.get("/users")
    async def users(request: Request) -> list[dict[str, Any]]:
        _admin(request)
        return [_user_dict(u, len(store.credentials(u.id))) for u in store.users()]

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
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            store.end_session(token)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router
