"""Web tools: web_fetch with address/scheme/content-type/size guardrails.

The network is reached only through two injectable seams, so tests never touch a socket:
a `WebClient` (one request, redirects disabled) and a `Resolver` (hostname -> addresses).
Every address a name resolves to is checked with `check_address` before any request, and
again for every redirect target, so a public name that points at loopback, tailnet,
link-local or private space is refused before it is fetched."""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from airlie.tools.protocol import ToolContext, ToolResult

UNTRUSTED_MARKER = "[untrusted web text]"
MAX_REDIRECT_HOPS = 5
TAILNET = ipaddress.ip_network("100.64.0.0/10")


def check_address(ip: str) -> str | None:
    """Refuse an address that is not public internet, or None if it is.

    Order matters: 100.64.0.0/10 (tailnet) is also private, so it must be checked first."""
    a = ipaddress.ip_address(ip)
    if a.is_loopback:
        return "refused: loopback address"
    if a in TAILNET:
        return "refused: tailnet address"
    if a.is_link_local:
        return "refused: link-local address"
    if a.is_private:
        return "refused: private address"
    return None


class WebResponse(Protocol):
    status: int
    headers: dict[str, str]
    body: bytes
    final_url: str


class WebClient(Protocol):
    def request(self, url: str, timeout_s: float) -> WebResponse: ...


class Resolver(Protocol):
    def resolve(self, host: str) -> list[str]: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class UrllibClient:
    """The real WebClient: stdlib urllib with redirects disabled, so every hop is checked."""

    def request(self, url: str, timeout_s: float) -> WebResponse:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        req = urllib.request.Request(url, headers={"User-Agent": "airlie-web-fetch"})
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                return _UrllibResponse(
                    status=resp.status,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=resp.read(),
                    final_url=resp.url,
                )
        except urllib.error.HTTPError as e:
            return _UrllibResponse(
                status=e.code,
                headers={k.lower(): v for k, v in e.headers.items()},
                body=e.read(),
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
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return ToolResult(False, "refused: only http and https")
        if not parts.hostname:
            return ToolResult(False, "error: no host in url")
        if refused := self._check_host(parts.hostname):
            return ToolResult(False, refused)

        current = url
        for _ in range(MAX_REDIRECT_HOPS + 1):
            resp = self._client.request(current, self._timeout_s)
            if 300 <= resp.status < 400:
                location = resp.headers.get("location")
                if not location:
                    return ToolResult(False, "error: redirect without a location")
                target = urlsplit(location)
                if target.scheme not in ("http", "https"):
                    return ToolResult(False, "refused: only http and https")
                if not target.hostname:
                    return ToolResult(False, "error: no host in redirect target")
                if refused := self._check_host(target.hostname):
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
        out = f"{UNTRUSTED_MARKER}\n{text}"
        if len(out) > ctx.output_cap_chars:
            out = out[: ctx.output_cap_chars] + f"\n… [truncated {len(out) - ctx.output_cap_chars} chars]"
        return ToolResult(True, out)

    def _check_host(self, host: str) -> str | None:
        try:
            addresses = self._resolver.resolve(host)
        except Exception:
            return "error: could not resolve host"
        if not addresses:
            return "error: could not resolve host"
        for ip in addresses:
            if refused := check_address(ip):
                return refused
        return None
