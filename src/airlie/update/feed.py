"""Reads the update feed: which release the project publishes, and whether we may get there.

The feed is the only place a non-dev deployment learns what to run. Two kinds, decided by config
and nothing else: a channel pointer over HTTPS, or a pin checked into a directory. A pointer feed
also carries its layout: ``update.feed.layout`` says whether ``base_url`` serves the mirrored
directory tree or a host that carries the pointer as a flattened release asset, and nothing here
infers one from the other. Either way this module only reads. No download, no instance call, no
state write, and no local-version parameter — comparing versions is the delta's job, not the
reader's, and a reader that could pick a version would be a second opinion nobody asked for.

The token is the one secret here, and it is named by config rather than stored in it:
``registry_token_env`` holds an environment variable name, and the token is sent to no host but the
one ``base_url`` names.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx

from airlie.config.schema import FeedSpec
from airlie.update.version import is_dev_version

DEFAULT_TIMEOUT_S: Final = 10.0

#: The seven ways a feed can refuse, as data (§K). Anything outside this set is a bug, not a
#: reason: the panel and CLI show these strings, and a fourth spelling of "network" is a bug.
NOT_CONFIGURED: Final = "not_configured"
BAD_STATUS: Final = "bad_status"
UNSUPPORTED_SHAPE: Final = "unsupported_shape"
NO_DIGEST: Final = "no_digest"
MISMATCHED_DIGEST: Final = "mismatched_digest"
DEV_ON_STABLE: Final = "dev_on_stable"
UNREACHABLE: Final = "unreachable"

#: A digest is always ``alg:hex``. A bare hex string is an old document, and a document with no
#: digest at all is an instruction to run whatever the parser guessed.
DIGEST_RE: Final = re.compile(r"sha256:[0-9a-f]{64}")

#: The only channel a production deployment reads. ``latest`` exists in config and may carry a dev
#: build; ``stable`` may never point at one.
STABLE_CHANNEL: Final = "stable"


class FeedError(RuntimeError):
    """The feed refused. Carries one of the seven reasons and a detail safe to show."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail or reason)


@dataclass(frozen=True)
class ChannelPointer:
    """``channels/<name>.json``: one version, and the digest that release's own document."""

    version: str
    digest: str
    released_at: str
    release_json: str
    etag: str | None


@dataclass(frozen=True)
class ImageRef:
    """One release's image, named two ways because two questions get asked of it.

    ``digest`` is what the running container states about itself — the value the release job bakes
    into ``/etc/airlie/image-digest``, and the only one ``drift.classify`` can compare against a
    deployment, since an image cannot contain its own manifest digest.

    ``pull_digest`` is the registry's digest for ``repo:tag``, so the value a declarative manifest
    has to pin and the only one a registry can check a pull against. It is never comparable with a
    self-report: the image that self-reports ``digest`` is the build whose registry digest is
    ``pull_digest`` (the two-push bake, spec §A).
    """

    repo: str
    tag: str
    digest: str
    #: None for every document published before the pull digest existed, and for a mirror that
    #: rendered one without a registry push. Nothing may guess it: an invented pin is a manifest
    #: no registry can serve.
    pull_digest: str | None = None


@dataclass(frozen=True)
class ReleaseJson:
    """§B's release document, parsed. Everything §B requires is required here."""

    version: str
    released_at: str
    git_sha: str
    image: ImageRef
    notes: str
    breaking: bool
    state_migration: bool
    # None only for a config pin, which has no document to read the epoch from. Guessing this one
    # would decide whether a destructive upgrade is offered, and nothing here is allowed to guess.
    schema_epoch: int | None


@dataclass(frozen=True)
class FeedResult:
    changed: bool
    release: ReleaseJson | None
    pointer: ChannelPointer | None
    etag: str | None
    checked_at: str


def _fail(reason: str, detail: str) -> FeedError:
    return FeedError(reason, detail)


def _checked_at() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _text(document: dict[str, object], key: str) -> str:
    value = document.get(key)
    return "" if value is None else str(value)


