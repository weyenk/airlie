"""Mockups: the directions contract, the page check, the files and the look (D4, D13, D14 of the specs phase,
P4 of the planning line, 2026-10-25)."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.prompts import text
from airlie.story.mockups import (
    FORBIDDEN,
    OPTION_FILE,
    Direction,
    check_page,
    mockup_dir,
    mockup_look,
    option_name,
    page_from_reply,
    parse_directions,
    pick_from_comment,
)
from airlie.story.model import StoryError

CLEAN = (
    "<!doctype html><html><head><style>:root{--bg:#09090b}body{background:var(--bg);color:#fff}"
    ".logo{background:url(data:image/png;base64,iVBOR//8AAAANSUhEUg//==)}</style></head>"
    "<body><h1>Board</h1><p>Move a card with a key. Read more at the docs.</p></body></html>"
)


def direction(n: int, **over: str) -> dict[str, str]:
    return {
        "name": f"Direction {n}",
        "idea": f"Idea {n}",
        "screen": f"Screen {n}, top to bottom",
        "differs": f"It differs in {n}",
        **over,
    }


def test_three_directions_parse_and_fewer_or_alike_ones_are_refused() -> None:
    ok = parse_directions({"type": "directions", "directions": [direction(n) for n in (1, 2, 3)]}, count=3)
    assert [d.name for d in ok] == ["Direction 1", "Direction 2", "Direction 3"]
    assert ok[0] == Direction("Direction 1", "Idea 1", "Screen 1, top to bottom", "It differs in 1")
    for obj, why in [
        (None, "no JSON"),
        ({"type": "plan"}, "directions"),
        ({"type": "directions", "directions": [direction(1), direction(2)]}, "exactly 3 directions, not 2"),
        (
            {"type": "directions", "directions": [direction(n) for n in range(4)]},
            "exactly 3 directions, not 4",
        ),
        ({"type": "directions"}, "exactly 3 directions, not 0"),
        (
            {"type": "directions", "directions": [direction(1), direction(2), direction(3, differs=" ")]},
            "direction 3 needs a non-empty 'differs'",
        ),
        (
            {
                "type": "directions",
                "directions": [direction(1), direction(2), direction(3, name="direction  1")],
            },
            "share a name",
        ),
        (
            {"type": "directions", "directions": [direction(1), direction(2), "three"]},
            "direction 3 must be an object",
        ),
    ]:
        with pytest.raises(StoryError, match=why):
            parse_directions(obj, count=3)


SAMPLES = {
    FORBIDDEN[0]: '<script>alert("x")</script>',
    FORBIDDEN[1]: '<link rel="stylesheet" href="style.css">',
    FORBIDDEN[2]: "<style>@import url(style.css);</style>",
    FORBIDDEN[3]: '<img src="https://evil.example/pixel.png">',
    FORBIDDEN[4]: '<iframe src="about:blank"></iframe>',
    FORBIDDEN[5]: '<object data="x.swf"></object>',
    FORBIDDEN[6]: '<embed src="x.swf">',
    FORBIDDEN[7]: '<base href="https://evil.example/">',
    FORBIDDEN[8]: '<meta http-equiv="refresh" content="0;url=x">',
    FORBIDDEN[9]: '<a href="javascript:alert(1)">go</a>',
    FORBIDDEN[10]: '<button onclick="steal()">go</button>',
}


@pytest.mark.parametrize("pattern", FORBIDDEN)
def test_a_page_with_a_script_or_an_outside_url_is_never_kept(pattern: str) -> None:
    found = check_page(CLEAN.replace("</body>", SAMPLES[pattern] + "</body>"))
    assert found and all("a mockup holds no script" in f for f in found)
    assert check_page(CLEAN) == []  # base64 holds "//" and a data: image is allowed


@pytest.mark.parametrize(
    "snippet",
    [
        '<img src="//cdn.example.com/a.png">',
        "<style>.a{background:url(//cdn.example.com/a.png)}</style>",
        "<style>.a{background:url('http://x.test/a.png')}</style>",
        "<p>see HTTPS://EXAMPLE.COM/x</p>",
        "<SCRIPT SRC=x></SCRIPT>",
    ],
)
def test_outside_urls_are_found_in_every_spelling(snippet: str) -> None:
    assert check_page("<html><body>" + snippet + "</body></html>")


def test_the_check_quotes_what_it_found() -> None:
    [finding] = check_page("<html><body><a href='https://evil.example/x'>x</a></body></html>")
    assert "https://evil.example/x" in finding


def test_a_page_is_read_from_the_last_html_fence_or_the_document_start() -> None:
    fenced = "First try:\n```html\n<html>old</html>\n```\nBetter:\n```html\n<html>new</html>\n```\n"
    assert page_from_reply(fenced) == "<html>new</html>"
    assert page_from_reply("Here you go\n<!DOCTYPE html><html>x</html>") == "<!DOCTYPE html><html>x</html>"
    assert page_from_reply("sorry, I cannot") == ""


def test_the_option_files_and_their_directory_are_only_what_the_route_may_serve(tmp_path: Path) -> None:
    assert mockup_dir(tmp_path, "AIR-40", "E1") == tmp_path / "initiatives" / "AIR-40" / "mockups" / "E1"
    for bad in [("../AIR-40", "E1"), ("AIR-40", "../E1"), ("AIR-40", "e1"), ("AIR-40", "E1/x")]:
        with pytest.raises(ValueError, match="not an initiative key"):
            mockup_dir(tmp_path, *bad)
    assert option_name(2) == "option-2.html" and OPTION_FILE.fullmatch(option_name(9))
    for n in (0, 10):
        with pytest.raises(ValueError, match="1 to 9"):
            option_name(n)
    for odd in ("option-0.html", "option-10.html", "../option-1.html", "option-1.html/x", "index.html"):
        assert OPTION_FILE.fullmatch(odd) is None


def test_the_look_is_the_repositorys_file_when_it_has_one_else_the_hub_text(tmp_path: Path) -> None:
    assert mockup_look(None) == text("specs-mockup-look")
    assert "--primary #34d399" in mockup_look(tmp_path)  # no file: airlie's tokens
    (tmp_path / "docs" / "design").mkdir(parents=True)
    (tmp_path / "docs" / "design" / "look.md").write_text("Light, serif, one blue accent.\n")
    assert mockup_look(tmp_path) == "Light, serif, one blue accent."
    (tmp_path / "docs" / "design" / "look.md").write_text("   \n")
    assert "--primary" in mockup_look(tmp_path)  # an empty file is no look


@pytest.mark.parametrize(
    ("comment", "pick"),
    [
        ("option 2", 2),
        ("Option 2", 2),
        ("I like OPTION #3 best", 3),
        ("#2 please", 2),
        ("go with option 9.", 9),
        ("option 10", None),
        ("option 0", None),
        ("the second one", None),
        ("what is the option for?", None),
        ("see #2 later", None),
    ],
)
def test_a_pick_is_read_from_a_comment(comment: str, pick: int | None) -> None:
    assert pick_from_comment(comment) == pick
