"""The layer package: what the architect writes just before a layer is built (P1 of the planning line,
2026-10-24). It holds the layer's tests as code (whole test files), the files the layer creates or changes,
the interfaces at its boundary, the steps a coder follows, and which test covers which criterion. The
pipeline writes the tests into the worktree, checks them (`package_checks`), locks them, and the coder builds
against them; no tester agent writes a test.

The architect's reply is model output, read as data: test files follow `### test file: <path>` headings in
fenced blocks (a test's own text may hold fences, so a block ends at the last fence line before the next
heading or the package object, not the first), and the package object is the last ```json fence of the reply,
as every role's reply is read. Nothing here executes the text; a path is checked to be inside the repository
before anything is written (`package_checks.place_package`)."""

from __future__ import annotations

import ast
import re
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, cast

from airlie.story.js_tests import JsParseError, js_tests
from airlie.story.layer_checks import test_functions
from airlie.story.model import NO_JSON, Layer, Story, StoryError, _encodable, _strs, _text
from airlie.tools.scope import is_js_test, is_test_file
from airlie.topology.studio_plan import extract_json_block

PACKAGE_CONTRACT = """Reply with, for each test file, a line `### test file: <path from the repository root>` followed by one fenced block (```python for a pytest module, ```ts for a Vitest *.test.ts) holding that file's whole text (the file as it must be after this layer: new, or the existing file with your tests added). Then end your reply with one ```json block holding exactly one object:
{"type": "package",
 "files": [{"path": "src/pkg/mod.py", "action": "create" | "modify", "why": "one line"}],
 "interfaces": [{"file": "src/pkg/mod.py", "signature": "def name(arg: T) -> R", "behaviour": "one line: what it does, the exact message or value the tests assert"}],
 "steps": ["ordered, one change each, naming the file and the function"],
 "covers": {"AC1": ["tests/unit/test_x.py::test_name"]},
 "done_when": "one sentence"}
Every criterion this layer owns appears in covers with at least one test; every test id names a test in one of your test files."""  # noqa: E501

AGREED_PROOF_CONTRACT = """`covers` names every agreed test, each under the criterion it proves (a test that proves no criterion goes under "behaviour"), written with the asserts and the state the plan names; a test beyond them is kept, checked like the rest, and reported. When the proof itself is wrong (a test cannot be made red in this layer, or asserts what the layer cannot do), reply instead with one ```json block holding {"type": "proof_change", "changes": [{"test": "<test id>", "why": "one sentence"}]} and no package. When an acceptance criterion itself is false (it states something untrue about the domain, or no correct code could meet it), name it in the same block as {"criterion": "<its id>", "why": "one sentence"}: product rewords it, then the tester revises the tests."""  # noqa: E501
BEHAVIOUR = "behaviour"
"""The key of `covers` a proof test with no criterion is listed under: a behaviour of its layer."""

_HEADING = re.compile(r"^### test file: (?P<path>\S+)\s*$")
_FENCE = re.compile(r"^(?P<fence>`{3,})(?:python|py|ts|typescript|tsx|js|javascript|jsx)?\s*$")
"""A test file's opening fence: ```python for a pytest module, ```ts (and kin) for a Vitest file, or bare."""
_JSON_OPENER = re.compile(r"^`{3,}json[ \t]*$", re.IGNORECASE)
_PARAMS = re.compile(r"\[.*\]$")
_ACTIONS = ("create", "modify")


@dataclass(frozen=True)
class PackageFile:
    """A source file the layer creates or changes, and why."""

    path: str
    action: Literal["create", "modify"]
    why: str = ""


@dataclass(frozen=True)
class Interface:
    """A signature the layer adds or changes in `file`, and what it does (the exact message or value the
    tests assert), so the coder names what the tests import."""

    file: str
    signature: str
    behaviour: str = ""


