"""The model directory airlie builds for a body whose MTP head is published apart (engine/mtp.py)."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import mlx.core as mx

from airlie.engine.mtp import MANIFEST, mtp_model_dir, rekey_safetensors


def head_file(path: Path) -> Path:
    mx.save_safetensors(
        str(path), {"fc.weight": mx.ones((2, 4)), "norm.weight": mx.arange(3, dtype=mx.float32)}
    )
    return path


def body_dir(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "config.json").write_text('{"mtp_num_hidden_layers": 1}')
    (path / "model-00001-of-00001.safetensors").write_bytes(b"body")
    return path


def test_rekeying_prefixes_every_tensor_and_keeps_its_values(tmp_path: Path) -> None:
    src = head_file(tmp_path / "head.safetensors")
    assert rekey_safetensors(src, tmp_path / "out.safetensors", "mtp.") == 2
    got = mx.load(str(tmp_path / "out.safetensors"))
    assert sorted(got) == ["mtp.fc.weight", "mtp.norm.weight"]
    assert got["mtp.norm.weight"].tolist() == [0.0, 1.0, 2.0]
    assert got["mtp.fc.weight"].shape == (2, 4)
    (size,) = struct.unpack("<Q", (tmp_path / "out.safetensors").read_bytes()[:8])
    assert size % 8 == 0  # the data stays 8-byte aligned


def test_the_directory_links_the_body_and_holds_the_rekeyed_head_with_its_manifest(tmp_path: Path) -> None:
    body = body_dir(tmp_path / "body")
    head = tmp_path / "head"
    head.mkdir()
    head_file(head / "model.safetensors")
    out = mtp_model_dir(body, head, tmp_path / "built")
    assert (out / "config.json").resolve() == (body / "config.json").resolve()
    assert (out / "model-00001-of-00001.safetensors").is_symlink()
    assert sorted(mx.load(str(out / "mtp" / "weights.safetensors"))) == ["mtp.fc.weight", "mtp.norm.weight"]
    assert json.loads((out / "mtp" / "manifest.json").read_text()) == MANIFEST


def test_a_built_directory_is_kept_and_one_for_another_body_is_relinked(tmp_path: Path) -> None:
    body = body_dir(tmp_path / "body")
    head = tmp_path / "head"
    head.mkdir()
    head_file(head / "model.safetensors")
    out = mtp_model_dir(body, head, tmp_path / "built")
    written = (out / "mtp" / "weights.safetensors").stat().st_mtime_ns
    mtp_model_dir(body, head, out)
    assert (out / "mtp" / "weights.safetensors").stat().st_mtime_ns == written
    other = body_dir(tmp_path / "other")
    mtp_model_dir(other, head, out)
    assert (out / "config.json").resolve() == (other / "config.json").resolve()