def _epoch(value: object, version: str) -> int:
    """§B says the epoch is an integer counting up from 1. Advisory, but never guessed: an
    unreadable epoch would silently decide whether a bridge is offered."""
    if isinstance(value, bool) or value is None:
        raise _fail(UNSUPPORTED_SHAPE, f"release {version} has no schema_epoch")
    try:
        return int(str(value))
    except ValueError:
        raise _fail(UNSUPPORTED_SHAPE, f"release {version} has schema_epoch {value!r}") from None


def _digest(value: object, *, where: str) -> str:
    """A digest that is a digest. Anything else is a refusal, never a guess."""
    text = "" if value is None else str(value)
    if not text:
        raise _fail(NO_DIGEST, f"{where} has no digest")
    if DIGEST_RE.fullmatch(text) is None:
        raise _fail(MISMATCHED_DIGEST, f"{where} digest {text!r} is not alg:hex")
    return text


def _optional_digest(value: object, *, where: str) -> str | None:
    """A digest a document need not carry. Absent is a fact (nothing is published to pin); a
    present-but-malformed one is the same refusal ``_digest`` makes, never a silent None — a pin
    handed to an operator has to be one a registry can serve."""
    if value is None or not str(value):
        return None
    return _digest(value, where=where)


def _same_digest(configured: str | None, published: str, *, where: str) -> None:
    """A pin that the feed contradicts is not a newer release; it is someone's mistake."""
    if configured is not None and configured != published:
        raise _fail(MISMATCHED_DIGEST, f"{where} is {published}, not the pinned {configured}")


def _not_dev_on_stable(channel: str, version: str, *, where: str) -> None:
    # Derived from the channel vocabulary, never from the document: a ``-dev`` tag is how a
    # development build is marked, and stable is where a person's machine lives.
    if channel == STABLE_CHANNEL and is_dev_version(version):
        raise _fail(DEV_ON_STABLE, f"{where} {version} is a development build on {STABLE_CHANNEL}")


def _parse_pointer(document: object, etag: str | None, *, channel: str) -> ChannelPointer:
    if not isinstance(document, dict):
        raise _fail(UNSUPPORTED_SHAPE, "the channel pointer is not a JSON object")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise _fail(UNSUPPORTED_SHAPE, "the channel pointer has no version")
    _not_dev_on_stable(channel, version, where="The channel pointer")
    release_json = document.get("release_json")
    if not isinstance(release_json, str) or not release_json:
        raise _fail(UNSUPPORTED_SHAPE, f"{version} names no release document")
    released_at = document.get("released_at")
    return ChannelPointer(
        version=version,
        digest=_digest(document.get("digest"), where=version),
        released_at="" if released_at is None else str(released_at),
        release_json=release_json,
        etag=etag,
    )


def _parse_release(document: object, *, pointer: ChannelPointer, channel: str) -> ReleaseJson:
    if not isinstance(document, dict):
        raise _fail(UNSUPPORTED_SHAPE, "the release document is not a JSON object")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise _fail(UNSUPPORTED_SHAPE, "the release document has no version")
    # The pointer named one release and the body names another: which to run is not ours to guess.
    if version != pointer.version:
        raise _fail(
            UNSUPPORTED_SHAPE, f"the release document is {version}, the pointer named {pointer.version}"
        )
    _not_dev_on_stable(channel, version, where="The release document")
    image = document.get("image")
    if not isinstance(image, dict):
        raise _fail(UNSUPPORTED_SHAPE, f"{version} has no image")
    repo = image.get("repo")
    tag = image.get("tag")
    if not isinstance(repo, str) or not repo or not isinstance(tag, str) or not tag:
        raise _fail(UNSUPPORTED_SHAPE, f"{version} has an image with no repo and tag")
    return ReleaseJson(
        version=version,
        released_at=_text(document, "released_at"),
        git_sha=_text(document, "git_sha"),
        image=ImageRef(
            repo=repo,
            tag=tag,
            digest=_digest(image.get("digest"), where=f"{version} image"),
            pull_digest=_optional_digest(image.get("pull_digest"), where=f"{version} image pull_digest"),
        ),
        notes=str(document.get("notes") or ""),
        breaking=bool(document.get("breaking")),
        state_migration=bool(document.get("state_migration")),
        schema_epoch=_epoch(document.get("schema_epoch"), version),
    )


