"""The server's VAPID identity (RFC 8292): one P-256 key kept private on disk, whose public point the browser
is given as its applicationServerKey."""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02

LOAD_TRIES = 20
LOAD_WAIT_SECONDS = 0.05


def _load(path: Path) -> Vapid02:
    """Read the key; one a winning process created but has not finished writing is awaited briefly."""
    for _ in range(LOAD_TRIES - 1):
        try:
            return Vapid02.from_file(str(path))
        except Exception:  # empty or half-written PEM: py_vapid raises whatever the parser did
            time.sleep(LOAD_WAIT_SECONDS)
    return Vapid02.from_file(str(path))


def load_or_create_vapid(path: Path) -> Vapid02:
    """The key at `path`, made (mode 0600) when missing."""
    if path.exists():
        return _load(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    vapid = Vapid02()
    vapid.generate_keys()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another process made the key between our look and our create: use theirs
        return _load(path)
    with os.fdopen(fd, "wb") as f:
        f.write(vapid.private_pem())
    return vapid


def application_server_key(v: Vapid02) -> str:
    """Base64url, unpadded, of the uncompressed public point: what `pushManager.subscribe` wants."""
    point = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(point).rstrip(b"=").decode()
