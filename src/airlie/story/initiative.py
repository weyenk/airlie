"""The initiative and what product says about it (the specs phase, P4 of the planning line, 2026-10-25).

An initiative is a board item at level `initiative`: a person's paragraph as the description, one or more
`repo:<owner>/<name>` labels, an optional `budget:<hours>h` label. It is read as data. A repository name
reaches a checkout only through the labels and `story.repos_root`; an upload linked in the text is named by
its id and resolved through the upload store, never fetched by URL."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from airlie.board import Item
from airlie.story.board import Card
from airlie.story.checkout import repo_labels
from airlie.story.epic import (
    REPOSITORY,
    SIZES,
    Decision,
    EpicCriterion,
    EpicSpec,
    Estimate,
    Mockups,
    StorySketch,
    Viability,
)
from airlie.story.model import ISSUE_KEY, NO_JSON, StoryError, _encodable, _one_line, _strs

LABEL_INITIATIVE = "initiative:"
"""The label that ties an epic card to the initiative it came from (its board key)."""
BUDGET_LABEL = re.compile(r"budget:(\d+(?:\.\d+)?)h")
UPLOAD_LINK = re.compile(r"/v1/airlie/uploads/([A-Za-z0-9_-]+)")
SKIP_PHRASES = ("no questions", "skip the questions")
_END_WORDS = frozenset({"go", "skip"})
_BUDGET_PREFIX = "budget:"


@dataclass(frozen=True)
class Initiative:
    """What the phase reads from the card: the person's text is `text`, quoted into briefs, never paraphrased
    into an epic as theirs. `attachments` are the ids of the uploads the text links, in order, once each."""

    key: str
    title: str
    text: str
    repositories: tuple[str, ...]
    budget_hours: float | None
    skip_questions: bool
    attachments: tuple[str, ...]

    @classmethod
    def from_item(cls, item: Item) -> Initiative:
        """StoryError (a reason a person can act on): the item's level is not initiative; it has no repo:
        label, or one that is not owner/name; a budget label that is not a number of hours."""
        if item.level != "initiative":
            raise StoryError(f"{item.key} is a {item.level}, not an initiative")
        repos: list[str] = []
        for label in repo_labels(item.labels):
            named = label.split(":", 1)[1]
            if not REPOSITORY.fullmatch(named) or ".." in named.split("/"):
                raise StoryError(
                    f"the label {label!r} is not repo:<owner>/<name> (letters, digits, '.', '-', '_')"
                )
            if named not in repos:
                repos.append(named)
        if not repos:
            raise StoryError(
                "the initiative names no repository: add a label repo:<owner>/<name> for each repository it "
                "concerns"
            )
        budget: float | None = None
        for label in item.labels:
            if not label.lower().startswith(_BUDGET_PREFIX):
                continue
            m = BUDGET_LABEL.fullmatch(label)
            if m is None:
                raise StoryError(
                    f"the label {label!r} is not a budget: write budget:<hours>h, budget:20h say"
                )
            budget = float(m.group(1))
        lowered = item.description.lower()
        return cls(
            item.key,
            item.title,
            item.description,
            tuple(repos),
            budget,
            any(phrase in lowered for phrase in SKIP_PHRASES),
            tuple(dict.fromkeys(UPLOAD_LINK.findall(item.description))),
        )

    def card(self) -> Card:
        """The initiative as the briefs that take a Card read it (the architect's survey)."""
        return Card(key=self.key, id=self.key, ref=self.key, title=self.title, description=self.text)


def answer_ends_questions(text: str) -> bool:
    """A reply of "go" or "skip" alone, in any case, with trailing punctuation: the person has said enough."""
    return text.strip().strip(".!?,;: ").lower() in _END_WORDS


# ---- the question stage (D2) ----

QUESTION_CONTRACT = """End your reply with one ```json block holding exactly one object, either
{"type": "question", "text": "one open question"}
or, when everything that matters is in the request,
{"type": "questions_done", "assumptions": ["what you will work from, one line each"]}"""

MAX_QUESTION_CHARS = 600


@dataclass(frozen=True)
class QuestionReply:
    """Product's reply in the question stage: one question, or the end of the questions with the assumptions
    it goes on with."""

    question: str | None
    assumptions: tuple[str, ...]


def parse_question_reply(obj: dict[str, Any] | None) -> QuestionReply:
    """StoryError (product's next try): no JSON; a type that is neither `question` nor `questions_done`; a
    question that is empty, over 600 characters or holds two question marks (one question at a time);
    assumptions that are not strings."""
    if obj is None:
        raise StoryError(NO_JSON)
    kind = obj.get("type")
    if kind == "questions_done":
        raw = obj.get("assumptions")
        if raw is not None and not isinstance(raw, list | str):
            raise StoryError("'assumptions' must be a list of strings")
        return QuestionReply(None, _strs(raw, "'assumptions'"))
    if kind != "question":
        raise StoryError('reply with {"type": "question", ...} or {"type": "questions_done", ...}')
    text = obj.get("text")
    if not isinstance(text, str) or not text.strip():
        raise StoryError("a question needs a non-empty 'text'")
    text = _encodable(text.strip(), "the question")
    if len(text) > MAX_QUESTION_CHARS:
        raise StoryError(f"the question is {len(text)} characters; keep it under {MAX_QUESTION_CHARS}")
    if text.count("?") > 1:
        raise StoryError("ask one question at a time: that message holds more than one question mark")
    return QuestionReply(text, ())


# ---- imagining: the consultation rounds and the epics (D3, D5) ----

CONSULT_CONTRACT = """To consult the specialists, end your reply with one ```json block holding exactly one object:
{"type": "consult", "asks": [{"seat": "ux", "question": "..."},
 {"seat": "design", "kind": "mockups", "epic": "E1", "brief": "what the screen is for and who uses it"}]}"""  # noqa: E501

EPICS_CONTRACT = """When you are done, end your reply with one ```json block holding exactly one object:
{"type": "epics", "summary": "what the initiative is, in two sentences, in the person's terms",
 "epics": [
  {"id": "E1", "kind": "core", "title": "...", "why": "the value, in your words",
   "goals": ["..."], "non_goals": ["..."], "anti_goals": ["..."],
   "decisions": [{"what": "...", "why": "..."}], "constraints": ["..."],
   "criteria": [{"id": "EC1", "text": "one line, at the epic's grain"}],
   "repositories": ["owner/name"],
   "stories": [{"title": "...", "size": "S", "scope": "one line"}],
   "risks": ["..."], "viability": {"verdict": "viable", "reasons": ["..."]},
   "needs_mockups": false, "depends_on": [], "open": ["what the planner cannot settle alone"]}]}
Exactly one epic has "kind": "core"; the others are "extension" epics. A size is S, M or L; a verdict is
viable, risky or not_viable."""  # noqa: E501

EPIC_ID = re.compile(r"E[0-9]+", re.ASCII)
Verdict = Literal["viable", "risky", "not_viable"]
VERDICTS: tuple[Verdict, ...] = ("viable", "risky", "not_viable")
MAX_ASK_CHARS = 2_000


@dataclass(frozen=True)
class Ask:
    """One thing product wants from a seat in a round: a question to answer from the code and the seat's
    craft, or (`mockups`) the three directions and pages of a screen for epic `epic`, `question` being the
    brief for the screen."""

    seat: str
    question: str
    kind: Literal["question", "mockups"] = "question"
    epic: str | None = None


@dataclass(frozen=True)
class EpicDraft:
    """One epic as product wrote it, before it is priced or carded (D5)."""

    id: str
    kind: Literal["core", "extension"]
    title: str
    why: str
    goals: tuple[str, ...]
    non_goals: tuple[str, ...]
    anti_goals: tuple[str, ...]
    decisions: tuple[Decision, ...]
    constraints: tuple[str, ...]
    criteria: tuple[EpicCriterion, ...]
    repositories: tuple[str, ...]
    stories: tuple[StorySketch, ...]
    risks: tuple[str, ...]
    viability: Viability
    needs_mockups: bool
    depends_on: tuple[str, ...]
    open: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "why": self.why,
            "goals": list(self.goals),
            "non_goals": list(self.non_goals),
            "anti_goals": list(self.anti_goals),
            "decisions": [{"what": d.what, "why": d.why} for d in self.decisions],
            "constraints": list(self.constraints),
            "criteria": [{"id": c.id, "text": c.text} for c in self.criteria],
            "repositories": list(self.repositories),
            "stories": [{"title": s.title, "size": s.size, "scope": s.scope} for s in self.stories],
            "risks": list(self.risks),
            "viability": {"verdict": self.viability.verdict, "reasons": list(self.viability.reasons)},
            "needs_mockups": self.needs_mockups,
            "depends_on": list(self.depends_on),
            "open": list(self.open),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EpicDraft:
        """The draft of a dict this module wrote (`to_dict`): trusted, so only the shape is read."""
        via = d["viability"]
        return cls(
            str(d["id"]),
            d["kind"],
            str(d["title"]),
            str(d["why"]),
            tuple(map(str, d.get("goals") or ())),
            tuple(map(str, d.get("non_goals") or ())),
            tuple(map(str, d.get("anti_goals") or ())),
            tuple(Decision(str(x["what"]), str(x["why"])) for x in d.get("decisions") or ()),
            tuple(map(str, d.get("constraints") or ())),
            tuple(EpicCriterion(str(c["id"]), str(c["text"])) for c in d["criteria"]),
            tuple(map(str, d["repositories"])),
            tuple(StorySketch(str(s["title"]), s["size"], str(s.get("scope", ""))) for s in d["stories"]),
            tuple(map(str, d.get("risks") or ())),
            Viability(via["verdict"], tuple(map(str, via.get("reasons") or ()))),
            bool(d.get("needs_mockups")),
            tuple(map(str, d.get("depends_on") or ())),
            tuple(map(str, d.get("open") or ())),
        )


@dataclass(frozen=True)
class EpicsReply:
    summary: str
    epics: tuple[EpicDraft, ...]

    def order(self) -> tuple[EpicDraft, ...]:
        """The core first, then the extensions in dependency order (an extension after every sibling it
        depends on; ties in product's order)."""
        core = [e for e in self.epics if e.kind == "core"]
        rest = [e for e in self.epics if e.kind != "core"]
        placed = {e.id for e in core}
        ordered = list(core)
        while rest:
            ready = next((e for e in rest if all(d in placed for d in e.depends_on)), rest[0])
            rest.remove(ready)
            ordered.append(ready)
            placed.add(ready.id)
        return tuple(ordered)


def _text(value: Any, what: str, limit: int = 4_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoryError(f"{what} must be a non-empty string")
    if len(value) > limit:
        raise StoryError(f"{what} is {len(value)} characters; keep it under {limit}")
    return _encodable(value.strip(), what)


def _draft(raw: Any, n: int, initiative: Initiative) -> EpicDraft:
    """One epic object as a draft, or a StoryError naming the epic and the part that is wrong."""
    if not isinstance(raw, dict):
        raise StoryError(f"epic {n} must be an object")
    eid = raw.get("id")
    if not isinstance(eid, str) or not EPIC_ID.fullmatch(eid):
        raise StoryError(f"epic {n} needs an 'id' like E1 (E, then digits), not {eid!r}")
    kind = raw.get("kind")
    if kind not in ("core", "extension"):
        raise StoryError(f"epic {eid}: 'kind' must be core or extension, not {kind!r}")
    where = f"epic {eid}"
    title = _one_line(_text(raw.get("title"), f"{where} 'title'", 200), f"{where} title", 200)
    why = _text(raw.get("why"), f"{where} 'why'")
    decisions: list[Decision] = []
    for k, dec in enumerate(raw.get("decisions") or (), 1):
        if not isinstance(dec, dict):
            raise StoryError(f"{where}: decision {k} must be an object with 'what' and 'why'")
        decisions.append(
            Decision(
                _text(dec.get("what"), f"{where} decision {k} 'what'"),
                _text(dec.get("why"), f"{where} decision {k} 'why'"),
            )
        )
    raw_criteria = raw.get("criteria")
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise StoryError(f"{where} needs at least one criterion ({{'id': 'EC1', 'text': '...'}})")
    criteria: list[EpicCriterion] = []
    for k, c in enumerate(raw_criteria, 1):
        if not isinstance(c, dict):
            raise StoryError(f"{where}: criterion {k} must be an object with an id and a text")
        cid = _one_line(_text(c.get("id"), f"{where} criterion {k} 'id'", 20), f"{where} criterion id", 20)
        criteria.append(EpicCriterion(cid, _text(c.get("text"), f"{where} criterion {cid} 'text'", 600)))
    ids = [c.id for c in criteria]
    if len(set(ids)) != len(ids):
        raise StoryError(f"{where}: criterion ids repeat ({ids}); give each its own id")
    repos = _strs(raw.get("repositories"), f"{where} 'repositories'")
    if not repos:
        raise StoryError(f"{where} needs at least one repository, among {list(initiative.repositories)}")
    strangers = [r for r in repos if r not in initiative.repositories]
    if strangers:
        raise StoryError(
            f"{where} names {strangers}, which are not the initiative's repositories "
            f"({list(initiative.repositories)})"
        )
    raw_stories = raw.get("stories")
    if not isinstance(raw_stories, list) or not raw_stories:
        raise StoryError(f"{where} needs a story sketch: a list of {{'title', 'size', 'scope'}}")
    sketch: list[StorySketch] = []
    for k, s in enumerate(raw_stories, 1):
        if not isinstance(s, dict):
            raise StoryError(f"{where}: story {k} must be an object with a title, a size and a scope")
        size = s.get("size")
        if size not in SIZES:
            raise StoryError(
                f"{where}: story {k} needs a 'size' of S, M or L (the estimate needs it), not {size!r}"
            )
        story_title = _text(s.get("title"), f"{where} story {k} 'title'", 200)
        sketch.append(
            StorySketch(
                _one_line(story_title, f"{where} story title", 200),
                size,
                _text(s.get("scope") or story_title, f"{where} story {k} 'scope'", 400),
            )
        )
    via = raw.get("viability")
    if not isinstance(via, dict) or via.get("verdict") not in VERDICTS:
        raise StoryError(f"{where} needs a 'viability' with a verdict of {', '.join(VERDICTS)}")
    reasons = _strs(via.get("reasons"), f"{where} viability 'reasons'")
    if not reasons:
        raise StoryError(f"{where}: a viability verdict needs at least one reason")
    needs = raw.get("needs_mockups", False)
    if not isinstance(needs, bool):
        raise StoryError(f"{where}: 'needs_mockups' must be true or false")
    return EpicDraft(
        eid,
        kind,
        title,
        why,
        _strs(raw.get("goals"), f"{where} 'goals'"),
        _strs(raw.get("non_goals"), f"{where} 'non_goals'"),
        _strs(raw.get("anti_goals"), f"{where} 'anti_goals'"),
        tuple(decisions),
        _strs(raw.get("constraints"), f"{where} 'constraints'"),
        tuple(criteria),
        repos,
        tuple(sketch),
        _strs(raw.get("risks"), f"{where} 'risks'"),
        Viability(via["verdict"], reasons),
        needs,
        _strs(raw.get("depends_on"), f"{where} 'depends_on'"),
        _strs(raw.get("open"), f"{where} 'open'"),
    )


def parse_epics(obj: dict[str, Any] | None, *, initiative: Initiative, single: bool = False) -> EpicsReply:
    """Product's epics as drafts (D5). StoryError, in words product can act on: no JSON or another type;
    an epic that is flawed (see `_draft`); ids that repeat; not exactly one core (any kind alone when
    `single`, a revision of one epic); a `depends_on` that names no sibling, makes a cycle, or puts the core
    after an extension."""
    if obj is None:
        raise StoryError(NO_JSON)
    if obj.get("type") != "epics":
        raise StoryError('the reply needs "type": "epics"')
    raw = obj.get("epics")
    if not isinstance(raw, list) or not raw:
        raise StoryError("'epics' must be a list with at least the core epic")
    drafts = tuple(_draft(e, n, initiative) for n, e in enumerate(raw, 1))
    ids = [d.id for d in drafts]
    if len(set(ids)) != len(ids):
        raise StoryError(f"epic ids repeat ({ids}); give each its own id")
    cores = [d.id for d in drafts if d.kind == "core"]
    if not single and len(cores) != 1:
        raise StoryError(
            f"exactly one epic must have kind core, not {len(cores)} ({cores or 'none'}): cut the initiative "
            "into a core epic and optional extensions"
        )
    if single and len(drafts) != 1:
        raise StoryError("a revision holds exactly one epic")
    by_id = {d.id: d for d in drafts}
    for d in drafts:
        for dep in d.depends_on:
            if dep == d.id:
                raise StoryError(f"epic {d.id} depends on itself")
            if single:
                if not EPIC_ID.fullmatch(dep):
                    raise StoryError(f"epic {d.id} depends on {dep!r}, which is not an epic id like E1")
                continue
            if dep not in by_id:
                raise StoryError(f"epic {d.id} depends on {dep!r}, which is not one of the epics ({ids})")
            if d.kind == "core" and by_id[dep].kind != "core":
                raise StoryError(
                    f"the core {d.id} depends on the extension {dep}: only an extension may wait"
                )
    if not single:
        _acyclic(by_id)
    summary = "" if single else _text(obj.get("summary"), "'summary'")
    return EpicsReply(summary, drafts)


def _acyclic(by_id: dict[str, EpicDraft]) -> None:
    """StoryError naming the cycle when the epics' `depends_on` make one."""
    state: dict[str, int] = {}

    def visit(eid: str, path: list[str]) -> None:
        if state.get(eid) == 2:
            return
        if state.get(eid) == 1:
            cycle = [*path[path.index(eid) :], eid]
            raise StoryError("epics depend on each other in a cycle: " + " -> ".join(cycle))
        state[eid] = 1
        for dep in by_id[eid].depends_on:
            visit(dep, [*path, eid])
        state[eid] = 2

    for eid in by_id:
        visit(eid, [])


def parse_imagine_reply(
    obj: dict[str, Any] | None, *, initiative: Initiative, consultable: Sequence[str], max_asks: int
) -> tuple[Ask, ...] | EpicsReply:
    """One round of product's imagining: a consult (the asks) or the epics. StoryError: no JSON; a type that
    is neither; no ask, more than `max_asks`, a seat that is not in `consultable`, an empty question, a
    mockups ask without an epic id or a brief; else `parse_epics`'s."""
    if obj is None:
        raise StoryError(NO_JSON)
    kind = obj.get("type")
    if kind == "epics":
        return parse_epics(obj, initiative=initiative)
    if kind != "consult":
        raise StoryError('reply with {"type": "consult", ...} or {"type": "epics", ...}')
    raw = obj.get("asks")
    if not isinstance(raw, list) or not raw:
        raise StoryError("a consult needs at least one ask")
    if len(raw) > max_asks:
        raise StoryError(f"a round holds at most {max_asks} asks, not {len(raw)}: keep the ones that matter")
    asks: list[Ask] = []
    for n, a in enumerate(raw, 1):
        if not isinstance(a, dict):
            raise StoryError(f"ask {n} must be an object with a seat and a question")
        seat = a.get("seat")
        if not isinstance(seat, str) or seat not in consultable:
            raise StoryError(
                f"ask {n}: the seat {seat!r} is not one you can consult ({', '.join(consultable)})"
            )
        if a.get("kind") == "mockups":
            epic = a.get("epic")
            if not isinstance(epic, str) or not EPIC_ID.fullmatch(epic):
                raise StoryError(f"ask {n}: a mockups ask needs the 'epic' id (E1) the screens are for")
            brief = _text(a.get("brief"), f"ask {n} 'brief' (a mockups ask needs one)", MAX_ASK_CHARS)
            asks.append(Ask(seat, brief, "mockups", epic))
            continue
        if a.get("kind") not in (None, "question"):
            raise StoryError(f"ask {n}: 'kind' is question or mockups, not {a.get('kind')!r}")
        asks.append(Ask(seat, _text(a.get("question"), f"ask {n} 'question'", MAX_ASK_CHARS)))
    return tuple(asks)


@dataclass(frozen=True)
class EpicFinding:
    """A mechanical finding on product's epics that goes back to it once (`over_budget`)."""

    kind: Literal["over_budget"]
    epic: str
    why: str


def check_epics(
    reply: EpicsReply,
    *,
    budget_hours: float | None,
    estimate: Callable[[Sequence[StorySketch]], Estimate],
) -> list[EpicFinding]:
    """Mechanical checks on epics that parse. `over_budget`: the core's estimate is over the budget (one
    finding, the hours, their basis and the budget named)."""
    if budget_hours is None:
        return []
    found: list[EpicFinding] = []
    for d in reply.epics:
        if d.kind != "core":
            continue
        est = estimate(d.stories)
        if est.hours > budget_hours:
            found.append(
                EpicFinding(
                    "over_budget",
                    d.id,
                    f"the core {d.id} is priced at {est.hours:g} h ({est.basis}) against the person's "
                    f"{budget_hours:g} h budget: move work to an extension, or cut it",
                )
            )
    return found


def epics_feedback(findings: Sequence[EpicFinding]) -> str:
    return "; ".join(f.why for f in findings)


# ---- the planner's read and the epic's document (D5, D7) ----

READ_CONTRACT = """End your reply with one ```json block holding exactly one object:
{"type": "read", "epic": "E1", "ok": true, "questions": ["what you would have to ask the person"]}
Set "ok" to false and list the questions when you could not plan stories from the epic as written."""

EPIC_SECTIONS = (
    "Why",
    "Goals",
    "Non-goals",
    "Anti-goals",
    "Decisions",
    "Constraints",
    "Acceptance criteria",
    "Stories, a sketch",
    "Risks",
    "Viability",
    "Estimate",
    "Consulted",
    "Questions asked",
    "Open for you",
)
"""The sections of an epic's document, in order (D5, D11): the renderer writes them and a person reads
them; a section with nothing says "none"."""


@dataclass(frozen=True)
class ReadReply:
    """The architect's read of one epic as the planner it will meet: could stories be planned from it
    without going back to the person, and if not, what would have to be asked."""

    epic: str
    ok: bool
    questions: tuple[str, ...]


def parse_read(obj: dict[str, Any] | None, *, epic: str) -> ReadReply:
    """StoryError: no JSON; another type; a different epic id; `ok` that is not true or false; `ok` false
    with no question."""
    if obj is None:
        raise StoryError(NO_JSON)
    if obj.get("type") != "read":
        raise StoryError('the reply needs "type": "read"')
    if obj.get("epic") != epic:
        raise StoryError(f'this is a read of epic {epic}, not {obj.get("epic")!r}: set "epic": "{epic}"')
    ok = obj.get("ok")
    if not isinstance(ok, bool):
        raise StoryError('"ok" must be true or false')
    questions = _strs(obj.get("questions"), "'questions'")
    if not ok and not questions:
        raise StoryError('"ok" is false but no question says what would have to be asked')
    return ReadReply(epic, ok, questions)


def _bullets(items: Sequence[str]) -> list[str]:
    return [f"- {' '.join(i.split())}" for i in items] or ["none"]


def render_epic_document(
    draft: EpicDraft,
    *,
    estimate: Estimate,
    consulted: Sequence[Mapping[str, Any]],
    qa: Sequence[Mapping[str, Any]],
    option_urls: Sequence[str],
    initiative_key: str,
    depends_keys: Sequence[str],
    picked: int | None = None,
) -> str:
    """The epic's card as a person reads it, closed by the JSON block P3 reads (`EpicSpec.to_dict` with the
    specs phase's fields): the sections of `EPIC_SECTIONS` in order, the estimate's hours and basis, the
    consultations as "<seat>: <question> -> <first line of the answer>", the Q&A verbatim, then the block with
    the mockups' URLs as its links. A section with nothing says "none"."""
    via = draft.viability
    decisions = [f"- {' '.join(d.what.split())}: {' '.join(d.why.split())}" for d in draft.decisions]
    asked: list[str] = []
    for n, entry in enumerate(qa, 1):
        asked.append(f"- Q{n}: {' '.join(str(entry.get('q', '')).split())}")
        answer = entry.get("a")
        asked.append(f"  A{n}: {' '.join(str(answer).split())}" if answer else f"  A{n}: (not answered)")
    heard = []
    for c in consulted:
        first = next((ln.strip() for ln in str(c.get("answer", "")).splitlines() if ln.strip()), "")
        heard.append(f"{c.get('seat')}: {' '.join(str(c.get('question', '')).split())} -> {first}")
    estimate_lines = [f"{estimate.hours:g} h: {estimate.basis}"]
    if option_urls:
        estimate_lines.append("")
        estimate_lines.append("Mockups: " + ", ".join(option_urls))
    sections: dict[str, list[str]] = {
        "Why": [draft.why.strip()],
        "Goals": _bullets(draft.goals),
        "Non-goals": _bullets(draft.non_goals),
        "Anti-goals": _bullets(draft.anti_goals),
        "Decisions": decisions or ["none"],
        "Constraints": _bullets(draft.constraints),
        "Acceptance criteria": [f"- {c.id}: {' '.join(c.text.split())}" for c in draft.criteria],
        "Stories, a sketch": [f"- [{s.size}] {s.title}: {' '.join(s.scope.split())}" for s in draft.stories],
        "Risks": _bullets(draft.risks),
        "Viability": [f"**{via.verdict}**", *_bullets(via.reasons)],
        "Estimate": estimate_lines,
        "Consulted": _bullets(heard),
        "Questions asked": asked or ["none"],
        "Open for you": _bullets(draft.open),
    }
    lines = [f"# {draft.title}", ""]
    if depends_keys:
        lines += [f"Waits for {', '.join(depends_keys)} (its core epic) before it is planned.", ""]
    for name in EPIC_SECTIONS:
        lines += [f"## {name}", *sections[name], ""]
    spec = EpicSpec(
        draft.repositories,
        draft.criteria,
        draft.constraints,
        tuple(option_urls),
        initiative_key,
        draft.kind,
        tuple(depends_keys),
        draft.stories,
        estimate,
        draft.viability,
        Mockups(tuple(option_urls), picked) if option_urls else None,
    )
    return (
        "\n".join(lines)
        + "\n```json\n"
        + json.dumps(spec.to_dict(), indent=1, ensure_ascii=False)
        + "\n```\n"
    )


def initiative_key_of(labels: Sequence[str]) -> str | None:
    """The board key in an epic's `initiative:<key>` label, or None (a label that is not a key, say a person's
    typo, is no initiative: the key goes on to name a state directory only after this check)."""
    for label in labels:
        key = label[len(LABEL_INITIATIVE) :]
        if label.startswith(LABEL_INITIATIVE) and ISSUE_KEY.fullmatch(key):
            return key
    return None


# ---- the person's comment on an epic: one revision (D8) ----

REVISION_CONTRACT = """End your reply with one ```json block holding exactly one object:
{"type": "revision", "epic": {<the whole revised epic, in the same form as before>},
 "redraw_mockups": false, "changed": "one line: what you changed"}"""


@dataclass(frozen=True)
class Revision:
    """Product's answer to a person's comment on an epic: the epic as it now reads, whether its mockup pages
    are to be drawn again, and one line saying what changed."""

    epic: EpicDraft
    redraw_mockups: bool
    changed: str


def parse_revision(obj: dict[str, Any] | None, *, initiative: Initiative, id: str) -> Revision:
    """StoryError: no JSON or another type; the epic is flawed (`parse_epics`, one epic of any kind) or has
    another id than `id`; `redraw_mockups` that is not true or false; no line saying what changed."""
    if obj is None:
        raise StoryError(NO_JSON)
    if obj.get("type") != "revision":
        raise StoryError('the reply needs "type": "revision"')
    reply = parse_epics({"type": "epics", "epics": [obj.get("epic")]}, initiative=initiative, single=True)
    draft = reply.epics[0]
    if draft.id != id:
        raise StoryError(f'this is a revision of epic {id}, not {draft.id}: keep "id": "{id}"')
    redraw = obj.get("redraw_mockups", False)
    if not isinstance(redraw, bool):
        raise StoryError('"redraw_mockups" must be true or false')
    changed = _one_line(_text(obj.get("changed"), "'changed'", 400), "'changed'", 400)
    return Revision(draft, redraw, changed)
