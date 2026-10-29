"""The story meeting of the planning line (P2, 2026-10-24): product, the architect and the tester agree, in
one AutoGen group chat with airlie's checker as a fourth, model-less participant, on the story's
requirements, the changes that will be made and the proof that delivery is right. This module holds the
contracts the three parts travel in (the story's own JSON, the layers' JSON, and here the proof plan),
every agent's closing verdict and the agreement the meeting ends with; the meeting itself is `Meeting`.

Everything an agent says is data: a part is parsed and validated before anyone relies on it, a proof's `file`
and `level` are checked against the repository's test tree before they can form a path, and a turn that does
not parse is a finding the owner fixes on the next turn, never a crash or a silent "agree".

Origin: AIR-22 layer 02 (2026-10-23), a tester spent forty minutes on a contradiction between two criteria
that nobody had read against each other, and fourteen of fifty-seven story blocks were a layer's tests that
tested the wrong thing at the wrong level; the meeting finds those before any layer is built."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast

from autogen_agentchat.agents import AssistantAgent, BaseChatAgent
from autogen_agentchat.base import ChatAgent, Response, Team, TerminatedException, TerminationCondition
from autogen_agentchat.messages import BaseAgentEvent, BaseChatMessage, StopMessage, TextMessage
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core import CancellationToken
from autogen_core.models import ChatCompletionClient

from airlie.config import AirlieConfig, RoleSpec
from airlie.prompts import text as hub_text
from airlie.skills import prompt_text
from airlie.story.amigos_checks import (
    NO_FINDINGS,
    check_agreement,
    check_proof,
    findings_message,
    split_problems,
)
from airlie.story.layer_checks import Finding
from airlie.story.lessons import Lesson
from airlie.story.model import (
    NO_JSON,
    Layer,
    NotActionable,
    Story,
    StoryError,
    _encodable,
    _one_line,
    parse_layers,
    parse_story,
)
from airlie.teams.autogen_team import build_function_tools, build_model_client
from airlie.tools import ToolRegistry
from airlie.tools.scope import is_js_test, is_test_module
from airlie.topology.studio_plan import extract_json_block

Part = Literal["requirements", "changes", "proof"]
PARTS: tuple[Part, ...] = ("requirements", "changes", "proof")
OWNER: dict[Part, str] = {"requirements": "product", "changes": "architect", "proof": "tester"}
"""Who owns each part of the agreement: the owner alone changes the text, and has the last word on it."""

PROOF_CONTRACT = """Post the proof plan as one ```json block holding exactly one object:
{"type": "proof", "tests": [{"criterion": "AC1", "layer": "<the layer's slug>", "file": "tests/unit/test_x.py",
 "name": "test_what_it_proves", "level": "unit", "guideline": "unit",
 "asserts": "the exact value, message or state users observe", "needs": "fixtures, fakes, state"}]}
Each test names the level of the repository's test tree it lives at (a directory under tests/) and a file under
it. A Vitest level (the repository's JS tests, such as web) takes a file under that directory, such as
web/src/screens/Wiki.test.ts, and the test's it(...) title as its name, "Describe title > it title" when it sits
in a describe: a Vue layer is proven there. guideline is a file of the guideline library, or omitted when the
repository or the level has none. Every
criterion needs at least one test and every layer at least one. criterion is null for a test that proves a
behaviour of a layer with no criterion. A criterion no test can prove because the test gate itself proves it
(the whole suite passes, the new tests are found) is one entry {"criterion": "AC9", "layer": "<slug>",
"gate": true}, with no file and no name: never a test that runs the test runner."""  # noqa: E501

VERDICT_CONTRACT = """End every turn with your verdict, one ```json block holding exactly one object:
{"type": "verdict", "requirements": "agree" | "object", "changes": "agree" | "object", "proof": "agree" | "object",
 "objections": [{"part": "proof", "about": "AC2", "claim": "one sentence", "suggest": "one sentence"}]}
