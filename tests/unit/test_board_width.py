"""The board's six columns fill the page width in one row. Layer 1 of 3: `h()` applies `--` style keys
through `el.style.setProperty`, so a custom property actually lands on the element. Today a literal 5 in
the grid template makes the sixth column wrap, and the earlier fix regressed to one stacked column because
the custom property was set through `Object.assign`, which browsers ignore. This layer fixes the mechanism;
layers 02/03 (board.js, app.css) build on it. The served `/app/dom.js` is what a browser receives, so the
test reads it back over the wire and checks the style branch routes `--` keys through `setProperty` while
keeping `Object.assign` for normal keys (pattern from tests/unit/test_serve_web.py)."""

from __future__ import annotations

import re
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


def _balanced(text: str, open_idx: int) -> str:
    """The balanced block opened at `open_idx` ('{' or '('), honouring string literals so a quote or
    paren inside one does not end the block early. Returns from `open_idx` to its matching close."""
    opener = text[open_idx]
    closer = {"{": "}", "(": ")"}[opener]
    depth = 0
    in_str: str | None = None
    for i in range(open_idx, len(text)):
        c = text[i]
        if in_str is not None:
            if c == "\\":  # a backslash escapes the next char, even inside a template literal
                i += 1
            elif c == in_str:
                in_str = None
            continue
        if c in "\"'`":
            in_str = c
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return text[open_idx : i + 1]
    raise AssertionError(f"unbalanced {opener!r} at index {open_idx}")


def _columns_call(text: str) -> str:
    """The `h(...)` call that creates the `.columns` element — the one whose options carry
    `class: "columns"` (plural; the per-status sections use the singular `"column"`)."""
    anchor = text.find('class: "columns"')
    assert anchor != -1, 'the .columns element (h("div", { class: "columns" })) is missing'
    start = text.rfind("h(", 0, anchor)
    assert start != -1, "could not find the h( call that creates the .columns element"
    return _balanced(text, start + 1)


def test_board_columns_element_carries_board_columns_derived_from_statuses(tmp_path: Path) -> None:
    """AC4: the served board.js sets --board-columns on the .columns element, with a value derived from
    STATUSES.length (= 6), not a hard-coded 6. The property must be tied to the columns element itself,
    not merely present somewhere in the file (the PR 108 trap: a name-present test passes on the broken
    build). It may ride the dom.js h() style branch or a direct columns.style.setProperty call."""
    client = TestClient(create_app(cfg(tmp_path), transport=httpx.MockTransport(FakeUpstream())))
    board = client.get("/app/screens/board.js")
    assert board.status_code == 200
    text = board.text

    # The value must be derived from STATUSES.length, never a hard-coded 6: the expression assigned to
    # --board-columns references STATUSES.length (a literal "6" would not).
    assert "STATUSES.length" in text, "--board-columns must be derived from STATUSES.length, not a literal 6"

    # And it must land on the .columns element. Two shapes are acceptable:
    #   (a) the h() options of the .columns element carry style { "--board-columns": ... } (dom.js branch), or
    #   (b) a direct columns.style.setProperty('--board-columns', ...) on that same element.
    call = _columns_call(text)
    in_h_options = '"--board-columns"' in call or "'--board-columns'" in call
    direct_setter = (
        re.search(r"\bcolumns\.style\.setProperty\(\s*['\"]--board-columns['\"]", text) is not None
    )
    assert in_h_options or direct_setter, (
        "--board-columns must be set on the .columns element (via its h() style or a direct "
        "columns.style.setProperty), not on some other element"
    )

    # Whichever shape it takes, the value expression for --board-columns is derived from STATUSES.length.
    if in_h_options:
        value = re.search(r"['\"]--board-columns['\"]\s*:\s*([^,}]+)", call)
    else:
        value = re.search(r"columns\.style\.setProperty\(\s*['\"]--board-columns['\"]\s*,\s*([^)]+)", text)
    assert value is not None, "could not read the --board-columns value expression"
    assert "STATUSES.length" in value.group(1), (
        f"--board-columns value {value.group(1)!r} is not derived from STATUSES.length"
    )
