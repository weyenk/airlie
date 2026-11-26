"""§C's table, row by row, plus the honesty rule a pinned feed depends on."""

from __future__ import annotations

import pytest

from airlie.update.drift import Drift, classify
from airlie.update.feed import ImageRef, ReleaseJson

RUN = "sha256:run"
PIN = "sha256:pin"
CHAN = "sha256:chan"
#: The channel release's registry digest. Never compared with anything: no container can state it.
PULL = "sha256:pull"
#: And its baked digest: what the container running that release self-reports. Two pushes, so it is
#: never equal to PULL.
BAKED = "sha256:baked"


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


def test_manifest_digest_names_the_digest_a_manifest_can_pin() -> None:
    """`manifest_digest` is the answer to "what do I put in my manifest", so it is the pull digest: the
    registry will serve that digest for the tag, and it will serve the self-reported one only as the
    pre-bake build, which reports nothing about itself. The desired pin still wins, so a stale pin is
    reported the way it always was."""
    assert (
        classify(
            running_digest=RUN,
            desired_digest=None,
            channel_digest=CHAN,
            feed_ok=True,
            channel_pull_digest=PULL,
        ).manifest_digest
        == PULL
    )
    assert (
        classify(
            running_digest=RUN,
            desired_digest=PIN,
            channel_digest=CHAN,
            feed_ok=True,
            channel_pull_digest=PULL,
        ).manifest_digest
        == PIN
    )


def test_a_release_without_a_pull_digest_offers_what_it_has() -> None:
    """Every document published before the pull digest existed carries one digest. The panel must keep
    offering it — the best available answer, not a null the operator cannot act on."""
    assert (
        classify(
            running_digest=RUN,
            desired_digest=None,
            channel_digest=CHAN,
            feed_ok=True,
            channel_pull_digest=None,
        ).manifest_digest
        == CHAN
    )


def test_the_pull_digest_never_enters_a_comparison() -> None:
    """Two digests, two kinds of comparison, and never one against the other.

    The rows below pin the same release's *pull* digest while the container self-reports the release's
    baked digest — the state an operator following the runbook actually reaches. Each row's expected
    answer is what the same row gives with no pull digest published at all: adding the second digest
    must not move a state, because the only honest `requested` condition for a pull pin ("the pin is
    the channel's pull digest and the self-report is that release's baked digest") implies the
    self-report equals the channel digest, and row 2 already answered `current` there.
    """
    rows = [
        # The channel still names the release that was pinned: current, from row 2.
        ({"running_digest": BAKED, "desired_digest": PULL, "channel_digest": BAKED}, "current"),
        # The channel moved on: nothing about a pull pin can say "this is what I meant", so behind.
        ({"running_digest": BAKED, "desired_digest": PULL, "channel_digest": CHAN}, "behind"),
        # Feed down with the same pin: row 5, unchanged.
        (
            {"running_digest": BAKED, "desired_digest": PULL, "channel_digest": CHAN, "feed_ok": False},
            "current",
        ),
    ]
    for over, expected in rows:
        over.setdefault("feed_ok", True)
        assert classify(**over).drift == expected  # type: ignore[arg-type]
        with_pull = classify(**over, channel_pull_digest=PULL)  # type: ignore[arg-type]
        assert with_pull.drift == expected, f"{over} moved from {expected} to {with_pull.drift}"


def test_requested_still_holds_a_self_reported_pin() -> None:
    """The state did not go anywhere: it turns on the one digest the running container can state, which
    is still a pin the API accepts (`api._known_digests` keeps the running digest in the set)."""
    result = classify(
        running_digest=BAKED,
        desired_digest=BAKED,
        channel_digest=CHAN,
        feed_ok=True,
        channel_pull_digest=PULL,
    )
    assert result.drift == "requested"


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
