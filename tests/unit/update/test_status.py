"""build_status: the read-only projection the panel, the CLI and the admin route all read.

No network. It only reads the config, the ledger and the running identity, so the tests build a
real UpdateStore over sqlite, seed the ledger, and check the dict shape the panel binds to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from airlie.config.schema import AirlieConfig
from airlie.update import identity as identity_module
from airlie.update import status as st
from airlie.update import store as store_module
from airlie.update.check import CheckOutcome
from airlie.update.feed import ChannelPointer, ImageRef, ReleaseJson
from tests.support.serve import cfg as base_cfg

# The keys the plan's Task 6 contract names. `history` is deliberately absent: it is a route of
# its own, so the main projection must not grow a second copy of the ledger.
CONTRACT_KEYS = (
    "enabled",
    "panel",
    "feed",
    "running",
    "available",
    "desired_digest",
    "channel",
    "last_check",
    "drift",
)


def _outcome(
    release: ReleaseJson | None, pointer: ChannelPointer | None, etag: str, at: str, ok: bool = True
) -> CheckOutcome:
    """A check row, for seeding the ledger without a network."""
    return CheckOutcome(
        checked_at=at, ok=ok, reason=None if ok else "http 503", release=release, pointer=pointer, etag=etag
    )


def _cfg(tmp_path: Path) -> AirlieConfig:
    """A section-on config with a pinned feed, so nothing here can reach the network. Built over
    the shared serve fixture so the rest of the config stays valid."""
    cfg = base_cfg(tmp_path)
    cfg.update.enabled = True
    cfg.update.panel = True
    cfg.update.feed.kind = "pinned"
    cfg.update.feed.pinned_version = "1.0.0"
    cfg.update.feed.pinned_digest = "b" * 64
    return cfg


def _store(tmp_path: Path) -> store_module.UpdateStore:
    return store_module.UpdateStore(tmp_path / "update.sqlite")


def _release(version: str, digest: str = "c" * 64, pull_digest: str | None = None) -> ReleaseJson:
    """A minimal release document. The panel only reads what release_summary gives it.

    `pull_digest` stays None unless a test asks for it: the projection has to answer correctly for a
    release published before that digest existed, and half the tests here are that case by default.
    """
    return ReleaseJson(
        version=version,
        released_at="2026-11-24T00:00:00Z",
        git_sha="0" * 40,
        image=ImageRef(repo="r", tag=version, digest=digest, pull_digest=pull_digest),
        notes="",
        breaking=False,
        state_migration=False,
        schema_epoch=1,
    )


def _pointer(version: str, digest: str) -> ChannelPointer:
    return ChannelPointer(
        version=version,
        digest=digest,
        released_at="2026-11-24T00:00:00Z",
        release_json=f"releases/{version}.json",
        etag="p1",
    )


def _running() -> identity_module.RunningRelease:
    return identity_module.RunningRelease(version="0.1.0", git_sha=None, image_digest=None, schema_epoch=1)


def test_panel_enabled_requires_section_on_and_panel(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert st.panel_enabled(cfg) is True
    # panel off closes the nav gate (the routes stay admin-readable)
    cfg.update.panel = False
    assert st.panel_enabled(cfg) is False
    # the section off closes it too
    cfg.update.panel = True
    cfg.update.enabled = False
    assert st.panel_enabled(cfg) is False


def test_disabled_reports_a_quiet_answer(tmp_path: Path) -> None:
    """A disabled updater is a present, empty answer, not an error: the routes are the 404."""
    cfg = _cfg(tmp_path)
    cfg.update.enabled = False
    store = _store(tmp_path)
    try:
        body = st.build_status(cfg, store, _running())
        assert body["enabled"] is False
        assert body["panel"] is False
        assert body["available"] == []
        assert body["channel"] is None
        assert body["feed_unavailable"] is False
        assert body["drift"] is None
    finally:
        store.close()


def test_build_status_contract_keys(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    try:
        store.record_check(
            _outcome(_release("1.0.0"), _pointer("1.0.0", "b" * 64), "e1", "2026-11-24T00:00:00Z")
        )
        body = st.build_status(cfg, store, _running())
        for key in CONTRACT_KEYS:
            assert key in body, f"missing key {key}"
    finally:
        store.close()


def test_build_status_fields(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    try:
        store.record_check(
            _outcome(_release("1.0.0"), _pointer("1.0.0", "b" * 64), "e1", "2026-11-24T00:00:00Z")
        )
        body = st.build_status(cfg, store, _running())
        assert body["enabled"] is True
        assert body["panel"] is True
        assert body["feed"] == {"kind": "pinned", "channel": "stable", "base_url": cfg.update.feed.base_url}
        assert body["running"]["version"] == "0.1.0"
        assert set(body["running"]) == {"version", "git_sha", "image_digest", "schema_epoch"}
        assert body["desired_digest"] is None
        # the channel is the last-good release summary: what a pin would point at
        assert body["channel"]["version"] == "1.0.0"
        assert body["channel"]["digest"] == "c" * 64
        assert body["available"] == [body["channel"]]
        assert body["last_check"] == {
            "at": "2026-11-24T00:00:00Z",
            "ok": True,
            "reason": None,
            "detail": None,
        }
        assert body["feed_unavailable"] is False
        assert body["ledger_unreadable"] is False
    finally:
        store.close()


def test_history_is_not_on_the_main_projection(tmp_path: Path) -> None:
    """History has its own route; carrying it here would give the panel two answers to sort out."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    try:
        store.record_check(
            _outcome(_release("1.0.0"), _pointer("1.0.0", "b" * 64), "e1", "2026-11-24T00:00:00Z")
        )
        assert "history" not in st.build_status(cfg, store, _running())
    finally:
        store.close()


