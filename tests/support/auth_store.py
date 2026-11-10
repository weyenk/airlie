"""Shared helpers extracted from tests/unit/test_auth_store.py for tests.support.auth_store."""

from __future__ import annotations


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t