Object to a part only with an objection for it; never object to your own part."""  # noqa: E501


GATE_ASSERTS = "the layer's test gate passes"


@dataclass(frozen=True)
class ProofTest:
    """One test of the proof plan: the criterion it proves (None: a behaviour of a layer with no criterion),
    the layer it belongs to, where it lives, what it asserts and what state it needs."""

    criterion: str | None
    layer: str
    file: str
    name: str
    level: str
    guideline: str | None
    asserts: str
    needs: str
    gate: bool = False
    """A criterion only the test gate can prove (the whole suite passes, the new tests are found): no test,
    no file; the gate every layer passes proves it (airlie-sandbox runs 7 and 11, 2026-10-26)."""

    @property
    def id(self) -> str:
        """Its pytest node id from the repository's root, "file::name"; a gate entry's, "gate:<criterion>"."""
        return f"gate:{self.criterion}" if self.gate else f"{self.file}::{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ProofTest:
        crit, guide = d.get("criterion"), d.get("guideline")
        return cls(
            None if crit is None else str(crit),
            str(d["layer"]),
            str(d["file"]),
            str(d["name"]),
            str(d["level"]),
            None if guide is None else str(guide),
            str(d["asserts"]),
            str(d.get("needs") or ""),
            bool(d.get("gate", False)),
        )


def _field(d: Mapping[str, Any], key: str, where: str, *, required: bool = True) -> str:
    v = d.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise StoryError(f"{where} needs a non-empty '{key}'")
        return ""
    if not isinstance(v, str):
        raise StoryError(f"{where}: '{key}' must be text")
    return _encodable(v.strip(), f"{where}: '{key}'")


def _test_path(path: str, level: str, levels: Mapping[str, int], where: str) -> str:
    """`path` as a repository path under the test tree's `level`, or a StoryError. `tests/<level>/`, or
    `tests/` when the one level is `tests` itself; a Vitest file is under its level's own directory (`web/`,
    the web-tests design, 2026-10-27); never absolute and never climbing out."""
    clean = path.removeprefix("./")
    parts = PurePosixPath(clean).parts
    if not clean or clean.startswith("/") or ".." in parts:
        raise StoryError(f"{where}: file {path!r} must be a path inside the repository, from its root")
    if is_js_test(clean):
        if not clean.startswith(f"{level}/"):
            raise StoryError(f"{where}: file {path!r} is not under {level}/ (its level is '{level}')")
        return clean
    prefix = "tests/" if level == "tests" else f"tests/{level}/"
    if not clean.startswith(prefix):
        raise StoryError(f"{where}: file {path!r} is not under {prefix} (its level is '{level}')")
    if not is_test_module(clean):
        raise StoryError(
            f"{where}: file {path!r} is not a test file (a pytest test_*.py, or a Vitest *.test.ts)"
        )
    return clean


def _proof_entry(
    t: Any,
    where: str,
    criteria: Collection[str],
    slugs: Collection[str],
    levels: Mapping[str, int],
    guidelines: Collection[str],
) -> ProofTest:
    """One entry of a proof plan as a ProofTest, or a StoryError saying what is wrong with it. A level the
    repository's test tree lacks is its one level when it has only one (a flat tests/ directory)."""
    if not isinstance(t, dict):
        raise StoryError(f"{where} must be an object")
    crit = t.get("criterion")
    if crit is not None and str(crit) not in criteria:
        raise StoryError(f"{where}: criterion {crit!r} is not in the story; it has {sorted(criteria)}")
    layer = _field(t, "layer", where)
    if layer not in slugs:
        raise StoryError(f"{where}: layer {layer!r} is not in the split; the layers are {sorted(slugs)}")
    if t.get("gate") is True:
        if crit is None:
            raise StoryError(f"{where}: a gate entry names the criterion the gate proves")
        return ProofTest(str(crit), layer, "", "", "gate", None, GATE_ASSERTS, "", gate=True)
    level = _field(t, "level", where)
    if level not in levels and len(levels) == 1:
        level = next(iter(levels))  # the one level there is
    if level not in levels:
        raise StoryError(
            f"{where}: level {level!r} is no directory of the repository's test tree; "
            f"its levels are {sorted(levels)}"
        )
    file = _test_path(_field(t, "file", where), level, levels, where)
    name = _one_line(_field(t, "name", where), f"{where}: name", 200)
    if not is_js_test(file):  # unittest's own spelling, Class.test_x, is the id's Class::test_x
        name = _DOTTED_METHOD.sub(r"\1::\2", name)
    guideline = _field(t, "guideline", where, required=False) or None
    if guideline is not None and guideline not in guidelines:
        raise StoryError(
            f"{where}: guideline {guideline!r} is no file of the guideline library "
            f"({sorted(guidelines)}); omit it when none fits"
        )
    return ProofTest(
        None if crit is None else str(crit),
        layer,
        file,
        name,
        level,
        guideline,
        _field(t, "asserts", where),
        _field(t, "needs", where, required=False),
    )


_DOTTED_METHOD = re.compile(r"^([A-Za-z_]\w*)\.(test\w*)$")
"""A pytest name written as unittest writes it (RomanTest.test_x): the proof's ids are file::Class::test_x,
and a package's covers name them that way, so the dotted form matched none (roman, 2026-10-28: a package
sent back twice for 7 tests it covered, then blocked)."""


def parse_proof_entries(
    obj: dict[str, Any] | None,
    story: Story,
    layers: tuple[Layer, ...],
    levels: Mapping[str, int],
    guidelines: Collection[str],
) -> tuple[tuple[ProofTest, ...], list[str]]:
    """The proof plan's usable entries and what is wrong with each of the others, entry by entry: one bad
    entry no longer costs the plan its good ones (the roman bench, 2026-10-26: three proofs refused whole for
    one entry each, and the meeting ended with none). A StoryError only when there is no plan to read: no
    JSON, a type that is not "proof", or no tests list. Whether every criterion and layer has a test is the
    checker's to say."""
    if obj is None:
        raise StoryError(NO_JSON)
    if obj.get("type") != "proof":
        raise StoryError('the JSON object needs "type": "proof"')
    raw = obj.get("tests")
    if not isinstance(raw, list) or not raw:
        raise StoryError('the proof needs a non-empty "tests" list')
    criteria = {c.id for c in story.acceptance}
    slugs = {x.slug for x in layers}
    tests: list[ProofTest] = []
    problems: list[str] = []
    seen: set[str] = set()
    for n, t in enumerate(raw, 1):
        try:
            test = _proof_entry(t, f"proof test {n}", criteria, slugs, levels, guidelines)
        except StoryError as e:
            problems.append(str(e))
            continue
        if test.id in seen:
            problems.append(f"test id {test.id} appears twice in the proof; give each test its own name")
            continue
        seen.add(test.id)
        tests.append(test)
    return tuple(tests), problems


def parse_proof(
    obj: dict[str, Any] | None,
    story: Story,
    layers: tuple[Layer, ...],
    levels: Mapping[str, int],
    guidelines: Collection[str],
) -> tuple[ProofTest, ...]:
    """The tester's proof plan as ProofTests, all of it or a StoryError (the reason for the next try): every
    entry must parse (`parse_proof_entries`), and every criterion of the story and every layer needs a
    test (a gate entry proves its criterion; it is no layer's test)."""
    tests, problems = parse_proof_entries(obj, story, layers, levels, guidelines)
    if problems:
        raise StoryError("; ".join(problems))
    criteria = {c.id for c in story.acceptance}
    slugs = {x.slug for x in layers}
    missing = [f"criterion {c} has no test" for c in sorted(criteria - {t.criterion for t in tests})]
    missing += [f"layer '{s}' has no test" for s in sorted(slugs - {t.layer for t in tests if not t.gate})]
    if missing:
        raise StoryError("; ".join(missing) + ": every criterion and every layer needs a test in the proof")
    return tests


