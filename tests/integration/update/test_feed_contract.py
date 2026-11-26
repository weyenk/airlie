"""The feed contract: what the renderer writes, and what the reader accepts of it.

``scripts/release_feed.py`` is a pure renderer — the release job publishes its output, and an
air-gapped mirror re-runs it to fill a pinned directory. This module is the contract between the
two: render fixed documents into a temporary directory, read them back through
``airlie.update.feed.fetch_feed`` in pinned-directory mode (the real reader, not a copy of its
rules), and assert the ``ChannelPointer``/``ReleaseJson`` fields. The pointer-layout tests serve the
same rendered documents the way each host serves them and assert the exact URL the reader asked for.
Nothing here touches the network, so it runs in CI's integration container with ``--network none``.

The image smoke test is opt-in (``BUILD_RELEASE_IMAGE=1``): the gate must stay green without
docker, and CI's integration image has no daemon.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.config.schema import FeedSpec
from airlie.update.feed import FeedError, fetch_feed
from airlie.update.identity import SCHEMA_EPOCH

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "release_feed.py"

VERSION = "0.1.0"
DEV_VERSION = "0.2.0-dev1"
GIT_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
RELEASED_AT = "2026-11-22T08:30:00Z"
IMAGE_REPO = "registry.example.com/airlie"
IMAGE_TAG = f"v{VERSION}"
#: What the image states about itself (the baked digest), and what the registry holds for the tag
#: (the pull digest, the value a manifest pins). Two pushes, two digests: nothing here may confuse
#: them, so they are two different literals rather than one with a suffix.
DIGEST = "sha256:" + "b" * 64
PULL_DIGEST = "sha256:" + "d" * 64
NOTES = f"airlie {VERSION} built from {GIT_SHA}"
#: The release job uploads ``channels/<name>.json`` as assets of one rolling release, and the host
#: flattens the file name onto the release: the stable pointer is served at
#: ``<base>/channel-pointers/stable.json``, not under a ``channels/`` directory.
ASSET_BASE = "https://github.com/example/airlie/releases/download"
POINTER_RELEASE_TAG = "channel-pointers"
#: A customer's mirror serves the directory tree the renderer wrote. No one ``base_url`` covers both
#: shapes, which is why the reader takes ``update.feed.layout`` instead of guessing.
MIRROR_BASE = "https://gitops.example.com/airlie-releases"
RELEASE_URL = f"{ASSET_BASE}/v{VERSION}+a1b2c3d/release.json"


def run_renderer(*args: str) -> subprocess.CompletedProcess[str]:
    """The real CLI, not an import of its functions: the contract is about what CI executes."""
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True)


def expect_success(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode == 0, f"release_feed failed:\n{proc.stderr}"


def render_release(out: Path, *flags: str) -> None:
    proc = run_renderer(
        "render-release",
        "--version",
        VERSION,
        "--released-at",
        RELEASED_AT,
        "--git-sha",
        GIT_SHA,
        "--image-repo",
        IMAGE_REPO,
        "--image-tag",
        IMAGE_TAG,
        "--image-digest",
        DIGEST,
        "--image-pull-digest",
        PULL_DIGEST,
        *flags,
        "--out",
        str(out),
    )
    expect_success(proc)


def render_pointer(out: Path, channel: str, version: str = VERSION) -> None:
    proc = run_renderer(
        "render-pointer",
        "--channel",
        channel,
        "--version",
        version,
        "--digest",
        DIGEST,
        "--released-at",
        RELEASED_AT,
        "--release-json",
        f"releases/{version}/release.json",
        "--out",
        str(out),
    )
    expect_success(proc)


def test_render_release_writes_the_exact_document(tmp_path: Path) -> None:
    out = tmp_path / "release.json"
    render_release(out, "--notes", NOTES)
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document == {
        "version": VERSION,
        "released_at": RELEASED_AT,
        "git_sha": GIT_SHA,
        # Both digests of one release, because they answer different questions: `digest` is what the
        # container self-reports and drift compares, `pull_digest` what a manifest pins.
        "image": {"repo": IMAGE_REPO, "tag": IMAGE_TAG, "digest": DIGEST, "pull_digest": PULL_DIGEST},
        "notes": NOTES,
        "breaking": False,
        "state_migration": False,
        # The epoch defaults to the one compiled into airlie (code, not config).
        "schema_epoch": SCHEMA_EPOCH,
    }


def test_a_release_without_a_pull_digest_omits_the_key_rather_than_inventing_one(tmp_path: Path) -> None:
    """A mirror rendered without a registry push has nothing honest to name. Writing `""` would put a
    value in the slot the reader hands an operator as the digest to pin, and no registry serves `""`."""
    out = tmp_path / "release.json"
    proc = run_renderer(
        "render-release",
        "--version",
        VERSION,
        "--released-at",
        RELEASED_AT,
        "--git-sha",
        GIT_SHA,
        "--image-repo",
        IMAGE_REPO,
        "--image-tag",
        IMAGE_TAG,
        "--image-digest",
        DIGEST,
        "--out",
        str(out),
    )
    expect_success(proc)
    assert "pull_digest" not in json.loads(out.read_text(encoding="utf-8"))["image"]


def test_render_release_refuses_a_pull_digest_that_is_not_alg_colon_hex(tmp_path: Path) -> None:
    out = tmp_path / "release.json"
    proc = run_renderer(
        "render-release",
        "--version",
        VERSION,
        "--released-at",
        RELEASED_AT,
        "--git-sha",
        GIT_SHA,
        "--image-repo",
        IMAGE_REPO,
        "--image-tag",
        IMAGE_TAG,
        "--image-digest",
        DIGEST,
        "--image-pull-digest",
        "d" * 64,  # bare hex: the shape of a git blob id, not of anything a registry answers
        "--out",
        str(out),
    )
    assert proc.returncode != 0, "a bare-hex pin was accepted"
    assert "--image-pull-digest" in proc.stderr
    assert not out.exists()


def test_render_pointer_writes_the_exact_document(tmp_path: Path) -> None:
    out = tmp_path / "channels" / "latest.json"
    render_pointer(out, "latest")
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document == {
        "version": VERSION,
        "digest": DIGEST,
        "released_at": RELEASED_AT,
        "release_json": f"releases/{VERSION}/release.json",
    }


@pytest.mark.parametrize("channel", ["latest", "stable"])
async def test_rendered_documents_round_trip_through_the_reader(tmp_path: Path, channel: str) -> None:
    """The reader's own rules decide what the renderer may have written — no copied fixtures."""
    root = tmp_path / "feed"
    render_release(root / "releases" / VERSION / "release.json")
    render_pointer(root / "channels" / f"{channel}.json", channel)

    result = await fetch_feed(FeedSpec(kind="pinned", channel=channel, path=str(root)))

    assert result.changed is True
    pointer = result.pointer
    release = result.release
    assert pointer is not None and release is not None
    # The pointer: the channel's one version, and the digest that its own document carries.
    assert pointer.version == VERSION
    assert pointer.digest == DIGEST
    assert pointer.released_at == RELEASED_AT
    assert pointer.release_json == f"releases/{VERSION}/release.json"
    assert pointer.etag is None  # a pinned directory has no etag to carry
    # The document the pointer names, field for field.
    assert release.version == VERSION
    assert release.released_at == RELEASED_AT
    assert release.git_sha == GIT_SHA
    assert release.image.repo == IMAGE_REPO
    assert release.image.tag == IMAGE_TAG
    assert release.image.digest == DIGEST
    # The pin the operator is handed travels with the document it belongs to, and the pointer does
    # not carry it: a pointer's digest is only ever what a deployment compares itself with.
    assert release.image.pull_digest == PULL_DIGEST
    assert "pull_digest" not in json.loads((root / "channels" / f"{channel}.json").read_text("utf-8"))
    assert release.notes == ""  # rendered without --notes: the reader must not invent any
    assert release.breaking is False
    assert release.state_migration is False
    # The epoch the reader reports is the one compiled into airlie, not a guess.
    assert release.schema_epoch == SCHEMA_EPOCH


