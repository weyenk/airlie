"""What this process is running, from the three places that answer that."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path

import pytest

from airlie.update import SCHEMA_EPOCH, UNKNOWN, running_release


def test_missing_digest_file_is_none(tmp_path: Path) -> None:
    absent = tmp_path / "image-digest"

    assert running_release("abc", digest_path=absent).image_digest is None


def test_digest_file_is_read_verbatim(tmp_path: Path) -> None:
    path = tmp_path / "image-digest"
    path.write_text("sha256:abc\n", encoding="utf-8")

    assert running_release("abc", digest_path=path).image_digest == "sha256:abc"


def test_source_tree_without_metadata_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def explode(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("airlie")

    monkeypatch.setattr(importlib.metadata, "version", explode)

    release = running_release("abc", digest_path=tmp_path / "absent")

    assert release.version == UNKNOWN
    assert release.schema_epoch == SCHEMA_EPOCH
