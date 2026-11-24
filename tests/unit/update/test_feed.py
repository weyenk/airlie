"""The feed reader: what it refuses, and the one secret it carries.

``httpx.MockTransport`` keeps the network out; the directory tests use ``tmp_path``.
Every assertion here is about a rule, not about a shape we happen to produce.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from airlie.config.schema import FeedSpec
from airlie.update import feed
from airlie.update.feed import (
    BAD_STATUS,
    DEV_ON_STABLE,
    MISMATCHED_DIGEST,
    NO_DIGEST,
    NOT_CONFIGURED,
    UNREACHABLE,
    UNSUPPORTED_SHAPE,
    FeedError,
    fetch_feed,
)

DIGEST = "sha256:" + "a" * 64

#: §B's release document, field for field. The contract test pins this shape against the fixture.
RELEASE = {
    "version": "10.1.0",
    "released_at": "2026-11-22T00:00:00Z",
    "git_sha": "0" * 40,
    "image": {"repo": "ghcr.io/example/airlie", "tag": "10.1.0", "digest": DIGEST},
    "notes": "notes",
    "breaking": False,
    "state_migration": False,
    "schema_epoch": 1,
}


def channel_feed(**over: object) -> FeedSpec:
    base: dict[str, object] = {"kind": "channel_pointer", "base_url": "https://feed.example.com"}
    base.update(over)
    return FeedSpec.model_validate(base)


def directory_feed(root: Path | None = None, **over: object) -> FeedSpec:
    base: dict[str, object] = {"kind": "pinned"}
    if root is not None:
        base["path"] = str(root)
    base.update(over)
    return FeedSpec.model_validate(base)


def pointer_body(**over: object) -> dict[str, object]:
    doc = {
        "version": "10.1.0",
        "digest": DIGEST,
        "released_at": "2026-11-22T00:00:00Z",
        "release_json": "releases/10.1.0.json",
    }
    doc.update(over)
    return doc


def write_feed(root: Path, **pointer_over: object) -> None:
    """A feed directory: a pointer under ``channels/`` and a document relative to the root."""
    (root / "channels").mkdir(parents=True)
    (root / "releases").mkdir(parents=True)
    (root / "channels" / "stable.json").write_text(json.dumps(pointer_body(**pointer_over)), encoding="utf-8")
    (root / "releases" / "10.1.0.json").write_text(json.dumps(RELEASE), encoding="utf-8")


def stub(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_304_carries_the_etag_and_no_release() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["if-none-match"] == '"v7"'
        return httpx.Response(304)

    result = await fetch_feed(channel_feed(), current_etag='"v7"', transport=stub(handler))
    assert result.changed is False
    assert result.release is None
    assert result.pointer is None
    assert result.etag == '"v7"'
    assert seen == ["/channels/stable.json"]


async def test_200_parses_the_pointer_then_the_release_document() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/channels/stable.json":
            return httpx.Response(200, json=pointer_body(), headers={"etag": '"v8"'})
        return httpx.Response(200, json=RELEASE)

    result = await fetch_feed(channel_feed(), transport=stub(handler))
    assert seen == ["/channels/stable.json", "/releases/10.1.0.json"]
    assert result.changed is True
    assert result.etag == '"v8"'
    assert result.pointer is not None and result.pointer.version == "10.1.0"
    assert result.release is not None
    assert result.release.image.digest == DIGEST
    assert result.release.released_at == "2026-11-22T00:00:00Z"
    assert result.release.git_sha == "0" * 40
    assert result.release.schema_epoch == 1
    assert result.release.state_migration is False
    assert result.release.notes == "notes"


async def test_a_pointer_with_no_release_yet_reports_no_candidate() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "channels" in request.url.path:
            return httpx.Response(200, json=pointer_body())
        return httpx.Response(404)

    result = await fetch_feed(channel_feed(), transport=stub(handler))
    assert result.changed is True
    assert result.release is None
    assert result.pointer is not None


async def test_an_empty_digest_is_refused() -> None:
    blank = {**RELEASE, "image": {**RELEASE["image"], "digest": ""}}

    def handler(request: httpx.Request) -> httpx.Response:
        if "channels" in request.url.path:
            return httpx.Response(200, json=pointer_body())
        return httpx.Response(200, json=blank)

    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(channel_feed(), transport=stub(handler))
    assert excinfo.value.reason == NO_DIGEST


async def test_a_digest_that_is_not_alg_colon_hex_is_refused() -> None:
    odd = {**RELEASE, "image": {**RELEASE["image"], "digest": "deadbeef" * 8}}

    def handler(request: httpx.Request) -> httpx.Response:
        if "channels" in request.url.path:
            return httpx.Response(200, json=pointer_body())
        return httpx.Response(200, json=odd)

    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(channel_feed(), transport=stub(handler))
    assert excinfo.value.reason == MISMATCHED_DIGEST


async def test_a_channel_that_is_not_there_is_bad_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="no such channel")

    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(channel_feed(), transport=stub(handler))
    assert excinfo.value.reason == BAD_STATUS
    assert "404" in str(excinfo.value)


async def test_a_plain_http_feed_is_refused_before_it_reaches_the_network() -> None:
    # The token rides on this request, so it never goes anywhere without TLS.
    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(channel_feed(base_url="http://updates.example.com"), transport=stub(_boom))
    assert excinfo.value.reason == NOT_CONFIGURED


async def test_a_non_200_is_a_bad_status_and_the_body_stays_unread() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="the operator's private error text")

    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(channel_feed(), transport=stub(handler))
    assert excinfo.value.reason == BAD_STATUS
    assert "500" in str(excinfo.value)
    assert "private" not in str(excinfo.value)


async def test_a_transport_failure_is_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(channel_feed(), transport=stub(handler))
    assert excinfo.value.reason == UNREACHABLE


async def test_a_dev_build_on_the_stable_channel_is_refused() -> None:
    dev = {
        "version": "10.1.1-rc.1",
        "released_at": "2026-07-29T00:00:00Z",
        "git_sha": "f" * 40,
        "image": {"repo": "ghcr.io/noahkruswicki/airlie", "tag": "10.1.1-rc.1", "digest": DIGEST},
        "notes": "notes",
        "breaking": False,
        "state_migration": False,
        "schema_epoch": 1,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/channels/" in request.url.path:
            return httpx.Response(200, json=pointer_body(version="10.1.1-rc.1"))
        return httpx.Response(200, json=dev)

    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(channel_feed(), transport=stub(handler))
    assert excinfo.value.reason == DEV_ON_STABLE


async def test_the_same_build_on_latest_is_not_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/channels/" in request.url.path:
            return httpx.Response(200, json=pointer_body(version="10.1.1-rc.1"))
        return httpx.Response(
            200,
            json={
                **RELEASE,
                "version": "10.1.1-rc.1",
                "image": {**RELEASE["image"], "tag": "10.1.1-rc.1"},
            },
        )

    result = await fetch_feed(channel_feed(channel="latest"), transport=stub(handler))
    assert result.release is not None and result.release.version == "10.1.1-rc.1"


async def test_no_configured_feed_is_not_configured() -> None:
    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(FeedSpec(kind="channel_pointer", channel="stable"))
    assert excinfo.value.reason == NOT_CONFIGURED


def _boom(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never run
    raise AssertionError(f"the pinned source reached the network: {request.url}")


async def test_a_pinned_version_and_digest_synthesises_a_release() -> None:
    result = await fetch_feed(
        FeedSpec(kind="pinned", pinned_version="10.1.0", pinned_digest=DIGEST),
        transport=stub(_boom),
    )
    assert result.changed is True
    assert result.release is not None
    assert result.release.image.digest == DIGEST
    assert result.pointer is None


async def test_a_pinned_path_reads_the_feed_directory(tmp_path: Path) -> None:
    write_feed(tmp_path)
    result = await fetch_feed(directory_feed(tmp_path), transport=stub(_boom))
    assert result.changed is True
    assert result.pointer is not None and result.pointer.digest == DIGEST
    assert result.release is not None and result.release.image.digest == DIGEST
    assert result.release.notes == "notes"


async def test_a_pin_the_feed_disagrees_with_is_refused(tmp_path: Path) -> None:
    write_feed(tmp_path)
    cfg = directory_feed(tmp_path, pinned_digest="sha256:" + "f" * 64)
    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(cfg, transport=stub(_boom))
    assert excinfo.value.reason == MISMATCHED_DIGEST


async def test_a_pinned_release_path_escaping_the_root_is_refused(tmp_path: Path) -> None:
    (tmp_path.parent / "outside.json").write_text(json.dumps(RELEASE), encoding="utf-8")
    write_feed(tmp_path, release_json="../outside.json")
    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(directory_feed(tmp_path), transport=stub(_boom))
    assert excinfo.value.reason == UNSUPPORTED_SHAPE


def test_the_reader_only_ever_gets() -> None:
    # §B: the deployment reads the feed and never writes to it. A push anywhere in this
    # module is a design change, not a refactor, so the source itself is the guard.
    source = Path(feed.__file__).read_text(encoding="utf-8")
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert verb not in source
    for call in (".post(", ".put(", ".patch(", ".delete("):
        assert call not in source