async def test_a_release_document_from_before_the_pull_digest_still_reads(tmp_path: Path) -> None:
    """Every `release.json` published so far carries one digest, and the deployments reading it are
    live. The new key is optional: such a document parses, with no pull digest — nothing may invent a
    value an operator would put in a manifest no registry serves."""
    root = tmp_path / "feed"
    release_path = root / "releases" / VERSION / "release.json"
    render_release(release_path)
    document = json.loads(release_path.read_text(encoding="utf-8"))
    del document["image"]["pull_digest"]
    release_path.write_text(json.dumps(document), encoding="utf-8")
    render_pointer(root / "channels" / "stable.json", "stable")

    result = await fetch_feed(FeedSpec(kind="pinned", channel="stable", path=str(root)))

    assert result.release is not None
    assert result.release.image.digest == DIGEST
    assert result.release.image.pull_digest is None


async def test_a_malformed_pull_digest_refuses_the_document(tmp_path: Path) -> None:
    """Absent is a fact; malformed is somebody's mistake, and the reader hands this exact string to an
    operator as the digest to pin. Refusing is the only answer that cannot strand a deployment."""
    root = tmp_path / "feed"
    release_path = root / "releases" / VERSION / "release.json"
    render_release(release_path)
    document = json.loads(release_path.read_text(encoding="utf-8"))
    document["image"]["pull_digest"] = "d" * 64
    release_path.write_text(json.dumps(document), encoding="utf-8")
    render_pointer(root / "channels" / "stable.json", "stable")

    with pytest.raises(FeedError) as excinfo:
        await fetch_feed(FeedSpec(kind="pinned", channel="stable", path=str(root)))
    assert excinfo.value.reason == "mismatched_digest"


