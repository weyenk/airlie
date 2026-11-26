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
    channel_pull_digest: str | None = None,
) -> DriftResult:
    """Apply §C's table top-down; first match wins.

    ``unknown``
        Nothing authoritative to compare. The action is the whole line: pin
        ``manifest_digest`` yourself — no comparison was possible.
    ``requested``
        Running matches the *desired* digest. It sits ahead of ``behind`` and
        its guard allows a down feed, so a deliberate pin never degrades into
        ``behind`` or ``unknown``. The desired digest has to be the one the
        container states about itself for this to fire — see the note at
        ``manifest_digest``.
    ``current``
        Running matches the channel pointer, feed up or down.
    ``behind``
        Feed up, pointer and running both known and different: the pointer
        digest is what to pin.

    ``current`` on a down feed reads wrong and is kept anyway — that is what
    the table says. It means "still honouring what the cache last showed", not
    "you are up to date".

    Both digests reach the table only as far as their own kind of comparison:
    ``channel_digest`` is the release's self-reported digest, the one value a
    deployment can state about itself, so rows 2 and 4 compare it with
    ``running_digest`` and nothing else. ``channel_pull_digest`` is that same
    release's registry digest — the value a manifest pins, unknowable from
    inside the container — so it never enters a comparison, only
    ``manifest_digest``, the answer to "what do I put in my manifest". That is
    why row 3 cannot fire for a pull-digest pin: see the note on
    ``manifest_digest`` below.
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
        # What to put in the manifest. The pull digest is the only value a registry can serve against
        # a pin, so it is preferred over the self-reported one, which names the pre-bake build.
        #
        # Row 3 (`requested`) can then only ever fire for a self-report pin, because a container's
        # self-report is never the registry digest of the image it runs — that is the hash fixed
        # point the two-push bake exists around. The honest pull-pinning equivalent would be
        # "the pin is the channel's pull digest *and* the self-report is that release's baked
        # digest", but whenever both hold the channel's baked digest equals the running one, so row
        # 2 already answered `current` — a stricter, better answer. Row 3 is left as it is rather
        # than widened to a condition it could never reach.
        manifest_digest=desired_digest or channel_pull_digest or channel_digest,
    )
