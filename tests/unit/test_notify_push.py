"""Web Push: the VAPID key, the aes128gcm payload and the sender. No network: httpx.MockTransport."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import http_ece
import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from airlie.auth.store import PushSubscription
from airlie.notify.push import PushSender, encrypt_payload
from airlie.notify.vapid import application_server_key, load_or_create_vapid


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _public_point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _subscription(endpoint: str) -> PushSubscription:
    receiver = ec.generate_private_key(ec.SECP256R1())
    return PushSubscription(
        endpoint=endpoint,
        user_id="u1",
        p256dh=_b64url(_public_point(receiver)),
        auth=_b64url(os.urandom(16)),
        created_at=0.0,
    )


def test_payload_round_trips_through_the_subscribers_key() -> None:
    receiver = ec.generate_private_key(ec.SECP256R1())
    auth = os.urandom(16)
    body = encrypt_payload(b'{"title":"t","url":"/"}', _b64url(_public_point(receiver)), _b64url(auth))
    # http_ece 1.2 names its pair encrypt/decrypt
    plain = http_ece.decrypt(body, private_key=receiver, auth_secret=auth, version="aes128gcm")
    assert plain == b'{"title":"t","url":"/"}'


async def test_send_posts_the_encrypted_body_with_vapid_headers(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = PushSender(load_or_create_vapid(tmp_path / "v.pem"), "mailto:noah@example.com", client)
        status = await sender.send(
            _subscription("https://web.push.apple.com/abc"), {"title": "t", "url": "/"}
        )
    assert status == 201
    r = seen[0]
    assert str(r.url) == "https://web.push.apple.com/abc"
    assert r.headers["content-encoding"] == "aes128gcm"
    assert r.headers["ttl"] == "86400"
    assert r.headers["urgency"] == "normal"
    assert r.headers["authorization"].startswith("vapid t=") and ",k=" in r.headers["authorization"]
    assert len(r.content) > 86  # salt, record size, key id and a tagged record


async def test_send_returns_the_push_services_refusal(tmp_path: Path) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(410))) as client:
        sender = PushSender(load_or_create_vapid(tmp_path / "v.pem"), "mailto:noah@example.com", client)
        assert await sender.send(_subscription("https://push.example.com:8443/x"), {"title": "t"}) == 410


def test_the_vapid_key_is_made_once_and_kept_private(tmp_path: Path) -> None:
    a = load_or_create_vapid(tmp_path / "v.pem")
    b = load_or_create_vapid(tmp_path / "v.pem")
    assert application_server_key(a) == application_server_key(b)
    assert (tmp_path / "v.pem").stat().st_mode & 0o777 == 0o600
    assert len(_b64url_decode(application_server_key(a))) == 65
    assert "=" not in application_server_key(a)
