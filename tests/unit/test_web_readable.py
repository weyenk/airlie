"""web_fetch returns readable text (not markup) and logs every fetch; and never reads more than its cap."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.tools.protocol import ToolContext
from airlie.tools.web import MAX_BODY_BYTES, UNTRUSTED_MARKER, UrllibClient, WebFetch, html_to_text
from tests.unit.test_web_tools import FakeClient, FakeResolver, FakeResponse

PAGE = """<!doctype html><html lang=en><head><title> Release  notes </title>
<meta charset=utf-8><style>body { color: red }</style><script>var secret = "do not show";</script></head>
<body><nav>Home | About</nav>
<h1>Version 3.15</h1>
<p>Out now &amp; <b>stable</b>.<br>See the <a href="/docs">docs</a>.</p>
<ul><li>faster</li><li>smaller</li></ul>
<noscript>enable javascript</noscript><!-- a comment -->
<svg><text>hidden drawing</text></svg>
<h2>Fixes</h2><div>Many   fixes.</div>
</body></html>"""


def test_html_becomes_its_visible_text_with_the_title_first_and_headings_marked() -> None:
    text = html_to_text(PAGE)
    assert text.startswith("Title: Release notes\n\n")
    assert "# Version 3.15" in text and "# Fixes" in text
    assert "Out now & stable." in text and "See the docs." in text
    assert "faster\nsmaller" in text and "Many fixes." in text
    for gone in (
        "<",
        "var secret",
        "do not show",
        "color: red",
        "enable javascript",
        "a comment",
        "hidden drawing",
    ):
        assert gone not in text


def test_a_page_with_no_title_or_no_markup_still_reads() -> None:
    assert html_to_text("<p>just text</p>") == "just text"
    assert html_to_text("plain, no tags at all") == "plain, no tags at all"
    assert html_to_text("") == ""


def ctx_for(tmp_path: Path) -> ToolContext:
    run = tmp_path / "run"
    run.mkdir()
    return ToolContext(worktree=tmp_path, run_dir=run, role="research", output_cap_chars=4000)


def tool(client: FakeClient) -> WebFetch:
    table = {"example.org": ["93.184.216.34"], "127.0.0.1": ["127.0.0.1"]}
    return WebFetch(client=client, resolver=FakeResolver(table))


def page_response(body: str, ctype: str = "text/html; charset=utf-8") -> FakeResponse:
    return FakeResponse(200, {"content-type": ctype}, body.encode(), "https://example.org/")


async def test_an_html_page_comes_back_as_readable_text_marked_untrusted(tmp_path: Path) -> None:
    r = await tool(FakeClient([page_response(PAGE)])).run({"url": "https://example.org/"}, ctx_for(tmp_path))
    assert r.ok and r.output.startswith(f"{UNTRUSTED_MARKER}\nTitle: Release notes")
    assert "<h1>" not in r.output and "# Version 3.15" in r.output


async def test_plain_text_and_json_are_returned_as_they_are(tmp_path: Path) -> None:
    body = '{"a": "<b>kept</b>"}'
    r = await tool(FakeClient([page_response(body, "application/json")])).run(
        {"url": "https://example.org/"}, ctx_for(tmp_path)
    )
    assert r.ok and body in r.output


async def test_the_readable_text_is_what_the_size_cap_applies_to(tmp_path: Path) -> None:
    long_page = "<html><body>" + "<p>word " * 5000 + "</body></html>"  # 40k of markup, 5k words
    r = await tool(FakeClient([page_response(long_page)])).run(
        {"url": "https://example.org/"}, ctx_for(tmp_path)
    )
    assert r.ok and len(r.output) < 4200 and "truncated" in r.output


def logged(ctx: ToolContext) -> list[dict[str, str]]:
    assert ctx.run_dir is not None
    return [json.loads(x) for x in (ctx.run_dir / "web_fetch.jsonl").read_text().splitlines()]


async def test_every_fetch_is_logged_with_its_role_url_and_how_it_went(tmp_path: Path) -> None:
    c = ctx_for(tmp_path)
    ok = tool(FakeClient([page_response(PAGE)]))
    await ok.run({"url": "https://example.org/a"}, c)
    await ok.run({"url": "ftp://example.org/b"}, c)
    await ok.run({"url": "http://127.0.0.1:8090/c"}, c)
    assert [(x["role"], x["url"], x["status"]) for x in logged(c)] == [
        ("research", "https://example.org/a", "ok"),
        ("research", "ftp://example.org/b", "refused: only http and https"),
        ("research", "http://127.0.0.1:8090/c", "refused: loopback address"),
    ]


async def test_a_fetch_without_a_run_directory_logs_nothing_and_still_works(tmp_path: Path) -> None:
    r = await tool(FakeClient([page_response(PAGE)])).run(
        {"url": "https://example.org/"}, ToolContext(worktree=tmp_path)
    )
    assert r.ok and not list(tmp_path.glob("*.jsonl"))


def test_the_real_client_reads_no_more_than_the_body_cap(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    reads: list[int | None] = []

    class Resp:
        status = 200
        url = "https://example.org/"
        headers = {"Content-Type": "text/plain"}

        def read(self, n: int | None = None) -> bytes:
            reads.append(n)
            return b"x" * 10

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

    class Opener:
        def open(self, req: object, timeout: float) -> Resp:
            return Resp()

    monkeypatch.setattr("airlie.tools.web.urllib.request.build_opener", lambda *h: Opener())
    UrllibClient().request("https://example.org/", 5.0)
    assert reads == [MAX_BODY_BYTES]
