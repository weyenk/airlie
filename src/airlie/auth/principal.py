"""Who a request is. The order is the plan's: the admin Bearer key, then the Tailscale identity header (only
on a request `tailscale serve` proxied, which arrives from loopback, and only with `auth.tailscale: true`),
then the `airlie_session` cookie a passkey sign-in set. A fresh install (no users, no key) lets loopback in as
a bootstrap admin so `task auth:admin` can create the first person."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import Request

from airlie.auth.store import AuthStore
from airlie.config.schema import AuthSpec

SESSION_COOKIE = "airlie_session"
TAILSCALE_HEADER = "tailscale-user-login"
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


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
    return request.client is not None and request.client.host in LOOPBACK


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
        if login and self.cfg.tailscale and is_loopback(request):
            user = self.store.user_by_login(login)
            if user is None:
                raise NotInvited(login)
            return Principal(user.id, user.name, user.role, "tailscale")
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            user = self.store.session_user(token)
            if user is not None:
                return Principal(user.id, user.name, user.role, "session")
        if self.api_key is None and is_loopback(request) and not self.store.users():
            return BOOTSTRAP
        return None
