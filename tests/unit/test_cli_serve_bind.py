"""`airlie serve` refuses a routable bind with no API key before the app starts."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from airlie import cli
from tests.support.serve import cfg

runner = CliRunner()


def _patch_keyless_serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[object, str]]:
    """No minted key, no uvicorn: the serve command still builds the app and hits the bind guard."""
    monkeypatch.setattr(cli, "_load", lambda config=None: cfg(tmp_path))
    monkeypatch.setattr("airlie.serve.bridge.session.ensure_serve_api_key_stores", lambda cfg: None)
    monkeypatch.setattr(
        "airlie.serve.bridge.session.resolve_serve_api_key",
        lambda cfg, explicit=None: None,
    )
    monkeypatch.setattr(
        "airlie.serve.bridge.session.accepted_serve_api_keys",
        lambda cfg, explicit=None: (),
    )
    started: list[tuple[object, str]] = []

    def fake_create_app(cfg: object, **kw: object) -> FastAPI:
        app = FastAPI()
        app.state.allow_bootstrap = kw.get("allow_bootstrap")
        return app

    def fake_run(app: object, *, host: str, port: int) -> None:
        started.append((app, host))

    monkeypatch.setattr("airlie.serve.create_app", fake_create_app)
    monkeypatch.setattr("airlie.serve.server.run", fake_run)
    return started


def test_serve_exits_when_host_is_routable_and_no_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _patch_keyless_serve(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["serve", "--host", "0.0.0.0", "--port", "8090"])
    assert result.exit_code == 1, result.output
    assert "0.0.0.0" in result.output
    assert started == []


def test_serve_starts_on_loopback_with_no_api_key_and_keeps_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _patch_keyless_serve(monkeypatch, tmp_path)
    result = runner.invoke(cli.app, ["serve", "--host", "127.0.0.1", "--port", "8090"])
    assert result.exit_code == 0, result.output
    assert len(started) == 1
    app, host = started[0]
    assert host == "127.0.0.1"
    assert app.state.allow_bootstrap is True


def test_serve_on_routable_host_with_a_key_disables_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _patch_keyless_serve(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "airlie.serve.bridge.session.resolve_serve_api_key",
        lambda cfg, explicit=None: "s3cret",
    )
    monkeypatch.setattr(
        "airlie.serve.bridge.session.accepted_serve_api_keys",
        lambda cfg, explicit=None: ("s3cret",),
    )
    result = runner.invoke(cli.app, ["serve", "--host", "0.0.0.0", "--port", "8090"])
    assert result.exit_code == 0, result.output
    assert len(started) == 1
    app, host = started[0]
    assert host == "0.0.0.0"
    assert app.state.allow_bootstrap is False
