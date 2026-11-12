"""web_fetch tool: address/scheme/content-type/size guardrails, called with fake client and resolver."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from airlie.tools.web import UrllibClient, WebFetch, _PinnedHTTPSConnection, check_address
from tests.support.tools import ctx
from tests.support.web_tools import FakeClient, FakeResolver, FakeResponse, RebindingResolver


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


@pytest.mark.parametrize(
    ("ip", "refusal"),
    [
        ("::ffff:127.0.0.1", "refused: loopback address"),  # an IPv4 loopback written as IPv6
        ("::ffff:10.0.0.5", "refused: private address"),
        ("::ffff:100.64.0.9", "refused: tailnet address"),
        ("64:ff9b::7f00:1", "refused: loopback address"),  # NAT64 around 127.0.0.1
        ("2002:0a00:0005::1", "refused: private address"),  # 6to4 around 10.0.0.5
        ("fd00::1", "refused: private address"),  # ULA
        ("::1", "refused: loopback address"),
        ("0.0.0.0", "refused: private address"),
        ("::", "refused: private address"),
        ("224.0.0.1", "refused: non-public address"),  # multicast
        ("240.0.0.1", "refused: private address"),  # reserved
        ("ff02::1", "refused: non-public address"),
    ],
)
def test_check_address_refuses_every_form_of_a_non_public_address(ip: str, refusal: str) -> None:
    assert check_address(ip) == refusal


def test_check_address_allows_a_public_ipv6_address() -> None:
    assert check_address("2606:4700:4700::1111") is None


async def test_a_relative_redirect_is_followed_on_the_same_host(tmp_path: Path) -> None:
    client = FakeClient(
        responses=[
            FakeResponse(302, {"location": "/moved"}, b"", "http://public.example/start"),
            FakeResponse(200, {"content-type": "text/plain"}, b"arrived", "http://public.example/moved"),
        ]
    )
    resolver = FakeResolver({"public.example": ["93.184.216.34"]})
    r = await fetch(client, resolver).run({"url": "http://public.example/start"}, ctx(tmp_path))
    assert r.ok and "arrived" in r.output
    assert client.requests == ["http://public.example/start", "http://public.example/moved"]


async def test_a_redirect_to_a_host_that_resolves_to_loopback_is_refused(tmp_path: Path) -> None:
    client = FakeClient(
        responses=[FakeResponse(301, {"location": "https://rebind.example/"}, b"", "http://a.example/")]
    )
    resolver = FakeResolver(
        {"a.example": ["93.184.216.34"], "rebind.example": ["93.184.216.35", "127.0.0.1"]}
    )
    r = await fetch(client, resolver).run({"url": "http://a.example/"}, ctx(tmp_path))
    assert not r.ok and "refused: loopback address" in r.output
    assert client.requests == ["http://a.example/"]


# ---- DNS rebinding: the connection goes to the address that was vetted ----


async def test_the_request_is_pinned_to_the_vetted_address_not_resolved_again(tmp_path: Path) -> None:
    client = FakeClient(
        responses=[FakeResponse(200, {"content-type": "text/plain"}, b"ok", "http://a.example/")]
    )
    resolver = RebindingResolver()
    r = await WebFetch(client=client, resolver=resolver).run({"url": "http://a.example/"}, ctx(tmp_path))
    assert r.ok
    assert client.pins == ["93.184.216.34"]  # the client never gets to ask DNS again
    assert resolver.calls == ["a.example"]


async def test_every_redirect_hop_is_vetted_and_pinned_to_its_own_address(tmp_path: Path) -> None:
    client = FakeClient(
        responses=[
            FakeResponse(302, {"location": "http://b.example/x"}, b"", "http://a.example/"),
            FakeResponse(200, {"content-type": "text/plain"}, b"ok", "http://b.example/x"),
        ]
    )
    resolver = FakeResolver({"a.example": ["93.184.216.34"], "b.example": ["93.184.216.99"]})
    r = await fetch(client, resolver).run({"url": "http://a.example/"}, ctx(tmp_path))
    assert r.ok and client.pins == ["93.184.216.34", "93.184.216.99"]


async def test_a_redirect_to_a_loopback_literal_is_refused(tmp_path: Path) -> None:
    client = FakeClient(
        responses=[FakeResponse(302, {"location": "http://127.0.0.1/"}, b"", "http://a.example/")]
    )
    resolver = FakeResolver({"a.example": ["93.184.216.34"], "127.0.0.1": ["127.0.0.1"]})
    r = await fetch(client, resolver).run({"url": "http://a.example/"}, ctx(tmp_path))
    assert not r.ok and "refused: loopback address" in r.output
    assert client.requests == ["http://a.example/"]


async def test_a_rebinding_redirect_target_is_pinned_where_it_first_resolved(tmp_path: Path) -> None:
    client = FakeClient(
        responses=[
            FakeResponse(302, {"location": "http://a.example/next"}, b"", "http://a.example/"),
            FakeResponse(200, {"content-type": "text/plain"}, b"ok", "http://a.example/next"),
        ]
    )
    resolver = RebindingResolver()
    r = await WebFetch(client=client, resolver=resolver).run({"url": "http://a.example/"}, ctx(tmp_path))
    # second hop re-vets: the rebinder now answers loopback, so it is refused before any request
    assert not r.ok and "refused: loopback address" in r.output
    assert client.pins == ["93.184.216.34"]


@pytest.mark.parametrize(
    "ip",
    [
        "0.0.0.0",
        "::",
        "::1",
        "::ffff:127.0.0.1",
        "::ffff:7f00:1",
        "::ffff:0.0.0.0",
        "::7f00:1",
        "::a9fe:a9fe",
        "::127.0.0.1",
        "fec0::1",
        "fe80::1",
        "fc00::1",
        "64:ff9b::7f00:1",
    ],
)
def test_unspecified_loopback_and_mapped_forms_are_refused(ip: str) -> None:
    assert check_address(ip) is not None


def test_a_public_ipv6_address_is_allowed() -> None:
    assert check_address("2606:4700:4700::1111") is None


def _serve_once(seen: list[str]) -> tuple[HTTPServer, int]:
    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen.append(self.headers.get("Host", ""))
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"hi")

        def log_message(self, *a: object) -> None: ...

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    return srv, srv.server_address[1]


def test_the_real_client_connects_to_the_pinned_address_and_keeps_the_host_header() -> None:
    seen: list[str] = []
    srv, port = _serve_once(seen)
    try:
        # the name does not resolve anywhere: only the pin can reach the server
        resp = UrllibClient().request(f"http://pinned.invalid:{port}/", 5.0, "127.0.0.1")
    finally:
        srv.server_close()
    assert resp.status == 200 and resp.body == b"hi"
    assert seen == [f"pinned.invalid:{port}"]


def test_the_pinned_tls_connection_keeps_the_name_for_sni(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class FakeCtx:
        def wrap_socket(self, sock: object, server_hostname: str | None = None) -> object:
            calls["sni"] = server_hostname
            return sock

    def fake_create(addr: tuple[str, int], timeout: object = None, *a: object, **k: object) -> object:
        calls["addr"] = addr
        return object()

    monkeypatch.setattr(socket, "create_connection", fake_create)
    conn = _PinnedHTTPSConnection("example.com", 443, pinned_ip="93.184.216.34", context=FakeCtx())  # type: ignore[arg-type]
    conn.connect()
    assert calls == {"addr": ("93.184.216.34", 443), "sni": "example.com"}


def test_an_environment_proxy_never_steers_the_pinned_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    srv, port = _serve_once(seen)
    monkeypatch.delenv("NO_PROXY", raising=False)  # the unit conftest sets it to "*"
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:9")
    try:
        resp = UrllibClient().request(f"http://pinned.invalid:{port}/", 5.0, "127.0.0.1")
    finally:
        srv.server_close()
    assert resp.status == 200 and seen == [f"pinned.invalid:{port}"]


async def test_the_blocking_request_runs_off_the_event_loop(tmp_path: Path) -> None:
    threads: list[int] = []

    class ThreadClient(FakeClient):
        def request(self, url: str, timeout_s: float, pinned_ip: str | None = None) -> FakeResponse:
            threads.append(threading.get_ident())
            return super().request(url, timeout_s, pinned_ip)

    client = ThreadClient(
        responses=[FakeResponse(200, {"content-type": "text/plain"}, b"ok", "http://a.example/")]
    )
    r = await fetch(client, FakeResolver({"a.example": ["93.184.216.34"]})).run(
        {"url": "http://a.example/"}, ctx(tmp_path)
    )
    assert r.ok and threads and threads[0] != threading.get_ident()
