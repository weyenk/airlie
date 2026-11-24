"""One check pass: ask the feed, write the row, return.

A refused feed is a row, never an exception. The panel (Task 7), the CLI (Task 10) and the
tick (Task 8) all read the same ledger, and a caller that had to catch seven different
feed reasons is a caller that ends up swallowing them with a bare ``except``. The row
carries the reason instead, and ``UpdateStore.record_check`` leaves the last good release
untouched, so "the feed is down" and "you are up to date" stay distinguishable on screen.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from airlie.config.schema import UpdateSpec
from airlie.update.feed import FeedError, fetch_feed
from airlie.update.store import CheckOutcome, UpdateStore

__all__ = ["run_check"]


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
    """
    last = store.last_check()
    try:
        result = await fetch_feed(
            cfg.feed,
            current_etag=last.etag if last is not None else None,
            transport=transport,
            timeout_s=cfg.check.timeout_s,
        )
    except FeedError as exc:
        outcome = CheckOutcome(
            checked_at=_now(), ok=False, reason=exc.reason, release=None, pointer=None, etag=None
        )
    else:
        carried = None if result.changed else store.last_good()
        release = result.release
        pointer = result.pointer
        if release is None and carried is not None:
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
