"""The token vault: user tokens sealed with AES-256-GCM before they reach the user store. The 32-byte key
lives in a file that is made 0600 on first use and refused when group or others can read it. The associated
data binds a token to its person, source and field, so a row copied to another person does not open.
Nothing in here logs, and no error carries key or token bytes."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
LOAD_TRIES = 20
LOAD_WAIT_SECONDS = 0.05


class VaultError(RuntimeError):
    """The key file is unusable, or a sealed token does not open (wrong key, wrong person, altered)."""


def aad(user_id: str, source: str, field: Literal["access", "refresh"]) -> str:
    return "\0".join((user_id, source, field))


class TokenVault:
    def __init__(self, key_file: Path) -> None:
        self._path = key_file.expanduser()
        self._aead: AESGCM | None = None

    def _read(self) -> bytes:
        """The key at the path; one a winning process created but has not finished writing is awaited."""
        for attempt in range(LOAD_TRIES):
            key = self._path.read_bytes()
            if len(key) == KEY_BYTES or attempt == LOAD_TRIES - 1:
                return key
            time.sleep(LOAD_WAIT_SECONDS)
        raise AssertionError("unreachable")  # pragma: no cover

    def _create(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:  # another process made the key between our look and our create: use theirs
            return
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(KEY_BYTES))

    def _key(self) -> AESGCM:
        if self._aead is None:
            if not self._path.exists():
                self._create()
            if stat.S_IMODE(self._path.stat().st_mode) & 0o077:
                raise VaultError(f"the token key {self._path} can be read by others: chmod 600 {self._path}")
            key = self._read()
            if len(key) != KEY_BYTES:
                raise VaultError(f"the token key {self._path} must be {KEY_BYTES} bytes")
            self._aead = AESGCM(key)
        return self._aead

    def seal(self, plaintext: str, *, aad: str) -> bytes:
        """nonce(12) || ciphertext, a fresh nonce each call."""
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._key().encrypt(nonce, plaintext.encode(), aad.encode())

    def open(self, sealed: bytes, *, aad: str) -> str:
        key = self._key()
        if len(sealed) <= NONCE_BYTES:
            raise VaultError("a sealed token is too short to open")
        try:
            return key.decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], aad.encode()).decode()
        except (InvalidTag, UnicodeDecodeError):
            raise VaultError(
                "a sealed token does not open: another key, another person, or altered"
            ) from None
