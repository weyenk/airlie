"""The token vault: user tokens sealed with AES-256-GCM before they reach the user store. The 32-byte key
lives in a file that is made 0600 on first use and refused when group or others can read it. The associated
data binds a token to its person, source and field, so a row copied to another person does not open.
Nothing in here logs, and no error carries key or token bytes."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
KEY_VERSION = 1  # the first byte of every sealed token; a rotation that keeps old tokens bumps it
LOAD_TRIES = 20
LOAD_WAIT_SECONDS = 0.05


class VaultError(RuntimeError):
    """The key file is unusable, or a sealed token does not open (wrong key, wrong person, altered)."""


def aad(user_id: str, source: str, field: Literal["access", "refresh"]) -> str:
    """Each part length-prefixed, so no two tuples share a string."""
    return "".join(f"{len(part)}:{part}" for part in (user_id, source, field))


class TokenVault:
    def __init__(self, key_file: Path, *, keep_out: Sequence[Path] = ()) -> None:
        """`keep_out`: directories the key must not live in (the user store's, the repository)."""
        self._path = key_file.expanduser()
        self._keep_out = [p.expanduser().resolve() for p in keep_out]
        self._aead: AESGCM | None = None

    def _create(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # another process made the key between our look and our create: use theirs
            return
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(KEY_BYTES))

    def _read(self) -> bytes:
        """Open once, without following a symlink, check the mode on that descriptor, read from it. A key a
        winning process created but has not finished writing is awaited briefly."""
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise VaultError(
                    f"the token key {self._path} is a symlink: point the setting at the file"
                ) from None
            raise
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if st.st_mode & 0o077:
                raise VaultError(f"the token key {self._path} can be read by others: chmod 600 {self._path}")
            if st.st_uid != os.geteuid():
                raise VaultError(f"the token key {self._path} belongs to another user")
            for attempt in range(LOAD_TRIES):
                f.seek(0)
                key = f.read()
                if len(key) == KEY_BYTES or attempt == LOAD_TRIES - 1:
                    return key
                time.sleep(LOAD_WAIT_SECONDS)
        raise AssertionError("unreachable")  # pragma: no cover

    def _key(self) -> AESGCM:
        if self._aead is None:
            where = self._path.parent.resolve() / self._path.name
            for out in self._keep_out:
                if where.is_relative_to(out):
                    raise VaultError(
                        f"the token key {self._path} is inside {out}: keep it outside the state and repo"
                    )
            if not os.path.lexists(self._path):
                self._create()
            key = self._read()
            if len(key) != KEY_BYTES:
                raise VaultError(f"the token key {self._path} must be {KEY_BYTES} bytes")
            self._aead = AESGCM(key)
        return self._aead

    def seal(self, plaintext: str, *, aad: str) -> bytes:
        """version(1) || nonce(12) || ciphertext, a fresh nonce each call."""
        nonce = os.urandom(NONCE_BYTES)
        return bytes([KEY_VERSION]) + nonce + self._key().encrypt(nonce, plaintext.encode(), aad.encode())

    def open(self, sealed: bytes, *, aad: str) -> str:
        key = self._key()
        if len(sealed) <= 1 + NONCE_BYTES:
            raise VaultError("a sealed token is too short to open")
        if sealed[0] != KEY_VERSION:
            raise VaultError(f"a sealed token is of key version {sealed[0]}; this airlie knows {KEY_VERSION}")
        body = sealed[1:]
        try:
            return key.decrypt(body[:NONCE_BYTES], body[NONCE_BYTES:], aad.encode()).decode()
        except (InvalidTag, UnicodeDecodeError):
            raise VaultError(
                "a sealed token does not open: another key, another person, or altered"
            ) from None