def test_promoting_a_state_migration_with_empty_notes_is_refused(tmp_path: Path) -> None:
    document = tmp_path / "release.json"

    render_release(document, "--state-migration")  # and no notes
    out = tmp_path / "stable.json"
    proc = run_renderer(
        "promote-stable", "--release-json", str(document), "--release-url", RELEASE_URL, "--out", str(out)
    )
    assert proc.returncode != 0
    assert "notes" in proc.stderr
    assert not out.exists()


def test_promoting_a_state_migration_with_notes_writes_the_stable_pointer(tmp_path: Path) -> None:
    document = tmp_path / "release.json"
    render_release(document, "--state-migration", "--notes", NOTES)
    out = tmp_path / "stable.json"
    proc = run_renderer(
        "promote-stable", "--release-json", str(document), "--release-url", RELEASE_URL, "--out", str(out)
    )
    expect_success(proc)
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "version": VERSION,
        "digest": DIGEST,
        "released_at": RELEASED_AT,
        "release_json": RELEASE_URL,
    }


def promoted_stable(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The stable pointer and the release document it names, both rendered by the real script."""
    release_path = tmp_path / "release.json"
    render_release(release_path, "--notes", NOTES)
    pointer_path = tmp_path / "stable.json"
    expect_success(
        run_renderer(
            "promote-stable",
            "--release-json",
            str(release_path),
            "--release-url",
            RELEASE_URL,
            "--out",
            str(pointer_path),
        )
    )
    pointer: dict[str, Any] = json.loads(pointer_path.read_text(encoding="utf-8"))
    release: dict[str, Any] = json.loads(release_path.read_text(encoding="utf-8"))
    return pointer, release


def serving(
    pointer_url: str,
    release_url: str,
    pointer: dict[str, Any],
    release: dict[str, Any],
) -> tuple[httpx.MockTransport, list[str]]:
    """A static host serving exactly these two documents; any other path is a failing request."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if url == pointer_url:
            return httpx.Response(200, json=pointer)
        if url == release_url:
            return httpx.Response(200, json=release)
        raise AssertionError(f"the reader asked for a path nobody publishes: {url}")

    return httpx.MockTransport(handler), seen


async def test_the_release_asset_layout_reads_the_uploaded_pointer(tmp_path: Path) -> None:
    """The release job runs ``gh release upload channel-pointers channels/stable.json``, so the URL
    the world reads is ``<base>/<tag>/<channel>.json``. The promoted pointer's ``release_json`` is an
    absolute asset URL on a different release, and it resolves as written."""
    pointer, release = promoted_stable(tmp_path)
    pointer_url = f"{ASSET_BASE}/{POINTER_RELEASE_TAG}/stable.json"
    transport, seen = serving(pointer_url, RELEASE_URL, pointer, release)

    result = await fetch_feed(
        FeedSpec(
            kind="channel_pointer",
            base_url=ASSET_BASE,
            layout="release_asset",
            pointer_release_tag=POINTER_RELEASE_TAG,
        ),
        transport=transport,
    )

    assert seen == [pointer_url, RELEASE_URL]
    assert result.release is not None and result.release.version == VERSION
    assert result.release.image.digest == DIGEST


async def test_the_mirror_layout_still_reads_the_rendered_directory(tmp_path: Path) -> None:
    """The default layout, and every config written before ``layout`` existed: the mirrored tree. The
    absolute ``release_json`` crosses over to the public asset host from there too."""
    pointer, release = promoted_stable(tmp_path)
    pointer_url = f"{MIRROR_BASE}/channels/stable.json"
    transport, seen = serving(pointer_url, RELEASE_URL, pointer, release)

    result = await fetch_feed(FeedSpec(kind="channel_pointer", base_url=MIRROR_BASE), transport=transport)

    assert seen == [pointer_url, RELEASE_URL]
    assert result.pointer is not None and result.pointer.digest == DIGEST
    assert result.release is not None and result.release.schema_epoch == SCHEMA_EPOCH


def test_promoting_a_dev_build_is_refused(tmp_path: Path) -> None:
    document = tmp_path / "release.json"
    proc = run_renderer(
        "render-release",
        "--version",
        DEV_VERSION,
        "--released-at",
        RELEASED_AT,
        "--git-sha",
        GIT_SHA,
        "--image-repo",
        IMAGE_REPO,
        "--image-tag",
        f"v{DEV_VERSION}",
        "--image-digest",
        DIGEST,
        "--out",
        str(document),
    )
    expect_success(proc)  # a dev build is publishable to latest; it is stable that refuses it
    out = tmp_path / "stable.json"
    proc = run_renderer(
        "promote-stable", "--release-json", str(document), "--release-url", RELEASE_URL, "--out", str(out)
    )
    assert proc.returncode != 0


def test_render_release_refuses_a_digest_that_is_not_alg_colon_hex(tmp_path: Path) -> None:
    out = tmp_path / "release.json"
    for bad in ("abc", "b" * 64):  # a bare hex string is an old document, not a digest
        proc = run_renderer(
            "render-release",
            "--version",
            VERSION,
            "--released-at",
            RELEASED_AT,
            "--git-sha",
            GIT_SHA,
            "--image-repo",
            IMAGE_REPO,
            "--image-tag",
            IMAGE_TAG,
            "--image-digest",
            bad,
            "--out",
            str(out),
        )
        assert proc.returncode != 0, f"{bad!r} was accepted"
    assert not out.exists()


def test_render_pointer_refuses_a_digest_that_is_not_alg_colon_hex(tmp_path: Path) -> None:
    out = tmp_path / "latest.json"
    proc = run_renderer(
        "render-pointer", "--channel", "latest", "--version", VERSION, "--digest", "abc", "--out", str(out)
    )
    assert proc.returncode != 0


def test_render_pointer_refuses_a_dev_build_on_stable(tmp_path: Path) -> None:
    out = tmp_path / "stable.json"
    proc = run_renderer(
        "render-pointer",
        "--channel",
        "stable",
        "--version",
        DEV_VERSION,
        "--digest",
        DIGEST,
        "--release-json",
        "releases/dev/release.json",
        "--out",
        str(out),
    )
    assert proc.returncode != 0
    assert "development build" in proc.stderr


def test_the_same_dev_build_on_latest_is_accepted(tmp_path: Path) -> None:
    out = tmp_path / "latest.json"
    proc = run_renderer(
        "render-pointer",
        "--channel",
        "latest",
        "--version",
        DEV_VERSION,
        "--digest",
        DIGEST,
        "--release-json",
        "releases/dev/release.json",
        "--out",
        str(out),
    )
    expect_success(proc)


@pytest.mark.skipif(
    os.environ.get("BUILD_RELEASE_IMAGE") != "1",
    reason="set BUILD_RELEASE_IMAGE=1 to build the release image in a test (gate stays green without docker)",
)
@pytest.mark.timeout(1800)  # a web bundle plus uv.lock's runtime dependencies (torch included)
def test_the_release_image_carries_its_labels_digest_and_user(tmp_path: Path) -> None:
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("no docker daemon (CI's integration image runs without one)")

    # The wheel the image installs: dist/ is a build artifact, built exactly as the release job builds it.
    assert subprocess.run(["uv", "build"], cwd=ROOT, capture_output=True).returncode == 0

    image = "airlie-release-smoke"
    build = subprocess.run(
        [
            "docker",
            "build",
            "--file",
            str(ROOT / "ops" / "release" / "Containerfile"),
            f"--build-arg VERSION={VERSION}",
            "--build-arg REVISION=smoke-test",
            f"--build-arg CREATED={RELEASED_AT}",
            f"--build-arg IMAGE_DIGEST={DIGEST}",
            "--tag",
            image,
            str(ROOT),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    try:
        assert build.returncode == 0, f"docker build failed:\n{build.stderr[-4000:]}"

        inspect = json.loads(
            subprocess.run(["docker", "inspect", image], capture_output=True, text=True).stdout
        )
        config = inspect[0]["Config"]
        labels = config.get("Labels") or {}
        assert labels["org.opencontainers.image.version"] == VERSION
        assert labels["org.opencontainers.image.revision"] == "smoke-test"
        assert labels["org.opencontainers.image.created"] == RELEASED_AT

        # The digest, verbatim: what airlie.update.identity.read_image_digest will report at boot.
        run = subprocess.run(
            ["docker", "run", "--rm", image, "cat", "/etc/airlie/image-digest"],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0
        assert run.stdout.strip() == DIGEST

        # Not root: the server user is uid 1000, and the image runs it.
        assert config["User"] == "airlie"
        uid = subprocess.run(["docker", "run", "--rm", image, "id", "-u"], capture_output=True, text=True)
        assert uid.stdout.strip() == "1000"

        # The image is the server and nothing else: no self-update, no pull of its own runtime.
        assert config["Cmd"] == ["airlie", "serve"]
    finally:
        subprocess.run(["docker", "rmi", "--force", image], capture_output=True)
