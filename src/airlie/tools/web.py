"""Web tools: web_fetch with address/scheme/content-type/size guardrails.

The network is reached only through two injectable seams, so tests never touch a socket:
a `WebClient` (one request, redirects disabled) and a `Resolver` (hostname -> addresses).
Every address a name resolves to is checked with `check_address` before any request, and
again for every redirect target, so a public name that points at loopback, tailnet,
link-local or private space is refused before it is fetched."""

from __future__ import annotations

import functools
import http.client
import ipaddress
import json
import socket
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import urlencode, urljoin, urlsplit

from airlie.tools.protocol import ToolContext, ToolResult

UNTRUSTED_MARKER = "[untrusted web text]"
MAX_REDIRECT_HOPS = 5
TAILNET = ipaddress.ip_network("100.64.0.0/10")
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _embedded_v4(a: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 one carries: `::ffff:a.b.c.d`, NAT64 `64:ff9b::/96` or 6to4 `2002::/16`.
    A gateway turns these back into IPv4, so they are held to the same rules."""
    if a.ipv4_mapped is not None:
        return a.ipv4_mapped
    if a in NAT64:
        return ipaddress.IPv4Address(int(a) & 0xFFFFFFFF)
    if a.sixtofour is not None:
        return a.sixtofour
    return None


def check_address(ip: str) -> str | None:
    """Refuse an address that is not public internet, or None if it is.

    Order matters: 100.64.0.0/10 (tailnet) is also private, so it must be checked first."""
    a = ipaddress.ip_address(ip)
    if isinstance(a, ipaddress.IPv6Address) and (inner := _embedded_v4(a)) is not None:
        a = inner
    if a.is_loopback:
        return "refused: loopback address"
    if a in TAILNET:
        return "refused: tailnet address"
    if a.is_link_local:
        return "refused: link-local address"
    if a.is_private:
        return "refused: private address"
    if a.is_multicast or not a.is_global:  # multicast, and the rest of what is not the public internet
        return "refused: non-public address"
    return None


HTML_TYPES = ("text/html", "application/xhtml+xml")
MAX_BODY_BYTES = 2_000_000
"""The most of a response web_fetch reads: a page is read to this, whatever its Content-Length says."""
_SKIPPED = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "canvas", "head"})
_BLOCKS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer", "main"}
    | {"blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "figcaption", "hr"}
)


class _Readable(HTMLParser):
    """HTML to the text a reader sees: no scripts, styles or markup; the title first; one line per block."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _SKIPPED and tag != "head":
            self._skip += 1
        if tag in _BLOCKS and not self._skip:
            self._parts.append("\n")
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit() and not self._skip:
            self._parts.append("# ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _SKIPPED and tag != "head" and self._skip:
            self._skip -= 1
        if tag in _BLOCKS and not self._skip:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._parts).splitlines()]
        body = "\n".join(line for line in lines if line)
        title = " ".join(self.title.split())
        return f"Title: {title}\n\n{body}" if title else body


def html_to_text(html: str) -> str:
    """The readable text of an HTML page: the title, then its visible text one block to a line, headings
    marked with `# `. Scripts, styles, frames and markup are dropped, and so are comments (the parser ignores
    them); nothing in the page is executed or fetched."""
    parser = _Readable()
    parser.feed(html)
    parser.close()
    return parser.text()


class WebResponse(Protocol):
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


class WebClient(Protocol):
    def request(self, url: str, timeout_s: float, pinned_ip: str | None = None) -> WebResponse:
        """One request. With `pinned_ip` the connection goes to exactly that address (the Host header and the
        TLS name stay the URL's), so the name is never resolved a second time."""
        ...


class Resolver(Protocol):
    def resolve(self, host: str) -> list[str]: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to a fixed address; the Host header is still the URL's."""

    def __init__(self, *args: Any, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to a fixed address and runs TLS under the URL's name (SNI and certificate check)."""

    def __init__(self, *args: Any, pinned_ip: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(functools.partial(_PinnedHTTPConnection, pinned_ip=self._pinned_ip), req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip: str) -> None:
        super().__init__()
        self._pinned_ip = pinned_ip

    def https_open(self, req: urllib.request.Request) -> Any:
        connection = functools.partial(_PinnedHTTPSConnection, pinned_ip=self._pinned_ip)
        return self.do_open(connection, req, context=self._context)  # type: ignore[attr-defined]


class UrllibClient:
    """The real WebClient: stdlib urllib with redirects disabled, so every hop is checked, and the connection
    pinned to the address the caller vetted."""

    def request(self, url: str, timeout_s: float, pinned_ip: str | None = None) -> WebResponse:
        handlers: list[Any] = [_NoRedirectHandler()]
        if pinned_ip is not None:
            handlers += [_PinnedHTTPHandler(pinned_ip), _PinnedHTTPSHandler(pinned_ip)]
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers={"User-Agent": "airlie-web-fetch"})
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                return _UrllibResponse(
                    status=resp.status,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=resp.read(MAX_BODY_BYTES),
                    final_url=resp.url,
                )
        except urllib.error.HTTPError as e:
            return _UrllibResponse(
                status=e.code,
                headers={k.lower(): v for k, v in e.headers.items()},
                body=e.read(MAX_BODY_BYTES),
                final_url=url,
            )


class _UrllibResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes, final_url: str) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.final_url = final_url


