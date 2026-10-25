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

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from airlie.story.epic import REPOSITORY, EpicSpec, split_block
from airlie.story.model import (
    ISSUE_KEY,
    NO_JSON,
    Criterion,
    EpicHeader,
    Story,
    StoryError,
    _encodable,
    _one_line,
    _strs,
    _text,
)
from airlie.teams.worktree import find_file

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

PLAN_REFINE_FIELD = (
    'With every plan, add a top-level "refine" object: {"worth_another_round": true or false, "reason": one '
    'sentence, "check": what you would read next and why}. Say false when more reading would not change the '
    "stories, which epic criteria each one covers, or what each one depends on; say true only with something "
    "specific to check."
)
"""What the story-plan brief adds when the architect plans in rounds: the plan carries its own verdict (the
layer plan's is rounds.REFINE_FIELD)."""


def same_plans(a: StoryPlan, b: StoryPlan) -> bool:
    """The same plan for the rounds to stop on: the same stories, in order, each covering the same epic
    criteria and depending on the same stories. Titles, scopes and prose are what a model rewords between
    rounds for nothing (as same_layers does not compare them either)."""
    return [(s.id, s.epic_criteria, s.depends_on) for s in a.stories] == [
        (s.id, s.epic_criteria, s.depends_on) for s in b.stories
    ]


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
        here = {s.id for s in self.stories}  # a dependency outside the plan (a kept story) is built already
        while left:
            ready = next((s for s in left if all(d in done or d not in here for d in s.depends_on)), None)
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
    """The ids of a dependency cycle, in order, or None. A dependency outside `stories` is not in one."""
    after = {s.id: s.depends_on for s in stories}
    state: dict[str, int] = {}
    trail: list[str] = []

    def visit(sid: str) -> list[str] | None:
        state[sid] = 1
        trail.append(sid)
        for dep in after[sid]:
            if dep not in after:
                continue
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


def _stackable(stories: Sequence[PlannedStory], kept: Mapping[str, PlannedStory]) -> None:
    """A story is built on its dependency's branch (a stack, bottom up, one branch below it): a dependency in
    another repository has no branch in this one, and a story that depends on several must have them on one
    line, each below the latest, or the one branch it is built on lacks the others' code. A StoryError says
    which, and what to change."""
    by_id = {**kept, **{s.id: s for s in stories}}
    position = {**dict.fromkeys(kept, -1), **{s.id: n for n, s in enumerate(stories)}}
    for s in stories:
        for dep in s.depends_on:
            other = by_id[dep]
            if other.repository != s.repository:
                raise StoryError(
                    f"{s.id} ({s.repository}) depends on {dep} ({other.repository}), and stories in "
                    "different repositories cannot stack: drop the dependency, or plan both in one repository"
                )

    def below(sid: str) -> set[str]:
        seen: set[str] = set()
        todo = list(by_id[sid].depends_on)
        while todo:
            x = todo.pop()
            if x not in seen:
                seen.add(x)
                todo.extend(by_id[x].depends_on)
        return seen

    for s in stories:
        if len(s.depends_on) < 2:
            continue
        last = max(s.depends_on, key=lambda d: position[d])
        for dep in s.depends_on:
            if dep != last and dep not in below(last):
                raise StoryError(
                    f"{s.id} depends on {dep} and {last}, which are independent of each other; a story is "
                    f"built on one branch, so make {last} depend on {dep}, or drop one of the two"
                )


def parse_story_plan(
    obj: dict[str, Any] | None,
    epic: EpicSpec,
    *,
    max_stories: int,
    max_scope_files: int,
    kept: Mapping[str, PlannedStory] | None = None,
    retired: Collection[str] = (),
) -> StoryPlan:
    """The architect's story plan, or a StoryError whose text is the next try's feedback.

    A re-plan (D8) lists only what is still to be done. `kept` are the stories already built, in review or
    merged, as they were planned: they are not listed again (an id of one is refused), a new story may depend
    on them by id, and the epic criteria they cover count as covered. `retired` are ids that may not be used
    again (a story the re-plan retires or drops, or that an earlier re-plan did)."""
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
    kept = kept or {}
    for sid in ids:
        if sid in kept:
            raise StoryError(
                f"{sid} is already built or under review and is kept as it is: do not list it (depend on "
                "it by id), and give any new story its own id"
            )
        if sid in retired:
            raise StoryError(f"{sid} was retired or dropped: give its replacement a new id")
    known, epic_ids = {*ids, *kept}, {c.id for c in epic.criteria}
    stories = tuple(_parse_story(d, n, epic, known, epic_ids, max_scope_files) for n, d in enumerate(raw, 1))
    cycle = _cycle(stories)
    if cycle is not None:
        raise StoryError(f"stories depend on each other in a cycle ({' -> '.join(cycle)}); break the cycle")
    _stackable(stories, kept)
    named = {c for s in (*stories, *kept.values()) for c in s.epic_criteria}
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
        story = next((s for s in (*stories, *kept.values()) if m and s.id == m.group("story")), None)
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


