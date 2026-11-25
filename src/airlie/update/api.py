"""The update surface: `/v1/admin/update`. Admin-only, and mounted only when the section is on, so
with it off these paths do not exist at all.

The one write here is the `desired` pin, and it is checked before it is kept: a digest no release
publishes would strand the operation half-applied (deployed, then rolled back, with the panel unable
to explain why), so an unknown digest never reaches the ledger.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from airlie.auth.api import _admin
from airlie.config.schema import AirlieConfig
from airlie.update.check import run_check
from airlie.update.identity import RunningRelease
from airlie.update.status import build_status
from airlie.update.store import UpdateStore

__all__ = ["updates_router"]


class DesiredPin(BaseModel):
    digest: str


def _known_digests(store: UpdateStore, running: RunningRelease | None) -> set[str]:
    """Digests the operator may pin: the last-good release's, plus what is actually running.

    The running digests are in the set so an installation can pin itself — the hold that stops a
    revert after a deploy you have decided to keep.
    """
    good = store.last_good()
    digests: set[str] = set()
    if good is not None and good.release is not None:
        digests.add(good.release.image.digest)
    if running is not None and running.image_digest:
        digests.add(running.image_digest)
    return digests


def updates_router(cfg: AirlieConfig, store: UpdateStore, running: RunningRelease | None) -> APIRouter:
    """Built only when the update section is on. Every route here is admin-gated."""
    router = APIRouter(prefix="/v1/admin/update", tags=["update"])

    @router.get("")
    def get_status(request: Request) -> dict[str, Any]:
        _admin(request)
        return build_status(cfg, store, running)

    @router.get("/history")
    def get_history(request: Request, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
        _admin(request)
        return {"history": store.history(limit)}

    @router.put("/desired")
    async def put_desired(body: DesiredPin, request: Request) -> dict[str, Any]:
        _admin(request)
        if body.digest not in _known_digests(store, running):
            raise HTTPException(
                status_code=400,
                detail="digest is not published on the last-good release and is not what is running",
            )
        principal = request.state.principal
        store.set_desired(body.digest, actor=principal.user_id)
        await run_check(cfg.update, store)
        return build_status(cfg, store, running)

    @router.delete("/desired")
    async def delete_desired(request: Request) -> dict[str, Any]:
        _admin(request)
        principal = request.state.principal
        store.clear_desired(actor=principal.user_id)
        await run_check(cfg.update, store)
        return build_status(cfg, store, running)

    @router.post("/check")
    async def post_check(request: Request) -> dict[str, Any]:
        _admin(request)
        await run_check(cfg.update, store)
        return build_status(cfg, store, running)

    return router
