"""RepoAccess: each person's repositories from the sources they linked, cached, with the user token refreshed
at most once per person and source at a time (a refresh token works once). `access` is the server-side rule
every repository chat asks: "source" (in the person's own list), "extra" (a pinned local checkout no source
reaches) or nothing. With no source configured every local name is "source", as before sources existed. No
log line or error here holds a token. The locks are per process: only serve builds a RepoAccess over the
user store (a second process could spend one refresh token twice)."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from airlie.auth.store import AuthStore, SourceLink
from airlie.sources.protocol import RepoSource, SourceError, SourceRepo, TokenRefused, UserTokens
from airlie.sources.vault import TokenVault, VaultError, aad

log = logging.getLogger("airlie.sources")

Access = Literal["source", "extra"]
REFRESH_MARGIN_S = 300
STALE_AFTER = 3  # cache lifetimes a last good list may be kept while the source fails
REACH_CONCURRENCY = 8
ENDED = "GitHub ended airlie's access: connect again"


@dataclass(frozen=True)
class SourceView:
    name: str
    title: str
    linked: bool
    login: str | None
    repositories: tuple[SourceRepo, ...]
    error: str | None
    listed_at: float | None


@dataclass(frozen=True)
class PersonRepos:
    sources: tuple[SourceView, ...]
    extras: tuple[str, ...]
    """The person's pins that are extra candidates, in pin order."""
    extra_candidates: tuple[str, ...]
    """Local names no source reaches."""