# ---- plan-time checks against the trunk (D4) ------------------------------------------------------------

PlanFindingKind = Literal["file_missing", "order_violation", "empty_criterion"]
_ABSENT = frozenset({"missing", "dangling", "loop", "notdir"})
"""What `find_file` answers for a path that leads to nothing in the commit."""


@dataclass(frozen=True)
class PlanFinding:
    """A flaw the plan-time checks found in a plan that parsed: its kind, the story it is in and why, in words
    the architect can act on."""

    kind: PlanFindingKind
    story: str
    why: str


def check_story_plan(
    plan: StoryPlan, *, trunk_of: Callable[[str], tuple[Path, str] | None]
) -> list[PlanFinding]:
    """The mechanical checks (D4) a plan passes before any card is made, in the plan's listed order:
    `file_missing` (a scope path not marked new that neither the trunk has nor an earlier story of the plan,
    in that repository, creates), `order_violation` (a dependency listed after its dependent) and
    `empty_criterion` (a Given/When/Then with a blank part). `trunk_of(repository)` is the checkout and the
    trunk's commit, or None when the line has no checkout of it: its paths pass unchecked."""
    found: list[PlanFinding] = []
    position = {s.id: n for n, s in enumerate(plan.stories)}
    created: dict[str, set[str]] = {}
    for s in plan.stories:
        for dep in s.depends_on:
            if position.get(dep, -1) > position[s.id]:
                found.append(
                    PlanFinding(
                        "order_violation",
                        s.id,
                        f"{s.id} depends on {dep}, which is listed after it; list the stories in dependency "
                        "order",
                    )
                )
        where = trunk_of(s.repository)
        mine = created.setdefault(s.repository, set())
        for path in s.scope:
            if path.new or path.path in mine or where is None:
                continue
            checkout, sha = where
            if find_file(checkout, sha, path.path)[0] in _ABSENT:
                found.append(
                    PlanFinding(
                        "file_missing",
                        s.id,
                        f"scope path {path.path} is not on the trunk of {s.repository} and no "
                        "earlier "
                        "story creates it; mark it new, or fix the path",
                    )
                )
        mine.update(p.path for p in s.scope if p.new)
        for c in s.acceptance:
            blank = [k for k, v in (("given", c.given), ("when", c.when), ("then", c.then)) if not v.strip()]
            if blank:
                found.append(
                    PlanFinding(
                        "empty_criterion",
                        s.id,
                        f"acceptance {c.id} has an empty {' and '.join(blank)}; "
                        "write all of Given, When and Then",
                    )
                )
    return found


def plan_feedback(findings: Sequence[PlanFinding]) -> str:
    """One line per finding, for `brief-reply-again`."""
    return "\n".join(f"- {f.story}: {f.why}" for f in findings)


@dataclass(frozen=True)
class StoryProgress:
    """Where one story of a plan stands when the epic is re-planned (D8): `kind` says what the re-plan may do
    with it (kept: as it is; change: it has not started, so the re-plan may keep, change or drop it; retire:
    it stopped with no layer built, so the re-plan replaces it) and `standing` says why, in words."""

    id: str
    key: str
    title: str
    kind: Literal["kept", "change", "retire"]
    standing: str


# ---- the story's card (D5, D6, D7) -----------------------------------------------------------------------

STORY_CARD_CONTRACT = """The story's card ends with one ```json block holding exactly one object:
{"type": "story_card", "id": "S2", "story": {"title": ..., "summary": ..., "acceptance": [...], "out_of_scope": [...], "assumptions": [...]},
 "scope": [{"path": "src/pkg/mod.py", "new": false}], "consumes": ["S1: def name(...) -> R"], "produces": ["def name(...) -> R"],
 "depends_on": ["AIR-12"], "size": "S" | "M", "epic_criteria": ["EC1"],
 "epic": {"key": "AIR-9", "summary": "...", "constraints": ["..."], "risk": "..." | null}}
The story's card is written by airlie from the story plan; the pipeline reads this block instead of refining the story again (`size` is read later to price an epic from its finished stories)."""  # noqa: E501
LABEL_AFTER = "after:"
LABEL_EPIC = "epic:"
LABEL_PLAN = "plan:"
LABEL_RESUMED = "resumed:"
"""On a story the tick moved back to approved after the line stopped it: resumed:1, resumed:2, ..."""
LABEL_REPLANNED = "replanned_for:"
"""On an epic the tick re-planned because a story stopped again: replanned_for:<story key>."""


