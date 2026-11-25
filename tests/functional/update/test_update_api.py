"""The updater's HTTP surface over a real serve app: the panel read, the pin, the forced check.

Disabled section → the routes do not exist (404), per spec §C. Enabled → every route is admin-only
(``_admin``), which is why a member's read is 403 and an anonymous one 401: the panel's visibility
is a nav gate (``/v1/auth/me``'s ``update_panel``), never an authorization shortcut.

The feed is ``pinned``, so a check pass needs no network: the writes below can assert on a real
ledger row instead of a mocked one.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from airlie.auth.api import SESSION_COOKIE
from airlie.auth.store import AuthStore
from airlie.config.schema import AirlieConfig
from airlie.serve.app import create_app
from airlie.update.feed import ChannelPointer, ImageRef, ReleaseJson
from airlie.update.store import CheckOutcome, UpdateStore
from tests.support.serve import cfg as base_cfg
from tests.unit.serve_helpers import TestClient

ORIGIN = "http://localhost:8090"
ELSEWHERE = ("10.0.0.5", 1)

PINNED_DIGEST = "sha256:" + "a" * 64
LAST_GOOD_DIGEST = "sha256:" + "b" * 64
UNKNOWN_DIGEST = "sha256:" + "c" * 64

STATUS = "/v1/admin/update"
DESIRED = "/v1/admin/update/desired"
CHECK = "/v1/admin/update/check"
HISTORY = "/v1/admin/update/history"


def _cfg(tmp_path: Path, *, enabled: bool) -> AirlieConfig:
    cfg = base_cfg(tmp_path)
    cfg.update.enabled = enabled
    if enabled:
        cfg.update.feed.kind = "pinned"
        cfg.update.feed.pinned_version = "1.0.0"
        cfg.update.feed.pinned_digest = PINNED_DIGEST
    return cfg


def _release(version: str, digest: str) -> ReleaseJson:
    return ReleaseJson(
        version=version,
        released_at="2026-11-24T00:00:00Z",
        git_sha="0" * 40,
        image=ImageRef(repo="r", tag=version, digest=digest),
        notes="",
        breaking=False,
        state_migration=False,
        schema_epoch=1,
    )


def _seed(store: UpdateStore, digest: str) -> None:
    """One good pass, so the pin has a published digest to be allowed against."""
    store.record_check(
        CheckOutcome(
            checked_at="2026-11-24T00:00:00Z",
            ok=True,
            reason=None,
            release=_release("1.0.0", digest),
            pointer=ChannelPointer(
                version="1.0.0",
                digest=digest,
                released_at="2026-11-24T00:00:00Z",
                release_json="releases/1.0.0.json",
                etag="p1",
            ),
            etag="e1",
        )
    )


class World:
    """An app, its ledger, and the three callers the gate has to tell apart."""

    def __init__(self, tmp_path: Path, *, enabled: bool, panel: bool = True, seed: bool = True) -> None:
        self.cfg = _cfg(tmp_path, enabled=enabled)
        self.cfg.update.panel = panel
        self.store = UpdateStore(tmp_path / "update.sqlite")
        if seed:
            _seed(self.store, LAST_GOOD_DIGEST)
        self.auth = AuthStore(tmp_path / "auth.sqlite")
        member = self.auth.add_user("Noah", "member")
        admin = self.auth.add_user("Ops", "admin")
        # both callers come from off-loopback, so nothing here leans on the bootstrap-admin bypass
        self.member = {"Cookie": f"{SESSION_COOKIE}={self.auth.make_session(member.id, 30)}"}
        self.admin_cookie = {"Cookie": f"{SESSION_COOKIE}={self.auth.make_session(admin.id, 30)}"}
        self.app = create_app(
            self.cfg,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            auth_store=self.auth,
            update_store=self.store,
        )

    def admin(self) -> TestClient:
        client = TestClient(self.app, base_url=ORIGIN, client=ELSEWHERE)
        client.headers.update(self.admin_cookie)
        return client

    def anon(self) -> TestClient:
        return TestClient(self.app, base_url=ORIGIN, client=ELSEWHERE)

    def as_member(self) -> TestClient:
        client = self.anon()
        client.headers.update(self.member)
        return client


class TestRead:
    def test_admin_reads_the_projection(self, tmp_path: Path) -> None:
        world = World(tmp_path, enabled=True)
        r = world.admin().get(STATUS)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True
        assert body["feed"]["kind"] == "pinned"
        assert body["channel"]["digest"] == LAST_GOOD_DIGEST
        assert body["desired_digest"] is None
        assert "history" not in body

    def test_member_is_403(self, tmp_path: Path) -> None:
        world = World(tmp_path, enabled=True)
        assert world.as_member().get(STATUS).status_code == 403

    def test_anonymous_is_401(self, tmp_path: Path) -> None:
        """401/403, never 500 — the gate runs before anything touches the ledger."""
        world = World(tmp_path, enabled=True)
        assert world.anon().get(STATUS).status_code == 401

    def test_panel_flag_does_not_open_the_route(self, tmp_path: Path) -> None:
        """`panel` is a nav gate. An open panel is not an unauthenticated read of the deployment."""
        world = World(tmp_path, enabled=True, panel=True)
        assert world.anon().get(STATUS).status_code == 401
        assert world.admin().get(STATUS).status_code == 200

    def test_history_is_its_own_route(self, tmp_path: Path) -> None:
        world = World(tmp_path, enabled=True)
        r = world.admin().get(HISTORY)
        assert r.status_code == 200, r.text
        rows = r.json()["history"]
        assert len(rows) == 1
        assert rows[0]["ok"] is True


class TestDesired:
    def test_member_cannot_pin(self, tmp_path: Path) -> None:
        world = World(tmp_path, enabled=True)
        assert world.as_member().put(DESIRED, json={"digest": LAST_GOOD_DIGEST}).status_code == 403

    def test_admin_pins_a_published_digest(self, tmp_path: Path) -> None:
        world = World(tmp_path, enabled=True)
        r = world.admin().put(DESIRED, json={"digest": LAST_GOOD_DIGEST})
        assert r.status_code == 200, r.text
        assert r.json()["desired_digest"] == LAST_GOOD_DIGEST
        # the pin is in the ledger, not just the response
        assert world.store.desired()[0] == LAST_GOOD_DIGEST

    def test_an_unknown_digest_is_400_and_is_not_stored(self, tmp_path: Path) -> None:
        """Pinning a digest no release publishes strands an install: refused, and nothing written."""
        world = World(tmp_path, enabled=True)
        r = world.admin().put(DESIRED, json={"digest": UNKNOWN_DIGEST})
        assert r.status_code == 400, r.text
        assert world.store.desired() == (None, None)

    def test_delete_clears_the_pin(self, tmp_path: Path) -> None:
        world = World(tmp_path, enabled=True)
        admin = world.admin()
        admin.put(DESIRED, json={"digest": LAST_GOOD_DIGEST})
        r = admin.delete(DESIRED)
        assert r.status_code == 200, r.text
        assert r.json()["desired_digest"] is None
        assert world.store.desired() == (None, None)

    def test_the_audit_trail_names_the_actor(self, tmp_path: Path) -> None:
        world = World(tmp_path, enabled=True)
        world.admin().put(DESIRED, json={"digest": LAST_GOOD_DIGEST})
        actions = [row["action"] for row in world.store.audit()]
        assert "desired_set" in actions


class TestCheck:
    def test_check_records_a_row(self, tmp_path: Path) -> None:
        world = World(tmp_path, enabled=True)
        before = len(world.store.history())
        r = world.admin().post(CHECK)
        assert r.status_code == 200, r.text
        assert len(world.store.history()) == before + 1
        # the response is the same projection the panel reads, not a second answer
        assert r.json()["feed"]["kind"] == "pinned"

    def test_check_is_admin_only(self, tmp_path: Path) -> None:
        world = World(tmp_path, enabled=True)
        assert world.as_member().post(CHECK).status_code == 403


class TestDisabled:
    def test_every_route_is_404_when_the_section_is_off(self, tmp_path: Path) -> None:
        """Absent, not forbidden: an install that never turned this on has no such surface."""
        world = World(tmp_path, enabled=False, seed=False)
        admin = world.admin()
        assert admin.get(STATUS).status_code == 404
        assert admin.get(HISTORY).status_code == 404
        assert admin.put(DESIRED, json={"digest": LAST_GOOD_DIGEST}).status_code == 404
        assert admin.delete(DESIRED).status_code == 404
        assert admin.post(CHECK).status_code == 404

    def test_me_reports_the_panel_closed(self, tmp_path: Path) -> None:
        world = World(tmp_path, enabled=False, seed=False)
        assert world.admin().get("/v1/auth/me").json()["update_panel"] is False


def test_me_carries_the_panel_flag(tmp_path: Path) -> None:
    """The nav gates on this one boolean; it is config, not a separate permission."""
    world = World(tmp_path, enabled=True)
    body = world.admin().get("/v1/auth/me").json()
    assert body["update_panel"] is True
    assert body["role"] == "admin"


def test_me_flag_follows_the_panel_setting(tmp_path: Path) -> None:
    world = World(tmp_path, enabled=True, panel=False)
    assert world.admin().get("/v1/auth/me").json()["update_panel"] is False
