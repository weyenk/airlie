"""One check pass: ask the feed, write the row, return.

A refused feed is a row, never an exception. The panel (Task 7), the CLI (Task 10) and the
tick (Task 8) all read the same ledger, and a caller that had to catch seven different
feed reasons is a caller that ends up swallowing them with a bare ``except``. The row
carries the reason instead, and ``UpdateStore.record_check`` leaves the last good release
untouched, so "the feed is down" and "you are up to date" stay distinguishable on screen.

The same holds for a failure the feed module never got to classify. A malformed configured
``base_url`` raises ``httpx.InvalidURL`` — which is not an ``httpx.HTTPError`` — out of the
client before a request is even built, and a socket error can surface as an ``OSError``.
Neither is a reason the feed named, so both become the ``unreachable`` row here: a crash would
take down the admin route that called it and break the ticker's documented never-a-crash
invariant, and it would leave no row behind to explain the gap in the ledger.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from airlie.config.schema import UpdateSpec
from airlie.update.feed import UNREACHABLE, FeedError, fetch_feed
from airlie.update.store import CheckOutcome, UpdateStore

__all__ = ["run_check"]


def _hint(read: Callable[[], CheckOutcome | None]) -> CheckOutcome | None:
    """One ledger read a pass only uses as a hint: the etag to offer, the release to carry.

    Neither is worth losing the pass for. A ledger that will not open is reported by the readers
    that answer the operator — ``build_status`` reads it directly — and the write at the end of
    ``run_check`` is what quarantines a corrupt file and starts the history over. Letting the
    failure out of here instead would mean a check could never reach that write, so a corrupt
    ledger would stay broken for as long as the ticker kept asking about it.
    """
    try:
        return read()
    except sqlite3.DatabaseError:
        return None


async def run_check(
    cfg: UpdateSpec,
    store: UpdateStore,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CheckOutcome:
    """Fetch the configured feed, record the answer, and return it.

    The conditional-GET etag comes from the previous check, so a feed with nothing new costs
    a 304. On that 304 the feed hands back no documents, so the row carries the last known
    good release forward: ``check_result`` stores no ``changed`` flag, and a row that claimed
    ``ok`` with no release would read as "nothing is known" to every reader of the ledger.
    That carry is not limited to the 304 — see the comment at the carry itself.
    """
    last = _hint(store.last_check)
    try:
        result = await fetch_feed(
            cfg.feed,
            current_etag=last.etag if last is not None else None,
            transport=transport,
            timeout_s=cfg.check.timeout_s,
        )
    except FeedError as exc:
        # ``detail`` is the half the operator needs: the reason says ``unsupported_shape``, the
        # detail names the file or URL that was wrong. Seven ``_fail`` sites build one, and
        # dropping it sent people to read the traversal code to learn what to fix.
        outcome = CheckOutcome(
            checked_at=_now(),
            ok=False,
            reason=exc.reason,
            detail=exc.detail or None,
            release=None,
            pointer=None,
            etag=None,
        )
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
        # A transport or URL failure the feed module could not name. It is still a pass with a
        # reason, because every caller reads the ledger rather than the exception, and ``reason``
        # must stay inside feed.py's seven-reason vocabulary for the panel to render it.
        outcome = CheckOutcome(
            checked_at=_now(),
            ok=False,
            reason=UNREACHABLE,
            detail=f"{type(exc).__name__}: {exc}",
            release=None,
            pointer=None,
            etag=None,
        )
    else:
        release = result.release
        pointer = result.pointer
        if release is None:
            # Carry whenever this pass produced no release, not only when nothing changed. The
            # pointer answered 200 and its release document 404 is a good pass with nothing to
            # show, and storing it as-is replaced the panel's answer with "nothing is known" for
            # the whole window — a later 304 then carried the hole forward, since recovery needed
            # the etag to change. Carrying the pointer with the release keeps the stored pair
            # self-consistent; ``etag`` stays the one this pass was actually given.
            carried = _hint(store.last_good)
            if carried is not None:
                release, pointer = carried.release, carried.pointer
        outcome = CheckOutcome(
            checked_at=result.checked_at,
            ok=True,
            reason=None,
            release=release,
            pointer=pointer,
            etag=result.etag,
        )
    store.record_check(outcome)
    return outcome


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