@dataclass(frozen=True)
class StoryCard:
    """A story as its board card carries it: the story (with its epic header), the plan's id for it, the
    architect's opening proposal for its files and interfaces, the board keys of the stories it depends on,
    its size and the epic criteria it serves."""

    id: str
    story: Story
    scope: tuple[ScopedPath, ...]
    consumes: tuple[str, ...]
    produces: tuple[str, ...]
    depends_on: tuple[str, ...]
    size: str
    epic_criteria: tuple[str, ...]


def render_story_card(ps: PlannedStory, header: EpicHeader, depends_keys: Sequence[str]) -> str:
    """The card's description: the story as a person reads it, closed by the JSON block the pipeline reads
    (`STORY_CARD_CONTRACT`)."""
    lines = [ps.why, "", "**Acceptance criteria**", *(f"- {c.line()}" for c in ps.acceptance)]
    if ps.scope:
        lines += ["", "**Files** (the architect's opening proposal, not a fence)"]
        lines += [f"- {s.path}" + (" (new)" if s.new else "") for s in ps.scope]
    if ps.consumes:
        lines += ["", "**Consumes**", *(f"- {x}" for x in ps.consumes)]
    if ps.produces:
        lines += ["", "**Produces**", *(f"- {x}" for x in ps.produces)]
    if ps.out_of_scope:
        lines += ["", "**Out of scope**", *(f"- {x}" for x in ps.out_of_scope)]
    if depends_keys:
        lines += ["", "**Stacks on** " + ", ".join(depends_keys)]
    lines += ["", f"**Epic {header.key}** · criteria {', '.join(ps.epic_criteria)} · size {ps.size}"]
    block = {
        "type": "story_card",
        "id": ps.id,
        "story": replace(ps.story(), epic=None).to_dict(),
        "scope": [{"path": s.path, "new": s.new} for s in ps.scope],
        "consumes": list(ps.consumes),
        "produces": list(ps.produces),
        "depends_on": list(depends_keys),
        "size": ps.size,
        "epic_criteria": list(ps.epic_criteria),
        "epic": asdict(header),
    }
    return "\n".join(lines) + "\n\n```json\n" + json.dumps(block, indent=1, ensure_ascii=False) + "\n```\n"


def story_card_of(description: str) -> StoryCard | None:
    """The story card a board card's closing block holds, or None when it holds none (a Jira issue, a person's
    story: it is refined). A block that says "story_card" and is flawed is a StoryError."""
    _, obj = split_block(description)
    if obj is None or obj.get("type") != "story_card":
        return None
    try:
        story = Story.from_dict(obj["story"])
        header = EpicHeader.from_dict(obj["epic"])
        depends = tuple(str(k) for k in obj.get("depends_on") or ())
        bad = [k for k in depends if not ISSUE_KEY.fullmatch(k)]
        if bad:
            raise ValueError(f"depends_on {bad} are not board keys")
        size = str(obj["size"])
        if size not in SIZES:
            raise ValueError(f"size {size!r}")
        return StoryCard(
            str(obj["id"]),
            replace(story, epic=header),
            tuple(ScopedPath(str(x["path"]), bool(x.get("new"))) for x in obj.get("scope") or ()),
            tuple(str(x) for x in obj.get("consumes") or ()),
            tuple(str(x) for x in obj.get("produces") or ()),
            depends,
            size,
            tuple(str(x) for x in obj.get("epic_criteria") or ()),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise StoryError(f"the story card's closing block is flawed ({type(e).__name__}: {e})") from None


def _labelled(labels: Sequence[str], prefix: str) -> list[str]:
    return [x.removeprefix(prefix) for x in labels if x.startswith(prefix)]


def epic_key_of(labels: Sequence[str]) -> str | None:
    """The epic key of a story's `epic:<key>` label, or None; a label that is not a board key is none."""
    found = [k for k in _labelled(labels, LABEL_EPIC) if ISSUE_KEY.fullmatch(k)]
    return found[0] if found else None


def plan_id_of(labels: Sequence[str]) -> str | None:
    """The plan id of a story's `plan:<id>` label, or None."""
    found = [k for k in _labelled(labels, LABEL_PLAN) if _ID.fullmatch(k)]
    return found[0] if found else None


def after_keys(labels: Sequence[str]) -> list[str]:
    """The board keys of a story's `after:<key>` labels, in label order (the plan's order: the last is the
    story it is built on); a label that is not a board key (a person's typo) is dropped, and the key goes on
    to name a state directory only after this check."""
    return [k for k in _labelled(labels, LABEL_AFTER) if ISSUE_KEY.fullmatch(k)]
