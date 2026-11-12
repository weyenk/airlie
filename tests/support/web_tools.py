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
    pins: list[str | None] = field(default_factory=list)

    def request(self, url: str, timeout_s: float, pinned_ip: str | None = None) -> FakeResponse:
        self.requests.append(url)
        self.pins.append(pinned_ip)
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


class RebindingResolver:
    """A Resolver double that answers each host's first lookup with a public address and every later one with
    loopback: what a DNS-rebinding attacker serves."""

    def __init__(self, public: str = "93.184.216.34", later: str = "127.0.0.1") -> None:
        self._public = public
        self._later = later
        self.calls: list[str] = []

    def resolve(self, host: str) -> list[str]:
        self.calls.append(host)
        return [self._public] if self.calls.count(host) == 1 else [self._later]
