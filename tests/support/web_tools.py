"""Shared helpers extracted from tests/unit/test_web_tools.py for tests.support.web_tools."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


@dataclass
class FakeClient:
    """A WebClient double: returns queued responses and records every request it is asked to make."""

    responses: list[FakeResponse] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)

    def request(self, url: str, timeout_s: float) -> FakeResponse:
        self.requests.append(url)
        if not self.responses:
            raise AssertionError(f"unexpected request: {url}")
        return self.responses.pop(0)


class FakeResolver:
    """A Resolver double: maps hostnames to fixed address lists."""

    def __init__(self, table: dict[str, list[str]] | None = None) -> None:
        self._table = table or {}

    def resolve(self, host: str) -> list[str]:
        if host not in self._table:
            raise AssertionError(f"unexpected resolve: {host}")
        return self._table[host]
