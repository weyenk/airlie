"""Spec §C's digest drift table — pure, no I/O.

Digests are opaque strings here; nothing in this module orders one against
another.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from airlie.update.feed import ReleaseJson

Drift = Literal["unknown", "requested", "current", "behind"]


@dataclass(frozen=True)
class DriftResult:
    drift: Drift
    running_digest: str | None
    desired_digest: str | None
    channel_digest: str | None
    feed_ok: bool
    breaking: bool
    state_migration: bool
    schema_epoch_feed: int | None
    manifest_digest: str | None


def classify(
    *,
    running_digest: str | None,
    desired_digest: str | None,
    channel_digest: str | None,
    feed_ok: bool,
    release: ReleaseJson | None = None,
) -> DriftResult:
    """Apply §C's table top-down; first match wins.

    ``unknown``
        Nothing authoritative to compare. The action is the whole line: pin
        ``manifest_digest`` yourself — no comparison was possible.
    ``requested``
        Running matches the *desired* digest. It sits ahead of ``behind`` and
        its guard allows a down feed, so a deliberate pin never degrades into
        ``behind`` or ``unknown``.
    ``current``
        Running matches the channel pointer, feed up or down.
    ``behind``
        Feed up, pointer and running both known and different: the pointer
        digest is what to pin.

    ``current`` on a down feed reads wrong and is kept anyway — that is what
    the table says. It means "still honouring what the cache last showed", not
    "you are up to date".
    """
    if not feed_ok and channel_digest is None:
        # Row 1: feed down and nothing ever cached, so there is no comparison
        # to make.
        drift: Drift = "unknown"
    elif (
        feed_ok
        and running_digest is not None
        and channel_digest is not None
        and running_digest == channel_digest
    ):
        drift = "current"
    elif desired_digest is not None and running_digest == desired_digest:
        drift = "requested"
    elif (
        feed_ok
        and running_digest is not None
        and channel_digest is not None
        and running_digest != channel_digest
    ):
        drift = "behind"
    elif not feed_ok and running_digest is not None:
        drift = "current"
    else:
        drift = "unknown"

    return DriftResult(
        drift=drift,
        running_digest=running_digest,
        desired_digest=desired_digest,
        channel_digest=channel_digest,
        feed_ok=feed_ok,
        breaking=release.breaking if release is not None else False,
        state_migration=release.state_migration if release is not None else False,
        schema_epoch_feed=release.schema_epoch if release is not None else None,
        manifest_digest=desired_digest or channel_digest,
    )
