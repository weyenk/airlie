"""`/v1/airlie/chat/pins`: the repositories a person pinned to the chat panel, kept per person in the user
store so a phone and a laptop show the same panel: at most MAX_PINS, each one airlie knows. Unpinning
hides nothing: a repository's chats stay listed. Each change publishes the feed's `chat` hint with no
key (the panel refetches its pins and chats)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from airlie.auth.principal import Principal
from airlie.auth.store import AuthStore
from airlie.chat.repo_session import valid_repo

Publish = Callable[[str, str | None], None]
Pinnable = Callable[[], Awaitable[list[str]]]
"""The repositories a person may pin ("owner/name"): `GET /v1/airlie/repos/names`."""
MAX_PINS = 50


def error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def pins_router(store: AuthStore, publish: Publish, pinnable: Pinnable) -> APIRouter:
    router = APIRouter(prefix="/v1/airlie/chat/pins")

    def who(request: Request) -> str | None:
        principal: Principal | None = getattr(request.state, "principal", None)
        return None if principal is None else principal.user_id

    @router.get("")
    async def list_pins(request: Request) -> Any:
        user = who(request)
        if user is None:
            return error("not signed in", 401)
        return {"pins": store.pins(user)}

    @router.put("/{owner}/{name}")
    async def pin(owner: str, name: str, request: Request) -> Any:
        user = who(request)
        if user is None:
            return error("not signed in", 401)
        repo = f"{owner}/{name}"
        if not valid_repo(repo):
            return error("a pinned repository is owner/name", 422)
        pinned = [r.lower() for r in store.pins(user)]
        if repo.lower() not in pinned:
            if repo.lower() not in {r.lower() for r in await pinnable()}:
                return error(f"{repo} is not a repository airlie knows (GET /v1/airlie/repos/names)", 422)
            if len(pinned) >= MAX_PINS:
                return error(f"at most {MAX_PINS} pinned repositories: unpin one first", 422)
        store.pin(user, repo)
        publish("chat", None)
        return Response(status_code=204)

    @router.delete("/{owner}/{name}")
    async def unpin(owner: str, name: str, request: Request) -> Any:
        user = who(request)
        if user is None:
            return error("not signed in", 401)
        repo = f"{owner}/{name}"
        if not store.unpin(user, repo):
            return error(f"{repo} is not pinned", 404)
        publish("chat", None)
        return Response(status_code=204)

    return router
