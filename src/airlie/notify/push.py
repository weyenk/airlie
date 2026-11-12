"""Web Push delivery: RFC 8291 aes128gcm encryption to a subscription, signed with the server's VAPID key."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import urlsplit

import http_ece
import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from airlie.auth.store import PushSubscription

_TTL_SECONDS = 86400
_VAPID_LIFETIME_SECONDS = 12 * 3600


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encrypt_payload(payload: bytes, p256dh: str, auth: str) -> bytes:
    """The aes128gcm body for one subscription, from a fresh ECDH key (its public point is in the header)."""
    body: bytes = http_ece.encrypt(
        payload,
        private_key=ec.generate_private_key(ec.SECP256R1()),
        dh=_b64url_decode(p256dh),
        auth_secret=_b64url_decode(auth),
        version="aes128gcm",
    )
    return body


class PushSender:
    def __init__(self, vapid: Vapid02, contact: str, client: httpx.AsyncClient) -> None:
        self._vapid = vapid
        self._contact = contact
        self._client = client

    async def send(self, sub: PushSubscription, payload: dict[str, str]) -> int:
        """POST one notification; the push service's status code (201 accepted, 404/410 means the subscription
        is gone)."""
        parts = urlsplit(sub.endpoint)
        claims = {
            "sub": self._contact,
            "aud": f"{parts.scheme}://{parts.netloc}",
            "exp": int(time.time()) + _VAPID_LIFETIME_SECONDS,
        }
        headers = {
            **self._vapid.sign(claims),
            "TTL": str(_TTL_SECONDS),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "Urgency": "normal",
        }
        body = encrypt_payload(json.dumps(payload).encode(), sub.p256dh, sub.auth)
        response = await self._client.post(sub.endpoint, content=body, headers=headers)
        return response.status_code
