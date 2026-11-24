"""§C's table, row by row, plus the honesty rule a pinned feed depends on."""

from __future__ import annotations

import pytest

from airlie.update.drift import Drift, classify
from airlie.update.feed import ImageRef, ReleaseJson

RUN = "sha256:run"
PIN = "sha256:pin"
CHAN = "sha256:chan"


def _release(
    *,
    breaking: bool = False,
    state_migration: bool = False,
    schema_epoch: int | None = 4,
) -> ReleaseJson:
    return ReleaseJson(
        version="2026.11.2",
        released_at="2026-11-02T00:00:00Z",
        git_sha="b" * 40,
        image=ImageRef("registry.example.com/gateway", "2026.11.2", "sha256:i2"),
        notes="",
        breaking=breaking,
        state_migration=state_migration,
        schema_epoch=schema_epoch,
    )


# Cases per §C row: (running, desired, channel, feed_ok) -> expected drift. Each row is
# evaluated in the table's order, first match wins.
@pytest.mark.parametrize(
    ("running", "desired", "channel", "feed_ok", "expected"),
    [
        # Row 1: feed down and nothing cached at all.
        (None, None, None, False, "unknown"),
        # A cached pointer keeps row 1 from firing; with no running digest row 7 still answers.
        (None, None, CHAN, False, "unknown"),
        # Row 2: running the channel pointer.
        (RUN, None, RUN, True, "current"),
        # Row 2 is evaluated before the pin, so running the pointer reads current even
        # while a pin is set.
        (CHAN, PIN, CHAN, True, "current"),
        # Row 3: an explicit pin holds, feed up.
        (PIN, PIN, CHAN, True, "requested"),
        # Row 3 does not require feed_ok, so a pinned source never reads as a missed upgrade.
        (PIN, PIN, CHAN, False, "requested"),
        # Row 4: feed up, no pin, running is neither the pointer nor a pin.
        (RUN, None, CHAN, True, "behind"),
        # Row 5: feed down, no pin, last good cache showed running == pointer.
        (CHAN, None, CHAN, False, "current"),
        # Row 6 is the one that reads wrong: feed down, running is *not* the pointer,
        # and it still says current. Kept because the table is the spec.
        (RUN, None, CHAN, False, "current"),
        # Row 7: feed up, nothing to compare.
        (None, None, None, True, "unknown"),
        (None, None, CHAN, True, "unknown"),
    ],
)
def test_classify_table_rows(
    running: str | None,
    desired: str | None,
    channel: str | None,
    feed_ok: bool,
    expected: Drift,
) -> None:
    result = classify(
        running_digest=running,
        desired_digest=desired,
        channel_digest=channel,
        feed_ok=feed_ok,
    )
    assert result.drift == expected


def test_pinned_feed_down_still_requests_the_digest_it_honours() -> None:
    """The honesty rule.

    The feed is down and a pin holds an older digest, which the code keeps running.
    Reporting `current` here would let the Studio say "up to date" while a known
    release waits, so row 3 needs no `feed_ok` guard and is evaluated before row 6,
    which would otherwise answer `current`.
    """
    result = classify(
        running_digest=RUN,
        desired_digest=RUN,
        channel_digest=CHAN,
        feed_ok=False,
    )
    assert result.drift == "requested"
    assert result.feed_ok is False


def test_manifest_digest_is_the_desired_digest_else_the_pointer() -> None:
    assert (
        classify(
            running_digest=RUN,
            desired_digest=PIN,
            channel_digest=CHAN,
            feed_ok=True,
        ).manifest_digest
        == PIN
    )
    assert (
        classify(
            running_digest=RUN,
            desired_digest=None,
            channel_digest=CHAN,
            feed_ok=True,
        ).manifest_digest
        == CHAN
    )


def test_flags_come_from_the_cached_release() -> None:
    result = classify(
        running_digest=RUN,
        desired_digest=None,
        channel_digest=CHAN,
        feed_ok=True,
        release=_release(breaking=True, state_migration=True, schema_epoch=5),
    )
    assert (result.breaking, result.state_migration, result.schema_epoch_feed) == (True, True, 5)


def test_flags_are_quiet_without_a_cached_release() -> None:
    """No document means no claim: not breaking, not destructive, epoch unknown."""
    result = classify(
        running_digest=RUN,
        desired_digest=None,
        channel_digest=CHAN,
        feed_ok=True,
    )
    assert (result.breaking, result.state_migration, result.schema_epoch_feed) == (
        False,
        False,
        None,
    )