class SocketResolver:
    """The real Resolver: socket.getaddrinfo, one address per result."""

    def resolve(self, host: str) -> list[str]:
        return [str(r[4][0]) for r in socket.getaddrinfo(host, None)]


class SearchProvider(Protocol):
    """Tool-agnostic search seam: a query in, results (url/title/content dicts) out."""

    async def search(self, query: str) -> list[dict[str, str]]: ...


class SearXNGProvider:
    """The SearXNG backend of SearchProvider: GET {base_url}/search?q=<query>&format=json.

    The request goes through the injected WebClient, so tests never open a socket."""

    def __init__(self, base_url: str, client: WebClient, timeout_s: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_s = timeout_s

    async def search(self, query: str) -> list[dict[str, str]]:
        url = f"{self._base_url}/search?{urlencode({'q': query, 'format': 'json'})}"
        resp = self._client.request(url, self._timeout_s)
        if resp.status != 200:
            raise RuntimeError(f"searxng returned HTTP {resp.status}")
        data = json.loads(resp.body.decode("utf-8", errors="replace"))
        return [
            {
                "url": str(r.get("url") or ""),
                "title": str(r.get("title") or ""),
                "content": str(r.get("content") or ""),
            }
            for r in data.get("results", [])
        ]


class WebSearch:
    name = "web_search"
    description = (
        "Search the web through the configured SearXNG backend and return the results "
        "(url, title, snippet) as text, marked untrusted."
    )
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def __init__(self, provider: SearchProvider) -> None:
        self._provider = provider

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "")
        status = "ok"
        try:
            results = await self._provider.search(query)
        except Exception as e:  # noqa: BLE001 - a provider failure is an observation, not a crash
            status = "error"
            self._log(ctx, query, status)
            return ToolResult(False, f"search failed: {type(e).__name__}: {e}")
        self._log(ctx, query, status)
        out = f"{UNTRUSTED_MARKER}\n" + "\n".join(
            f"{r.get('title', '')}\n{r.get('url', '')}\n{r.get('content', '')}" for r in results
        )
        if len(out) > ctx.output_cap_chars:
            out = out[: ctx.output_cap_chars] + f"\n… [truncated {len(out) - ctx.output_cap_chars} chars]"
        return ToolResult(True, out)

    def _log(self, ctx: ToolContext, query: str, status: str) -> None:
        if ctx.run_dir is None:
            return
        line = json.dumps({"role": ctx.role, "query": query, "status": status})
        with (ctx.run_dir / "web_search.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class WebFetch:
    name = "web_fetch"
    description = (
        "Fetch a web page and return its text, marked untrusted. Refuses non-http(s) schemes, "
        "loopback/tailnet/link-local/private addresses, redirects into them, and non-text content."
    )
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    }

    def __init__(
        self,
        client: WebClient | None = None,
        resolver: Resolver | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._client = client or UrllibClient()
        self._resolver = resolver or SocketResolver()
        self._timeout_s = timeout_s

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url") or "")
        result = await self._fetch(url, ctx)
        self._log(ctx, url, "ok" if result.ok else result.output.splitlines()[0][:120])
        return result

    def _log(self, ctx: ToolContext, url: str, status: str) -> None:
        """One line per fetch in the run directory: role, URL, and "ok" or why it was refused or failed."""
        if ctx.run_dir is None:
            return
        line = json.dumps({"role": ctx.role, "url": url, "status": status})
        with (ctx.run_dir / "web_fetch.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def _fetch(self, url: str, ctx: ToolContext) -> ToolResult:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return ToolResult(False, "refused: only http and https")
        if not parts.hostname:
            return ToolResult(False, "error: no host in url")
        pinned, refused = self._vet_host(parts.hostname)
        if pinned is None:
            return ToolResult(False, refused)

        current = url
        for _ in range(MAX_REDIRECT_HOPS + 1):
            resp = self._client.request(current, self._timeout_s, pinned)
            if 300 <= resp.status < 400:
                location = resp.headers.get("location")
                if not location:
                    return ToolResult(False, "error: redirect without a location")
                location = urljoin(current, location)  # a Location may be relative to the page
                target = urlsplit(location)
                if target.scheme not in ("http", "https"):
                    return ToolResult(False, "refused: only http and https")
                if not target.hostname:
                    return ToolResult(False, "error: no host in redirect target")
                pinned, refused = self._vet_host(target.hostname)
                if pinned is None:
                    return ToolResult(False, refused)
                current = location
                continue
            break
        else:
            return ToolResult(False, "error: too many redirects")

        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if not (ctype.startswith("text/") or ctype == "application/json"):
            return ToolResult(False, "refused: not readable text")

        text = resp.body.decode("utf-8", errors="replace")
        if ctype in HTML_TYPES:
            text = html_to_text(text)
        out = f"{UNTRUSTED_MARKER}\n{text}"
        if len(out) > ctx.output_cap_chars:
            out = out[: ctx.output_cap_chars] + f"\n… [truncated {len(out) - ctx.output_cap_chars} chars]"
        return ToolResult(True, out)

    def _vet_host(self, host: str) -> tuple[str | None, str]:
        """Resolve `host` once and vet every address: (the address to connect to, "") or (None, why not).
        The caller connects to that address, so the name is never resolved again."""
        try:
            addresses = self._resolver.resolve(host)
        except Exception:
            return None, "error: could not resolve host"
        if not addresses:
            return None, "error: could not resolve host"
        for ip in addresses:
            if refused := check_address(ip):
                return None, refused
        return addresses[0], ""
