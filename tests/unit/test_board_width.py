"""The board's six columns fill the page width in one row. Layer 1 of 3: `h()` applies `--` style keys
through `el.style.setProperty`, so a custom property actually lands on the element. Today a literal 5 in
the grid template makes the sixth column wrap, and the earlier fix regressed to one stacked column because
the custom property was set through `Object.assign`, which browsers ignore. This layer fixes the mechanism;
layers 02/03 (board.js, app.css) build on it. The served `/app/dom.js` is what a browser receives, so the
test reads it back over the wire and checks the style branch routes `--` keys through `setProperty` while
keeping `Object.assign` for normal keys (pattern from tests/unit/test_serve_web.py)."""

from __future__ import annotations

from pathlib import Path

import httpx

from airlie.serve.app import create_app
from tests.unit.serve_helpers import TestClient
from tests.unit.test_serve import FakeUpstream, cfg


def test_h_applies_custom_style_properties_via_set_property(tmp_path: Path) -> None:
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream())))
    dom = client.get("/app/dom.js")
    assert dom.status_code == 200
    text = dom.text
    # AC3: a style key that starts with '--' is applied with el.style.setProperty, not Object.assign
    # (which browsers ignore for custom properties — the PR 108 regression that stacked the board).
    assert "el.style.setProperty" in text
    # ...and a non-'--' style key keeps the existing Object.assign path, so ordinary styles are unchanged.
    assert "Object.assign(el.style" in text
