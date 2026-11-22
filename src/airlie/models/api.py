"""`/v1/airlie/models`: the upstream models airlie may use, and Admin's switch for each. A model nobody
has switched is allowed (airlie.models.allowlist). The list is the engine's live ids plus every id with a
saved choice; when the engine cannot be asked the saved ones still list, `live: false`, so Admin can toggle
offline."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from airlie.auth.principal import Principal
from airlie.models.allowlist import ModelAllowlist
from airlie.models.live import LiveModelsError

log = logging.getLogger("airlie.models")

LiveIds = Callable[[], Awaitable[list[str]]]


class AllowedBody(BaseModel):
    allowed: bool


def _admin(request: Request) -> Principal:
    p = getattr(request.state, "principal", None)
    if p is None:
        raise HTTPException(401, "not signed in")
    if p.role != "admin":
        raise HTTPException(403, "admins only")
    return p  # type: ignore[no-any-return]


def models_router(
    allowlist: ModelAllowlist,
    live_ids: LiveIds,
    labels: Mapping[str, str | None],
) -> APIRouter:
    """`live_ids` asks the engine for its model ids (it may raise LiveModelsError); `labels` is the catalog's
    label per model name."""
    router = APIRouter(prefix="/v1/airlie/models")

    @router.get("")
    async def list_models(request: Request) -> list[dict[str, object]]:
        _admin(request)
        try:
            live = await live_ids()
        except LiveModelsError as e:
            log.warning("model list: engine not reachable: %s", e)
            live = []
        live_set = set(live)
        ids = [*live, *(i for i in allowlist.stored_ids() if i not in live_set)]
        known = {k: v for k, v in labels.items() if v}
        return [
            {"id": r.id, "label": r.label, "allowed": r.allowed, "live": r.id in live_set}
            for r in allowlist.merge(ids, labels=known)
        ]

    @router.patch("/{model_id:path}", status_code=204)
    async def set_allowed(model_id: str, body: AllowedBody, request: Request) -> Response:
        _admin(request)
        allowlist.set_allowed(model_id, body.allowed)
        return Response(status_code=204)

    return router