def _base(cfg: FeedSpec) -> str:
    if cfg.base_url is None or not cfg.base_url.startswith("https://"):
        raise _fail(NOT_CONFIGURED, "update.feed.base_url must be an https:// URL")
    return cfg.base_url.rstrip("/")


def _usable(url: str) -> str:
    """The URL, but only if httpx will accept it.

    A typo'd configured base — a stray space, an unclosed IPv6 bracket — raises ``httpx.InvalidURL``,
    which is not an ``HTTPError``, so it would walk straight past every guard in this module and
    reach the panel as a 500. One malformed value in an operator's config is a feed that refuses, and
    a refusal is a ledger row.
    """
    try:
        httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise _fail(UNREACHABLE, f"{url} is not a URL airlie can request: {exc}") from exc
    return url


def _target(cfg: FeedSpec, suffix: str) -> str:
    """A path below the feed root: the pointer directory, or a release document named relatively."""
    return _usable(f"{_base(cfg)}/{suffix.lstrip('/')}")


def pointer_target(cfg: FeedSpec) -> str:
    """The channel pointer document, at whichever of the two layouts ``update.feed.layout`` names.

    A mirror serves the directory the release renderer writes. A release-asset host flattens the
    uploaded file name onto the release, so the pointer the publisher uploads as
    ``channels/stable.json`` is served as ``<tag>/stable.json``. The layouts differ by one path
    segment and both hosts answer a wrong one with a 404, so the choice is config: nothing here
    sniffs ``base_url`` and nothing retries the other layout.

    ``airlie.doctor.update_check`` asks this same URL, so the doctor and the reader cannot disagree
    about which layout a config means.
    """
    if cfg.layout == "release_asset":
        assert cfg.pointer_release_tag is not None  # enforced by FeedSpec
        return _target(cfg, f"{cfg.pointer_release_tag}/{cfg.channel}.json")
    return _target(cfg, f"channels/{cfg.channel}.json")