class RepoAccess:
    def __init__(
        self,
        store: AuthStore,
        vault: TokenVault,
        sources: Mapping[str, RepoSource],
        local_names: Callable[[], Awaitable[list[str]]],
        *,
        cache_s: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store, self._vault, self._sources = store, vault, sources
        self._local_names, self._cache_s, self._clock = local_names, cache_s, clock
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._listed: dict[tuple[str, str], tuple[float, tuple[SourceRepo, ...]]] = {}
        self._ended: dict[tuple[str, str], str] = {}
        self._candidates: tuple[float, tuple[str, ...]] | None = None

    # --- the rule --------------------------------------------------------------------------------------

    async def view(self, user_id: str, *, refresh: bool = False) -> PersonRepos:
        return await self._view_of(user_id, refresh, refresh)

    async def _view_of(self, user_id: str, refresh_lists: bool, refresh_candidates: bool) -> PersonRepos:
        views = tuple([await self._source_view(user_id, s, refresh_lists) for s in self._sources.values()])
        candidates = await self._extra_candidates(refresh_candidates)
        wanted = {c.lower() for c in candidates}
        extras = tuple(p for p in self._store.pins(user_id) if p.lower() in wanted)
        return PersonRepos(views, extras, candidates)

    async def access(self, user_id: str, repo: str, *, fresh: bool = False) -> Access | None:
        found = await self._view_of(user_id, fresh, False)
        name = repo.lower()
        if fresh and any(v.linked and v.error for v in found.sources):
            return None  # a live listing was asked for and failed: never grant from the stale one
        if not self._sources:
            return "source" if name in {c.lower() for c in found.extra_candidates} else None
        if any(r.repo.lower() == name for v in found.sources for r in v.repositories):
            return "source"
        return "extra" if name in {e.lower() for e in found.extras} else None

    async def may_push(self, user_id: str, repo: str) -> str | None:
        """The refusal, or None when the person may have airlie push `repo` for them."""
        if not self._sources:
            return None  # the mirror's own rule decides
        found = await self._view_of(user_id, True, False)
        name = repo.lower()
        if any(v.linked and v.error for v in found.sources):
            return f"airlie could not check your access to {repo} on GitHub: try again"
        for v in found.sources:
            for r in v.repositories:
                if r.repo.lower() == name:
                    if r.push:
                        return None
                    return f"you cannot push to {repo} on GitHub, so airlie will not push for you"
        if name in {e.lower() for e in found.extras}:
            return f"{repo} is a local checkout: airlie does not push it"
        return f"you have no access to {repo}: connect GitHub, or ask for access to it on GitHub"

    # --- linking ---------------------------------------------------------------------------------------

    async def save(self, user_id: str, source: str, tokens: UserTokens) -> str:
        """Keep a person's tokens for `source` (sealed) and return their login there."""
        async with self._lock((user_id, source)):
            return await self._save(user_id, source, tokens)

    async def _save(self, user_id: str, source: str, tokens: UserTokens) -> str:
        login, account_id = await self._sources[source].account(tokens.access_token)
        refresh = (
            None
            if tokens.refresh_token is None
            else self._vault.seal(tokens.refresh_token, aad=aad(user_id, source, "refresh"))
        )
        self._store.save_link(
            SourceLink(
                user_id=user_id,
                source=source,
                login=login,
                account_id=account_id,
                access_token=self._vault.seal(tokens.access_token, aad=aad(user_id, source, "access")),
                access_expires_at=tokens.access_expires_at,
                refresh_token=refresh,
                refresh_expires_at=tokens.refresh_expires_at,
                created_at=self._clock(),
            )
        )
        self._listed.pop((user_id, source), None)
        self._ended.pop((user_id, source), None)
        return login

    async def forget(self, user_id: str, source: str) -> bool:
        """Revoke at the source (best effort), drop the link, clear the cache. False when there was none."""
        async with self._lock((user_id, source)):  # after any refresh in flight, which then cannot write back
            link = self._store.link(user_id, source)
            if link is not None and source in self._sources:
                try:
                    token = self._vault.open(link.access_token, aad=aad(user_id, source, "access"))
                except VaultError:
                    token = None
                if token is not None:
                    await self._sources[source].revoke(token)
            self._listed.pop((user_id, source), None)
            self._ended.pop((user_id, source), None)
            return self._store.drop_link(user_id, source)

    # --- one person at one source ----------------------------------------------------------------------

    def _lock(self, key: tuple[str, str]) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def _end(self, key: tuple[str, str], why: str) -> None:
        self._store.drop_link(*key)
        self._listed.pop(key, None)
        self._ended[key] = ENDED
        log.warning("source link dropped: user=%s source=%s why=%s", key[0], key[1], why)

    def _unlinked(self, source: RepoSource, key: tuple[str, str]) -> SourceView:
        return SourceView(source.name, source.title, False, None, (), self._ended.get(key), None)

    async def _source_view(self, user_id: str, source: RepoSource, refresh: bool) -> SourceView:
        key = (user_id, source.name)
        async with self._lock(key):
            link = self._store.link(*key)
            if link is None:
                return self._unlinked(source, key)
            try:
                link, token = await self._usable(source, link)
            except TokenRefused as e:
                self._end(key, str(e))
                return self._unlinked(source, key)
            except VaultError as e:
                self._end(key, str(e))
                return self._unlinked(source, key)
            except SourceError as e:
                return self._view(source, link, key, str(e))
            cached = self._listed.get(key)
            if cached is not None and not refresh and self._clock() - cached[0] < self._cache_s:
                return self._view(source, link, key, None)
            try:
                repos = await source.repositories(token)
            except TokenRefused as e:
                self._end(key, str(e))
                return self._unlinked(source, key)
            except SourceError as e:
                return self._view(source, link, key, str(e))
            self._listed[key] = (self._clock(), tuple(repos))
            return self._view(source, link, key, None)

    def _view(
        self, source: RepoSource, link: SourceLink, key: tuple[str, str], error: str | None
    ) -> SourceView:
        got = self._listed.get(key)
        if got is not None and self._clock() - got[0] >= STALE_AFTER * self._cache_s:
            got = None  # a list kept through failures ages out
        return SourceView(
            source.name,
            source.title,
            True,
            link.login,
            got[1] if got else (),
            error,
            got[0] if got else None,
        )

    async def _usable(self, source: RepoSource, link: SourceLink) -> tuple[SourceLink, str]:
        """The link with its access token open, refreshed first when it has under REFRESH_MARGIN_S left."""
        user_id = link.user_id
        token = self._vault.open(link.access_token, aad=aad(user_id, source.name, "access"))
        now = self._clock()
        if link.access_expires_at is None or link.access_expires_at - now >= REFRESH_MARGIN_S:
            return link, token
        if link.refresh_token is None or (
            link.refresh_expires_at is not None and link.refresh_expires_at <= now
        ):
            raise TokenRefused("the refresh token is gone or expired")
        old = self._vault.open(link.refresh_token, aad=aad(user_id, source.name, "refresh"))
        new = await source.refresh(old)
        link = dataclasses.replace(
            link,
            access_token=self._vault.seal(new.access_token, aad=aad(user_id, source.name, "access")),
            access_expires_at=new.access_expires_at,
            refresh_token=(
                None
                if new.refresh_token is None
                else self._vault.seal(new.refresh_token, aad=aad(user_id, source.name, "refresh"))
            ),
            refresh_expires_at=new.refresh_expires_at,
        )
        self._store.save_link(link)
        return link, new.access_token

    # --- the local checkouts ---------------------------------------------------------------------------

    async def _extra_candidates(self, refresh: bool) -> tuple[str, ...]:
        """Local names no source reaches, kept like a listing. A rebuild asks the sources concurrently and
        holds no lock: two rebuilds at once only repeat the asking."""
        held = self._candidates
        if held is not None and not refresh and self._clock() - held[0] < self._cache_s:
            return held[1]
        names = await self._local_names()
        gate = asyncio.Semaphore(REACH_CONCURRENCY)

        async def one(name: str) -> bool:
            async with gate:
                return await self._reached(name)

        reached = await asyncio.gather(*(one(n) for n in names))
        kept = tuple(n for n, r in zip(names, reached, strict=True) if not r)
        self._candidates = (self._clock(), kept)
        return kept

    async def _reached(self, name: str) -> bool:
        for source in self._sources.values():
            try:
                if await source.reaches(name):
                    return True
            except SourceError as e:
                log.warning("could not ask %s whether it reaches %s: %s", source.name, name, e)
                return True  # unsure: never offer a repository GitHub may own as an extra
        return False
