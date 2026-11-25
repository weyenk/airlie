"""App-lifespan test: the ticker starts only when enabled, stops on teardown.

``TestClient`` (used in the functional suite) does **not** run lifespan handlers, so no unit test had
evidence that "no ticker may start" when disabled.  This file uses the context-manager form so
lifespan startup/shutdown actually execute, and asserts on ``app.state.update_ticker`` at the right
moments.

Key point: ``TestClient(app)`` as a *context manager* (``with TestClient(...)``) calls the Starlette
lifespan handlers (startup → shutdown).  Without the context manager, nothing runs — which is
exactly why no existing test observed this wiring.

The ticker's ``_loop`` runs a checker that calls ``run_check``, which for a **pinned** feed reads
a local file — no network.  No transport is needed when the section is pinned to a ``path`` on
disk.

The enabled tests poll the ledger for the boot check's row with a bounded deadline: ``run_check``
records an ``ok=False`` row (with the feed's reason) when a document fails to parse, and no row at
all when it raises outside ``FeedError``, so a fixture that stops parsing is caught by the
assertions on the row, not swallowed by the ticker.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient as _TestClient

from airlie.config.schema import AirlieConfig
from airlie.serve.app import create_app
from airlie.update.identity import SCHEMA_EPOCH
from airlie.update.store import CheckOutcome, UpdateStore, update_store_path
from tests.support.config import MINIMAL

#: 64-hex-char digest (matching DIGEST_RE: sha256:[0-9a-f]{64}).
_PINNED_DIGEST = "sha256:" + "a" * 64

#: Strict three-segment semver: ``is_dev_version`` must read it as a release, or the stable
#: channel refuses the pointer before any document is read.
_RELEASE_VERSION = "1.4.2"


def _wait_for_check(store: UpdateStore, *, timeout_s: float = 2.0) -> CheckOutcome | None:
    """The boot check's ledger row, or ``None`` when the deadline passes before one lands."""
    deadline = time.monotonic() + timeout_s
    while True:
        row = store.last_check()
        if row is not None or time.monotonic() >= deadline:
            return row
        time.sleep(0.01)


def _make_pin_dir(pin_path: Path) -> None:
    """Create a valid pinned-feed directory layout for ``_read_pinned_documents``.

    Structure expected by the real code:
      <pin_path>/channels/stable.json   — pointer (has version, digest, **release_json**)
      <pin_path>/<pointer.release_json> — release (has version, image: {repo, tag, digest})

    Both documents must agree on ``version``.
    """
    pin_path.mkdir(parents=True, exist_ok=True)  # create root + channels/ tree
    (pin_path / "channels").mkdir(parents=True, exist_ok=True)  # creates channels/ inside pin_path
    stable = pin_path / "channels" / "stable.json"
    release_file = f"v{_RELEASE_VERSION}.json"  # pointer names this; file lives at pin_path/<name>
    stable.write_text(
        json.dumps(
            {
                "version": _RELEASE_VERSION,
                "digest": _PINNED_DIGEST,
                "release_json": release_file,
                "released_at": "2026-01-01T00:00:00Z",
            }
        ),
    )
    # Release document — version must match the pointer; schema_epoch is the code's declared epoch.
    (pin_path / release_file).write_text(
        json.dumps(
            {
                "version": _RELEASE_VERSION,
                "image": {"repo": "ghcr.io/example/app", "tag": _RELEASE_VERSION, "digest": _PINNED_DIGEST},
                "released_at": "",
                "git_sha": "",
                "schema_epoch": SCHEMA_EPOCH,
                "notes": "",
                "breaking": False,
                "state_migration": False,
            }
        ),
    )


def _cfg(tmp_path: Path, *, enabled: bool) -> AirlieConfig:
    """Build a config.  When enabled, pin the feed so checks don't touch the network."""
    base = copy.deepcopy(MINIMAL)
    # Pin observability state dir so nothing leaks into ~/.local/state/airlie (Finding 2).
    base["observability"] = {"state_dir": str(tmp_path / "state")}
    data: dict = {**base, "update": {"enabled": enabled}}
    if enabled:
        # A path-based pinned feed — _read_pinned_documents reads the local directory (Finding 3).
        pin_path = tmp_path / "pin_dir"
        _make_pin_dir(pin_path)
        data["update"]["feed"] = {
            "kind": "pinned",
            "path": str(pin_path),  # directory root, not a single file (Finding 3).
        }
    return AirlieConfig.model_validate(data)