@dataclass(frozen=True)
class Package:
    """A layer's package: tests (path -> the file's whole text), files, interfaces, steps, `covers`
    (criterion id -> test ids "path::name") and `done_when`."""

    layer: str
    tests: Mapping[str, str]
    files: tuple[PackageFile, ...]
    interfaces: tuple[Interface, ...]
    steps: tuple[str, ...]
    covers: Mapping[str, tuple[str, ...]]
    done_when: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-native: lists for tuples, so a saved package equals the one read back."""
        return {
            "layer": self.layer,
            "tests": dict(self.tests),
            "files": [asdict(f) for f in self.files],
            "interfaces": [asdict(i) for i in self.interfaces],
            "steps": list(self.steps),
            "covers": {k: list(v) for k, v in self.covers.items()},
            "done_when": self.done_when,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Package:
        return cls(
            str(d["layer"]),
            {str(k): str(v) for k, v in d["tests"].items()},
            tuple(
                PackageFile(str(f["path"]), cast(Any, f["action"]), str(f.get("why") or ""))
                for f in d["files"]
            ),
            tuple(
                Interface(str(i["file"]), str(i["signature"]), str(i.get("behaviour") or ""))
                for i in d["interfaces"]
            ),
            tuple(str(s) for s in d["steps"]),
            {str(k): tuple(str(t) for t in v) for k, v in d["covers"].items()},
            str(d.get("done_when") or ""),
        )

    def with_tests(self, tests: Mapping[str, str]) -> Package:
        """The same package with other test text (ruff's fixes, carried from the scratch worktree)."""
        return Package(
            self.layer, dict(tests), self.files, self.interfaces, self.steps, self.covers, self.done_when
        )

    @property
    def test_ids(self) -> tuple[str, ...]:
        """Every test id in `covers`, each once, sorted."""
        return tuple(sorted({t for ids in self.covers.values() for t in ids}))

    @property
    def source_files(self) -> tuple[str, ...]:
        """The paths of `files`, test modules excluded."""
        return tuple(f.path for f in self.files if not is_test_file(f.path))


@dataclass(frozen=True)
class ProofChange:
    """The architect's reply when it finds the agreed proof wrong (three-amigos D12): the tests it says are
    wrong, each with why, and no package. The pipeline asks the tester to revise the layer's tests."""

    changes: tuple[tuple[str, str], ...]
    criteria: tuple[tuple[str, str], ...] = ()
    """The acceptance criteria it says are false, each with why (2026-10-29, roman: "no canonical numeral
    exceeds four characters"): product rewords them before the tester revises the tests."""


def test_files_in(text: str) -> dict[str, str]:
    """The test files of a reply: each `### test file: <path>` heading and the fenced block after it. A
    block ends at the last line equal to its opening fence before the next heading (or, for the last file,
    before the package's ```json fence), so a fence inside the test's text does not end it. A heading with no
    closed block is a StoryError."""
    lines = text.split("\n")
    headings = [n for n, line in enumerate(lines) if _HEADING.match(line)]
    last_json = max((n for n, line in enumerate(lines) if _JSON_OPENER.match(line)), default=len(lines))
    out: dict[str, str] = {}
    for k, n in enumerate(headings):
        path = _HEADING.match(lines[n])
        assert path is not None
        end = headings[k + 1] if k + 1 < len(headings) else max(last_json, n + 1)
        opener = next((m for m in range(n + 1, end) if _FENCE.match(lines[m])), None)
        if opener is None:
            raise StoryError(
                f"test file {path['path']}: no fenced block (```python or ```ts) after its heading"
            )
        fence = _FENCE.match(lines[opener])
        assert fence is not None
        closer = next((m for m in range(end - 1, opener, -1) if lines[m].rstrip() == fence["fence"]), None)
        if closer is None:
            raise StoryError(f"test file {path['path']}: its fenced block is never closed")
        out[path["path"]] = "\n".join(lines[opener + 1 : closer]) + "\n"
    return out


def _normal(path: str, what: str) -> str:
    """`path` without a leading "./"; a StoryError when it is absolute or climbs out with "..", since the
    package's files are written inside the repository's worktree and nowhere else."""
    p = _encodable(path.strip(), what).removeprefix("./")
    parts = PurePosixPath(p).parts
    if not p or p.startswith("/") or ".." in parts:
        raise StoryError(f"{what} {path!r} must be a path inside the repository, from its root")
    return p


def _test_named(path: str, text: str, name: str) -> str | bool | None:
    """The full name of the test `name` that the file `path` (its `text`) defines: `name` itself, or the one
    test whose name ends with it, after a class (pytest, "TestY::test_x") or a describe (Vitest,
    "Wiki screen > lists the topics"; the story meeting may name either form); False when there is no such
    test, or more than one; None when the text does not parse (the readiness check reports that)."""
    if is_js_test(path):
        try:
            names = [t.name for t in js_tests(text)]
        except JsParseError:
            return None
        sep = " > "
    else:
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return None
        names = [n for n, _ in test_functions(tree)]
        sep = "::"
    if name in names:
        return name
    ending = [n for n in names if n.endswith(f"{sep}{name}")]
    return ending[0] if len(ending) == 1 else False


def _proof_change(obj: dict[str, Any], story: Story) -> ProofChange:
    raw = obj.get("changes")
    if not isinstance(raw, list):
        raise StoryError("proof_change needs 'changes': a list of {test, why} or {criterion, why} objects")
    if not raw:
        raise StoryError("proof_change needs at least one change: name the test and why it is wrong")
    changes: list[tuple[str, str]] = []
    criteria: list[tuple[str, str]] = []
    known = {c.id for c in story.acceptance}
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise StoryError(f"changes item {n} must be an object with test (or criterion) and why")
        where = f"changes item {n}"
        if "criterion" in item:
            cid = _text(item, "criterion", where)
            if cid not in known:
                raise StoryError(f"{where}: the story has no criterion {cid!r}; its ids are {sorted(known)}")
            criteria.append((cid, _text(item, "why", where)))
        else:
            changes.append((_text(item, "test", where), _text(item, "why", where)))
    return ProofChange(tuple(changes), tuple(criteria))


def _test_id(tid: str) -> str:
    """A pytest id as the architect wrote it, with any note after it dropped: a pytest id has no whitespace,
    so what follows the first space is words about the test (2026-10-30, roman: "…::test_x (re-agreed to
    the six named decodes only; …)" was refused and the story blocked). A Vitest id keeps its spaces."""
    where = tid.partition("::")[0]
    return tid if is_js_test(where) else tid.split(maxsplit=1)[0] if tid.split() else tid


def parse_package(
    text: str, layer: Layer, story: Story, gated: Collection[str] = ()
) -> Package | ProofChange:
    """The architect's reply as a Package, or as a ProofChange when it says the agreed proof is wrong and
    writes no package. A StoryError says what to fix, in words the architect acts on. `gated` are the
    criteria the agreed proof leaves to the gate: they need no covers."""
    obj = extract_json_block(text)
    if obj is not None and obj.get("type") == "proof_change":
        return _proof_change(obj, story)
    raw_tests = test_files_in(text)
    if obj is None:
        raise StoryError(NO_JSON)
    if obj.get("type") != "package":
        raise StoryError('the JSON object needs "type": "package"')
    if not raw_tests:
        raise StoryError("the reply holds no test file: write each as `### test file: <path>` and a block")
    tests: dict[str, str] = {}
    for raw_path, body in raw_tests.items():
        path = _normal(raw_path, "a test file path")
        if not is_test_file(path):
            raise StoryError(f"{path} is not a test file (a pytest test_*.py, or a Vitest *.test.ts)")
        tests[path] = body

    files: list[PackageFile] = []
    for n, f in enumerate(_list(obj.get("files"), "files"), 1):
        if not isinstance(f, dict):
            raise StoryError(f"files item {n} must be an object with path and action")
        action = f.get("action")
        if action not in _ACTIONS:
            raise StoryError(f"files item {n}: action must be create or modify, not {action!r}")
        files.append(
            PackageFile(
                _normal(_text(f, "path", f"files item {n}"), "a file path"),
                cast(Any, action),
                _encodable(str(f.get("why") or "").strip(), "a file's 'why'"),
            )
        )
    interfaces: list[Interface] = []
    for n, i in enumerate(_list(obj.get("interfaces"), "interfaces"), 1):
        if not isinstance(i, dict):
            raise StoryError(f"interfaces item {n} must be an object with file, signature and behaviour")
        interfaces.append(
            Interface(
                _normal(_text(i, "file", f"interfaces item {n}"), "an interface file"),
                _text(i, "signature", f"interfaces item {n}"),
                _encodable(str(i.get("behaviour") or "").strip(), "an interface's 'behaviour'"),
            )
        )
    steps = tuple(s.strip() for s in _strs(obj.get("steps"), "'steps'") if s.strip())
    if not steps:
        raise StoryError("the package needs at least one of steps: ordered, one change each")

    owned = list(layer.acceptance)
    raw_covers = obj.get("covers") or {}
    if not isinstance(raw_covers, dict):
        raise StoryError("'covers' must map each criterion id to a list of test ids")
    covers: dict[str, tuple[str, ...]] = {}
    for cid, ids in raw_covers.items():
        if str(cid) not in {c.id for c in story.acceptance} and str(cid) != BEHAVIOUR:
            raise StoryError(
                f"covers names {cid}, which the story does not have: {sorted(c.id for c in story.acceptance)}"
            )
        covers[str(cid)] = tuple(
            _test_id(_normal(t, "a test id")) for t in _strs(ids, f"covers {cid}", "test ids")
        )
    for own in owned:
        if own not in gated and not covers.get(own):  # a criterion the agreed proof leaves to the gate
            raise StoryError(
                f"criterion {own} is owned by this layer but no test covers it: add one to covers"
            )
    for cid, ids in covers.items():
        full: list[str] = []
        for tid in ids:
            where, _, name = tid.partition("::")
            text_of = tests.get(where)
            bare = name if is_js_test(where) else _PARAMS.sub("", name)  # a title may hold brackets
            found = None if text_of is None or not name else _test_named(where, text_of, bare)
            if text_of is None or not name or found is False:
                if cid in gated:  # words about the gate, for a criterion the agreed proof leaves to it
                    continue
                raise StoryError(f"covers {cid}: {tid} names a test in no test file of this reply")
            # stored as the test's full id, its parameters kept
            full.append(f"{where}::{found}{name[len(bare) :]}" if isinstance(found, str) else tid)
        covers[cid] = tuple(full)
    covers = {cid: ids for cid, ids in covers.items() if ids}
    return Package(
        layer.slug,
        tests,
        tuple(files),
        tuple(interfaces),
        steps,
        covers,
        str(obj.get("done_when") or "").strip(),
    )


def _list(value: Any, what: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise StoryError(f"'{what}' must be a list")
    return value