def _bearer(cfg: FeedSpec, url: str) -> dict[str, str]:
    """The token, and only for the host ``base_url`` names."""
    if cfg.base_url is None or httpx.URL(url).host != httpx.URL(cfg.base_url).host:
        return {}
    token = os.environ.get(cfg.registry_token_env or "", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _get(
    client: httpx.AsyncClient, cfg: FeedSpec, url: str, *, etag: str | None = None
) -> httpx.Response:
    try:
        # Everything that can fail before the first byte is inside the guard, the URL parse with it:
        # a malformed base, or a pointer document naming a malformed absolute release URL, is a
        # refused feed and never a crash.
        headers = _bearer(cfg, url)
        if etag is not None:
            headers["If-None-Match"] = etag
        # GET, and nothing else. This module has no write path to reach by accident.
        return await client.get(url, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as exc:
        raise _fail(UNREACHABLE, f"{url}: {exc}") from exc


def _carry(pointer: ChannelPointer, release: ReleaseJson, cfg: FeedSpec) -> None:
    _same_digest(cfg.pinned_digest, pointer.digest, where=f"Release {pointer.version}")
    _same_digest(cfg.pinned_digest, release.image.digest, where=f"Release {pointer.version} image")


async def _read_channel(
    cfg: FeedSpec,
    current_etag: str | None,
    client: httpx.AsyncClient,
) -> FeedResult:
    """The pointer, then the release document it names. Two documents, one set of rules."""
    pointer_url = pointer_target(cfg)
    response = await _get(client, cfg, pointer_url, etag=current_etag)
    if response.status_code == 304:
        # Nothing changed, and there is no body to read. The etag and the time are the whole answer.
        return FeedResult(
            changed=False, release=None, pointer=None, etag=current_etag, checked_at=_checked_at()
        )
    if response.status_code != 200:
        raise _fail(BAD_STATUS, f"{pointer_url} returned {response.status_code}")
    etag = response.headers.get("etag")
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _fail(UNSUPPORTED_SHAPE, f"{pointer_url} is not JSON: {exc}") from exc
    pointer = _parse_pointer(body, etag, channel=cfg.channel)

    # ``release_json`` is either a whole URL or a path relative to the feed. Nothing else.
    ref = pointer.release_json
    release_url = ref if ref.startswith("https://") else _target(cfg, ref)
    release_response = await _get(client, cfg, release_url)
    if release_response.status_code == 404:
        # A channel can point at a version whose document was never published. There is nothing
        # to run, and saying so is more honest than calling it a transport failure.
        return FeedResult(changed=True, release=None, pointer=pointer, etag=etag, checked_at=_checked_at())
    if release_response.status_code != 200:
        raise _fail(BAD_STATUS, f"{ref} returned {release_response.status_code}")
    try:
        release_body = release_response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _fail(UNSUPPORTED_SHAPE, f"{ref} is not JSON: {exc}") from exc
    release = _parse_release(release_body, pointer=pointer, channel=cfg.channel)
    _carry(pointer, release, cfg)
    return FeedResult(changed=True, release=release, pointer=pointer, etag=etag, checked_at=_checked_at())


def _read_pinned_documents(cfg: FeedSpec) -> tuple[ChannelPointer, ReleaseJson]:
    """The same two documents, read from a directory instead of the network.

    Air-gapped installs ship the feed on disk with the bundle, in the layout the feed uses, so the
    rules are the rules. Nothing here touches the network, and nothing here writes.
    """
    assert cfg.path is not None  # enforced by FeedSpec
    root = Path(cfg.path).expanduser()
    pointer_path = root / "channels" / f"{cfg.channel}.json"
    if not pointer_path.is_file():
        raise _fail(UNSUPPORTED_SHAPE, f"{pointer_path} does not exist")
    try:
        pointer_body = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _fail(UNSUPPORTED_SHAPE, f"{pointer_path} is not JSON: {exc}") from exc
    pointer = _parse_pointer(pointer_body, None, channel=cfg.channel)
    release_path = (root / pointer.release_json).resolve()
    if not release_path.is_relative_to(root.resolve()):
        raise _fail(UNSUPPORTED_SHAPE, f"{pointer.release_json} escapes {root}")
    if not release_path.is_file():
        raise _fail(UNSUPPORTED_SHAPE, f"{release_path} does not exist")
    try:
        release_body = json.loads(release_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _fail(UNSUPPORTED_SHAPE, f"{release_path} is not JSON: {exc}") from exc
    return pointer, _parse_release(release_body, pointer=pointer, channel=cfg.channel)


def _read_pinned(cfg: FeedSpec) -> FeedResult:
    """A pin is the whole truth: either a document on disk, or the two values in config."""
    if cfg.path is not None:
        pointer, release = _read_pinned_documents(cfg)
        _carry(pointer, release, cfg)
        return FeedResult(changed=True, release=release, pointer=pointer, etag=None, checked_at=_checked_at())
    if cfg.pinned_version is None or cfg.pinned_digest is None:
        raise _fail(NOT_CONFIGURED, "a pinned feed needs update.feed.path or a pinned version + digest")
    version = cfg.pinned_version
    _not_dev_on_stable(cfg.channel, version, where="The pin")
    digest = _digest(cfg.pinned_digest, where=f"Pin {version}")
    # No document, so no repo and no notes: the deployment runs the digest, and the manager already
    # knows which repository to pull it from.
    release = ReleaseJson(
        version=version,
        released_at="",
        git_sha="",
        schema_epoch=None,
        image=ImageRef(repo="", tag=version, digest=digest),
        notes="",
        breaking=False,
        state_migration=False,
    )
    return FeedResult(changed=True, release=release, pointer=None, etag=None, checked_at=_checked_at())


async def fetch_feed(
    cfg: FeedSpec,
    *,
    current_etag: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> FeedResult:
    """Read whichever feed is configured, and refuse everything else.

    ``transport`` exists for tests and for a caller that owns its connection pool; the client built
    here is closed here, and a caller-supplied transport is never closed by us.
    """
    if cfg.kind == "pinned":
        return await asyncio.to_thread(_read_pinned, cfg)
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        return await _read_channel(cfg, current_etag, client)
