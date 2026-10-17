"""A software authenticator for tests: answers WebAuthn creation and request options the way a platform
authenticator (Face ID, Touch ID) does, with user presence and verification set, so the passkey API can be
driven end to end without a browser."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

UP_UV_AT = 0x45  # user present, user verified, attested credential data follows
UP_UV = 0x05


class SoftAuthenticator:
    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.user_handle: bytes | None = None
        self.sign_count = 0

    def _client_data(self, kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}
        ).encode()

    def create(self, options: dict[str, Any], origin: str) -> dict[str, Any]:
        """A registration credential, as `navigator.credentials.create(...).toJSON()` returns it."""
        self.user_handle = base64url_to_bytes(options["user"]["id"])
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")}
        )
        attested = bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + cose
        auth_data = (
            hashlib.sha256(options["rp"]["id"].encode()).digest()
            + bytes([UP_UV_AT])
            + struct.pack(">I", self.sign_count)
            + attested
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(
                    self._client_data("webauthn.create", options["challenge"], origin)
                ),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal"],
            },
        }

    def get(self, options: dict[str, Any], origin: str, *, sign_count: int | None = None) -> dict[str, Any]:
        """An assertion, as `navigator.credentials.get(...).toJSON()` returns it. The sign count advances by
        one unless given, as a cloned authenticator's would not."""
        self.sign_count = self.sign_count + 1 if sign_count is None else sign_count
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        auth_data = (
            hashlib.sha256(options["rpId"].encode()).digest()
            + bytes([UP_UV])
            + struct.pack(">I", self.sign_count)
        )
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(self.user_handle) if self.user_handle else None,
            },
        }
