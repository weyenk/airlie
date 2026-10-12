"""Live tests need `task up` (engine on 127.0.0.1:8080 from airlie.yaml); skipped unless it answers."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from airlie.config import load_config
from airlie.engine.http_engine import HTTPEngine

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "airlie.yaml")


@pytest.fixture
def engine(cfg):
    """Function-scoped: the httpx client binds to the event loop, which pytest-asyncio recreates per test."""
    e = cfg.engines["main"]
    try:
        if httpx.get(f"{e.base_url.removesuffix('/v1')}/health", timeout=3).status_code != 200:
            pytest.skip("engine not healthy; run `task up`")
    except httpx.HTTPError:
        pytest.skip("engine not running; run `task up`")
    return HTTPEngine(
        e.base_url or "",
        served_name_for_role=lambda role: e.served_name(cfg.roles[role].model),
        role_spec_for=lambda role: cfg.roles[role],
        api_key=e.api_key,
    )
