"""A story and its layers as the pipeline keeps them, parsed from the product and architect replies with
errors a role can act on; slugs, branch names, the issue-key pattern and the story exceptions."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from airlie.tools.scope import is_test_file
from airlie.topology.studio_plan import json_block_problem


class StoryError(ValueError):
    """A reply that cannot be used; the message goes back to the role with the next attempt."""

    reply: str = ""
    """The refused reply's end (REPLY_TAIL), where the parser that refused it keeps it: the retry event
    carries it, because the reply is kept nowhere else."""


REPLY_TAIL = 1500
"""How much of a refused reply, from its end, a retry event keeps (its ```json block ends it)."""


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


PLANNED = "layers"
"""The stage at which a story is planned (planning-line P5b): the layers are split and, with the story
meeting on, the agreement is stored (P2: the `layers` stage finishes then). The planning lane ends here; the
build lane takes a story that has reached it."""


class StoryBusy(RuntimeError):
    """A lane's lock is held by another job: the build lane builds one story at a time and the planning lane
    plans one thing at a time; the message names the lane. (Defined here so the serve routes can name it
    without importing the factory; `factory.StoryBusy` is this class.)"""


class NoTestGate(Exception):
    """A repository has no test gate at a commit: no AGENTS.md, no Test gate section in it, or no command in
    that section's block. The message says which."""


NO_JSON = "no JSON object in the reply; end it with one ```json block"


def unusable_why(e: StoryError, reply: str) -> str:
    """What a role is told when its `reply` was refused with `e`: NO_JSON becomes what is wrong with the
    ```json block the reply did hold (`json_block_problem`), so the role can mend that block."""
    if str(e) == NO_JSON:
        return json_block_problem(reply) or NO_JSON
    return str(e)


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
    the signatures at its boundary that later layers import (three-amigos D14, 2026-10-24). `unchanged` is
    the ids of `acceptance` the architect marks as behaviour that stays as it was, riding with this layer's
    change (AIR-62, 2026-11-05): with those whose wording says so, they do not count toward the soft cap."""

    slug: str
    title: str
    scope: str
    brief: str
    acceptance: tuple[str, ...] = ()
    files: tuple[ScopedPath, ...] = ()
    interfaces: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

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
        unchanged = tuple(str(a) for a in d.get("unchanged") or ())
        return cls(
            str(d["slug"]),
            str(d["title"]),
            str(d["scope"]),
            str(d["brief"]),
            acceptance,
            files,
            interfaces,
            unchanged,
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


def _title(value: str, what: str, limit: int) -> str:
    """`value` as a title: its control characters and line breaks made spaces, its runs of spaces one, and
    clipped at the last word that fits `limit`. A title is a label, so it is mended here rather than sent
    back: 8 of 20 layer-plan fixes in the roman bench (2026-10-26/28) were a title over 80 characters or with
    a line break, each a whole architect turn."""
    _encodable(value, what)
    flat = " ".join(CONTROL.sub(" ", value).split())
    if len(flat) <= limit:
        return flat
    cut = flat[: limit + 1].rsplit(" ", 1)[0]
    return (cut if cut and len(cut) <= limit else flat[:limit]).rstrip(" ,;:-")


def _text(d: dict[str, Any], key: str, where: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise StoryError(f"{where} needs a non-empty '{key}'")
    return _encodable(v.strip(), f"{where}: '{key}'")


_AC_ID = re.compile(r"AC\d+", re.IGNORECASE)
"""How a criterion id is written in a plan (`AC3`): under `unchanged`, anything else is words."""


def _unchanged_ids(marked: Sequence[str], acc: Sequence[str], story: Story) -> tuple[str, ...]:
    """A layer's `unchanged` entries as criterion ids. The architect writes a criterion's words there as
    readily as its id (AIR-55, 2026-11-07: three plans in a row refused for one then-clause quoted under
    `unchanged`, each refusal read as a request for more words): an entry that is no id of the story's is
    the one criterion of the layer whose then-clause it quotes or is quoted by, and is left out when none or
    several are, since the list only spares the size cap. Anything written as an id (AC9) is kept as
    written, for the check that it is one of the layer's."""
    known = {c.id: c for c in story.acceptance}
    out: list[str] = []
    for m in marked:
        if m in known or _AC_ID.fullmatch(m.strip()):
            out.append(m)
            continue
        words = m.strip().lower()
        if not words:
            continue
        hits = [
            a
            for a in acc
            if a in known and (words in known[a].then.lower() or known[a].then.lower() in words)
        ]
        if len(hits) == 1:
            out.append(hits[0])
    return tuple(dict.fromkeys(out))


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


def parse_story(obj: dict[str, Any] | None, *, renumber: bool = False) -> Story:
    """The story in `obj`, or a StoryError saying what to mend. With `renumber` (refine, where nothing cites
    the ids yet), a criterion whose id repeats an earlier one's takes the next unused ACn instead of costing
    the whole refine again (roman, 2026-10-31: AC1, AC2, AC3, AC3, AC4, AC5 was a fresh refine run); the
    first of each keeps its id, so whatever the story's own text says of the others still holds. The meeting
    reads a revised story without it: there the layers cite the ids."""
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
    if renumber:
        criteria = _renumbered(criteria)
    ids = [c.id for c in criteria]
    if len(set(ids)) != len(ids):
        raise StoryError(f"acceptance ids repeat ({ids}); give each criterion its own id")
    return Story(
        _title(_text(obj, "title", "the story"), "the story title", 80),
        _text(obj, "summary", "the story"),
        tuple(criteria),
        _strs(obj.get("out_of_scope"), "'out_of_scope'"),
        _strs(obj.get("assumptions"), "'assumptions'"),
    )


def _renumbered(criteria: list[Criterion]) -> list[Criterion]:
    """`criteria` with each repeat of an earlier id given the next ACn no criterion has."""
    taken = {c.id for c in criteria}
    seen: set[str] = set()
    out: list[Criterion] = []
    n = 0
    for c in criteria:
        if c.id in seen:
            n += 1
            while f"AC{n}" in taken:
                n += 1
            taken.add(f"AC{n}")
            c = replace(c, id=f"AC{n}")
        seen.add(c.id)
        out.append(c)
    return out


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
        title = _title(_text(d, "title", f"layer {n}"), f"layer {n} title", 80)
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
        same = _unchanged_ids(_strs(d.get("unchanged"), f"{where}: 'unchanged'", "criterion ids"), acc, story)
        stray = [a for a in same if a not in acc]
        if stray:
            raise StoryError(
                f"layer '{slug}' marks {stray} unchanged but does not list them under its 'acceptance' "
                f"{list(acc)}; mark only the criteria the layer lists"
            )
        files = _scoped_paths(d.get("files"), where)
        if files and all(is_test_file(f.path) for f in files):
            raise StoryError(
                f"layer '{slug}' changes only test files ({', '.join(f.path for f in files)}): a layer's "
                "tests must fail before its code and pass after it, and with no source file to change they "
                "cannot. A criterion that existing behaviour stays unchanged rides with the layer whose "
                "change could break it, never in a layer of its own: give this layer's criteria to that "
                "layer, or name the source files this layer changes"
            )
        layers.append(
            Layer(
                slug,
                title,
                _text(d, "scope", where),
                _text(d, "brief", where),
                acc,
                files,
                _strs(d.get("interfaces"), f"{where}: 'interfaces'"),
                same,
            )
        )
    missing = sorted(known - set(owner))
    if missing:
        raise StoryError(
            f"no layer covers acceptance {missing}; give each criterion to the layer that makes it true"
        )
    return tuple(layers)


_UNCHANGED = re.compile(
    r"\bunchanged\b|\bas (?:it |they )?(?:did |does |do )?before\b|\bas on (?:the )?(?:main|base|trunk)\b"
    r"|\b(?:does|do|did)(?: not|n't) change\b|\bnot changed\b|\bstill\b|\bregressions?\b"
    r"|\bkeeps? (?:its|their) (?:existing|current)\b",
    re.IGNORECASE,
)


def is_unchanged(c: Criterion) -> bool:
    """Whether `c`'s then-clause says existing behaviour stays as it was (unchanged, as before, as on main,
    does not change, still, keeps its existing ...): a regression criterion, which rides with the change that
    could break it. AIR-62 (2026-11-05): "(unchanged from before: fenced blocks keep their existing
    rendering)" and two more like it were ruled over the cap with its one real change, split into a layer of
    their own, and that layer had nothing to build."""
    return bool(_UNCHANGED.search(c.then))


def change_criteria(layer: Layer, story: Story) -> tuple[str, ...]:
    """The ids of `layer`'s criteria that its change makes true: not marked unchanged in the plan and not
    worded as unchanged behaviour (`is_unchanged`); an id the story lacks counts as a change."""
    worded = {c.id for c in story.acceptance if is_unchanged(c)}
    return tuple(a for a in layer.acceptance if a not in layer.unchanged and a not in worded)


def oversized_layers(layers: Sequence[Layer], cap: int, story: Story) -> tuple[Layer, ...]:
    """The layers of a plan that make more than `cap` acceptance criteria of `story` true
    (story.max_layer_criteria); none when `cap` is 0. A soft cap (2026-11-05): a layer's whole package of
    tests is written in one reply, and layers of 6 criteria could not be packaged in it (AIR-62 layer 01,
    AIR-55 and AIR-61 layer 02), but a story that cannot be cut further keeps its layer, so the reviewer
    rules on these, not the parser. Only `change_criteria` count: an unchanged criterion is proven inside a
    test of the change, and split off alone it is a layer with nothing to build (AIR-62's re-plan)."""
    return tuple(x for x in layers if cap and len(change_criteria(x, story)) > cap)


SIZE_VERDICTS = ("split", "keep")


def parse_size_verdict(obj: dict[str, Any] | None, slugs: Sequence[str]) -> dict[str, tuple[str, str]]:
    """The reviewer's ruling on each layer of `slugs` (the plan's layers over the cap): slug -> (split or
    keep, its one line: the suggested split or the reason to keep). A StoryError when the object is missing
    or names no usable verdict for one of them; the pipeline then accepts the plan, the check is advisory."""
    if obj is None:
        raise StoryError(NO_JSON)
    raw = obj.get("layers")
    if not isinstance(raw, list):
        raise StoryError(
            'the JSON object needs a "layers" list, a verdict for each layer you were asked about'
        )
    out: dict[str, tuple[str, str]] = {}
    for d in raw:
        if not isinstance(d, dict) or str(d.get("slug") or "") not in slugs:
            continue  # a layer it was not asked about says nothing
        slug, verdict = str(d["slug"]), d.get("verdict")
        if verdict not in SIZE_VERDICTS:
            raise StoryError(f"layer '{slug}': 'verdict' must be split or keep, not {verdict!r}")
        out[slug] = (verdict, _one_line(_text(d, "why", f"layer '{slug}'"), f"layer '{slug}': 'why'", 400))
    missing = [s for s in slugs if s not in out]
    if missing:
        raise StoryError(f"no verdict on layer '{missing[0]}'; give one for each layer you were asked about")
    return out
