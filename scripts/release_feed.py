#!/usr/bin/env python3
"""Render the update feed's documents: release.json and channels/<name>.json.

A pure renderer — CLI arguments in, JSON out: no network, no registry, and no clock of its own
(the caller stamps --released-at). The pipeline publishes what this script writes, and an air-gapped
mirror re-runs it to fill its pinned directory, so one code path owns both. The reader it must
satisfy is airlie.update.feed.fetch_feed; tests/integration/update/test_feed_contract.py renders
fixed documents into a temporary directory and reads them back through that reader.

Documents, per docs/superpowers/specs/2026-11-22-update-channel-design.md §A/§B:

  release.json          the immutable per-version document, an asset of that version's GitHub Release
  channels/<name>.json  a rolling pointer: one version, and the digest that release's document carries

The renderer refuses what every reader would refuse — a malformed digest, a development build on
stable — and the promotion (promote-stable) additionally refuses a release that migrates state
without saying how in its notes: such a pointer would hand a deployment a destructive upgrade with
no instructions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from airlie.update.feed import DIGEST_RE, ChannelPointer, FeedError, _parse_release
from airlie.update.identity import SCHEMA_EPOCH
from airlie.update.version import is_dev_version, parse_semver

CHANNELS = ("stable", "latest")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _fail(detail: str) -> None:
    print(f"release_feed: {detail}", file=sys.stderr)
    raise SystemExit(2)


def _write(out: Path, document: dict[str, object]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _require_digest(value: str, where: str) -> str:
    if DIGEST_RE.fullmatch(value) is None:
        _fail(f"{where} must be a full digest, sha256:<64 hex>; got {value!r}")
    return value


def _require_version(value: str, where: str) -> None:
    if parse_semver(value) is None:
        _fail(f"{where} must be a semver version (major.minor.patch, optional -prerelease); got {value!r}")


def render_release(args: argparse.Namespace) -> None:
    """§B's release document, from arguments. The schema epoch defaults to the one compiled into
    airlie.update.identity (code, not config): a deployment cannot configure itself into claiming a
    layout it does not have."""
    _require_version(args.version, "--version")
    if GIT_SHA_RE.fullmatch(args.git_sha) is None:
        _fail(f"--git-sha must be a 40-hex commit; got {args.git_sha!r}")
    if not args.released_at:
        _fail("--released-at is required (the caller stamps it; the renderer keeps no clock)")
    if args.schema_epoch < 1:
        _fail(f"--schema-epoch counts up from 1; got {args.schema_epoch}")
    _write(
        Path(args.out),
        {
            "version": args.version,
            "released_at": args.released_at,
            "git_sha": args.git_sha,
            "image": {
                "repo": args.image_repo,
                "tag": args.image_tag,
                "digest": _require_digest(args.image_digest, "--image-digest"),
            },
            "notes": args.notes,
            "breaking": args.breaking,
            "state_migration": args.state_migration,
            "schema_epoch": args.schema_epoch,
        },
    )


def render_pointer(args: argparse.Namespace) -> None:
    """§A's channel pointer, from arguments. ``--release-json`` is an https:// URL for the public
    feed or a path inside the pinned directory for its mirror; the reader accepts both."""
    _require_version(args.version, "--version")
    if args.channel == "stable" and is_dev_version(args.version):
        _fail(f"{args.version} is a development build; stable may never point at one")
    if not args.release_json:
        _fail("--release-json is required (an https:// URL, or a path for a pinned directory)")
    _write(
        Path(args.out),
        {
            "version": args.version,
            "digest": _require_digest(args.digest, "--digest"),
            "released_at": args.released_at,
            "release_json": args.release_json,
        },
    )


def promote_stable(args: argparse.Namespace) -> None:
    """channels/stable.json from a published release document.

    The promotion re-validates the document through the reader's own parser with channel "stable",
    so a development build is refused here as it would be by every stable deployment, and the
    state-migration guard applies to what was actually published — not to arguments.
    """
    source = Path(args.release_json)
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(f"cannot read {source}: {exc}")
    if not isinstance(document, dict):
        _fail(f"refusing to promote {source.name}: the release document is not a JSON object")
    image = document.get("image")
    digest = "" if not isinstance(image, dict) or image.get("digest") is None else str(image["digest"])
    pointer = ChannelPointer(
        version="" if document.get("version") is None else str(document["version"]),
        digest=digest,
        released_at="",
        release_json="promoted",
        etag=None,  # a promotion reads from disk; there is no HTTP etag to carry
    )
    try:
        release = _parse_release(document, pointer=pointer, channel="stable")
    except FeedError as exc:
        _fail(f"refusing to promote {source.name}: {exc.detail or exc.reason}")
    if release.state_migration and not release.notes.strip():
        _fail(
            f"refusing to promote {release.version}: it migrates state but its notes are empty — "
            "a deployment must be told what to do with its data before stable points at it"
        )
    _write(
        Path(args.out),
        {
            "version": release.version,
            "digest": release.image.digest,
            "released_at": release.released_at,
            "release_json": args.release_url,
        },
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release_feed.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    release = sub.add_parser("render-release", help="render §B's release.json")
    release.add_argument("--version", required=True)
    release.add_argument("--released-at", required=True, dest="released_at")
    release.add_argument("--git-sha", required=True, dest="git_sha")
    release.add_argument("--image-repo", required=True, dest="image_repo")
    release.add_argument("--image-tag", required=True, dest="image_tag")
    release.add_argument("--image-digest", required=True, dest="image_digest")
    release.add_argument("--notes", default="")
    release.add_argument("--breaking", action="store_true")
    release.add_argument("--state-migration", action="store_true", dest="state_migration")
    release.add_argument(
        "--schema-epoch",
        type=int,
        default=SCHEMA_EPOCH,
        dest="schema_epoch",
        help=f"defaults to airlie.update.identity.SCHEMA_EPOCH ({SCHEMA_EPOCH})",
    )
    release.add_argument("--out", required=True)

    pointer = sub.add_parser("render-pointer", help="render §A's channels/<name>.json")
    pointer.add_argument("--channel", required=True, choices=CHANNELS)
    pointer.add_argument("--version", required=True)
    pointer.add_argument("--digest", required=True)
    pointer.add_argument("--released-at", default="", dest="released_at")
    pointer.add_argument("--release-json", required=True, dest="release_json")
    pointer.add_argument("--out", required=True)

    promote = sub.add_parser(
        "promote-stable", help="render channels/stable.json from a published release document"
    )
    promote.add_argument("--release-json", required=True, dest="release_json")
    promote.add_argument(
        "--release-url",
        required=True,
        dest="release_url",
        help="the https:// URL the stable pointer names for this document",
    )
    promote.add_argument("--out", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers = {
        "render-release": render_release,
        "render-pointer": render_pointer,
        "promote-stable": promote_stable,
    }
    handlers[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
