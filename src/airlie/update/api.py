"""The update surface: `/v1/admin/update`. Admin-only, and mounted only when the section is on, so
with it off these paths do not exist at all.

The one write here is the `desired` pin, and it is checked before it is kept: a digest no release
publishes would strand the operation half-applied (deployed, then rolled back, with the panel unable
to explain why), so an unknown digest never reaches the ledger. That validation is what guards the
pin — the feed check that follows only refreshes the answer the response carries, so a down feed
reports `feed_unavailable` next to an honestly recorded pin rather than blocking the record.
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
    """Digests the operator may pin: what the last-good release publishes, plus what is running.

    The release contributes two values because it advertises two. Its ``image.digest`` is the digest
    the image states about itself; ``image.pull_digest`` is the registry digest of the same image's
    ``repo:tag``, and it is the one the runbook tells an operator to put in a manifest — a pin the
    registry can serve and check. Refusing it would make the documented value unpinnable, so the
    panel would reject the exact string it had just recommended.

    The running digests are in the set so an installation can pin itself — the hold that stops a
    revert after a deploy you have decided to keep. It is also the only digest the running container
    can be *compared* against, which is why the self-report is what the ``requested`` drift state
    still turns on (see ``drift.classify``).
    """
    good = store.last_good()
    digests: set[str] = set()
    if good is not None and good.release is not None:
        digests.add(good.release.image.digest)
        if good.release.image.pull_digest:
            digests.add(good.release.image.pull_digest)
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
        # Recorded, then refreshed — deliberately in that order, not an oversight. The 400 above is
        # what guards the pin: it must be published by the last-good release or be what is already
        # running. This check only refreshes the answer the response carries, so a feed that is down
        # comes back as `feed_unavailable` beside a pin that was honestly recorded. Refusing the
        # write on a down feed would stop an operator recording the digest they are mid-deploying
        # during exactly the outage `docs/wiki/ops/deploying.md` tells them to work through.
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
