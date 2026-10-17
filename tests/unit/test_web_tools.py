"""web_fetch tool: address/scheme/content-type/size guardrails, called with fake client and resolver."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from airlie.tools.web import WebFetch, check_address
from tests.unit.test_tools import ctx


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


def fetch(client: FakeClient, resolver: FakeResolver, timeout_s: float = 5.0) -> WebFetch:
    return WebFetch(client=client, resolver=resolver, timeout_s=timeout_s)


# ---- AC3: loopback address is refused before any request ----


async def test_loopback_address_is_refused_without_a_request(tmp_path: Path) -> None:
    client = FakeClient()
    resolver = FakeResolver({"127.0.0.1": ["127.0.0.1"]})
    r = await fetch(client, resolver, "http://127.0.0.1:8080/x").run(
        {"url": "http://127.0.0.1:8080/x"}, ctx(tmp_path)
    )
    assert not r.ok and "refused: loopback address" in r.output
    assert client.requests == []


# ---- AC4: a name resolving to a private address is refused, no request to that IP ----


async def test_private_address_is_refused_without_a_request(tmp_path: Path) -> None:
    client = FakeClient()
    resolver = FakeResolver({"internal.example": ["10.0.0.5"]})
    r = await fetch(client, resolver, "http://internal.example/page").run(
        {"url": "http://internal.example/page"}, ctx(tmp_path)
    )
    assert not r.ok and "refused: private address" in r.output
    assert client.requests == []


# ---- AC5: a redirect to a tailnet address is refused without fetching the target ----


async def test_redirect_to_tailnet_address_is_refused_without_fetching_it(tmp_path: Path) -> None:
    client = FakeClient(
        responses=[
            FakeResponse(
                302,
                {"location": "http://100.64.0.9/"},
                b"",
                "http://public.example/start",
            )
        ]
    )
    resolver = FakeResolver({"public.example": ["93.184.216.34"], "100.64.0.9": ["100.64.0.9"]})
    r = await fetch(client, resolver, "http://public.example/start").run(
        {"url": "http://public.example/start"}, ctx(tmp_path)
    )
    assert not r.ok and "refused: tailnet address" in r.output
    assert client.requests == ["http://public.example/start"]  # the target was never fetched


# ---- AC6: a non-http(s) scheme is refused without a request ----


async def test_non_http_scheme_is_refused_without_a_request(tmp_path: Path) -> None:
    client = FakeClient()
    resolver = FakeResolver()
    r = await fetch(client, resolver, "ftp://files.example/x").run(
        {"url": "ftp://files.example/x"}, ctx(tmp_path)
    )
    assert not r.ok and "refused: only http and https" in r.output
    assert client.requests == []


# ---- AC7: a large text body is marked untrusted and truncated to the cap ----


async def test_large_text_body_is_marked_untrusted_and_truncated_to_the_cap(tmp_path: Path) -> None:
    body = ("x" * 500000).encode()
    client = FakeClient(
        responses=[FakeResponse(200, {"content-type": "text/html"}, body, "http://public.example/big")]
    )
    resolver = FakeResolver({"public.example": ["93.184.216.34"]})
    r = await fetch(client, resolver).run({"url": "http://public.example/big"}, ctx(tmp_path, cap=2000))
    assert r.ok
    assert "[untrusted web text]" in r.output
    assert len(r.output) <= 2000 + 80  # the cap plus the truncation note
    assert "truncated" in r.output


# ---- AC8: a non-text content type is refused with no body ----


async def test_non_text_content_type_is_refused_with_no_body(tmp_path: Path) -> None:
    client = FakeClient(
        responses=[FakeResponse(200, {"content-type": "image/png"}, b"\x89PNG", "http://public.example/img")]
    )
    resolver = FakeResolver({"public.example": ["93.184.216.34"]})
    r = await fetch(client, resolver, "http://public.example/img").run(
        {"url": "http://public.example/img"}, ctx(tmp_path)
    )
    assert not r.ok and "refused: not readable text" in r.output
    assert r.output == "refused: not readable text"  # no body leaks through


# ---- check_address: the order of the checks matters ----


def test_check_address_reports_tailnet_before_private() -> None:
    assert check_address("100.64.0.9") == "refused: tailnet address"
    assert check_address("10.0.0.5") == "refused: private address"
    assert check_address("127.0.0.1") == "refused: loopback address"
    assert check_address("169.254.10.1") == "refused: link-local address"
    assert check_address("93.184.216.34") is None
