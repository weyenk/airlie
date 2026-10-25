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
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from airlie.story.model import (
    NO_JSON,
    Layer,
    Story,
    StoryError,
    _encodable,
    _one_line,
    parse_story,
)
from airlie.tools.scope import is_test_module
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
it; guideline is a file of the guideline library, or omitted when the repository or the level has none. Every
criterion needs at least one test and every layer at least one. criterion is null for a test that proves a
behaviour of a layer with no criterion."""  # noqa: E501

VERDICT_CONTRACT = """End every turn with your verdict, one ```json block holding exactly one object:
{"type": "verdict", "requirements": "agree" | "object", "changes": "agree" | "object", "proof": "agree" | "object",
 "objections": [{"part": "proof", "about": "AC2", "claim": "one sentence", "suggest": "one sentence"}]}
Object to a part only with an objection for it; never object to your own part."""  # noqa: E501


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

    @property
    def id(self) -> str:
        """Its pytest node id from the repository's root: "file::name"."""
        return f"{self.file}::{self.name}"

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
    `tests/` when the one level is `tests` itself; never absolute and never climbing out."""
    clean = path.removeprefix("./")
    parts = PurePosixPath(clean).parts
    if not clean or clean.startswith("/") or ".." in parts:
        raise StoryError(f"{where}: file {path!r} must be a path inside the repository, from its root")
    prefix = "tests/" if level == "tests" else f"tests/{level}/"
    if not clean.startswith(prefix):
        raise StoryError(f"{where}: file {path!r} is not under {prefix} (its level is '{level}')")
    if not is_test_module(clean):
        raise StoryError(f"{where}: file {path!r} is not a pytest test module (test_*.py or *_test.py)")
    return clean


def parse_proof(
    obj: dict[str, Any] | None,
    story: Story,
    layers: tuple[Layer, ...],
    levels: Mapping[str, int],
    guidelines: Collection[str],
) -> tuple[ProofTest, ...]:
    """The tester's proof plan as ProofTests. A StoryError (the reason for the next turn) when: there is no
    JSON, its type is not "proof", a test has a criterion the story lacks, a layer not in the split, a level
    the repository's test tree lacks, a file outside `tests/<level>/`, a guideline not in the library, or two
    tests share an id; and, all together, when a criterion of the story or a layer has no test."""
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
    seen: set[str] = set()
    for n, t in enumerate(raw, 1):
        where = f"proof test {n}"
        if not isinstance(t, dict):
            raise StoryError(f"{where} must be an object")
        crit = t.get("criterion")
        if crit is not None and str(crit) not in criteria:
            raise StoryError(f"{where}: criterion {crit!r} is not in the story; it has {sorted(criteria)}")
        layer = _field(t, "layer", where)
        if layer not in slugs:
            raise StoryError(f"{where}: layer {layer!r} is not in the split; the layers are {sorted(slugs)}")
        level = _field(t, "level", where)
        if level not in levels:
            raise StoryError(
                f"{where}: level {level!r} is no directory of the repository's test tree; "
                f"its levels are {sorted(levels)}"
            )
        file = _test_path(_field(t, "file", where), level, levels, where)
        name = _one_line(_field(t, "name", where), f"{where}: name", 200)
        guideline = _field(t, "guideline", where, required=False) or None
        if guideline is not None and guideline not in guidelines:
            raise StoryError(
                f"{where}: guideline {guideline!r} is no file of the guideline library "
                f"({sorted(guidelines)}); omit it when none fits"
            )
        test = ProofTest(
            None if crit is None else str(crit),
            layer,
            file,
            name,
            level,
            guideline,
            _field(t, "asserts", where),
            _field(t, "needs", where, required=False),
        )
        if test.id in seen:
            raise StoryError(f"test id {test.id} appears twice in the proof; give each test its own name")
        seen.add(test.id)
        tests.append(test)
    problems = [f"criterion {c} has no test" for c in sorted(criteria - {t.criterion for t in tests})]
    problems += [f"layer '{s}' has no test" for s in sorted(slugs - {t.layer for t in tests})]
    if problems:
        raise StoryError("; ".join(problems) + ": every criterion and every layer needs a test in the proof")
    return tuple(tests)


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
        return {
            "story": self.story.to_dict(),
            "layers": [x.to_dict() for x in self.layers],
            "proof": [t.to_dict() for t in self.proof],
            "turns": self.turns,
            "unresolved": [asdict(o) for o in self.unresolved],
            "decided_by_owner": list(self.decided_by_owner),
            "dropped": dict(self.dropped),
        }

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
