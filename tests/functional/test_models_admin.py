"""The Admin models API at /v1/airlie/models: which upstream models airlie may use. Live ids are faked."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from airlie.auth.store import AuthStore
from airlie.config.schema import ModelSpec
from airlie.models.live import LiveModelsError
from airlie.serve.app import create_app
from tests.support.auth_store import Clock
from tests.support.serve import FakeUpstream, cfg
from tests.unit.serve_helpers import TestClient

ORIGIN = "http://localhost:8090"
ELSEWHERE = ("10.0.0.5", 40000)


class Studio:
    """Serve over a store: `admin` is signed in as an admin, `member` as a member."""

    def __init__(self, tmp_path: Path, live: list[str] | Exception) -> None:
        self.live = live
        self.store = AuthStore(tmp_path / "state" / "auth.sqlite", now=Clock())

        async def live_ids() -> list[str]:
            if isinstance(self.live, Exception):
                raise self.live
            return list(self.live)

        conf = cfg(tmp_path)
        conf.models["base"] = ModelSpec(label="Base model")
        app = create_app(
            conf,
            transport=httpx.MockTransport(FakeUpstream()),
            auth_store=self.store,
            model_live_ids=live_ids,
        )
        admin = self.store.add_user("Noah", "admin", login="noah@github")
        member = self.store.add_user("Pat", "member", login="pat@github")
        self.admin = self._client(app, admin.id)
        self.member = self._client(app, member.id)

    def _client(self, app, user_id: str) -> TestClient:
        session = self.store.make_session(user_id, 30)
        return TestClient(
            app,
            base_url=ORIGIN,
            headers={"Origin": ORIGIN, "Cookie": f"airlie_session={session}"},
            client=ELSEWHERE,
        )


@pytest.fixture
def make_studio(tmp_path: Path):
    made: list[Studio] = []

    def make(live: list[str] | Exception) -> Studio:
        s = Studio(tmp_path, live)
        made.append(s)
        return s

    yield make
    for s in made:
        s.store.close()


def test_member_cannot_list_or_toggle_models(make_studio) -> None:
    s = make_studio(["keep"])
    assert s.member.get("/v1/airlie/models").status_code == 403
    assert s.member.patch("/v1/airlie/models/keep", json={"allowed": False}).status_code == 403
    assert s.admin.get("/v1/airlie/models").json() == [
        {"id": "keep", "label": None, "allowed": True, "live": True}
    ]


def test_admin_lists_live_and_toggles(make_studio) -> None:
    s = make_studio(["keep", "drop"])
    rows = s.admin.get("/v1/airlie/models").json()
    assert {r["id"] for r in rows} >= {"keep", "drop"}
    assert s.admin.patch("/v1/airlie/models/drop", json={"allowed": False}).status_code == 204
    again = {r["id"]: r for r in s.admin.get("/v1/airlie/models").json()}
    assert again["drop"]["allowed"] is False
    assert again["keep"]["allowed"] is True
    assert again["drop"]["live"] is True


def test_label_comes_from_the_catalog(make_studio) -> None:
    s = make_studio(["base", "other"])
    rows = {r["id"]: r["label"] for r in s.admin.get("/v1/airlie/models").json()}
    assert rows == {"base": "Base model", "other": None}


def test_a_toggled_model_that_left_upstream_stays_listed_not_live(make_studio) -> None:
    s = make_studio(["keep", "gone"])
    assert s.admin.patch("/v1/airlie/models/gone", json={"allowed": False}).status_code == 204
    s.live = ["keep"]
    again = {r["id"]: r for r in s.admin.get("/v1/airlie/models").json()}
    assert again["gone"] == {"id": "gone", "label": None, "allowed": False, "live": False}
    assert again["keep"]["live"] is True


def test_upstream_failure_still_lists_known_models_not_live(make_studio) -> None:
    s = make_studio(["keep"])
    assert s.admin.patch("/v1/airlie/models/keep", json={"allowed": False}).status_code == 204
    s.live = LiveModelsError("engine down")
    r = s.admin.get("/v1/airlie/models")
    assert r.status_code == 200
    rows = {x["id"]: x for x in r.json()}
    assert rows["keep"]["live"] is False and rows["keep"]["allowed"] is False
    assert s.admin.patch("/v1/airlie/models/keep", json={"allowed": True}).status_code == 204


def test_patch_rejects_a_body_without_allowed(make_studio) -> None:
    s = make_studio(["keep"])
    assert s.admin.patch("/v1/airlie/models/keep", json={}).status_code == 422