def test_desired_digest_is_the_pin(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    pin = "d" * 64
    try:
        store.set_desired(pin, actor="ops")
        body = st.build_status(cfg, store, _running())
        assert body["desired_digest"] == pin
        # the digest the operator should put in their manifest: the pin wins over the channel
        assert body["drift"]["manifest_digest"] == pin
    finally:
        store.close()


def test_a_down_feed_keeps_the_last_good_release(tmp_path: Path) -> None:
    """A refused feed must not erase the cache: the last good release stands, beside the error."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    try:
        store.record_check(
            _outcome(_release("1.0.0"), _pointer("1.0.0", "b" * 64), "e1", "2026-11-24T00:00:00Z")
        )
        store.record_check(_outcome(None, None, "e2", "2026-11-24T01:00:00Z", ok=False))
        body = st.build_status(cfg, store, _running())
        assert body["channel"]["version"] == "1.0.0"
        assert body["last_check"]["ok"] is False
        assert body["last_check"]["reason"] == "http 503"
        assert body["feed_unavailable"] is True
        assert body["drift"]["feed_ok"] is False
    finally:
        store.close()


def test_no_checks_yet_is_unknown_not_current(tmp_path: Path) -> None:
    """Never checked: the panel must not read that as up to date."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    try:
        body = st.build_status(cfg, store, _running())
        assert body["last_check"] is None
        assert body["available"] == []
        assert body["feed_unavailable"] is True
        assert body["drift"]["drift"] == "unknown"
    finally:
        store.close()


def test_build_status_never_opens_a_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The projection is the panel's fast path; a fetch here would make every page load a request."""
    import httpx

    def _no_transport(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("build_status must not touch the network")

    monkeypatch.setattr(httpx, "Client", _no_transport)
    monkeypatch.setattr(httpx, "AsyncClient", _no_transport)
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    try:
        store.record_check(
            _outcome(_release("1.0.0"), _pointer("1.0.0", "b" * 64), "e1", "2026-11-24T00:00:00Z")
        )
        body: dict[str, Any] = st.build_status(cfg, store, _running())
        assert body["enabled"] is True
    finally:
        store.close()


def test_the_projection_offers_the_pull_digest_as_the_one_to_pin(tmp_path: Path) -> None:
    """The release's two digests reach the panel through the ledger, each in its own slot.

    `channel.pull_digest` is the string the runbook tells an operator to copy into a manifest, and
    `drift.manifest_digest` is that same string — the panel's "pin this" line has to be a value the
    registry will serve for the tag. `drift.channel_digest` and `channel.digest` stay the
    self-reported one, because that is what the running container can be compared with: the deployment
    below runs the image whose self-report matches the channel, and reads `current` even though the
    two digests differ.
    """
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    self_reported = "c" * 64
    pull = "d" * 64
    try:
        store.record_check(
            _outcome(
                _release("1.0.0", self_reported, pull_digest=pull),
                _pointer("1.0.0", self_reported),
                "e1",
                "2026-11-24T00:00:00Z",
            )
        )
        running = identity_module.RunningRelease(
            version="1.0.0", git_sha=None, image_digest=self_reported, schema_epoch=1
        )
        body = st.build_status(cfg, store, running)
        assert body["channel"]["digest"] == self_reported
        assert body["channel"]["pull_digest"] == pull
        assert body["available"][0]["pull_digest"] == pull
        assert body["drift"]["manifest_digest"] == pull
        assert body["drift"]["channel_digest"] == self_reported
        assert body["drift"]["drift"] == "current"
    finally:
        store.close()


def test_a_release_without_a_pull_digest_still_offers_something(tmp_path: Path) -> None:
    """A release published before the pull digest existed has one digest and the panel must not answer
    `manifest_digest: null` — an operator with no recommendation pins nothing."""
    cfg = _cfg(tmp_path)
    store = _store(tmp_path)
    self_reported = "c" * 64
    try:
        store.record_check(
            _outcome(_release("1.0.0", self_reported), _pointer("1.0.0", self_reported), "e1", "t")
        )
        body = st.build_status(cfg, store, _running())
        assert body["channel"]["pull_digest"] is None
        assert body["drift"]["manifest_digest"] == self_reported
    finally:
        store.close()
