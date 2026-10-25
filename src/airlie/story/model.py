"""A story and its layers as the pipeline keeps them, parsed from the product and architect replies with
errors a role can act on; slugs, branch names, the issue-key pattern and the story exceptions."""

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


class RepositoryError(StorySetupError):
    """The repository a Jira story is for cannot be used: its repo:<owner>/<name> label is missing, repeated,
    malformed or changed since the story started, or the checkout it names is missing, repeated or a clone of
    another repository. The message is for the issue, which is told and leaves intake."""


class StoryRunning(RuntimeError):
    """A story whose job already runs in this server (POST /v1/airlie/stories): a second start is refused
    before its issue is told or moved."""


class StoryBusy(RuntimeError):
    """Another story holds the one-story lock: stories are built one at a time. (Defined here so the serve
    routes can name it without importing the factory; `factory.StoryBusy` is this class.)"""


class NoTestGate(Exception):
    """A repository has no test gate at a commit: no AGENTS.md, no Test gate section in it, or no command in
    that section's block. The message says which."""


NO_JSON = "no JSON object in the reply; end it with one ```json block"
ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]+-\d+", re.ASCII)
"""A Jira issue key (AIR-7); always matched whole (fullmatch), ASCII only (a digit is 0-9, not any script's).
It names a story's directory, prefixes its branch names and becomes a URL path, so each of those checks it."""
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
"""A control character or a Unicode line separator. A title or an id holds none; PR text blanks them."""
SURROGATE = re.compile(r"[\ud800-\udfff]")
"""A lone UTF-16 surrogate: a JSON "\\ud800" escape that is not half of a pair. It cannot be encoded as UTF-8,
so a title, a commit message, a PR body or a note that held one would fail wherever it is written."""


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
class EpicHeader:
    """What a story born from an epic carries of it (D7 of the plans phase, 2026-10-24): the epic's card key,
    the plan's summary, the constraints copied from the epic and the risk pinned to this story's criteria
    (None when none is). It travels in the story's card and from there into the briefs that read the story,
    so a story's room sees the whole without reading the epic."""

    key: str
    summary: str
    constraints: tuple[str, ...] = ()
    risk: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EpicHeader:
        risk = d.get("risk")
        return cls(
            str(d["key"]),
            str(d["summary"]),
            tuple(str(c) for c in d.get("constraints") or ()),
            str(risk) if risk else None,
        )


@dataclass(frozen=True)
class Story:
    title: str
    summary: str
    acceptance: tuple[Criterion, ...]
    out_of_scope: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    epic: EpicHeader | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Story:
        epic = d.get("epic")
        return cls(
            str(d["title"]),
            str(d["summary"]),
            tuple(Criterion(**c) for c in d["acceptance"]),
            tuple(d.get("out_of_scope") or ()),
            tuple(d.get("assumptions") or ()),
            EpicHeader.from_dict(epic) if isinstance(epic, dict) else None,
        )


@dataclass(frozen=True)
class ScopedPath:
    """A file a layer creates (`new`) or changes, by its path from the repository's root."""

    path: str
    new: bool = False


@dataclass(frozen=True)
class Layer:
    """One layer of a story. `scope` is prose for people (and for states saved before D14); `files` and
    `interfaces` are what the architect names for the machine: every file the layer creates or changes and
    the signatures at its boundary that later layers import (three-amigos D14, 2026-10-24)."""

    slug: str
    title: str
    scope: str
    brief: str
    acceptance: tuple[str, ...] = ()
    files: tuple[ScopedPath, ...] = ()
    interfaces: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Layer:
        acceptance = tuple(str(a) for a in d.get("acceptance") or ())
        files = tuple(
            ScopedPath(str(f["path"]), bool(f.get("new", False)))
            if isinstance(f, dict)
            else ScopedPath(str(f))
            for f in d.get("files") or ()
        )
        interfaces = tuple(str(i) for i in d.get("interfaces") or ())
        return cls(
            str(d["slug"]), str(d["title"]), str(d["scope"]), str(d["brief"]), acceptance, files, interfaces
        )

    def paths(self) -> tuple[str, ...]:
        """The files the layer names, else the paths its prose scope mentions."""
        if self.files:
            return tuple(f.path for f in self.files)
        from airlie.story.context import named_paths  # not at the top: context imports the config

        return tuple(named_paths(self.scope))


