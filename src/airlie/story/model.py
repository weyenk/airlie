"""A story and its layers as the pipeline keeps them, parsed from the product and architect replies with
errors a role can act on; slugs, branch names and the story exceptions."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any


class StoryError(ValueError):
    """A reply that cannot be used; the message goes back to the role with the next attempt."""


class NotActionable(Exception):
    """Product judged the request not buildable as written; the message says what a person must decide."""


class StorySetupError(RuntimeError):
    """Something a story needs is missing: the repository, the test command, a role, a matching state."""


NO_JSON = "no JSON object in the reply; end it with one ```json block"


@dataclass(frozen=True)
class Criterion:
    id: str
    given: str
    when: str
    then: str

    def line(self) -> str:
        stop = "" if self.then.endswith((".", "?", "!")) else "."
        return f"**{self.id}** Given {self.given}, when {self.when}, then {self.then}{stop}"


@dataclass(frozen=True)
class Story:
    title: str
    summary: str
    acceptance: tuple[Criterion, ...]
    out_of_scope: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Story:
        return cls(
            str(d["title"]),
            str(d["summary"]),
            tuple(Criterion(**c) for c in d["acceptance"]),
            tuple(d.get("out_of_scope") or ()),
            tuple(d.get("assumptions") or ()),
        )


@dataclass(frozen=True)
class Layer:
    slug: str
    title: str
    scope: str
    brief: str
    acceptance: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Layer:
        acceptance = tuple(str(a) for a in d.get("acceptance") or ())
        return cls(str(d["slug"]), str(d["title"]), str(d["scope"]), str(d["brief"]), acceptance)


def slugify(text: str, max_len: int = 40) -> str:
    """Lowercase letters, digits and hyphens, at most `max_len` characters ("x" when nothing is left)."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "x"


def branch_name(ref: str, story_title: str, nn: str, layer_slug: str) -> str:
    """<issue key>-<story slug>-<NN>-<layer slug> (ai-mode's pr-stack naming). The key keeps its case: GitHub
    for Jira links branches and PRs by issue key. A written request (ref "") has no prefix."""
    tail = f"{slugify(story_title, 30)}-{nn}-{layer_slug}"
    return f"{ref}-{tail}" if ref else tail


def _text(d: dict[str, Any], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise StoryError(f"{where} needs a non-empty '{key}'")
    return v.strip()


def _strs(v: Any, what: str, of: str = "strings") -> tuple[str, ...]:
    """A list field read as `_str_list` reads one (studio_plan): missing or null is empty, a lone string is a
    list of one, a list is the str of each item; anything else is a StoryError naming `what`."""
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    if isinstance(v, list):
        return tuple(str(x) for x in v)
    raise StoryError(f"{what} must be a list of {of}")


def parse_story(obj: dict[str, Any] | None) -> Story:
    if obj is None:
        raise StoryError(NO_JSON)
    if obj.get("type") == "blocked":
        raise NotActionable(str(obj.get("reason") or "no reason given"))
    if obj.get("type") != "story":
        raise StoryError('the JSON object needs "type": "story" (or "blocked" with a reason)')
    raw = obj.get("acceptance")
    if raw is not None and not isinstance(raw, list):
        raise StoryError("'acceptance' must be a list of criteria")
    if not raw:
        raise StoryError("a story needs at least one acceptance criterion")
    criteria: list[Criterion] = []
    for n, c in enumerate(raw, 1):
        if not isinstance(c, dict):
            raise StoryError(f"acceptance item {n} must be an object with given, when and then")
        cid = str(c.get("id") or f"AC{n}")
        where = f"acceptance {cid}"
        given, when, then = (_text(c, k, where) for k in ("given", "when", "then"))
        criteria.append(Criterion(cid, given, when, then))
    ids = [c.id for c in criteria]
    if len(set(ids)) != len(ids):
        raise StoryError(f"acceptance ids repeat ({ids}); give each criterion its own id")
    return Story(
        _text(obj, "title", "the story"),
        _text(obj, "summary", "the story"),
        tuple(criteria),
        _strs(obj.get("out_of_scope"), "'out_of_scope'"),
        _strs(obj.get("assumptions"), "'assumptions'"),
    )


def parse_layers(obj: dict[str, Any] | None, story: Story, max_layers: int) -> tuple[Layer, ...]:
    if obj is None:
        raise StoryError(NO_JSON)
    raw = obj.get("layers")
    if not isinstance(raw, list) or not raw:
        raise StoryError('the JSON object needs a non-empty "layers" list')
    if len(raw) > max_layers:
        raise StoryError(f"{len(raw)} layers; at most {max_layers}: merge the smallest neighbours")
    known = {c.id for c in story.acceptance}
    layers: list[Layer] = []
    covered: set[str] = set()
    for n, d in enumerate(raw, 1):
        if not isinstance(d, dict):
            raise StoryError(f"layer {n} must be an object")
        title = _text(d, "title", f"layer {n}")
        slug = slugify(str(d.get("slug") or title), 30)
        if slug in {x.slug for x in layers}:
            raise StoryError(f"layer slug '{slug}' repeats; give each layer its own slug")
        acc = _strs(d.get("acceptance"), f"layer '{slug}': 'acceptance'", "criterion ids")
        unknown = [a for a in acc if a not in known]
        if unknown:
            raise StoryError(
                f"layer '{slug}' names unknown acceptance ids {unknown}; the story has {sorted(known)}"
            )
        covered |= set(acc)
        where = f"layer '{slug}'"
        layers.append(Layer(slug, title, _text(d, "scope", where), _text(d, "brief", where), acc))
    missing = sorted(known - covered)
    if missing:
        raise StoryError(
            f"no layer covers acceptance {missing}; give each criterion to the layer that makes it true"
        )
    return tuple(layers)