@dataclass(frozen=True)
class Objection:
    """One objection to a part: what it is about (a criterion id, a layer slug, a test id), the claim, the
    suggestion, and the role that raised it (`by`)."""

    part: Part
    about: str
    claim: str
    suggest: str
    by: str = ""

    def line(self) -> str:
        who = f"{self.by} on " if self.by else ""
        return f"{who}{self.part} ({self.about}): {self.claim} Suggested: {self.suggest}"


CRITIC_CONTRACT = """End your reply with one ```json block holding exactly one object:
{"type": "verdict", "proof": "agree" | "object", "objections": [{"about": "tests/unit/test_x.py::test_name", "claim": "one sentence", "suggest": "one sentence"}]}
Object only to a test that is missing, weakened, at another level or with other state than the agreed proof names, or to a test the proof does not name."""  # noqa: E501


@dataclass(frozen=True)
class Verdict:
    """A speaker's closing verdict on the three parts, with the objections for the parts it objects to."""

    role: str
    requirements: str
    changes: str
    proof: str
    objections: tuple[Objection, ...] = ()

    def agrees_to_all(self) -> bool:
        return self.requirements == self.changes == self.proof == "agree"

    def of(self, part: Part) -> str:
        return cast(str, getattr(self, part))


def parse_verdict(text: str, role: str) -> Verdict:
    """The verdict at the end of `role`'s turn: the object under the last ```json fence, whatever prose or
    channel text surrounds it (Muse wraps its answer). A StoryError, which the checker posts as a finding on
    the next turn, when there is no verdict, a value is not agree|object, an objection is for the role's own
    part or for a part it agrees to, or a part is objected to without an objection."""
    obj = extract_json_block(text)
    if obj is None:
        raise StoryError("no verdict: end your turn with the verdict JSON object, in one ```json block")
    if obj.get("type") != "verdict":
        raise StoryError(
            'the last JSON block of your turn is not your verdict ("type": "verdict"); put it last'
        )
    values: dict[str, str] = {}
    for part in PARTS:
        v = obj.get(part)
        if v not in ("agree", "object"):
            raise StoryError(f"verdict: '{part}' must be agree or object, not {v!r}")
        values[part] = v
    raw = obj.get("objections") or []
    if not isinstance(raw, list):
        raise StoryError("verdict: 'objections' must be a list")
    objections: list[Objection] = []
    for n, o in enumerate(raw, 1):
        where = f"objection {n}"
        if not isinstance(o, dict):
            raise StoryError(f"{where} must be an object")
        raw_part = o.get("part")
        if raw_part not in PARTS:
            raise StoryError(f"{where}: part must be one of {list(PARTS)}, not {raw_part!r}")
        part = cast(Part, raw_part)
        if OWNER[part] == role:
            raise StoryError(
                f"{where}: {part} is your own part ({role} owns it); change it in your turn, "
                "never object to it"
            )
        if values[part] == "agree":
            raise StoryError(
                f"{where} is for {part}, but your verdict agrees to {part}: say object, or drop it"
            )
        objections.append(
            Objection(
                part,
                _field(o, "about", where),
                _field(o, "claim", where),
                _field(o, "suggest", where),
                role,
            )
        )
    for part in PARTS:
        if values[part] == "object" and OWNER[part] == role:
            raise StoryError(f"verdict: you object to your own part, {part}")
        if values[part] == "object" and not any(o.part == part for o in objections):
            raise StoryError(
                f"verdict: you object to {part} but give no objection for it (part, about, claim, suggest)"
            )
    return Verdict(role, values["requirements"], values["changes"], values["proof"], tuple(objections))