def slugify(text: str, max_len: int = 40) -> str:
    """Lowercase letters, digits and hyphens, at most `max_len` characters ("x" when nothing is left)."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "x"


def branch_name(ref: str, story_title: str, nn: str, layer_slug: str) -> str:
    """<issue key>-<story slug>-<NN>-<layer slug> (ai-mode's pr-stack naming). The key keeps its case: GitHub
    for Jira links branches and PRs by issue key. A written request (ref "") has no prefix.

    The name goes to git and gh as an argument, so this is where an unchecked part stops: StorySetupError
    unless `ref` is "" or an issue key and the whole name is letters, digits, "-" and "_" (no leading "-", no
    slash, dot, space or shell syntax, whatever `nn` and `layer_slug` hold)."""
    if ref and not ISSUE_KEY.fullmatch(ref):
        raise StorySetupError(f"a branch name starts with an issue key (AIR-7), not {ref!r}")
    tail = f"{slugify(story_title, 30)}-{nn}-{layer_slug}"
    name = f"{ref}-{tail}" if ref else tail
    if not _BRANCH.fullmatch(name):
        raise StorySetupError(f"branch name {name!r} may hold only letters, digits, '-' and '_'")
    return name


def _encodable(value: str, what: str) -> str:
    """`value` if UTF-8 can encode it; else a StoryError naming `what`, so the role writes the character
    itself instead of half of an escape."""
    if SURROGATE.search(value):
        raise StoryError(
            f"{what} holds a lone surrogate (a \\ud800-\\udfff escape that is not half of a pair); "
            "write the character itself"
        )
    return value


def _one_line(value: str, what: str, limit: int) -> str:
    """`value` if it is one line of at most `limit` characters with no control character; else a StoryError
    naming `what`, so the role's next attempt can fix it."""
    _encodable(value, what)
    if len(value) > limit or CONTROL.search(value):
        raise StoryError(f"{what} must be one line of at most {limit} characters with no control characters")
    return value


def _text(d: dict[str, Any], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise StoryError(f"{where} needs a non-empty '{key}'")
    return _encodable(v.strip(), f"{where}: '{key}'")


def _strs(v: Any, what: str, of: str = "strings") -> tuple[str, ...]:
    """A list field read as `_str_list` reads one (studio_plan): missing or null is empty, a lone string is a
    list of one, a list is the str of each item; anything else is a StoryError naming `what`."""
    if v is None:
        return ()
    if isinstance(v, str):
        return (_encodable(v, what),)
    if isinstance(v, list):
        return tuple(_encodable(str(x), what) for x in v)
    raise StoryError(f"{what} must be a list of {of}")


def _scoped_paths(v: Any, where: str) -> tuple[ScopedPath, ...]:
    """A layer's `files`: a list of {"path", "new"} objects or of path strings. A path that is absolute or
    climbs out with ".." is refused: the architect's files become paths the checks read and a package may
    create."""
    if v is None:
        return ()
    if not isinstance(v, list):
        raise StoryError(f'{where}: \'files\' must be a list of {{"path", "new"}} objects')
    out: list[ScopedPath] = []
    for n, item in enumerate(v, 1):
        raw = item.get("path") if isinstance(item, dict) else item
        if not isinstance(raw, str) or not raw.strip():
            raise StoryError(f"{where}: files item {n} needs a 'path' (a path from the repository's root)")
        path = _encodable(raw.strip(), f"{where}: files item {n}").removeprefix("./")
        if path.startswith("/") or ".." in path.split("/"):
            raise StoryError(
                f"{where}: files item {n} {raw!r} must be a path inside the repository, from its root"
            )
        out.append(ScopedPath(path, bool(item.get("new", False)) if isinstance(item, dict) else False))
    return tuple(out)


def parse_story(obj: dict[str, Any] | None) -> Story:
    if obj is None:
        raise StoryError(NO_JSON)
    if obj.get("type") == "blocked":
        raise NotActionable(_encodable(str(obj.get("reason") or "no reason given"), "the blocked 'reason'"))
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
        cid = _one_line(str(c.get("id") or f"AC{n}"), f"acceptance item {n} id", 20)
        where = f"acceptance {cid}"
        given, when, then = (_text(c, k, where) for k in ("given", "when", "then"))
        criteria.append(Criterion(cid, given, when, then))
    ids = [c.id for c in criteria]
    if len(set(ids)) != len(ids):
        raise StoryError(f"acceptance ids repeat ({ids}); give each criterion its own id")
    return Story(
        _one_line(_text(obj, "title", "the story"), "the story title", 80),
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
    owner: dict[str, str] = {}
    for n, d in enumerate(raw, 1):
        if not isinstance(d, dict):
            raise StoryError(f"layer {n} must be an object")
        title = _one_line(_text(d, "title", f"layer {n}"), f"layer {n} title", 80)
        slug = slugify(str(d.get("slug") or title), 30)
        if slug in {x.slug for x in layers}:
            raise StoryError(f"layer slug '{slug}' repeats; give each layer its own slug")
        acc = _strs(d.get("acceptance"), f"layer '{slug}': 'acceptance'", "criterion ids")
        unknown = [a for a in acc if a not in known]
        if unknown:
            raise StoryError(
                f"layer '{slug}' names unknown acceptance ids {unknown}; the story has {sorted(known)}"
            )
        for a in acc:
            if owner.get(a, slug) != slug:
                raise StoryError(
                    f"acceptance {a} is listed under layers '{owner[a]}' and '{slug}'; "
                    "list each criterion under one layer"
                )
            owner[a] = slug
        where = f"layer '{slug}'"
        layers.append(
            Layer(
                slug,
                title,
                _text(d, "scope", where),
                _text(d, "brief", where),
                acc,
                _scoped_paths(d.get("files"), where),
                _strs(d.get("interfaces"), f"{where}: 'interfaces'"),
            )
        )
    missing = sorted(known - set(owner))
    if missing:
        raise StoryError(
            f"no layer covers acceptance {missing}; give each criterion to the layer that makes it true"
        )
    return tuple(layers)
