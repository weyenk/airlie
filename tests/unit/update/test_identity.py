"""What this process is running, from the three places that answer that."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

from airlie.update import SCHEMA_EPOCH, UNKNOWN, read_image_digest, running_release

#: The one form the release job bakes (§A): the prefix the registry uses, then 64 lowercase hex.
DIGEST = f"sha256:{'0f3a' * 16}"


def test_missing_digest_file_is_none(tmp_path: Path) -> None:
    absent = tmp_path / "image-digest"

    assert running_release("abc", digest_path=absent).image_digest is None


def test_digest_file_is_read_verbatim(tmp_path: Path) -> None:
    path = tmp_path / "image-digest"
    path.write_text(f"{DIGEST}\n", encoding="utf-8")

    assert running_release("abc", digest_path=path).image_digest == DIGEST


@pytest.mark.parametrize(
    "content",
    [
        "0f3a" * 16,  # bare hex: the old convention, and not comparable against the registry's
        "sha256:abc",  # truncated hex
        f"sha256:{'0F3A' * 16}",  # upper case, which no registry prints
        f"sha512:{'0f3a' * 16}",  # another algorithm than the one drift compares in
        "latest",  # a tag is a label, not an identity (non-negotiable #2)
        f"{DIGEST} extra",
        "",
        "   \n",
    ],
)
def test_digest_outside_the_sha256_hex_form_is_no_digest(content: str, tmp_path: Path) -> None:
    """A bundle file a writer outside the release job produced reads as unknown, never as a value.

    Anything passed through here would sit forever beside a channel digest it cannot match
    (drift `behind`) and be offered back to the operator as the digest to pin.
    """
    path = tmp_path / "image-digest"
    path.write_text(content, encoding="utf-8")

    assert read_image_digest(path) is None
    assert running_release("abc", digest_path=path).image_digest is None


def test_source_tree_without_metadata_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def explode(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("airlie")

    monkeypatch.setattr(importlib.metadata, "version", explode)

    release = running_release("abc", digest_path=tmp_path / "absent")

    assert release.version == UNKNOWN
    assert release.schema_epoch == SCHEMA_EPOCH
