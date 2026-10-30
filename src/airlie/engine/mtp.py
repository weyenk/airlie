"""A model directory vllm-mlx 0.5.0 serves with an MTP (multi-token prediction) head beside a quantized body.

mlx-community's quantized Qwen3.8 builds drop the checkpoint's `mtp.*` tensors; mlx-community publishes the
head alone (`Qwen3.8-27B-MTP-bf16`: keys without the `mtp.` prefix, RMSNorm weights already MLX gamma). The
engine's Qwen patch (`vllm_mlx/patches/qwen3_5_mtp.py`) reads `<model>/mtp/weights.safetensors`, keeps only
keys under `mtp.`, and trusts a `manifest.json` beside it that says the norms are MLX gamma (else it guesses
from each norm's mean, and one of this head's sits at 0.54 against a 0.5 threshold). So the directory links
every file of the body and holds the head re-keyed, its tensor bytes copied as they are."""

from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

PREFIX = "mtp."
MANIFEST = {"artifact_schema_version": 1, "rmsnorm_weight_format": "mlx_gamma"}
HEAD_FILE = "model.safetensors"
"""The head's file in its repository (mlx-community's MTP builds are one file)."""


def rekey_safetensors(src: Path, dst: Path, prefix: str) -> int:
    """Write `src` to `dst` with `prefix` before every tensor's name: the header is rewritten, the tensor data
    copied byte for byte (safetensors offsets are relative to the data, so they stand). Returns the number of
    tensors."""
    with src.open("rb") as f:
        (size,) = struct.unpack("<Q", f.read(8))
        header = json.loads(f.read(size))
        renamed = {k if k == "__metadata__" else f"{prefix}{k}": v for k, v in header.items()}
        raw = json.dumps(renamed, separators=(",", ":")).encode()
        raw += b" " * (-len(raw) % 8)  # the data starts 8-byte aligned, as the format's writers keep it
        tmp = dst.with_name(dst.name + ".tmp")
        with tmp.open("wb") as out:
            out.write(struct.pack("<Q", len(raw)))
            out.write(raw)
            shutil.copyfileobj(f, out, length=16 << 20)
    tmp.replace(dst)
    return sum(1 for k in header if k != "__metadata__")


def mtp_model_dir(body: Path, head: Path, out: Path) -> Path:
    """`out` as a model directory: a link to each file of the `body` snapshot, and `mtp/weights.safetensors`
    (the head's `HEAD_FILE` re-keyed under `mtp.`) with its manifest. Built once: a directory whose head is
    already written and whose links point at this body is kept as it is."""
    weights = out / "mtp" / "weights.safetensors"
    files = sorted(p for p in body.iterdir() if p.is_file())
    if weights.is_file() and all((out / p.name).resolve() == p.resolve() for p in files):
        return out
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.iterdir():
        if stale.is_symlink() or stale.is_file():
            stale.unlink()
    for p in files:
        (out / p.name).symlink_to(p)
    weights.parent.mkdir(exist_ok=True)
    rekey_safetensors(head / HEAD_FILE, weights, PREFIX)
    (weights.parent / "manifest.json").write_text(json.dumps(MANIFEST) + "\n")
    return out
