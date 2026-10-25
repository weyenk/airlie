"""Mockups: three directions, three pages, checked (D4, D13, D14 of the specs phase, P4 of the planning line,
2026-10-25). ux names directions that differ; design draws one self-contained HTML page per direction; a page
is checked before it is kept and the app serves it locked down. The line never executes a page: it writes
a file and serves a file.

The check is mechanical and strict (`check_page`): a page that holds a script, a link, an import, a frame, an
event handler or a URL that is not `data:` is never kept on disk, whatever else is right about it. The
serving side adds a Content-Security-Policy that forbids the same things again (serve/app.py): the check is
the line's guard, the header is the browser's."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airlie.prompts import text as hub_text
from airlie.story.model import ISSUE_KEY, NO_JSON, StoryError, _encodable

DIRECTIONS_CONTRACT = """End your reply with one ```json block holding exactly one object:
{"type": "directions", "directions": [{"name": "short name", "idea": "the idea in one sentence",
 "screen": "what is on the screen, top to bottom, and what a person can do",
 "differs": "how it differs from the other directions"}]}"""

FORBIDDEN = (
    r"<script\b",
    r"<link\b",
    r"@import\b",
    r"(?:\b(?:https?:)?|(?<=[\s\"'(=]))//[^\s\"')]+",
    r"<iframe\b",
    r"<object\b",
    r"<embed\b",
    r"<base\b",
    r"<meta\b[^>]*http-equiv",
    r"\bjavascript:",
    r"\bon[a-z]{3,}\s*=",
)
"""What a mockup page may not hold, as regular expressions (case-insensitive). The first seven are the plan's;
the rest close the other ways a page can run code or fetch: a <base>, a refresh, a javascript: URL, an event
handler attribute. A `//` that follows a space, a quote, a parenthesis or `=` is a protocol-relative URL."""

OPTION_FILE = re.compile(r"option-[1-9]\.html")
_EPIC_ID = re.compile(r"E[0-9]+", re.ASCII)
_FENCE = re.compile(r"```html[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_DOC_START = re.compile(r"<!doctype\s+html|<html\b", re.IGNORECASE)
_OPTION_WORD = re.compile(r"\boption\s*#?\s*([1-9])\b", re.IGNORECASE)
_DATA_URI = re.compile(r"data:[^\s\"')]*", re.IGNORECASE)
_HASH_PICK = re.compile(r"^\s*#([1-9])\b")
LOOK_FILE = Path("docs/design/look.md")
LOOK_CHARS = 6_000
MAX_FIELD_CHARS = 1_200


@dataclass(frozen=True)
class Direction:
    """One direction ux named: what is on the screen and how it differs from the others."""

    name: str
    idea: str
    screen: str
    differs: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "idea": self.idea, "screen": self.screen, "differs": self.differs}


def parse_directions(obj: dict[str, Any] | None, *, count: int) -> tuple[Direction, ...]:
    """ux's directions. StoryError: no JSON or another type; not exactly `count` directions; a part empty or
    not text; two names alike (case-folded) — three mockups that are one mockup three times."""
    if obj is None:
        raise StoryError(NO_JSON)
    if obj.get("type") != "directions":
        raise StoryError('the reply needs "type": "directions"')
    raw = obj.get("directions")
    if not isinstance(raw, list) or len(raw) != count:
        got = len(raw) if isinstance(raw, list) else 0
        raise StoryError(f"name exactly {count} directions, not {got}")
    found: list[Direction] = []
    for n, d in enumerate(raw, 1):
        if not isinstance(d, dict):
            raise StoryError(f"direction {n} must be an object with a name, an idea, a screen and a differs")
        parts: list[str] = []
        for part in ("name", "idea", "screen", "differs"):
            value = d.get(part)
            if not isinstance(value, str) or not value.strip():
                raise StoryError(f"direction {n} needs a non-empty '{part}'")
            if len(value) > MAX_FIELD_CHARS:
                raise StoryError(f"direction {n}'s '{part}' is {len(value)} characters; keep it shorter")
            parts.append(_encodable(" ".join(value.split()) if part == "name" else value.strip(), part))
        found.append(Direction(*parts))
    names = [d.name.casefold() for d in found]
    if len(set(names)) != len(names):
        raise StoryError("two directions share a name; they must differ in flow or layout, not only in label")
    return tuple(found)


def check_page(html: str) -> list[str]:
    """One finding per forbidden match in `html`, with the matched text; empty when the page is clean."""
    found: list[str] = []
    html = _DATA_URI.sub("data:", html)  # base64 holds "//" and an image is the one thing a page may embed
    for pattern in FORBIDDEN:
        for m in re.finditer(pattern, html, re.IGNORECASE):
            shown = " ".join(m.group(0).split())[:80]
            found.append(
                f"the page holds {shown!r}; a mockup holds no script, link, import, frame, handler or "
                "outside URL"
            )
    return found


def page_from_reply(text: str) -> str:
    """The page in design's reply: the body of the last ```html fence, else the text from the first
    <!doctype or <html; "" when there is neither."""
    fences = _FENCE.findall(text)
    if fences:
        return str(fences[-1]).strip()
    m = _DOC_START.search(text)
    return text[m.start() :].strip() if m else ""


def mockup_dir(state_dir: Path, initiative_key: str, epic_id: str) -> Path:
    """initiatives/<key>/mockups/<epic id>: where an epic's option pages are written and served from. A key or
    an id that is not one (the route's path parts) is a ValueError, so no path is ever built from one."""
    if not ISSUE_KEY.fullmatch(initiative_key) or not _EPIC_ID.fullmatch(epic_id):
        raise ValueError(f"not an initiative key and an epic id: {initiative_key!r}, {epic_id!r}")
    return state_dir / "initiatives" / initiative_key / "mockups" / epic_id


def option_name(n: int) -> str:
    if not 1 <= n <= 9:
        raise ValueError(f"an option is 1 to 9, not {n}")
    return f"option-{n}.html"


def mockup_look(repo: Path | None) -> str:
    """The look design draws in: the repository's own `docs/design/look.md` when it has one (its first 6,000
    characters), else airlie's tokens (the `specs-mockup-look` hub text)."""
    if repo is not None:
        try:
            own = (repo / LOOK_FILE).read_text(errors="replace").strip()
        except OSError:
            own = ""
        if own:
            return own[:LOOK_CHARS]
    return hub_text("specs-mockup-look")


def pick_from_comment(text: str) -> int | None:
    """The option a person's comment picks: "option 2", "Option 2", "#2 please": the first digit 1 to 9 after
    the word option, or after a leading #. None when it picks nothing."""
    m = _OPTION_WORD.search(text) or _HASH_PICK.match(text)
    return int(m.group(1)) if m else None
