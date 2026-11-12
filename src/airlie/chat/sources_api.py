"""`/v1/airlie/chat/repositories` and `/v1/airlie/chat/sources/...`: a person links an account (GitHub's
device flow: airlie shows a code, the person types it on github.com, airlie polls), sees the repositories it
reaches and unlinks it. Every route is the signed-in person's own. A device code stays in this process: the
person gets the user code and the page to type it on, and a link id only they can poll with; another
person's link id is a 404, as one that is not there. A poll sooner than the source's interval answers
pending without asking GitHub. Linking and unlinking publish the feed's `chat` hint with no key.

The listing is for showing: a source's `error` says the list may be stale (the last good one, kept through a
failure). Nothing here grants anything from it; access goes through RepoAccess.access and may_push."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from airlie.auth.principal import Principal
from airlie.sources.access import RepoAccess
from airlie.sources.protocol import (
    DeviceCode,
    LinkDenied,
    LinkExpired,
    LinkPending,
    RepoSource,
    SourceError,
    UserTokens,
)
from airlie.sources.vault import VaultError

Publish = Callable[[str, str | None], None]
NO_CLIENT_ID = "set story.github_app.client_id in airlie.local.yaml to connect GitHub"
NO_LINK = "there is no such link in progress: start again"


def error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@dataclass
class Attempt:
    user_id: str
    source: str
    code: DeviceCode
    expires_at: float
    next_poll_at: float
    interval: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LinkAttempts:
    """The device codes in flight, in memory: link id -> attempt. One per (person, source): a new start
    replaces the old one. A restart of serve ends them all (the person starts again)."""

    def __init__(self) -> None:
        self._by_id: dict[str, Attempt] = {}

    def start(self, user_id: str, source: str, code: DeviceCode, now: float) -> str:
        self._by_id = {
            i: a
            for i, a in self._by_id.items()
            if a.expires_at > now and (a.user_id, a.source) != (user_id, source)
        }
        link_id = secrets.token_urlsafe(16)
        self._by_id[link_id] = Attempt(
            user_id, source, code, now + code.expires_in, now + code.interval, code.interval
        )
        return link_id

    def find(self, link_id: str, user_id: str, source: str) -> Attempt | None:
        found = self._by_id.get(link_id)
        if found is None or (found.user_id, found.source) != (user_id, source):
            return None
        return found

    def end(self, link_id: str) -> None:
        self._by_id.pop(link_id, None)

    def end_for(self, user_id: str, source: str) -> None:
        self._by_id = {i: a for i, a in self._by_id.items() if (a.user_id, a.source) != (user_id, source)}


def sources_router(
    access: RepoAccess,
    sources: Mapping[str, RepoSource],
    publish: Publish,
    *,
    configured: Iterable[str] | None = None,
    clock: Callable[[], float] = time.time,
) -> APIRouter:
    """`sources`: the sources that can link. `configured`: every name chat.sources lists (default: the keys
    of `sources`); one of them with no source behind it answers 503 with the setting to make."""
    router = APIRouter(prefix="/v1/airlie/chat")
    known = set(sources) if configured is None else set(configured)
    attempts = LinkAttempts()

    def who(request: Request) -> str | None:
        principal: Principal | None = getattr(request.state, "principal", None)
        return None if principal is None else principal.user_id

    def source_of(name: str) -> RepoSource | JSONResponse:
        if name not in known:
            return error(f"{name} is not a source airlie lists repositories from", 404)
        if name not in sources:
            return error(NO_CLIENT_ID, 503)
        return sources[name]

    @router.get("/repositories")
    async def repositories(request: Request, refresh: bool = False) -> Any:
        user = who(request)
        if user is None:
            return error("not signed in", 401)
        found = await access.view(user, refresh=refresh)
        return {
            "sources": [
                {
                    "name": v.name,
                    "title": v.title,
                    "linked": v.linked,
                    "login": v.login,
                    "repositories": [
                        {"repo": r.repo, "private": r.private, "push": r.push, "url": r.url}
                        for r in v.repositories
                    ],
                    "error": v.error,
                    "listed_at": v.listed_at,
                }
                for v in found.sources
            ],
            "extras": list(found.extras),
            "extra_candidates": list(found.extra_candidates),
        }

    @router.post("/sources/{source}/link", status_code=201)
    async def start(source: str, request: Request) -> Any:
        user = who(request)
        if user is None:
            return error("not signed in", 401)
        found = source_of(source)
        if isinstance(found, JSONResponse):
            return found
        try:
            code = await found.start_link()
        except SourceError as e:
            return error(str(e), 502)
        now = clock()
        link_id = attempts.start(user, source, code, now)
        return {
            "link_id": link_id,
            "user_code": code.user_code,
            "verification_uri": code.verification_uri,
            "expires_at": now + code.expires_in,
            "interval": code.interval,
        }

    @router.post("/sources/{source}/link/{link_id}")
    async def poll(source: str, link_id: str, request: Request) -> Any:
        user = who(request)
        if user is None:
            return error("not signed in", 401)
        found = source_of(source)
        if isinstance(found, JSONResponse):
            return found
        attempt = attempts.find(link_id, user, source)
        if attempt is None:
            return error(NO_LINK, 404)
        async with attempt.lock:  # two polls at once ask GitHub once
            if attempts.find(link_id, user, source) is not attempt:
                return error(NO_LINK, 404)
            now = clock()
            if now >= attempt.expires_at:
                attempts.end(link_id)
                return error("the code expired: start again", 410)
            if now < attempt.next_poll_at:
                return JSONResponse({"state": "pending", "interval": attempt.interval}, status_code=202)
            try:
                got = await found.poll_link(attempt.code.device_code)
                if isinstance(got, LinkPending):
                    attempt.interval = got.interval
                    attempt.next_poll_at = clock() + got.interval
                    return JSONResponse({"state": "pending", "interval": got.interval}, status_code=202)
                assert isinstance(got, UserTokens)
                login = await access.save(user, source, got)
            except LinkExpired:
                attempts.end(link_id)
                return error("the code expired: start again", 410)
            except LinkDenied:
                attempts.end(link_id)
                return error("you declined on GitHub", 403)
            except VaultError as e:
                return error(str(e), 503)
            except SourceError as e:
                return error(str(e), 502)
            attempts.end(link_id)
        publish("chat", None)
        return {"state": "linked", "login": login}

    @router.delete("/sources/{source}/link")
    async def unlink(source: str, request: Request) -> Any:
        user = who(request)
        if user is None:
            return error("not signed in", 401)
        found = source_of(source)
        if isinstance(found, JSONResponse):
            return found
        attempts.end_for(user, source)
        if not await access.forget(user, source):
            return error(f"you have not linked {found.title}", 404)
        publish("chat", None)
        return Response(status_code=204)

    return router
