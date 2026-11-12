"""`/v1/notifications`: the key a browser subscribes with, this person's devices and kinds, a test push."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from airlie.auth.principal import Principal
from airlie.auth.store import AuthStore
from airlie.notify.kinds import KINDS
from airlie.notify.notifier import Notifier


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class Subscription(BaseModel):
    endpoint: str
    keys: PushKeys


class Endpoint(BaseModel):
    endpoint: str


def _is_push_service(endpoint: str, hosts: Sequence[str]) -> bool:
    """https, and a host that is (or is under) a known push service: serve POSTs to this URL."""
    try:
        parts = urlsplit(endpoint)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in hosts)


def notifications_router(
    store: AuthStore, notifier: Notifier, public_key: str | None, push_hosts: Sequence[str]
) -> APIRouter:
    router = APIRouter(prefix="/v1/notifications")

    def who(request: Request) -> str:
        principal: Principal = request.state.principal
        return principal.user_id

    @router.get("")
    def settings(request: Request) -> dict[str, object]:
        user = who(request)
        chosen = store.notification_kinds(user)
        return {
            "key": public_key,
            "kinds": {k: chosen.get(k, k in notifier.default_kinds) for k in KINDS},
            "devices": len(store.push_subscriptions(user)),
        }

    @router.post("/subscriptions", status_code=204)
    def subscribe(body: Subscription, request: Request) -> Response:
        if not _is_push_service(body.endpoint, push_hosts):
            raise HTTPException(422, "endpoint is not a known push service (https on one of the push hosts)")
        store.save_push(who(request), body.endpoint, body.keys.p256dh, body.keys.auth)
        return Response(status_code=204)

    @router.delete("/subscriptions", status_code=204)
    def unsubscribe(body: Endpoint, request: Request) -> Response:
        if any(s.endpoint == body.endpoint for s in store.push_subscriptions(who(request))):
            store.drop_push(body.endpoint)  # only this person's own device
        return Response(status_code=204)

    @router.put("/kinds", status_code=204)
    def set_kinds(body: dict[str, bool], request: Request) -> Response:
        unknown = sorted(set(body) - set(KINDS))
        if unknown:
            raise HTTPException(422, f"unknown notification kind: {', '.join(unknown)}")
        user = who(request)
        for kind, on in body.items():
            store.set_notification_kind(user, kind, on)
        return Response(status_code=204)

    @router.post("/test")
    async def test(request: Request) -> dict[str, int]:
        sent = await notifier.notify(
            who(request), "chat_reply", "airlie", "/", "Notifications are on", force=True
        )
        return {"sent": sent}

    return router
