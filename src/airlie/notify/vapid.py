"""The server's VAPID identity (RFC 8292): one P-256 key kept private on disk, whose public point the browser
is given as its applicationServerKey."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02


def load_or_create_vapid(path: Path) -> Vapid02:
    """The key at `path`, made (mode 0600) when missing."""
    if path.exists():
        return Vapid02.from_file(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    vapid = Vapid02()
    vapid.generate_keys()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another process made the key between our look and our create: use theirs
        return Vapid02.from_file(str(path))
    with os.fdopen(fd, "wb") as f:
        f.write(vapid.private_pem())
    return vapid


def application_server_key(v: Vapid02) -> str:
    """Base64url, unpadded, of the uncompressed public point: what `pushManager.subscribe` wants."""
    point = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(point).rstrip(b"=").decode()