def parse_critic_verdict(text: str) -> tuple[Objection, ...]:
    """The tester critic's verdict on a layer's package (D9): the object under the last ```json fence. The
    objections it raises (none when it agrees) are about the architect's tests against the agreed proof, so
    the tester's own-part rule of `parse_verdict` does not apply. A StoryError when there is no usable
    verdict: the pipeline then goes on to the mechanical checks, the critic being advisory."""
    obj = extract_json_block(text)
    if obj is None:
        raise StoryError("no verdict: end your reply with the verdict JSON object, in one ```json block")
    if obj.get("type") != "verdict":
        raise StoryError('the last JSON block of your reply is not your verdict ("type": "verdict")')
    value = obj.get("proof")
    if value not in ("agree", "object"):
        raise StoryError(f"verdict: 'proof' must be agree or object, not {value!r}")
    if value == "agree":
        return ()
    raw = obj.get("objections")
    if not isinstance(raw, list) or not raw:
        raise StoryError("verdict: you object but give no objection (about, claim, suggest)")
    out: list[Objection] = []
    for n, o in enumerate(raw, 1):
        where = f"objection {n}"
        if not isinstance(o, dict):
            raise StoryError(f"{where} must be an object")
        out.append(
            Objection(
                "proof",
                _field(o, "about", where),
                _field(o, "claim", where),
                _field(o, "suggest", where, required=False),
                "tester",
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class Agreement:
    """What the meeting ends with: the story as product finally words it, the layers as the architect
    finally splits them, the proof plan, the number of turns, the objections still open at the end, the parts
    their owner decided (not agreed by all), and the criteria product dropped, with why (D1, D8, D13)."""

    story: Story
    layers: tuple[Layer, ...]
    proof: tuple[ProofTest, ...]
    turns: int = 0
    unresolved: tuple[Objection, ...] = ()
    decided_by_owner: tuple[Part, ...] = ()
    dropped: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.dropped is None:
            object.__setattr__(self, "dropped", {})

    def to_dict(self) -> dict[str, Any]:
        """JSON-native (lists, not tuples), so the agreement in memory equals the one read back from the
        state file."""
        native: dict[str, Any] = json.loads(
            json.dumps(
                {
                    "story": self.story.to_dict(),
                    "layers": [x.to_dict() for x in self.layers],
                    "proof": [t.to_dict() for t in self.proof],
                    "turns": self.turns,
                    "unresolved": [asdict(o) for o in self.unresolved],
                    "decided_by_owner": list(self.decided_by_owner),
                    "dropped": dict(self.dropped),
                }
            )
        )
        return native

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Agreement:
        return cls(
            Story.from_dict(d["story"]),
            tuple(Layer.from_dict(x) for x in d["layers"]),
            tuple(ProofTest.from_dict(t) for t in d["proof"]),
            int(d.get("turns") or 0),
            tuple(Objection(**o) for o in d.get("unresolved") or ()),
            tuple(d.get("decided_by_owner") or ()),
            {str(k): str(v) for k, v in (d.get("dropped") or {}).items()},
        )

    def proof_for(self, layer_slug: str) -> tuple[ProofTest, ...]:
        """The proof tests of one layer, in the plan's order."""
        return tuple(t for t in self.proof if t.layer == layer_slug)


_JSON_FENCE = re.compile(r"```json[ \t]*\r?\n", re.IGNORECASE)


def json_blocks(text: str) -> list[dict[str, Any]]:
    """Every JSON object a turn posts under a ```json fence, in order; a fence whose body is not an object is
    left out (the contract it was meant for is then missing from the turn, which the checker reports)."""
    out: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for m in _JSON_FENCE.finditer(text):
        body = text[m.end() :].lstrip()
        if not body.startswith("{"):
            continue
        try:
            obj, _ = decoder.raw_decode(body)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def parse_story_revision(obj: dict[str, Any]) -> tuple[Story, dict[str, str]]:
    """Product's revised story, and the criteria it dropped with why: a criterion with `"dropped": "why"`
    leaves the story (it keeps its id in the record; ids are never renumbered, D13)."""
    kept: list[Any] = []
    dropped: dict[str, str] = {}
    raw = obj.get("acceptance")
    for c in raw if isinstance(raw, list) else ():
        if isinstance(c, dict) and c.get("dropped"):
            dropped[str(c.get("id") or "?")] = _encodable(
                str(c["dropped"]).strip(), "a dropped criterion's why"
            )
        else:
            kept.append(c)
    return parse_story({**obj, "acceptance": kept if isinstance(raw, list) else raw}), dropped


# ---- the meeting -------------------------------------------------------------------------------------------

CHECKER = "airlie"
"""The checker's name in the group chat: the fourth participant, with no model."""
ROLES: tuple[str, ...] = ("product", "architect", "tester")
"""The three amigos, in turn order; the checker speaks fourth."""
_PARTICIPANTS = (*ROLES, CHECKER)

ClientFactory = Callable[[str, RoleSpec], ChatCompletionClient]
"""Role name and its spec -> the model client that role's agent speaks through."""


class Meets(Protocol):
    """What the pipeline needs of a meeting: `Meeting`, or a double in a test."""

    async def run(
        self,
        opening: str,
        story: Story,
        layers: tuple[Layer, ...],
        levels: Mapping[str, int],
        guidelines: Collection[str],
        *,
        repo: Path,
        base: str,
        state: Mapping[str, Any] | None = None,
    ) -> Agreement: ...


MeetingFactory = Callable[
    [AirlieConfig, Callable[[str], ToolRegistry], Callable[["Turn", Mapping[str, Any]], None]], Meets
]
"""Makes a story's meeting from the config, the read-only tools for a role over the story's base and the
callback for each turn. The pipeline's default is `default_meeting`."""


@dataclass
class Turn:
    """One turn of the meeting as the pipeline sees it: who spoke, what, the verdict that ended it (None when
    it had no usable one), the parts it carried (`requirements`: a Story, `changes`: layers, `proof`: tests,
    `dropped`: criteria product dropped, `blocked`: why product says the request cannot be built), the
    problems it had (a verdict or a part that did not parse), and, for the checker's turn, its findings."""

    role: str
    text: str
    verdict: Verdict | None = None
    parts: dict[str, Any] = field(default_factory=dict)
    problems: tuple[Finding, ...] = ()
    findings: tuple[Finding, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_ms: float = 0.0


@dataclass(frozen=True)
class ReadRules:
    """What a turn's parts are parsed against: the most layers a split may have, the repository's test
    levels and the guidelines the tester may name."""

    max_layers: int
    levels: Mapping[str, int]
    guidelines: Collection[str]


@dataclass(frozen=True)
class Standing:
    """The parts as they stand: the last valid version of each, by its owner. `proof` is None until the
    tester has posted a valid one."""

    story: Story
    layers: tuple[Layer, ...]
    proof: tuple[ProofTest, ...] | None = None
    dropped: Mapping[str, str] = field(default_factory=dict)

    def after(self, turn: Turn) -> Standing:
        """These parts with the ones `turn` carried in place of the ones they replace."""
        return Standing(
            turn.parts.get("requirements", self.story),
            turn.parts.get("changes", self.layers),
            turn.parts.get("proof", self.proof),
            {**self.dropped, **turn.parts.get("dropped", {})},
        )


def part_of(obj: Mapping[str, Any]) -> Part | None:
    """Which part a posted JSON object is, by its shape; None for a verdict or anything else."""
    kind = obj.get("type")
    if kind in ("story", "blocked"):
        return "requirements"
    if kind == "proof":
        return "proof"
    if kind != "verdict" and "layers" in obj:
        return "changes"
    return None


def read_turn(role: str, text: str, standing: Standing, rules: ReadRules) -> Turn:
    """A role's turn as data. The verdict is the last ```json fence; each other fence that is a part is
    parsed against the parts as they stood (a part is only its owner's to post). A reply that does not
    parse is a Finding for the next checker turn (`verdict_missing`, `part_invalid`), never an exception."""
    problems: list[Finding] = []
    verdict: Verdict | None = None
    try:
        verdict = parse_verdict(text, role)
    except StoryError as e:
        problems.append(Finding("verdict_missing", role, str(e), ""))
    parts: dict[str, Any] = {}
    story, layers = standing.story, standing.layers
    for obj in json_blocks(text):
        part = part_of(obj)
        if part is None:
            continue
        if OWNER[part] != role:
            problems.append(
                Finding(
                    "part_invalid",
                    role,
                    f"you posted the {part}, which is {OWNER[part]}'s part; only its owner changes it: "
                    "object with a claim and a suggestion instead",
                    "",
                )
            )
            continue
        try:
            if obj.get("type") == "blocked":
                parts["blocked"] = str(obj.get("reason") or "no reason given")
            elif part == "requirements":
                story, dropped = parse_story_revision(obj)
                parts["requirements"], parts["dropped"] = story, dropped
            elif part == "changes":
                layers = parse_layers(obj, story, rules.max_layers)
                parts["changes"] = layers
            else:
                proof, wrong = parse_proof_entries(obj, story, layers, rules.levels, rules.guidelines)
                problems += [Finding("part_invalid", role, f"in your proof: {w}", "") for w in wrong]
                if proof:  # its usable tests stand; the checker names what is still unproven
                    parts["proof"] = proof
        except StoryError as e:
            problems.append(Finding("part_invalid", role, f"your {part} was not used: {e}", ""))
    return Turn(role, text, verdict, parts, tuple(problems))


def latest_parts(
    turns: Sequence[Turn],
    story: Story,
    layers: tuple[Layer, ...],
    proof: tuple[ProofTest, ...] | None,
) -> tuple[Story, tuple[Layer, ...], tuple[ProofTest, ...]]:
    """The last valid version of each part by its owner, from the opening parts on; the proof is () when
    the tester never posted a valid one."""
    standing = Standing(story, layers, proof)
    for turn in turns:
        standing = standing.after(turn)
    return standing.story, standing.layers, standing.proof or ()


class Checker(BaseChatAgent):
    """airlie's participant with no model: it speaks last in each round, re-reads the parts as they stand
    from the turn log the meeting keeps, and posts the problems it finds (`amigos_checks`) with the problems
    the turns since its last one carried, or "no findings". It always judges the parts as they stand, so a
    finding the owner has not fixed is posted again, and "no findings" is never a silence over a problem."""

    def __init__(
        self,
        *,
        turns: list[Turn],
        opening: Standing,
        repo: Path,
        base: str,
        max_scope_files: int,
    ) -> None:
        super().__init__(CHECKER, "airlie's mechanical checks on the three parts of the agreement")
        self._turns = turns
        self._opening = opening
        self._repo = repo
        self._base = base
        self._max_scope_files = max_scope_files
        self.last: tuple[Finding, ...] = ()
        """The findings of the latest turn, for the meeting's record."""

    @property
    def produced_message_types(self) -> Sequence[type[BaseChatMessage]]:
        return (TextMessage,)

    async def on_messages(
        self, messages: Sequence[BaseChatMessage], cancellation_token: CancellationToken
    ) -> Response:
        standing = self._opening
        since: list[Turn] = []
        for turn in self._turns:
            if turn.role == CHECKER:
                since = []
                continue
            standing = standing.after(turn)
            since.append(turn)
        found = [p for turn in since for p in turn.problems]
        if standing.proof is None:
            found.append(Finding("part_invalid", "tester", no_proof_why(self._turns), ""))
        else:
            found += check_agreement(
                standing.story, standing.layers, standing.proof, self._repo, self._base, self._max_scope_files
            )
        self.last = tuple(found)
        return Response(chat_message=TextMessage(content=findings_message(found), source=self.name))

    async def on_reset(self, cancellation_token: CancellationToken) -> None:
        self.last = ()


NO_PROOF = (
    "no proof plan has been posted yet: the tester posts it, whole, in its own ```json block "
    '{"type": "proof", "tests": [...]} before its verdict; saying a proof "stands as posted" posts nothing'
)


def no_proof_why(turns: Sequence[Turn]) -> str:
    """What the checker tells the tester while no proof is on record, naming any role that posted one in
    its place (roman, 2026-10-27: the tester's first turn ended with no part, product posted the proof in
    the tester's voice, and the tester, taking that post for its own, said for 25 turns that its proof
    "stands as posted")."""
    posted = sorted({t.role for t in turns for p in t.problems if p.why.startswith("you posted the proof")})
    if not posted:
        return NO_PROOF
    return f"{NO_PROOF}. The proof {' and '.join(posted)} posted is not the tester's and was not used"


class OneTurn(TerminationCondition):
    """Stops the group chat after each participant's turn, so the meeting's state is saved between turns,
    where it is consistent (mid-turn, AutoGen's round robin has already moved on to the next speaker)."""

    def __init__(self) -> None:
        self._stopped = False

    @property
    def terminated(self) -> bool:
        return self._stopped

    async def __call__(self, messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> StopMessage | None:
        if self._stopped:
            raise TerminatedException("Termination condition has already been reached")
        if any(isinstance(m, TextMessage) and m.source in _PARTICIPANTS for m in messages):
            self._stopped = True
            return StopMessage(content="one turn", source="OneTurn")
        return None

    async def reset(self) -> None:
        self._stopped = False


class AgreementReached(TerminationCondition):
    """The meeting's end by agreement: the latest message of each of the three roles carries a verdict that
    agrees to every part, none of them was written before the last part was changed (a verdict on a plan its
    speaker has not seen is no agreement; a part posted again unchanged changes nothing, lever 3 of the roman
    bench, 2026-10-26), and the checker's latest message, after that, is "no findings".
    It keeps the history it sees across the one-turn runs, and `observe` feeds it a saved thread."""

    def __init__(self) -> None:
        self._seq = 0
        self._verdicts: dict[str, tuple[int, Verdict | None]] = {}
        self._revised = 0
        self._posted: dict[Part, str] = {}
        """Each part as its owner last posted it, as canonical JSON: posted again unchanged, no revision."""
        self._checker: tuple[int, bool] | None = None
        self._stopped = False

    @property
    def terminated(self) -> bool:
        return self._stopped

    def observe(self, messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> None:
        """Take in messages without deciding anything: the history a resumed meeting replays."""
        for m in messages:
            if not isinstance(m, TextMessage) or m.source not in _PARTICIPANTS:
                continue
            self._seq += 1
            if m.source == CHECKER:
                # its first line: advice may follow "no findings" (ADVISORY), and holds nothing up
                self._checker = (self._seq, m.content.strip().split("\n", 1)[0] == NO_FINDINGS)
                continue
            try:
                verdict: Verdict | None = parse_verdict(m.content, m.source)
            except StoryError:
                verdict = None
            self._verdicts[m.source] = (self._seq, verdict)
            for obj in json_blocks(m.content):
                part = part_of(obj)
                if part is None or OWNER[part] != m.source:
                    continue
                text = json.dumps(obj, sort_keys=True)
                if self._posted.get(part) != text:  # a part posted again as it stands leaves the verdicts
                    self._posted[part] = text
                    self._revised = self._seq

    def agreed(self) -> bool:
        """Whether the meeting as observed so far has reached agreement."""
        for role in ROLES:
            seq, verdict = self._verdicts.get(role, (0, None))
            if verdict is None or not verdict.agrees_to_all() or seq < self._revised:
                return False
        return self._checker is not None and self._checker[1] and self._checker[0] > self._revised

    async def __call__(self, messages: Sequence[BaseAgentEvent | BaseChatMessage]) -> StopMessage | None:
        if self._stopped:
            raise TerminatedException("Termination condition has already been reached")
        self.observe(messages)
        if self.agreed():
            self._stopped = True
            return StopMessage(content="the three agree", source="AgreementReached")
        return None

    async def reset(self) -> None:
        self._stopped = False  # the history stays: the meeting is one conversation


def _lessons_text(lessons: Sequence[Lesson]) -> str:
    from airlie.story.briefs import _lesson_line  # briefs imports this module's contracts at its top

    return hub_text("brief-layers-lessons") + "\n" + "\n".join(_lesson_line(x) for x in reversed(lessons))


def _as_turn_part(contract: str) -> str:
    """A part's output contract as it reads inside a turn: posted as a block, then the turn ends with the
    verdict (the contracts say "end your reply with" when they are a role's whole answer)."""
    return contract.replace("End your reply with one", "In your turn, post it as one", 1)


def opening_message(
    story: Story,
    layers: Sequence[Layer],
    conventions_text: str,
    lessons: Sequence[Lesson],
    dossier: Mapping[str, str] | None = None,
) -> str:
    """The meeting's first message, which every agent reads: its purpose and rules (`amigos-opening`), the
    story as product refined it, the architect's split, the repository's testing conventions, the lessons
    recorded for the repository when there are any, and the contracts each part and the verdict travel in."""
    from airlie.story.briefs import (  # as in _lessons_text
        LAYERS_CONTRACT,
        STORY_CONTRACT,
        dossier_section,
        story_text,
    )

    split = json.dumps({"layers": [x.to_dict() for x in layers]}, indent=1)
    parts = [
        hub_text("amigos-opening"),
        "## Product's story: the requirements as they stand\n" + story_text(story),
        "## The architect's split: the changes as they stand\n```json\n" + split + "\n```",
        "## The proof\nThe tester has not posted it yet: its first turn proposes the proof plan. Only the "
        "tester posts it: product and architect never post a proof block, not even to help; they object "
        "to it once it is posted.",
        conventions_text,
        *dossier_section(dossier or {}),
        *([_lessons_text(lessons)] if lessons else []),
        "## How each part and your verdict travel\n"
        + "\n\n".join(
            [_as_turn_part(STORY_CONTRACT), _as_turn_part(LAYERS_CONTRACT), PROOF_CONTRACT, VERDICT_CONTRACT]
        ),
    ]
    return "\n\n".join(parts)


def _thread_messages(state: Mapping[str, Any]) -> list[TextMessage]:
    """The text messages of the meeting's saved thread, in order, from the round robin's manager state."""
    thread = state.get("agent_states", {}).get("RoundRobinGroupChatManager", {}).get("message_thread", [])
    return [
        TextMessage(content=str(m.get("content", "")), source=str(m["source"]))
        for m in thread
        if m.get("type") == "TextMessage"
    ]


_FINDING_PART: dict[str, Part] = {
    "criterion_unproven": "proof",
    "layer_unproven": "proof",
    "proof_layer_mismatch": "proof",
    "split_invalid": "changes",
    "over_size": "changes",
    "file_missing": "changes",
}


def _finding_objection(f: Finding, layer_slugs: Collection[str]) -> Objection | None:
    """A checker finding that stands at the end, as an Objection on the part it is about (D4); None for a
    kind that is about a turn, not a part."""
    part: Part | None = _FINDING_PART.get(f.kind)
    if f.kind == "stale_reference":
        part = "changes" if f.test in layer_slugs else "proof"
    if part is None:
        return None
    return Objection(part, f.test, f.why, f"{OWNER[part]} fixes it", CHECKER)


class Meeting:
    """One meeting per story: product, the architect and the tester as three `AssistantAgent`s and airlie's
    `Checker` in a `RoundRobinGroupChat`, each agent's conversation continuing for the whole meeting (its
    system prompt, then the shared history, append-only, so the engine's prefix cache serves every turn
    after the first). The chat is driven one turn at a time and its state saved between turns, where it is
    consistent: `on_turn` gets every turn and the team's state, for the pipeline to persist and emit; a
    story killed mid-meeting resumes by loading that state into fresh agents.

    The meeting ends when `AgreementReached`, or at `max_turns` turns (the checker's count), when each
    owner's part stands, the parts not agreed by all are `decided_by_owner` and the objections still open
    are `unresolved` (D8)."""

    def __init__(
        self,
        cfg: AirlieConfig,
        *,
        clients: ClientFactory,
        tools: Callable[[str], ToolRegistry] | None,
        max_turns: int,
        tool_iterations: int,
        on_turn: Callable[[Turn, Mapping[str, Any]], None],
        max_scope_files: int = 5,
    ) -> None:
        self.cfg = cfg
        self.clients = clients
        self.tools = tools
        self.max_turns = max_turns
        self.tool_iterations = tool_iterations
        self.on_turn = on_turn
        self.max_scope_files = max_scope_files
        self.before_turn: Callable[[], Awaitable[Any]] | None = None
        """Awaited before each turn of a role (not the checker's): the planning lane's long turns wait for
        the build lane's gap here (planning-line P5b, D2)."""

    def _agent(self, role: str) -> tuple[AssistantAgent, ChatCompletionClient]:
        spec = self.cfg.roles[role]
        client = self.clients(role, spec)
        registry = self.tools(role) if self.tools is not None and self.tool_iterations > 0 else None
        tools = build_function_tools(registry) if registry is not None else []
        system = "\n\n".join(
            [prompt_text(spec.prompt), hub_text("amigos-turn"), VERDICT_CONTRACT, hub_text(f"amigos-{role}")]
        )
        agent = AssistantAgent(
            role,
            model_client=client,
            tools=tools or None,
            system_message=system,
            reflect_on_tool_use=bool(tools),
            max_tool_iterations=max(self.tool_iterations, 1),
        )
        return agent, client

    async def run(
        self,
        opening: str,
        story: Story,
        layers: tuple[Layer, ...],
        levels: Mapping[str, int],
        guidelines: Collection[str],
        *,
        repo: Path,
        base: str,
        state: Mapping[str, Any] | None = None,
    ) -> Agreement:
        """Hold the meeting on `story` and `layers` (the opening split), `opening` being what
        `opening_message` made. `repo` and `base` are where the checker reads the files the split names.
        `state` is the saved team state of a meeting that was cut short: it goes on from the next speaker.
        Raises NotActionable when product says the request cannot be built as written."""
        rules = ReadRules(self.cfg.story.max_layers, levels, guidelines)
        start = Standing(story, layers)
        turns: list[Turn] = []
        agents = {role: self._agent(role) for role in ROLES}
        checker = Checker(
            turns=turns, opening=start, repo=repo, base=base, max_scope_files=self.max_scope_files
        )
        agreement = AgreementReached()
        participants: list[ChatAgent | Team] = [*(agents[r][0] for r in ROLES), checker]
        team = RoundRobinGroupChat(participants, termination_condition=OneTurn() | agreement)
        standing = start
        try:
            if state is not None:
                await team.load_state(state)
                for msg in _thread_messages(state):
                    if msg.source in ROLES:
                        turn = read_turn(msg.source, msg.content, standing, rules)
                    elif msg.source == CHECKER:
                        turn = Turn(CHECKER, msg.content)
                    else:
                        continue
                    turns.append(turn)
                    standing = standing.after(turn)
                    agreement.observe([msg])
            while len(turns) < self.max_turns and not agreement.agreed():
                if self.before_turn is not None and len(turns) % len(_PARTICIPANTS) != len(_PARTICIPANTS) - 1:
                    await self.before_turn()  # the checker, who speaks fourth, is code
                t0 = time.monotonic()
                said: TextMessage | None = None
                task = opening if not turns and state is None else None
                async for item in team.run_stream(task=task):
                    if isinstance(item, TextMessage) and item.source in _PARTICIPANTS:
                        said = item
                if said is None:
                    raise RuntimeError("the story meeting's turn produced no message")
                if said.source == CHECKER:
                    turn = Turn(CHECKER, said.content, findings=checker.last)
                else:
                    turn = read_turn(said.source, said.content, standing, rules)
                usage = said.models_usage
                turn.prompt_tokens = usage.prompt_tokens if usage else 0
                turn.completion_tokens = usage.completion_tokens if usage else 0
                turn.wall_ms = (time.monotonic() - t0) * 1000.0
                turns.append(turn)
                standing = standing.after(turn)
                if "blocked" in turn.parts:
                    raise NotActionable(turn.parts["blocked"])
                self.on_turn(turn, await team.save_state())
        finally:
            for _, client in agents.values():
                await client.close()
        return self._agreement(turns, standing, agreement.agreed())

    @staticmethod
    def _agreement(turns: Sequence[Turn], standing: Standing, agreed: bool) -> Agreement:
        """The agreement the meeting ends with: each part as its owner last had it, the objections open in the
        latest verdicts and the findings that stand, and (unless all three agreed) the parts not agreed."""
        latest: dict[str, Verdict] = {}
        for turn in turns:
            if turn.verdict is not None:
                latest[turn.role] = turn.verdict
        open_ = [o for v in latest.values() for o in v.objections]
        last_check = next((t for t in reversed(turns) if t.role == CHECKER), None)
        slugs = {x.slug for x in standing.layers}
        if last_check is not None:
            open_ += [o for f in last_check.findings if (o := _finding_objection(f, slugs)) is not None]
        decided: tuple[Part, ...] = ()
        if not agreed:
            decided = tuple(
                p
                for p in PARTS
                if len(latest) < len(ROLES)
                or any(v.of(p) == "object" for v in latest.values())
                or any(o.part == p for o in open_)
            )
        return Agreement(
            standing.story,
            standing.layers,
            standing.proof or (),
            len(turns),
            tuple(open_),
            decided,
            dict(standing.dropped),
        )


# ---- settling the agreement and saying it ------------------------------------------------------------------


def prune_references(agreement: Agreement) -> Agreement:
    """The agreement with no reference to a criterion its story does not have (D13): a layer's `acceptance`
    loses the ids product dropped, and a proof test that proved one is dropped, unless it is its layer's only
    test, when it stays as a test of the layer's behaviour (`criterion` None). The meeting asks the owners to
    fix these themselves; this is what stands when the turns ran out first, so nothing downstream ever reads
    a dangling id."""
    ids = {c.id for c in agreement.story.acceptance}
    layers = tuple(
        replace(x, acceptance=tuple(a for a in x.acceptance if a in ids)) for x in agreement.layers
    )

    def kept(t: ProofTest) -> bool:
        return t.criterion is None or t.criterion in ids

    proof: list[ProofTest] = []
    for t in agreement.proof:
        if kept(t):
            proof.append(t)
        elif not any(u.layer == t.layer and kept(u) for u in agreement.proof):
            proof.append(replace(t, criterion=None))
    return replace(agreement, layers=layers, proof=tuple(proof))


def unbuildable(agreement: Agreement) -> list[Finding]:
    """What makes an agreement impossible to build, whatever the meeting decided: a criterion no layer owns
    or two own (`split_invalid`), a criterion or a layer with no test, a test in a layer that does not own
    its criterion. The package call would fail on each; owner-decides does not stretch to them."""
    return [
        *split_problems(agreement.layers, agreement.story),
        *check_proof(agreement.proof, agreement.story, agreement.layers),
    ]


def _pipe(text: str) -> str:
    """`text` as a Markdown table cell: each pipe escaped."""
    return text.replace("|", "\\|")


def agreement_note(agreement: Agreement) -> str:
    """The agreement for a person, as the card's comment: the criteria, the layers bottom first with their
    files and interfaces, and the proof as a table."""
    lines = [
        f"agreement of product, the architect and the tester ({agreement.turns} turns)",
        "criteria:",
        *(f"- {c.line()}" for c in agreement.story.acceptance),
        "layers, bottom first:",
    ]
    for n, x in enumerate(agreement.layers, 1):
        owns = ", ".join(x.acceptance) or "enabling: no criterion"
        files = ", ".join(f"{f.path} (new)" if f.new else f.path for f in x.files) or x.scope
        lines.append(f"{n}. {x.title} ({owns}): {files}")
        lines += [f"   interface: {i}" for i in x.interfaces]
    lines += [
        "proof:",
        "| criterion | layer | level | test | asserts |",
        "| --- | --- | --- | --- | --- |",
        *(
            f"| {t.criterion or '-'} | {t.layer} | {t.level} | {t.id} | {_pipe(t.asserts)} |"
            for t in agreement.proof
        ),
    ]
    if agreement.decided_by_owner:
        lines.append("decided by their owner at the turn cap: " + ", ".join(agreement.decided_by_owner))
    return "\n".join(lines)


def criteria_note(original: Story, agreement: Agreement) -> str | None:
    """Which criteria the meeting reworded, added or dropped, as the card's comment; None when it changed
    none."""
    before = {c.id: c for c in original.acceptance}
    after = {c.id: c for c in agreement.story.acceptance}
    lines = [f"reworded: {c.line()}" for i, c in after.items() if i in before and before[i] != c]
    lines += [f"added: {c.line()}" for i, c in after.items() if i not in before]
    dropped = [i for i in before if i not in after]
    lines += [f"dropped: {i} ({agreement.dropped.get(i, 'no reason given')})" for i in dropped]
    if not lines:
        return None
    return "\n".join(["the meeting changed the criteria:", *lines])


def unresolved_note(agreement: Agreement) -> str | None:
    """The objections still open when the meeting ended, as the card's comment; None when there are none."""
    if not agreement.unresolved:
        return None
    return "\n".join(
        [
            "objections still open when the meeting ended (each owner's part stands):",
            *(f"- {o.line()}" for o in agreement.unresolved),
        ]
    )


# ---- the real meeting's wiring ----------------------------------------------------------------------------


def http_clients(cfg: AirlieConfig) -> ClientFactory:
    """The clients of a real meeting: each role's AutoGen client pointed at the engine that serves its model,
    as `Orchestrator._default_team` points the AutoGen team's."""

    def make(role_name: str, spec: RoleSpec) -> ChatCompletionClient:
        engine = cfg.engines[cfg.engine_for_model(spec.model)]
        return build_model_client(
            engine.base_url or "",
            engine.served_name(spec.model),
            spec,
            engine.api_key,
            model=cfg.models.get(spec.model),
        )

    return make


def default_meeting(
    cfg: AirlieConfig,
    tools: Callable[[str], ToolRegistry],
    on_turn: Callable[[Turn, Mapping[str, Any]], None],
) -> Meeting:
    """A meeting on the engine, as `story.amigos` configures it."""
    spec = cfg.story.amigos
    return Meeting(
        cfg,
        clients=http_clients(cfg),
        tools=tools,
        max_turns=spec.max_turns,
        tool_iterations=spec.tool_iterations,
        on_turn=on_turn,
        max_scope_files=spec.max_scope_files,
    )
