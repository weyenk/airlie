"""The story plan (D2 of the plans phase, P3 of the planning line, 2026-10-24): the architect's reply when it
turns an epic into the smallest units that each end in a pull request stack a reviewer can judge alone. The
contract and its parser sit together, as the layer plan's do (`model.parse_layers`).

The reply is model output, read as data. `parse_story_plan` refuses, with a reason the architect can act on
(it goes back as the next try's feedback, `story.stage_attempts` tries): a repeated or unusable story id, a
dependency or an interface that names a story the plan does not have, a cycle, an epic criterion no story
names, a story with no acceptance criterion, a repository the epic does not name, more stories than
`story.plan.max_stories`, a scope over `story.plan.max_scope_files` paths (or one that leaves the repository),
a size other than S or M (an L is a story to split), and a risk pinned to no story criterion. Nothing here
reaches git; the plan-time checks against the trunk are `check_story_plan`, below the parser."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from airlie.story.epic import REPOSITORY, EpicSpec
from airlie.story.model import (
    NO_JSON,
    Criterion,
    Story,
    StoryError,
    _encodable,
    _one_line,
    _strs,
    _text,
)

STORY_PLAN_CONTRACT = """A story is the smallest unit that ends in a pull request stack a reviewer can judge alone: its acceptance criteria are what the reviewer checks, its scope is what the diff touches. End your reply with one ```json block holding exactly one object:
{"type": "story_plan",
 "summary": "the epic's goal and the shape of the change, three sentences",
 "constraints": ["copied from the epic, one line each"],
 "risks": [{"risk": "what would bite a person", "pinned": "S2/AC1"}],
 "stories": [{"id": "S1", "title": "imperative, under 80 chars", "why": "one sentence from the epic",
              "repository": "owner/name", "epic_criteria": ["EC1"],
              "scope": [{"path": "src/pkg/mod.py", "new": false}],
              "consumes": ["S0: def name(...) -> R"], "produces": ["def name(...) -> R"],
              "acceptance": [{"id": "AC1", "given": "...", "when": "...", "then": "..."}],
              "out_of_scope": ["..."], "assumptions": ["..."], "depends_on": ["S0"], "size": "S" or "M"}]}
List the stories in dependency order. Every epic criterion appears in the epic_criteria of at least one story. Each risk is pinned to the story criterion that would catch it."""  # noqa: E501

_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,19}")
_PINNED = re.compile(r"(?P<story>[^/\s]+)/(?P<criterion>[^/\s]+)")
_CONSUMED = re.compile(r"(?P<story>[A-Z]+[0-9]+):")
"""A `consumes` line that starts "S0:" names the story it comes from."""
SIZES = ("S", "M")


@dataclass(frozen=True)
class ScopedPath:
    """A file a story may touch, and whether the story creates it."""

    path: str
    new: bool = False


@dataclass(frozen=True)
class PlannedStory:
    id: str
    title: str
    why: str
    repository: str
    epic_criteria: tuple[str, ...]
    scope: tuple[ScopedPath, ...]
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    acceptance: tuple[Criterion, ...]
    out_of_scope: tuple[str, ...]
    assumptions: tuple[str, ...]
    depends_on: tuple[str, ...]
    size: Literal["S", "M"]

    def story(self) -> Story:
        """The story as the pipeline holds it (the card's `Story`): the plan's `why` is its summary."""
        return Story(self.title, self.why, self.acceptance, self.out_of_scope, self.assumptions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "why": self.why,
            "repository": self.repository,
            "epic_criteria": list(self.epic_criteria),
            "scope": [{"path": s.path, "new": s.new} for s in self.scope],
            "consumes": list(self.consumes),
            "produces": list(self.produces),
            "acceptance": [
                {"id": c.id, "given": c.given, "when": c.when, "then": c.then} for c in self.acceptance
            ],
            "out_of_scope": list(self.out_of_scope),
            "assumptions": list(self.assumptions),
            "depends_on": list(self.depends_on),
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlannedStory:
        return cls(
            str(d["id"]),
            str(d["title"]),
            str(d["why"]),
            str(d["repository"]),
            tuple(str(x) for x in d["epic_criteria"]),
            tuple(ScopedPath(str(s["path"]), bool(s.get("new"))) for s in d["scope"]),
            tuple(str(x) for x in d.get("consumes") or ()),
            tuple(str(x) for x in d.get("produces") or ()),
            tuple(Criterion(**c) for c in d["acceptance"]),
            tuple(str(x) for x in d.get("out_of_scope") or ()),
            tuple(str(x) for x in d.get("assumptions") or ()),
            tuple(str(x) for x in d.get("depends_on") or ()),
            cast(Literal["S", "M"], d["size"]),
        )


@dataclass(frozen=True)
class Risk:
    risk: str
    pinned: str
    """"S2/AC1": the story and the criterion of it that would catch the risk."""


@dataclass(frozen=True)
class StoryPlan:
    summary: str
    constraints: tuple[str, ...]
    risks: tuple[Risk, ...]
    stories: tuple[PlannedStory, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "story_plan",
            "summary": self.summary,
            "constraints": list(self.constraints),
            "risks": [{"risk": r.risk, "pinned": r.pinned} for r in self.risks],
            "stories": [s.to_dict() for s in self.stories],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StoryPlan:
        return cls(
            str(d["summary"]),
            tuple(str(c) for c in d.get("constraints") or ()),
            tuple(Risk(str(r["risk"]), str(r["pinned"])) for r in d.get("risks") or ()),
            tuple(PlannedStory.from_dict(s) for s in d["stories"]),
        )

    def story_named(self, story_id: str) -> PlannedStory | None:
        return next((s for s in self.stories if s.id == story_id), None)

    def order(self) -> tuple[PlannedStory, ...]:
        """The stories in dependency order: each after all it depends on, ties by position in the plan. A plan
        with a cycle has no order (the parser refuses one); the stories a cycle holds are left out."""
        placed: list[PlannedStory] = []
        done: set[str] = set()
        left = list(self.stories)
        while left:
            ready = next((s for s in left if all(d in done for d in s.depends_on)), None)
            if ready is None:
                break
            placed.append(ready)
            done.add(ready.id)
            left.remove(ready)
        return tuple(placed)

    def risk_for(self, story_id: str) -> str | None:
        """The risks pinned to a criterion of story `story_id`, joined on one line, or None."""
        mine = [r.risk for r in self.risks if r.pinned.split("/", 1)[0] == story_id]
        return "; ".join(mine) or None


def _safe_path(raw: Any, where: str) -> str:
    """A scope path: text, relative, inside the repository, no option-like start and no control character. It
    reaches git only after this."""
    if not isinstance(raw, str) or not raw.strip():
        raise StoryError(f"{where}: a scope entry needs a 'path'")
    path = raw.strip()
    pure = PurePosixPath(path)
    bad = (
        pure.is_absolute()
        or ".." in pure.parts
        or path.startswith(("-", "~"))
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
    )
    if bad:
        raise StoryError(f"{where}: scope path {path!r} must be a relative path inside the repository")
    return _encodable(path, where)


def _scope(raw: Any, where: str, max_scope_files: int) -> tuple[ScopedPath, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise StoryError(f"{where}: 'scope' must be a list of {{path, new}} objects")
    out: list[ScopedPath] = []
    for entry in raw:
        if isinstance(entry, str):
            out.append(ScopedPath(_safe_path(entry, where), False))
        elif isinstance(entry, dict):
            out.append(ScopedPath(_safe_path(entry.get("path"), where), entry.get("new") is True))
        else:
            raise StoryError(f"{where}: each scope entry must be an object with a 'path'")
    paths = [s.path for s in out]
    if len(set(paths)) != len(paths):
        raise StoryError(f"{where}: scope lists a path twice; list each file once")
    if len(out) > max_scope_files:
        raise StoryError(
            f"{where}: scope has {len(out)} paths; at most {max_scope_files}: split the story so each "
            "touches fewer files"
        )
    return tuple(out)


def _criteria(raw: Any, where: str) -> tuple[Criterion, ...]:
    """The story's acceptance criteria. A blank given, when or then is kept (as ""): check_story_plan names
    it, with the others, as feedback."""
    if raw is not None and not isinstance(raw, list):
        raise StoryError(f"{where}: 'acceptance' must be a list of criteria")
    if not raw:
        raise StoryError(f"{where} needs at least one acceptance criterion")
    criteria: list[Criterion] = []
    for n, c in enumerate(raw, 1):
        if not isinstance(c, dict):
            raise StoryError(f"{where}: acceptance item {n} must be an object with given, when and then")
        cid = _one_line(str(c.get("id") or f"AC{n}"), f"{where}: acceptance item {n} id", 20)
        parts = [
            _encodable(str(c.get(k) or "").strip(), f"{where}: acceptance {cid} {k}")
            for k in ("given", "when", "then")
        ]
        criteria.append(Criterion(cid, *parts))
    ids = [c.id for c in criteria]
    if len(set(ids)) != len(ids):
        raise StoryError(f"{where}: acceptance ids repeat ({ids}); give each criterion its own id")
    return tuple(criteria)


def _parse_story(
    d: Any, n: int, epic: EpicSpec, known: set[str], epic_ids: set[str], max_scope_files: int
) -> PlannedStory:
    if not isinstance(d, dict):
        raise StoryError(f"story {n} must be an object")
    raw_id = d.get("id")
    if not isinstance(raw_id, str) or not _ID.fullmatch(raw_id.strip()):
        raise StoryError(f"story {n} needs an 'id' such as S1 (letters, digits, '-' and '_', at most 20)")
    sid = raw_id.strip()
    where = f"story {sid}"
    title = _one_line(_text(d, "title", where), f"{where} title", 80)
    why = _text(d, "why", where)
    repository = _text(d, "repository", where)
    if repository not in epic.repositories:
        raise StoryError(
            f"{where}: repository {repository!r} is not one of the epic's {list(epic.repositories)}"
        )
    covers = _strs(d.get("epic_criteria"), f"{where}: 'epic_criteria'", "epic criterion ids")
    unknown = [c for c in covers if c not in epic_ids]
    if unknown:
        raise StoryError(f"{where} names unknown epic criteria {unknown}; the epic has {sorted(epic_ids)}")
    size = str(d.get("size") or "").strip().upper()
    if size == "L":
        raise StoryError(f"{where} is size L: split it into stories of size S or M")
    if size not in SIZES:
        raise StoryError(f"{where}: 'size' must be S or M, not {d.get('size')!r}")
    depends = _strs(d.get("depends_on"), f"{where}: 'depends_on'", "story ids")
    missing = [x for x in depends if x not in known]
    if missing:
        raise StoryError(
            f"{where} depends on {missing}, which the plan does not have; the stories are {sorted(known)}"
        )
    if sid in depends:
        raise StoryError(f"{where} depends on itself")
    consumes = _strs(d.get("consumes"), f"{where}: 'consumes'")
    for line in consumes:
        m = _CONSUMED.match(line)
        if m is not None and m.group("story") not in known:
            raise StoryError(
                f"{where} consumes from {m.group('story')}, which the plan does not have; "
                f"the stories are {sorted(known)}"
            )
    return PlannedStory(
        sid,
        title,
        why,
        repository,
        covers,
        _scope(d.get("scope"), where, max_scope_files),
        consumes,
        _strs(d.get("produces"), f"{where}: 'produces'"),
        _criteria(d.get("acceptance"), where),
        _strs(d.get("out_of_scope"), f"{where}: 'out_of_scope'"),
        _strs(d.get("assumptions"), f"{where}: 'assumptions'"),
        depends,
        cast(Literal["S", "M"], size),
    )


def _cycle(stories: Sequence[PlannedStory]) -> list[str] | None:
    """The ids of a dependency cycle, in order, or None."""
    after = {s.id: s.depends_on for s in stories}
    state: dict[str, int] = {}
    trail: list[str] = []

    def visit(sid: str) -> list[str] | None:
        state[sid] = 1
        trail.append(sid)
        for dep in after[sid]:
            if state.get(dep) == 1:
                return [*trail[trail.index(dep) :], dep]
            if dep not in state and (found := visit(dep)) is not None:
                return found
        trail.pop()
        state[sid] = 2
        return None

    for s in stories:
        if s.id not in state and (found := visit(s.id)) is not None:
            return found
    return None


def parse_story_plan(
    obj: dict[str, Any] | None, epic: EpicSpec, *, max_stories: int, max_scope_files: int
) -> StoryPlan:
    """The architect's story plan, or a StoryError whose text is the next try's feedback."""
    if obj is None:
        raise StoryError(NO_JSON)
    if obj.get("type") != "story_plan":
        raise StoryError('the JSON object needs "type": "story_plan"')
    raw = obj.get("stories")
    if not isinstance(raw, list) or not raw:
        raise StoryError('the JSON object needs a non-empty "stories" list')
    if len(raw) > max_stories:
        raise StoryError(f"{len(raw)} stories; at most {max_stories}: merge the smallest neighbours")
    ids: list[str] = []
    for d in raw:
        sid = d.get("id") if isinstance(d, dict) else None
        if isinstance(sid, str) and sid.strip():
            ids.append(sid.strip())
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise StoryError(f"story ids repeat ({repeated}); give each story its own id")
    known, epic_ids = set(ids), {c.id for c in epic.criteria}
    stories = tuple(_parse_story(d, n, epic, known, epic_ids, max_scope_files) for n, d in enumerate(raw, 1))
    cycle = _cycle(stories)
    if cycle is not None:
        raise StoryError(f"stories depend on each other in a cycle ({' -> '.join(cycle)}); break the cycle")
    named = {c for s in stories for c in s.epic_criteria}
    uncovered = [c.id for c in epic.criteria if c.id not in named]
    if uncovered:
        raise StoryError(
            f"no story covers epic criteria {uncovered}; name each in the epic_criteria of the story that "
            "makes it true"
        )
    risks: list[Risk] = []
    raw_risks = obj.get("risks")
    if raw_risks is not None and not isinstance(raw_risks, list):
        raise StoryError("'risks' must be a list of {risk, pinned} objects")
    for n, r in enumerate(raw_risks or (), 1):
        if not isinstance(r, dict):
            raise StoryError(f"risk {n} must be an object with a risk and a pinned criterion")
        text, pinned = _text(r, "risk", f"risk {n}"), str(r.get("pinned") or "").strip()
        m = _PINNED.fullmatch(pinned)
        story = next((s for s in stories if m and s.id == m.group("story")), None)
        if m is None or story is None or m.group("criterion") not in {c.id for c in story.acceptance}:
            raise StoryError(
                f"risk {n} is pinned to {pinned!r}, which is no criterion of a story in the plan; pin it as "
                "STORY/CRITERION, such as S2/AC1"
            )
        risks.append(Risk(text, pinned))
    plan = StoryPlan(
        _text(obj, "summary", "the plan"),
        _strs(obj.get("constraints"), "'constraints'"),
        tuple(risks),
        stories,
    )
    for s in stories:  # a repository name reaches gh in a label: its spelling is checked once more here
        if not REPOSITORY.fullmatch(s.repository):
            raise StoryError(f"story {s.id}: repository {s.repository!r} must be owner/name")
    return plan


# ---- plan-time checks against the trunk (Task 3) --------------------------------------------------------
