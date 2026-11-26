"""Who a request is. The order is the plan's: a matching admin Bearer key, then the Tailscale identity header
(only with `auth.tailscale: true`, and only on a request a peer we take forwarded headers from proxied:
`tailscale serve` arrives from loopback with the device's tailnet address in X-Forwarded-For), then the
`airlie_session` cookie a passkey sign-in set. A wrong or stale Bearer does not short-circuit the rest (the
SPA may still hold an old admin key). A fresh install (no users, no key) lets a direct loopback request in as
a bootstrap admin so the web app works on the Mac before anyone is invited.

Trust comes from the TCP peer, never from a header a caller controls: serve runs uvicorn with proxy_headers
off, so `request.client` is the real peer. That makes the listen address a reachability choice, not a trust
one — `0.0.0.0` lets remote peers connect, they arrive with their own address, and no header they send is
read. A proxy that speaks for a client must be declared in `auth.trusted_proxies`; `auth.tailscale` is
already the declaration for `tailscale serve` on loopback. The peer a proxy forwards (X-Forwarded-For) is a
fact only about that proxy's upstream, so it is read from declared peers alone.

The fresh-install bootstrap admin is a **loopback-listen** convenience for `airlie serve` on the Mac
(`--host 127.0.0.1`). Binding a routable address (`0.0.0.0`, `::`, a LAN IP) means the process is reachable
past the machine: `airlie serve` then requires an API key and passes `allow_bootstrap=False` so nothing that
can talk to loopback inside that namespace (docker exec, an undeclared local proxy) inherits admin.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Literal

from fastapi import Request

from airlie.auth.store import AuthStore
from airlie.config.schema import AuthSpec

SESSION_COOKIE = "airlie_session"
TAILSCALE_HEADER = "tailscale-user-login"
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
#: Binds that mean "every interface" — reachable past the machine once a port is published.
ALL_INTERFACES = frozenset({"0.0.0.0", "::", "[::]"})
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


class ServeBindError(ValueError):
    """`airlie serve` was asked to listen past loopback without an API key."""


def bind_allows_bootstrap(host: str) -> bool:
    """True only when the listen address is this machine alone.

    Remote peers that reach a routable bind arrive with their own source address and never hit the
    bootstrap predicate — the residual risk is anything that can still talk to loopback *inside* the
    same network namespace (docker exec, an undeclared local proxy). So a routable listen turns the
    onboarding bypass off entirely; local `airlie serve` (default ``127.0.0.1``) keeps it.
    """
    text = host.strip().lower()
    if text in LOOPBACK:
        return True
    if text in ALL_INTERFACES:
        return False
    try:
        return ip_address(text.strip("[]")).is_loopback
    except ValueError:
        return False  # a hostname that is not localhost is treated as routable


def assert_bind_credentials(host: str, accepted_api_keys: tuple[str, ...]) -> None:
    """Refuse to start a routable listen with no Bearer key the process will accept."""
    if bind_allows_bootstrap(host) or accepted_api_keys:
        return
    raise ServeBindError(
        f"host {host!r} is reachable past loopback; pass --api-key (or set "
        "AIRLIE_SERVE_API_KEY / ~/.config/airlie/bridge.env) before publishing the port, "
        "or keep --host 127.0.0.1 for local bootstrap"
    )


def peer_host(request: Request) -> str | None:
    """The TCP peer's address. serve runs uvicorn with `proxy_headers` off, so this is the real peer — the
    machine itself, a peer on another network, or a proxy that shares this network namespace — and never
    something the caller wrote."""
    return None if request.client is None else request.client.host


def is_loopback(request: Request) -> bool:
    """The TCP peer is this machine: the Mac itself, or a proxy running on it. In a container that includes
    a proxy in the same network namespace, which may be carrying a remote client, so it says where the
    connection came from, not who the client is."""
    host = peer_host(request)
    return host is not None and host in LOOPBACK


@lru_cache(maxsize=32)
def _proxy_networks(entries: tuple[str, ...]) -> tuple[IPv4Network | IPv6Network, ...]:
    """`auth.trusted_proxies` parsed once per distinct setting (`config/schema.py` rejects anything that
    would not parse here, so a bad entry is a startup failure, never a silent non-match)."""
    return tuple(ip_network(entry, strict=False) for entry in entries)


def is_trusted_proxy(request: Request, cfg: AuthSpec) -> bool:
    """The TCP peer is a reverse proxy the operator named in `auth.trusted_proxies`. Listing it is the only
    way a peer that is not loopback vouches for a client, and the list is empty by default: no proxy is
    trusted until declared."""
    host = peer_host(request)
    if host is None:
        return False
    try:
        addr = ip_address(host)
    except ValueError:
        return False
    return any(addr in net for net in _proxy_networks(tuple(cfg.trusted_proxies)))


def speaks_for_clients(request: Request, cfg: AuthSpec) -> bool:
    """Peers whose forwarded headers are read at all: one declared in `auth.trusted_proxies`, or loopback
    when `auth.tailscale` is on — that flag is the operator saying the local proxy is `tailscale serve`.
    Every other peer's forwarded identity or protocol is a stranger's assertion."""
    return is_trusted_proxy(request, cfg) or (is_loopback(request) and cfg.tailscale)


def is_direct_loopback(request: Request, cfg: AuthSpec) -> bool:
    """The machine itself, with nothing forwarded and no proxy declared. A peer named in
    `auth.trusted_proxies` is a proxy even when it reaches us over loopback, so requests from it are not
    direct: a fresh install bootstraps for the machine alone, never through a proxy that forwards no
    headers."""
    return (
        is_loopback(request)
        and not is_trusted_proxy(request, cfg)
        and not any(h in request.headers for h in PROXY_HEADERS)
    )


def tailnet_peer(request: Request, cfg: AuthSpec) -> str | None:
    """The tailnet address a proxy forwarded, when the request came through one we take forwarded headers
    from: `tailscale serve` on loopback (`auth.tailscale`) or a declared `auth.trusted_proxies` peer, with
    an X-Forwarded-For whose first address is a Tailscale one. Any other proxy (task tunnel, Funnel, an
    undeclared reverse proxy) forwards other addresses, or none."""
    if not speaks_for_clients(request, cfg):
        return None
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    try:
        addr = ip_address(forwarded)
    except ValueError:
        return None
    return forwarded if any(addr in net for net in TAILNET) else None


def _bearer_matches_api_key(provided: str, expected: str) -> bool:
    """Timing-safe compare; different lengths are a miss (never raise)."""
    a, b = provided.encode(), expected.encode()
    if len(a) != len(b):
        secrets.compare_digest(b, b)
        return False
    return secrets.compare_digest(a, b)


class Resolver:
    def __init__(
        self,
        store: AuthStore,
        cfg: AuthSpec,
        api_key: str | None,
        *,
        accepted_api_keys: tuple[str, ...] | None = None,
        allow_bootstrap: bool = True,
    ) -> None:
        self.store, self.cfg, self.api_key = store, cfg, api_key
        self._allow_bootstrap = allow_bootstrap
        if accepted_api_keys is not None:
            self._accepted_api_keys = accepted_api_keys
        elif api_key:
            self._accepted_api_keys = (api_key,)
        else:
            self._accepted_api_keys = ()

    async def resolve(self, request: Request) -> Principal | None:
        """The request's principal, or None for nobody. Raises NotInvited for a trusted Tailscale login that
        has no user, so the caller can say so instead of asking for a key."""
        auth = request.headers.get("authorization", "")
        # Wrong/stale Bearer must not block Tailscale or a passkey session cookie (SPA may keep an old
        # admin key in localStorage while the person signs in with a passkey).
        if self._accepted_api_keys and auth.startswith("Bearer "):
            bearer = auth.removeprefix("Bearer ")
            if any(_bearer_matches_api_key(bearer, key) for key in self._accepted_api_keys):
                return API_KEY_PRINCIPAL
        login = request.headers.get(TAILSCALE_HEADER)
        unknown_login: str | None = None
        if login and self.cfg.tailscale and tailnet_peer(request, self.cfg):
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
        if (
            self._allow_bootstrap
            and not self._accepted_api_keys
            and is_direct_loopback(request, self.cfg)
            and not self.store.users()
        ):
            return BOOTSTRAP
        return None