class TestDisabledStartsNoTicker:
    """When update is off the ticker must not exist after lifespan startup."""

    def test_no_ticker_when_disabled(self, tmp_path: Path) -> None:
        """Disabled section → ``app.state.update_ticker`` is None after startup."""

        app = create_app(_cfg(tmp_path, enabled=False))
        with _TestClient(app) as tc:
            # ``__enter__`` ran lifespan startup.  With enabled=False the ticker block is skipped:
            assert tc.app.state.update_ticker is None

        # ``__exit__`` ran lifespan shutdown (a no-op when ticker is None).  No error.

    def test_disabled_routes_block_unauthenticated(self, tmp_path: Path) -> None:
        """Anonymous request to a disabled update section is 401 (auth), not 500.

        The routes are not mounted when disabled, but the auth middleware fires first
        and returns 401 regardless.  This is secure: no unauthorised information leak
        about route existence or non-existence.
        """
        app = create_app(_cfg(tmp_path, enabled=False))

        with _TestClient(app) as tc:
            r = tc.get("/v1/admin/update")
            assert r.status_code == 401

    def test_disabled_routes_block_unauthenticated_any(self, tmp_path: Path) -> None:
        """All four update endpoints get 401 (not mounted, not reached) when disabled."""
        app = create_app(_cfg(tmp_path, enabled=False))

        with _TestClient(app) as tc:
            for path in (
                "/v1/admin/update",
                "/v1/admin/update/history",
                "/v1/admin/update/desired",
                "/v1/admin/update/check",
            ):
                r = tc.get(path)
                assert r.status_code == 401, f"{path} returned {r.status_code}: {r.text}"


class TestEnabledLifecycle:
    """When update is on, the ticker starts (one check at boot), runs, and stops cleanly."""

    def test_ticker_present_when_enabled(self, tmp_path: Path) -> None:
        """Enabled section → ``app.state.update_ticker`` exists after startup and is running."""

        app = create_app(_cfg(tmp_path, enabled=True))
        with _TestClient(app) as tc:
            # ``__enter__`` ran lifespan startup.  With enabled=True, a ticker was created and
            # started (the first check fires immediately from ``_loop``):
            ticker = tc.app.state.update_ticker
            assert ticker is not None
            # (Finding 1: .running must be True; deleting the start() call would break this.)
            assert ticker.running is True

        # ``__exit__`` ran shutdown → ``ticker.stop()`` + ``store.close()``.

    def test_boot_check_records_the_pinned_release(self, tmp_path: Path) -> None:
        """The boot check parses the pinned feed and records an ok row with its version and digest."""
        store = UpdateStore(update_store_path(tmp_path / "state"))
        app = create_app(_cfg(tmp_path, enabled=True), update_store=store)

        with _TestClient(app):
            row = _wait_for_check(store)  # the boot check fires immediately from ``_loop``

        assert row is not None, "the boot check never reached the ledger"
        # A fixture that fails to parse lands here as ok=False with its reason, so this only
        # passes when the pinned read genuinely succeeds.
        assert row.ok is True, f"the pinned feed failed the check: {row.reason}"
        assert row.release is not None and row.pointer is not None
        assert row.release.version == _RELEASE_VERSION
        assert row.pointer.digest == _PINNED_DIGEST

    def test_ticker_stopped_after_shutdown(self, tmp_path: Path) -> None:
        """After ``TestClient.__exit__``, the ticker is stopped."""
        app = create_app(_cfg(tmp_path, enabled=True))

        # Use ASGI directly: enter the lifespan, verify state, exit (runs shutdown).
        with _TestClient(app) as tc:
            # Inside the context, ticker is started (one check already fired).
            assert tc.app.state.update_ticker is not None

        # (Finding 1: prove the ticker's running flag is False post-shutdown.)
        assert tc.app.state.update_ticker.running is False
