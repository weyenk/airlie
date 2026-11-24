"""The check pass end to end: a real ledger, a feed, and what survives a refused request.

``run_check`` runs over ``httpx.MockTransport`` into a real sqlite file. The unit tests prove
the store's rules and the feed's rules separately; this proves they are wired to each other,
which is what a caller depends on: a check that failed still leaves a release to show.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import httpx

from airlie.config.schema import UpdateSpec
from airlie.update.check import run_check
from airlie.update.feed import BAD_STATUS
from airlie.update.store import UpdateStore

DIGEST = "sha256:" + "a" * 64
DESIRED = "sha256:" + "b" * 64

POINTER = {
    "version": "10.1.0",
    "digest": DIGEST,
    "released_at": "2026-07-12T09:00:00Z",
    "release_json": "releases/10.1.0.json",
}

RELEASE = {
    "version": "10.1.0",
    "released_at": "2026-07-12T09:00:00Z",
    "git_sha": "0" * 40,
    "notes": "Nothing surprising.",
    "breaking": False,
    "state_migration": False,
    "schema_epoch": 1,
    "image": {"repo": "ghcr.io/noahkruswicki/airlie", "tag": "10.1.0", "digest": DIGEST},
}


def update_spec() -> UpdateSpec:
    return UpdateSpec.model_validate(
        {
            "enabled": True,
            "feed": {"kind": "channel_pointer", "base_url": "https://feed.example.com"},
        }
    )


def feed_transport(good_requests: int) -> httpx.MockTransport:
    """Answer ``good_requests`` calls with the two documents, then refuse everything else."""
    calls = itertools.count()

    def handler(request: httpx.Request) -> httpx.Response:
        if next(calls) >= good_requests:
            return httpx.Response(503, text="maintenance")
        if request.url.path.endswith(".json") and "/releases/" not in request.url.path:
            return httpx.Response(200, json=POINTER, headers={"etag": '"v7"'})
        return httpx.Response(200, json=RELEASE)

    return httpx.MockTransport(handler)


async def test_a_good_check_records_the_release_and_the_pointer(tmp_path: Path) -> None:
    store = UpdateStore(tmp_path / "update.sqlite")

    outcome = await run_check(update_spec(), store, transport=feed_transport(good_requests=2))

    assert outcome.ok is True and outcome.reason is None
    assert outcome.release is not None and outcome.release.version == "10.1.0"
    assert outcome.etag == '"v7"'
    check = store.last_check()
    assert check is not None and check.ok is True
    assert check.pointer is not None and check.pointer.version == "10.1.0"
    assert check.pointer.digest == DIGEST
    store.close()


async def test_a_refused_feed_leaves_the_last_good_release_standing(tmp_path: Path) -> None:
    store = UpdateStore(tmp_path / "update.sqlite")
    cfg = update_spec()
    store.set_desired(DESIRED, "10.1.0")

    good = await run_check(cfg, store, transport=feed_transport(good_requests=2))
    assert good.ok is True

    bad = await run_check(cfg, store, transport=feed_transport(good_requests=0))
    assert bad.ok is False and bad.reason == BAD_STATUS
    assert bad.release is None

    # The refusal is a row, not a rewrite: the newest check is the failure, the known
    # release is still the answer, and what the operator asked for is untouched.
    check = store.last_check()
    assert check is not None and check.ok is False and check.reason == BAD_STATUS
    last_good = store.last_good()
    assert last_good is not None and last_good.release is not None
    assert last_good.release.version == "10.1.0"
    assert store.desired() == (DESIRED, "10.1.0")
    store.close()


async def test_the_next_check_carries_the_stored_etag(tmp_path: Path) -> None:
    store = UpdateStore(tmp_path / "update.sqlite")
    cfg = update_spec()
    await run_check(cfg, store, transport=feed_transport(good_requests=2))

    sent: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get("if-none-match"))
        return httpx.Response(304, headers={"etag": '"v7"'})

    outcome = await run_check(cfg, store, transport=httpx.MockTransport(handler))

    assert sent == ['"v7"']
    assert outcome.ok is True
    assert outcome.release is not None and outcome.release.version == "10.1.0"
    store.close()
