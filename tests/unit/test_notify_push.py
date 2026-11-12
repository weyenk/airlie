"""Web Push: the VAPID key, the aes128gcm payload and the sender. No network: httpx.MockTransport."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import http_ece
import httpx
import pytest
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


def _claims(request: httpx.Request) -> dict[str, object]:
    token = request.headers["authorization"].removeprefix("vapid t=").split(",")[0]
    return json.loads(_b64url_decode(token.split(".")[1]))  # type: ignore[no-any-return]


async def _sent(tmp_path: Path, endpoint: str, contact: str = "mailto:noah@example.com") -> httpx.Request:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = PushSender(load_or_create_vapid(tmp_path / "v.pem"), contact, client)
        await sender.send(_subscription(endpoint), {"title": "t"})
    return seen[0]


async def test_the_vapid_claims_name_the_push_service_origin_the_contact_and_a_day_at_most(
    tmp_path: Path,
) -> None:
    claims = _claims(await _sent(tmp_path, "https://web.push.apple.com/some/path?x=1"))
    assert claims["aud"] == "https://web.push.apple.com"
    assert claims["sub"] == "mailto:noah@example.com"
    assert isinstance(claims["exp"], int) and 0 < claims["exp"] - time.time() <= 86400


async def test_a_port_in_the_endpoint_stays_in_the_vapid_audience(tmp_path: Path) -> None:
    claims = _claims(await _sent(tmp_path, "https://push.example.com:8443/x"))
    assert claims["aud"] == "https://push.example.com:8443"


def test_two_encryptions_of_one_payload_use_different_ephemeral_keys() -> None:
    sub = _subscription("https://push.example.com/x")
    a, b = (encrypt_payload(b"same", sub.p256dh, sub.auth) for _ in range(2))
    key_a, key_b = a[21 : 21 + a[20]], b[21 : 21 + b[20]]  # header: salt, rs, idlen, keyid
    assert len(key_a) == len(key_b) == 65 and key_a != key_b
    assert a != b


def test_a_payload_that_would_not_fit_one_push_message_is_refused() -> None:
    sub = _subscription("https://push.example.com/x")
    assert len(encrypt_payload(b"x" * 3990, sub.p256dh, sub.auth)) <= 4096
    with pytest.raises(ValueError, match="4096"):
        encrypt_payload(b"x" * 4000, sub.p256dh, sub.auth)


def test_a_second_process_winning_the_key_file_race_is_loaded_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v.pem"
    winner = load_or_create_vapid(path)
    monkeypatch.setattr(Path, "exists", lambda self: False)  # this process looked before the winner wrote
    loser = load_or_create_vapid(path)
    assert application_server_key(loser) == application_server_key(winner)


def test_a_loser_that_reads_the_key_before_the_winner_finished_writing_waits_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "v.pem"
    winner = load_or_create_vapid(tmp_path / "w.pem")
    pem = (tmp_path / "w.pem").read_bytes()
    path.write_bytes(b"")  # created by the winner, not yet written
    monkeypatch.setattr("airlie.notify.vapid.time.sleep", lambda _s: path.write_bytes(pem))
    loser = load_or_create_vapid(path)
    assert application_server_key(loser) == application_server_key(winner)
