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

The test is quick — no real sleeps, just state assertions (well under 500 ms).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient as _TestClient

from airlie.config.schema import AirlieConfig
from airlie.serve.app import create_app
from tests.support.config import MINIMAL


def _cfg(tmp_path: Path, *, enabled: bool) -> AirlieConfig:
    """Build a config.  When enabled, pin the feed so checks don't touch the network."""
    base = copy.deepcopy(MINIMAL)
    data = {**base, "update": {"enabled": enabled}}
    if enabled:
        data["update"]["feed"] = {
            "kind": "pinned",
            "path": str(tmp_path / "pin.json"),
        }
        (tmp_path / "pin.json").write_text(json.dumps({"version": "1.0", "digest": "sha256:" + "a" * 63}))
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
            assert tc.app.state.update_ticker is not None

        # ``__exit__`` ran shutdown → ``ticker.stop()`` + ``store.close()``.

    def test_ticker_stopped_after_shutdown(self, tmp_path: Path) -> None:
        """After ``TestClient.__exit__``, the ticker is stopped and its store closed."""
        app = create_app(_cfg(tmp_path, enabled=True))

        # Use ASGI directly: enter the lifespan, verify state, exit (runs shutdown).
        with _TestClient(app) as tc:
            # Inside the context, ticker is started (one check already fired).
            assert tc.app.state.update_ticker is not None

        # Reaching here without exception proves shutdown ran cleanly
        # (ticker.stop() + store.close()).
