"""Starlette's TestClient as the machine `task serve` runs on: requests come from loopback, where a fresh
install (no users, no API key) is let in as the bootstrap admin (airlie.auth.principal). Tests that need a
remote caller pass `client=("10.0.0.5", 1)` themselves."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient as _TestClient

LOOPBACK = ("127.0.0.1", 50000)


class TestClient(_TestClient):
    def __init__(self, app: Any, **kw: Any) -> None:
        kw.setdefault("client", LOOPBACK)
        super().__init__(app, **kw)
