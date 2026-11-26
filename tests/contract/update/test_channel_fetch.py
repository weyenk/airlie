"""The published documents are the contract, so this reads copies of what gets published.

The unit tests hand-build documents to exercise refusals. These serve the fixtures the way a
static host would and check the reader has not grown a schema of its own: a field the feed
publishes that the reader ignores, or one the reader demands that the publisher never writes,
fails here instead of in a customer's config.

Field names are literals on purpose. A constant shared with the reader would pass even after
both sides drifted together.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from airlie.config.schema import FeedSpec
from airlie.update.feed import ChannelPointer, ImageRef, ReleaseJson, fetch_feed

FIXTURES = Path(__file__).parent / "fixtures"

#: §B's ``release.json``, field for field.
SPEC_RELEASE_FIELDS = {
    "version",
    "released_at",
    "git_sha",
    "image",
    "notes",
    "breaking",
    "state_migration",
    "schema_epoch",
}
#: ``digest`` is what the image states about itself (the drift table compares it); ``pull_digest`` is
#: the registry's digest for ``repo:tag``, so the value a manifest pins. §B predates the second one, so
#: the reader treats it as optional — every document published before it exists has no such key, and
#: ``tests/integration/update/test_feed_contract.py`` reads one back.
SPEC_IMAGE_FIELDS = {"repo", "tag", "digest", "pull_digest"}

#: What ``channels/<name>.json`` publishes. ``etag`` is the transport's, not the document's.
SPEC_POINTER_FIELDS = {"version", "digest", "released_at", "release_json"}

#: The base_url is prefixed, so the published paths carry it. Asserted rather than
#: substring-matched: a reader that ignored the prefix would fetch from the wrong host root.
POINTER_PATH = "/airlie-releases/channels/stable.json"
RELEASE_PATH = "/airlie-releases/releases/10.1.0.json"


def fixture(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return document


def published_feed() -> FeedSpec:
    return FeedSpec(
        kind="channel_pointer", base_url="https://updates.example.com/airlie-releases", channel="stable"
    )


def serving_fixtures() -> httpx.MockTransport:
    pointer = fixture("channel-stable.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", f"the deployment must never write to the feed: {request.method}"
        if request.url.path == POINTER_PATH:
            return httpx.Response(200, json=pointer, headers={"ETag": '"10.1.0"'})
        return httpx.Response(200, json=fixture("release.json"), headers={"ETag": '"10.1.0-doc"'})

    return httpx.MockTransport(handler)


def test_the_published_release_document_carries_exactly_bs_fields() -> None:
    document = fixture("release.json")
    assert set(document) == SPEC_RELEASE_FIELDS
    assert set(document["image"]) == SPEC_IMAGE_FIELDS


def test_the_published_pointer_carries_exactly_bs_fields() -> None:
    assert set(fixture("channel-stable.json")) == SPEC_POINTER_FIELDS


def test_the_reader_expects_exactly_what_is_published() -> None:
    # The reader and the publisher must want the same document; either direction of drift is
    # a broken install for someone who upgrades airlie without republishing the feed.
    assert set(ReleaseJson.__dataclass_fields__) == SPEC_RELEASE_FIELDS
    assert set(ImageRef.__dataclass_fields__) == SPEC_IMAGE_FIELDS
    assert set(ChannelPointer.__dataclass_fields__) == SPEC_POINTER_FIELDS | {"etag"}


async def test_a_published_channel_reads_through_the_real_parser() -> None:
    document = fixture("release.json")
    pointer = fixture("channel-stable.json")

    result = await fetch_feed(published_feed(), transport=serving_fixtures())

    assert result.changed is True
    assert result.etag == '"10.1.0"'
    assert result.pointer is not None
    assert result.pointer.version == pointer["version"]
    assert result.pointer.release_json == pointer["release_json"]

    release = result.release
    assert release is not None
    # Field by field, not by repr: a value silently dropped in parsing looks fine in a diff.
    assert release.version == document["version"]
    assert release.released_at == document["released_at"]
    assert release.git_sha == document["git_sha"]
    assert release.image.repo == document["image"]["repo"]
    assert release.image.tag == document["image"]["tag"]
    assert release.image.digest == document["image"]["digest"]
    assert release.image.pull_digest == document["image"]["pull_digest"]
    assert release.notes == document["notes"]
    assert release.breaking is document["breaking"]
    assert release.state_migration is document["state_migration"]
    assert release.schema_epoch == document["schema_epoch"]


async def test_the_release_document_comes_from_the_path_the_pointer_names() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == POINTER_PATH:
            return httpx.Response(200, json=fixture("channel-stable.json"))
        return httpx.Response(200, json=fixture("release.json"))

    await fetch_feed(published_feed(), transport=httpx.MockTransport(handler))

    # Two documents and no third: the pointer is the only thing that says where to look next.
    assert seen == [POINTER_PATH, RELEASE_PATH]
