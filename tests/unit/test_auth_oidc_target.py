"""Where /callback may send a browser after a sign-in, and everything it must refuse.

/callback is public and its query is attacker-writable, so the post-login destination cannot be a
URL the caller supplies. An allowlist of the studio's own route prefixes is what closes that seam,
and the cases below are the ones a prefix check alone leaks: `//evil.net` and `/\\evil.net` are
protocol-relative to a browser, and `/%2F%2Fevil.net` decodes to the same thing after a redirect
hop.

Mirrors the reasoning already written down for `/next` (serve/app.py), which strips leading slashes
for the same reason. The list must stay in step with web/src/router.ts: a target outside it is a
silent round trip back to the studio root, so a test pins each route the app can name as `next`."""

from __future__ import annotations

from airlie.auth.oidc_flow import safe_target

ROOT = "/"


def test_the_studio_routes_are_all_allowed() -> None:
    """Every path web/src/router.ts can put in `?next=` survives, or a sign-in loses where the
    person was."""
    for path in (
        "/",
        "/chat",
        "/chat/abc123",
        "/workflows",
        "/workflows/story-1/node-2/job-3",
        "/workflows/runs/r1/n/j",
        "/workflows/build/my-flow",
        "/board",
        "/board/key",
        "/metrics",
        "/admin",
        "/update",
    ):
        assert safe_target(path, default=ROOT) == path, path


def test_a_query_and_fragment_survive_on_an_allowed_path() -> None:
    """The studio's own links carry them (/board?filter=open, /chat#m-3). Dropping them is a small
    breakage, but still a breakage of what the person clicked."""
    assert safe_target("/board?filter=open", default=ROOT) == "/board?filter=open"
    assert safe_target("/chat/s1#msg-9", default=ROOT) == "/chat/s1#msg-9"


def test_an_absolute_url_is_not_a_target() -> None:
    for target in (
        "https://evil.net/x",
        "http://evil.net",
        "HTTPS://EVIL.NET",
        "https://evil.net\\@airlie.acme.com/",
        "javascript:alert(1)",
        "data:text/html;base64,AAAA",
    ):
        assert safe_target(target, default=ROOT) == ROOT, target


def test_a_protocol_relative_path_is_not_a_target() -> None:
    """`//evil.net` is scheme-relative: a browser sends it off-site. A startswith("/") check alone
    accepts it."""
    for target in ("//evil.net", "///evil.net", "//evil.net/x"):
        assert safe_target(target, default=ROOT) == ROOT, target


def test_a_backslash_does_not_stay_inside_the_site() -> None:
    """/\\evil.net is treated as //evil.net by browsers. `/next` already guards this; the same care
    applies here."""
    for target in ("/\\evil.net", "/\\/evil.net", "/\\%2fevil.net"):
        assert safe_target(target, default=ROOT) == ROOT, target


def test_an_encoded_jump_is_not_a_target() -> None:
    """%2F%2F decodes to //, and an IdP or an intermediate may normalise it before the browser sees
    it."""
    for target in ("/%2f%2fevil.net", "/%2F%2Fevil.net", "/%09/evil.net", "/ /evil.net"):
        assert safe_target(target, default=ROOT) == ROOT, target


def test_a_control_or_whitespace_prefix_is_not_a_target() -> None:
    for target in ("/\t/chat", "\n/chat", "/\x00chat", " /chat"):
        assert safe_target(target, default=ROOT) == ROOT, target


def test_a_path_that_only_starts_like_a_route_is_refused() -> None:
    """/boardadmin is not /board. A bare prefix match would let an unrelated path through, and
    would also match shapes like `/board/../evil` on stacks that resolve first."""
    assert safe_target("/boardadmin", default=ROOT) == ROOT
    assert safe_target("/chatting", default=ROOT) == ROOT
    assert safe_target("/metricsss", default=ROOT) == ROOT


def test_a_traversal_shape_stays_inside_the_site_or_falls_back() -> None:
    """A dot segment must not escape the origin. Falling back is fine; resolving is not."""
    assert safe_target("/chat/../..", default=ROOT) == ROOT
    assert safe_target("/admin/../../etc/passwd", default=ROOT) == ROOT
    assert safe_target("/..%2f..%2f", default=ROOT) == ROOT


def test_an_empty_or_absent_target_falls_back_to_the_default() -> None:
    assert safe_target("", default=ROOT) == ROOT
    assert safe_target(None, default=ROOT) == ROOT
    assert safe_target("   ", default=ROOT) == ROOT
    assert safe_target("/board", default="/chat") == "/board"
    assert safe_target("/nope", default="/chat") == "/chat"  # a studio route is an honoured default
    assert safe_target("", default="/board?filter=open") == "/board?filter=open"


def test_a_default_outside_the_route_list_is_not_honoured() -> None:
    """The answer must always be on-site, so a default that is not one of the app's own routes
    cannot be the answer either. The call sites pass "/" or "/signin", both listed."""
    assert safe_target("/nope", default="/chatting") == ROOT
    assert safe_target("/nope", default="//evil.net") == ROOT
    assert safe_target("/nope", default="https://evil.net") == ROOT
    assert safe_target("/nope", default="") == ROOT
    assert safe_target("/nope", default="/signin") == "/signin"
    assert safe_target("/nope", default="/%2f%2fevil.net") == ROOT


def test_a_very_long_target_is_refused_rather_than_reflected() -> None:
    """Nothing legitimate is megabytes long, and a Location header that echoes it back is a
    reflection primitive."""
    assert safe_target("/chat/" + "a" * 5000, default=ROOT) == ROOT


def test_a_target_is_returned_unmodified_never_rebuilt() -> None:
    """Re-encoding a target invites a mismatch with what the studio asked for; the allowed value is
    the one the person was sent to before the redirect, byte for byte."""
    target = "/chat/sess-1?highlight=1&x=2#frag"
    assert safe_target(target, default=ROOT) == target
