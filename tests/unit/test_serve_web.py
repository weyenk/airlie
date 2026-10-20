"""The web app's static shell: `GET /` and `/app/*` serve `src/airlie/serve/web/` to anyone; `/v1` keeps the
key guard."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from airlie.serve.app import WEB_DIR, create_app
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream, cfg


def test_root_serves_the_shell(tmp_path: Path) -> None:
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream())))
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>airlie" in r.text
    assert r.headers["cache-control"] == "no-cache"  # revalidated on every load, so a redeploy shows at once
    assert (WEB_DIR / "index.html").is_file()


def test_app_files_are_served_under_app(tmp_path: Path) -> None:
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream())))
    css = client.get("/app/app.css")
    assert css.status_code == 200 and css.headers["cache-control"] == "no-cache"
    r = client.get("/app/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert client.get("/app/missing.js").status_code == 404
    logo = client.get("/app/logo.png")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    assert client.get("/app/apple-touch-icon.png").status_code == 200


def test_static_shell_is_public_but_v1_needs_the_key(tmp_path: Path) -> None:
    app = create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream()), api_key="k")
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.get("/app/app.css").status_code == 200
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer k"}).status_code == 200


def test_the_sign_in_join_and_people_screens_are_served(tmp_path: Path) -> None:
    """Task 6 of the sign-in plan: the screens exist and are wired; a browser check covers their behaviour."""
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream())))
    signin = client.get("/app/screens/signin.js").text
    assert "Use this device" in signin and "Admin key" in signin and "whoami" in signin
    join = client.get("/app/screens/join.js")
    assert join.status_code == 200 and "/v1/auth/register/options" in join.text
    assert "Create passkey" in join.text
    admin = client.get("/app/screens/admin.js")
    assert admin.status_code == 200 and "/v1/auth/users" in admin.text and "/v1/auth/invites" in admin.text
    app_js = client.get("/app/app.js").text
    assert "#/join/" in app_js.replace("\\/", "/") or "join" in app_js
    assert "screens/join.js" in app_js and "screens/admin.js" in app_js
    api = client.get("/app/api.js").text
    assert 'credentials: "same-origin"' in api and "navigator.credentials" in api


def _board_client(tmp_path: Path) -> TestClient:
    """The web app served the way `task serve` serves it, so the tests read the real files."""
    return TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream())))


def _rule(css: str, selector: str) -> str:
    """The body of the first rule whose selector list contains `selector` (e.g. ".item .chip")."""
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in sel:
            return body
    raise AssertionError(f"no rule for {selector!r} in app.css")


def test_board_js_sets_the_column_count_from_statuses(tmp_path: Path) -> None:
    """AC6: board.js sets --board-columns to the length of STATUSES on the .columns element."""
    js = _board_client(tmp_path).get("/app/screens/board.js").text
    assert "--board-columns" in js
    assert "STATUSES.length" in js


def test_app_css_uses_the_column_count_not_a_hardcoded_five(tmp_path: Path) -> None:
    """AC6: app.css drives .columns from var(--board-columns) in the base and 1023px rules, with no repeat(5,)."""
    css = _board_client(tmp_path).get("/app/app.css").text
    assert "var(--board-columns)" in css
    assert css.count("var(--board-columns)") >= 2  # base rule and the max-width: 1023px rule
    assert "repeat(5," not in css


def test_board_columns_are_capped_and_the_grid_can_shrink(tmp_path: Path) -> None:
    """AC1/AC2: each column is capped at 360px, and .column/.item carry min-width: 0 so the grid can shrink."""
    css = _board_client(tmp_path).get("/app/app.css").text
    assert "minmax(180px, 360px)" in css  # the new cap on the base .columns rule
    assert "min-width: 0" in _rule(css, ".column")
    assert "min-width: 0" in _rule(css, ".item")


def test_board_card_label_chips_wrap_inside_their_column(tmp_path: Path) -> None:
    """AC5: a card's label chip wraps (the global .chip is white-space: nowrap, so the scoped rule must override)."""
    css = _board_client(tmp_path).get("/app/app.css").text
    body = _rule(css, ".item .chip")
    assert "white-space: normal" in body
    assert "overflow-wrap: anywhere" in body


def test_board_columns_use_the_count_in_narrow_viewports(tmp_path: Path) -> None:
    """AC4: the 1023px .columns rule keeps its snap behaviour but takes its count from --board-columns."""
    css = _board_client(tmp_path).get("/app/app.css").text
    assert "scroll-snap-type: x mandatory" in css  # pre-existing phone behaviour, kept
    assert "scroll-snap-align: start" in css  # pre-existing, kept
    assert "repeat(5," not in css  # the new behaviour: the count, in both rules
    assert css.count("var(--board-columns)") >= 2
