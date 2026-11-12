"""/v1/notifications over serve: subscribe, the key, kinds, the test push. No browser, no push service."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from airlie.auth.store import AuthStore
from airlie.serve.app import create_app
from tests.support.auth_store import Clock
from tests.support.serve import FakeUpstream, cfg
from tests.unit.serve_helpers import TestClient

ORIGIN = "http://localhost:8090"
SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}


class Studio:
    def __init__(self, tmp_path: Path, contact: str | None) -> None:
        self.cfg = cfg(tmp_path)
        self.cfg.notifications.contact = contact
        self.store = AuthStore(tmp_path / "state" / "auth.sqlite", now=Clock())
        noah = self.store.add_user("Noah", "admin", login="noah@github")
        self.app = create_app(
            self.cfg, transport=httpx.MockTransport(FakeUpstream()), auth_store=self.store, now=Clock()
        )
        session = self.store.make_session(noah.id, 30)
        self.admin = TestClient(
            self.app, base_url=ORIGIN, headers={"Origin": ORIGIN, "Cookie": f"airlie_session={session}"}
        )
        self.phone = TestClient(self.app, base_url=ORIGIN, headers={"Origin": ORIGIN}, client=("10.0.0.5", 1))


@pytest.fixture
def studio(tmp_path: Path):
    s = Studio(tmp_path, "mailto:noah@example.com")
    yield s
    s.store.close()


@pytest.fixture
def no_contact(tmp_path: Path):
    s = Studio(tmp_path, None)
    yield s
    s.store.close()


def test_subscribe_shows_a_device_and_the_key(studio: Studio) -> None:
    assert studio.admin.post("/v1/notifications/subscriptions", json=SUB).status_code == 204
    got = studio.admin.get("/v1/notifications").json()
    assert got["devices"] == 1
    assert len(got["key"]) == 87
    base64.urlsafe_b64decode(got["key"] + "=")
    assert got["kinds"]["chat_reply"] is True


def test_unsubscribe_removes_the_device(studio: Studio) -> None:
    studio.admin.post("/v1/notifications/subscriptions", json=SUB)
    r = studio.admin.request("DELETE", "/v1/notifications/subscriptions", json={"endpoint": SUB["endpoint"]})
    assert r.status_code == 204
    assert studio.admin.get("/v1/notifications").json()["devices"] == 0


def test_a_kind_switched_off_is_reflected(studio: Studio) -> None:
    assert studio.admin.put("/v1/notifications/kinds", json={"chat_reply": False}).status_code == 204
    kinds = studio.admin.get("/v1/notifications").json()["kinds"]
    assert kinds["chat_reply"] is False
    assert kinds["model_ready"] is True


def test_an_unknown_kind_is_refused(studio: Studio) -> None:
    assert studio.admin.put("/v1/notifications/kinds", json={"nope": True}).status_code == 422


def test_the_test_push_with_no_device_sends_nothing(studio: Studio) -> None:
    assert studio.admin.post("/v1/notifications/test").json() == {"sent": 0}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/v1/notifications", None),
        ("POST", "/v1/notifications/subscriptions", SUB),
        ("DELETE", "/v1/notifications/subscriptions", {"endpoint": "x"}),
        ("PUT", "/v1/notifications/kinds", {"chat_reply": True}),
        ("POST", "/v1/notifications/test", None),
    ],
)
def test_every_route_needs_a_sign_in(studio: Studio, method: str, path: str, body: Any) -> None:
    assert studio.phone.request(method, path, json=body).status_code == 401


def test_without_a_contact_the_key_is_null(no_contact: Studio) -> None:
    assert no_contact.admin.get("/v1/notifications").json()["key"] is None
