"""The feed contract: what the renderer writes, and what the reader accepts of it.

``scripts/release_feed.py`` is a pure renderer — the release job publishes its output, and an
air-gapped mirror re-runs it to fill a pinned directory. This module is the contract between the
two: render fixed documents into a temporary directory, read them back through
``airlie.update.feed.fetch_feed`` in pinned-directory mode (the real reader, not a copy of its
rules), and assert the ``ChannelPointer``/``ReleaseJson`` fields. Nothing here touches the
network, so it runs in CI's integration container with ``--network none``.

The image smoke test is opt-in (``BUILD_RELEASE_IMAGE=1``): the gate must stay green without
docker, and CI's integration image has no daemon.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from airlie.config.schema import FeedSpec
from airlie.update.feed import fetch_feed
from airlie.update.identity import SCHEMA_EPOCH

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "release_feed.py"

VERSION = "0.1.0"
DEV_VERSION = "0.2.0-dev1"
GIT_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
RELEASED_AT = "2026-11-22T08:30:00Z"
IMAGE_REPO = "registry.example.com/airlie"
IMAGE_TAG = f"v{VERSION}"
DIGEST = "sha256:" + "b" * 64
NOTES = f"airlie {VERSION} built from {GIT_SHA}"
RELEASE_URL = f"https://github.com/example/airlie/releases/download/v{VERSION}+a1b2c3d/release.json"


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
        "image": {"repo": IMAGE_REPO, "tag": IMAGE_TAG, "digest": DIGEST},
        "notes": NOTES,
        "breaking": False,
        "state_migration": False,
        # The epoch defaults to the one compiled into airlie (code, not config).
        "schema_epoch": SCHEMA_EPOCH,
    }


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
    assert release.notes == ""  # rendered without --notes: the reader must not invent any
    assert release.breaking is False
    assert release.state_migration is False
    # The epoch the reader reports is the one compiled into airlie, not a guess.
    assert release.schema_epoch == SCHEMA_EPOCH


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
