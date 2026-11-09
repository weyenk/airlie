"""Live tests need `task up` (the engines from airlie.yaml); skipped unless the engine under test answers.

`AIRLIE_LIVE_ENGINE` names that engine (default `main`; `reviewer` runs the reviewer's subset). The engine
is built by `airlie.cli._http_engine`, the same code the line runs, so the dialect, the exact model path an
mlx_vlm server knows its model by, and the per-role routing come from the config: a bare HTTPEngine would
send the reviewer's or small's path to main, which makes an mlx-vlm server unload its model."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

import airlie.cli
from airlie.config import load_config

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "airlie.yaml")


@pytest.fixture
def engine(cfg):
    """Function-scoped: the httpx client binds to the event loop, which pytest-asyncio recreates per test."""
    name = os.environ.get("AIRLIE_LIVE_ENGINE", "main")
    e = cfg.engines[name]
    health = f"{(e.base_url or '').removesuffix('/v1')}/health"
    headers = {"Authorization": f"Bearer {e.api_key}"} if e.api_key else {}
    try:
        if httpx.get(health, headers=headers, timeout=3).status_code != 200:
            pytest.skip(f"engine {name} not healthy; run `task up`")
    except httpx.HTTPError:
        pytest.skip(f"engine {name} not running; run `task up`")
    return airlie.cli._http_engine(cfg, name)
