"""The check pass end to end: a real ledger, a feed, and what survives a refused request.

``run_check`` runs over ``httpx.MockTransport`` into a real sqlite file. The unit tests prove
the store's rules and the feed's rules separately; this proves they are wired to each other,
which is what a caller depends on: a check that failed still leaves a release to show, and a
feed that never answered at all still leaves a row.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import httpx
import pytest

from airlie.config.schema import AirlieConfig, UpdateSpec
from airlie.update.check import run_check
from airlie.update.feed import BAD_STATUS, UNREACHABLE
from airlie.update.status import build_status
from airlie.update.store import UpdateStore
from tests.support.serve import cfg as base_cfg

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


def update_spec(base_url: str = "https://feed.example.com") -> UpdateSpec:
    return UpdateSpec.model_validate(
        {
            "enabled": True,
            "feed": {"kind": "channel_pointer", "base_url": base_url},
        }
    )


def panel_cfg(tmp_path: Path) -> AirlieConfig:
    """The same feed as ``update_spec``, in the whole config ``build_status`` reads, so one test
    can look at the ledger and at the answer the panel draws from it."""
    cfg = base_cfg(tmp_path)
    cfg.update.enabled = True
    cfg.update.panel = True
    cfg.update.feed.kind = "channel_pointer"
    cfg.update.feed.base_url = "https://feed.example.com"
    return cfg


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


def pointer_only_transport(etag: str) -> httpx.MockTransport:
    """The pointer answers and the release document it names does not exist yet — the asset is
    still propagating after the upload. A good pass with nothing to show."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/releases/" in request.url.path:
            return httpx.Response(404, text="not published yet")
        return httpx.Response(200, json=POINTER, headers={"etag": etag})

    return httpx.MockTransport(handler)


def not_modified_transport(etag: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304, headers={"etag": etag})

    return httpx.MockTransport(handler)


def failing_transport(failure: Exception) -> httpx.MockTransport:
    """A transport that dies outside feed.py's taxonomy, so no ``FeedError`` is raised and only
    ``run_check``'s own widening can turn it into a row."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

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


async def test_a_pass_with_no_release_document_keeps_the_release_it_already_had(tmp_path: Path) -> None:
    """Four passes, the sequence a half-published release actually makes.

    A release-asset 404 is a good pass with nothing to show, and the row it leaves must not become
    the panel's answer: ``channel`` null while ``feed_unavailable`` is false is a false "nothing is
    known" certificate for every deployment reading that channel. The hole used to outlive the pass
    too, because a later 304 carried it forward and recovery needed the etag to change.
    """
    cfg = panel_cfg(tmp_path)
    store = UpdateStore(tmp_path / "update.sqlite")

    first = await run_check(cfg.update, store, transport=feed_transport(good_requests=2))
    assert first.ok is True and first.release is not None

    second = await run_check(cfg.update, store, transport=pointer_only_transport('"v8"'))
    assert second.ok is True and second.reason is None
    assert second.release is not None and second.release.version == "10.1.0"
    assert second.etag == '"v8"'  # the etag this pass was actually handed

    third = await run_check(cfg.update, store, transport=not_modified_transport('"v8"'))
    assert third.ok is True and third.release is not None and third.release.version == "10.1.0"

    fourth = await run_check(cfg.update, store, transport=pointer_only_transport('"v9"'))
    assert fourth.ok is True and fourth.release is not None and fourth.release.version == "10.1.0"

    assert store.last_good() is not None and store.last_good().release is not None
    panel = build_status(cfg, store, None)
    assert panel["feed_unavailable"] is False
    assert panel["channel"] is not None and panel["channel"]["digest"] == DIGEST
    assert [row["version"] for row in panel["available"]] == ["10.1.0"]
    store.close()


@pytest.mark.parametrize(
    "failure",
    [httpx.InvalidURL("Invalid URL"), OSError("too many open files")],
    ids=["invalid_url", "os_error"],
)
async def test_a_transport_failure_the_feed_never_named_is_a_row_not_a_crash(
    tmp_path: Path, failure: Exception
) -> None:
    """A check pass returns, it does not raise — whatever the transport did.

    ``httpx.InvalidURL`` is not an ``httpx.HTTPError``, so it sails straight past feed.py's
    ``except`` and out of ``run_check``, which takes down the admin route that called it and breaks
    the ticker's documented never-a-crash invariant. Anything outside the seven-reason taxonomy
    still has to arrive as a failed pass with a ledger row.
    """
    store = UpdateStore(tmp_path / "update.sqlite")

    outcome = await run_check(update_spec(), store, transport=failing_transport(failure))

    assert outcome.ok is False and outcome.reason == UNREACHABLE
    assert outcome.release is None and outcome.etag is None
    row = store.last_check()
    assert row is not None and row.ok is False and row.reason == UNREACHABLE
    store.close()


async def test_a_malformed_configured_base_url_is_a_row_not_a_crash(tmp_path: Path) -> None:
    """One typo'd air-gap URL used to make POST /v1/admin/update/check answer 500 with no row at
    all. Whether the feed names the reason or ``run_check``'s widening catches it, the operator
    gets a failed pass they can read, not a crash.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request may be built for a URL httpx cannot parse: {request.url}")

    store = UpdateStore(tmp_path / "update.sqlite")

    outcome = await run_check(
        update_spec(base_url="https://[::1"), store, transport=httpx.MockTransport(refuse)
    )

    assert outcome.ok is False and outcome.reason == UNREACHABLE
    assert store.last_check() is not None and store.last_check().ok is False
    store.close()


async def test_a_pass_heals_a_ledger_it_could_not_read(tmp_path: Path) -> None:
    """A ledger that will not open must not wedge the ticker.

    The reads a pass makes are only hints — the etag to offer, the release to carry — and the pass
    has to reach the write that quarantines the unreadable file and starts the history over.
    Otherwise every poll dies on the read, the ledger never heals, and only the operator's shell
    can fix it.
    """
    path = tmp_path / "update.sqlite"
    path.write_bytes(b"this is not a database at all")
    store = UpdateStore(path)

    outcome = await run_check(update_spec(), store, transport=feed_transport(good_requests=2))

    assert outcome.ok is True and outcome.release is not None
    kept = tmp_path / "update.sqlite.corrupt"
    assert kept.read_bytes() == b"this is not a database at all"  # kept, and renamed by the write
    good = store.last_good()
    assert good is not None and good.release is not None and good.release.version == "10.1.0"
    store.close()
