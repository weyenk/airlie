"""Who a request is. The order is the plan's: the admin Bearer key, then the Tailscale identity header (only
with `auth.tailscale: true`, and only on a request `tailscale serve` proxied: it arrives from loopback with
the device's tailnet address in X-Forwarded-For), then the `airlie_session` cookie a passkey sign-in set. A
fresh install (no users, no key) lets a direct loopback request in as a bootstrap admin so the web app works
on the Mac before anyone is invited. serve runs uvicorn with proxy_headers off, so `request.client` is the
real peer."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from ipaddress import ip_address, ip_network
from typing import Literal

from fastapi import Request

from airlie.auth.store import AuthStore
from airlie.config.schema import AuthSpec

SESSION_COOKIE = "airlie_session"
TAILSCALE_HEADER = "tailscale-user-login"
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
TAILNET = (ip_network("100.64.0.0/10"), ip_network("fd7a:115c:a1e0::/48"))
"""The addresses Tailscale gives devices; `tailscale serve` forwards the caller's in X-Forwarded-For."""
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", TAILSCALE_HEADER)


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    role: str
    via: Literal["tailscale", "session", "api-key"]


API_KEY_PRINCIPAL = Principal("api-key", "api-key", "admin", "api-key")
BOOTSTRAP = Principal("bootstrap", "bootstrap", "admin", "api-key")


class NotInvited(Exception):
    """A Tailscale identity serve trusts, for a person no one has invited."""

    def __init__(self, login: str) -> None:
        super().__init__(f"{login} is not invited")
        self.login = login


def is_loopback(request: Request) -> bool:
    """The TCP peer is this machine: the Mac itself, or a proxy running on it."""
    return request.client is not None and request.client.host in LOOPBACK


def is_direct_loopback(request: Request) -> bool:
    """The Mac itself, with no proxy in between (nothing forwarded)."""
    return is_loopback(request) and not any(h in request.headers for h in PROXY_HEADERS)


def tailnet_peer(request: Request) -> str | None:
    """The tailnet address `tailscale serve` forwarded, when the request came through it: a loopback peer and
    an X-Forwarded-For whose first address is a Tailscale one. Any other local proxy (task tunnel, Funnel, a
    reverse proxy) forwards other addresses, or none."""
    if not is_loopback(request):
        return None
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    try:
        addr = ip_address(forwarded)
    except ValueError:
        return None
    return forwarded if any(addr in net for net in TAILNET) else None


class Resolver:
    def __init__(self, store: AuthStore, cfg: AuthSpec, api_key: str | None) -> None:
        self.store, self.cfg, self.api_key = store, cfg, api_key

    async def resolve(self, request: Request) -> Principal | None:
        """The request's principal, or None for nobody. Raises NotInvited for a trusted Tailscale login that
        has no user, so the caller can say so instead of asking for a key."""
        auth = request.headers.get("authorization", "")
        if self.api_key and auth.startswith("Bearer "):
            if secrets.compare_digest(auth.removeprefix("Bearer "), self.api_key):
                return API_KEY_PRINCIPAL
            return None
        login = request.headers.get(TAILSCALE_HEADER)
        unknown_login: str | None = None
        if login and self.cfg.tailscale and tailnet_peer(request):
            user = self.store.user_by_login(login)
            if user is not None:
                return Principal(user.id, user.name, user.role, "tailscale")
            unknown_login = login  # maybe a person invited without a login, signed in with a passkey
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            user = self.store.session_user(token)
            if user is not None:
                return Principal(user.id, user.name, user.role, "session")
        if unknown_login is not None:
            raise NotInvited(unknown_login)
        if self.api_key is None and is_direct_loopback(request) and not self.store.users():
            return BOOTSTRAP
        return None
