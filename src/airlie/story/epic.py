"""The epic contract (D1 of the plans phase, P3 of the planning line, 2026-10-24). An epic is a board item at
level `epic` whose description is a document in the spec's shape closed by one JSON block: the repositories it
touches, its criteria (one line each, with an id, at the epic's grain: Given/When/Then belongs on the story,
where the tests are), its constraints and links. `EpicPipeline` plans from that block; `task epic:add` writes
it from a Markdown spec.

The block is read as data. A repository named in it reaches git and gh only after `REPOSITORY` has checked its
spelling; a block that is flawed is a StoryError with a reason a person (or P4's writer) can act on, never a
guess. A card with no epic block at all has no epic (`epic_of` returns None): an approved epic imported from
Jira, say, is set aside with a note and never planned from prose."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from airlie.story.model import NO_JSON, StoryError, _encodable, _one_line, _strs

EPIC_CONTRACT = """The epic's card ends with one ```json block holding exactly one object:
{"type": "epic", "repositories": ["owner/name"], "criteria": [{"id": "EC1", "text": "one line, at the epic's grain"}],
 "constraints": ["one line each"], "links": ["https://… (a mockup, a design)"]}"""  # noqa: E501

REPOSITORY = re.compile(r"[A-Za-z0-9_][\w.-]*/[A-Za-z0-9_][\w.-]*", re.ASCII)
"""A GitHub "owner/name": no leading "-" or ".", so neither half is an option or a path step when it reaches
git, gh or a label."""
_CRITERIA_HEADING = re.compile(r"^##[ \t]+acceptance criteria[ \t]*$", re.IGNORECASE)
_CONSTRAINTS_HEADING = re.compile(r"^##[ \t]+constraints[ \t]*$", re.IGNORECASE)
_BULLET = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+(?P<text>\S.*)$")
_TITLE = re.compile(r"^#[ \t]+(?P<title>\S.*)$", re.MULTILINE)
_OPENER = "```json"


@dataclass(frozen=True)
class EpicCriterion:
    id: str
    text: str


@dataclass(frozen=True)
class EpicSpec:
    """What the plan is argued from: the repositories the epic touches (the first is read by the architect),
    its criteria, its constraints (copied into every story's brief) and links."""

    repositories: tuple[str, ...]
    criteria: tuple[EpicCriterion, ...]
    constraints: tuple[str, ...] = ()
    links: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "epic",
            "repositories": list(self.repositories),
            "criteria": [{"id": c.id, "text": c.text} for c in self.criteria],
            "constraints": list(self.constraints),
            "links": list(self.links),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EpicSpec:
        return cls(
            tuple(str(r) for r in d["repositories"]),
            tuple(EpicCriterion(str(c["id"]), str(c["text"])) for c in d["criteria"]),
            tuple(str(c) for c in d.get("constraints") or ()),
            tuple(str(c) for c in d.get("links") or ()),
        )


def parse_epic(obj: dict[str, Any] | None) -> EpicSpec:
    """The epic of a card's JSON block. StoryError: no JSON; a type other than "epic"; no repository, or one
    that is not "owner/name"; no criterion, one without an id or a text, or ids that repeat."""
    if obj is None:
        raise StoryError(NO_JSON)
    if obj.get("type") != "epic":
        raise StoryError('the epic block needs "type": "epic"')
    raw_repos = obj.get("repositories")
    if raw_repos is not None and not isinstance(raw_repos, list):
        raise StoryError("'repositories' must be a list of owner/name strings")
    repos = _strs(raw_repos, "'repositories'")
    if not repos:
        raise StoryError("an epic needs at least one repository (owner/name)")
    for repo in repos:
        if not REPOSITORY.fullmatch(repo) or ".." in repo.split("/"):
            raise StoryError(f"repository {repo!r} must be owner/name (letters, digits, '.', '-' and '_')")
    raw = obj.get("criteria")
    if raw is not None and not isinstance(raw, list):
        raise StoryError("'criteria' must be a list of {id, text} objects")
    if not raw:
        raise StoryError("an epic needs at least one criterion")
    criteria: list[EpicCriterion] = []
    for n, c in enumerate(raw, 1):
        if not isinstance(c, dict):
            raise StoryError(f"criterion {n} must be an object with an id and a text")
        cid, text = c.get("id"), c.get("text")
        if not isinstance(cid, str) or not cid.strip():
            raise StoryError(f"criterion {n} needs a non-empty 'id'")
        if not isinstance(text, str) or not text.strip():
            raise StoryError(f"criterion {cid} needs a non-empty 'text'")
        criteria.append(
            EpicCriterion(_one_line(cid.strip(), f"criterion {n} id", 20), _encodable(text.strip(), cid))
        )
    ids = [c.id for c in criteria]
    if len(set(ids)) != len(ids):
        raise StoryError(f"criterion ids repeat ({ids}); give each its own id")
    return EpicSpec(
        repos,
        tuple(criteria),
        _strs(obj.get("constraints"), "'constraints'"),
        _strs(obj.get("links"), "'links'"),
    )


def split_block(description: str) -> tuple[str, dict[str, Any] | None]:
    """The card's prose and the object of the ```json block that closes it ("" and None when nothing closes
    it that way). A block that is not the last thing in the card, or does not hold an object, is not one."""
    at = description.rfind(_OPENER)
    if at < 0:
        return description, None
    body = description[at + len(_OPENER) :]
    first, _, rest = body.partition("\n")
    if first.strip() or not rest.rstrip().endswith("```"):
        return description, None
    try:
        obj = json.loads(rest.rstrip().removesuffix("```"))
    except ValueError:
        return description, None
    return (description[:at], obj) if isinstance(obj, dict) else (description, None)


def epic_of(card_description: str) -> EpicSpec | None:
    """The epic of a card: its closing block parsed, or None when the card has no epic block. A block that
    says "epic" and is flawed is a StoryError: the card was meant to be an epic."""
    _, obj = split_block(card_description)
    if obj is None or obj.get("type") != "epic":
        return None
    return parse_epic(obj)


def _section(lines: list[str], heading: re.Pattern[str]) -> list[str]:
    """The bullets under the "## ..." heading `heading` matches, each on one line (an indented continuation is
    joined to its bullet), up to the next "## " heading."""
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            inside = bool(heading.match(line.rstrip()))
            continue
        if not inside:
            continue
        m = _BULLET.match(line.strip()) if not line.startswith((" ", "\t")) else None
        if m is not None:
            out.append(m.group("text").strip())
        elif out and line.strip() and line.startswith((" ", "\t")):
            out[-1] += " " + line.strip()
    return out


def epic_title(text: str, fallback: str) -> str:
    """The first "# heading" of a Markdown spec, else `fallback`."""
    m = _TITLE.search(text)
    return m.group("title").strip() if m else fallback


def epic_from_markdown(text: str, repo: str | None) -> tuple[str, EpicSpec]:
    """The card description and the spec of a Markdown epic. With a closing JSON block the block is the spec
    (`repo` fills in a block that names no repository); without one, the criteria are the bullets under
    "## Acceptance criteria" (EC1, EC2, …), the constraints those under "## Constraints", the repository
    `repo`, and the block is written at the end of the text. StoryError when the text has neither a block nor
    that section, `repo` is None and the text names no repository, or `repo` is not "owner/name"."""
    if repo is not None and (not REPOSITORY.fullmatch(repo) or ".." in repo.split("/")):
        raise StoryError(f"--repo must be owner/name, not {repo!r}")
    prose, obj = split_block(text)
    if obj is not None and obj.get("type") == "epic":
        if not obj.get("repositories"):
            if repo is None:
                raise StoryError("the epic block names no repository: pass --repo owner/name")
            obj = {**obj, "repositories": [repo]}
            spec = parse_epic(obj)
            return prose.rstrip("\n") + "\n\n" + _fenced(spec) + "\n", spec
        return text if text.endswith("\n") else text + "\n", parse_epic(obj)
    lines = text.splitlines()
    criteria = _section(lines, _CRITERIA_HEADING)
    if not criteria:
        raise StoryError(
            "the spec has no '## Acceptance criteria' section with bullets, and no closing epic JSON block "
            f"({EPIC_CONTRACT.splitlines()[0]}): add one, one criterion per bullet"
        )
    if repo is None:
        raise StoryError("pass --repo owner/name: the spec's closing block would name no repository")
    spec = parse_epic(
        {
            "type": "epic",
            "repositories": [repo],
            "criteria": [{"id": f"EC{n}", "text": c} for n, c in enumerate(criteria, 1)],
            "constraints": _section(lines, _CONSTRAINTS_HEADING),
        }
    )
    return text.rstrip("\n") + "\n\n" + _fenced(spec) + "\n", spec


def _fenced(spec: EpicSpec) -> str:
    return f"{_OPENER}\n{json.dumps(spec.to_dict(), indent=1, ensure_ascii=False)}\n```"
