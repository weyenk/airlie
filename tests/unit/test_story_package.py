"""The layer package: the architect's reply for one layer (its tests as code, the files, the interfaces,
the steps, what covers which criterion) parsed into a Package, with refusals a role can act on (P1,
2026-10-24)."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from airlie.story import package as package_module
from airlie.story.model import Criterion, Layer, ScopedPath, Story, StoryError
from airlie.story.package import PACKAGE_CONTRACT, Package, ProofChange, Satisfied, parse_package

STORY = Story(
    "Store",
    "Keep a number.",
    (Criterion("AC1", "a store", "it is read", "it gives 1"), Criterion("AC2", "a store", "set", "it keeps")),
)
LAYER = Layer("store", "Store", "src/store.py", "build the store", ("AC1",))
FOUNDATION = Layer("base", "Base", "src/base.py", "a foundation", ())
PATH = "tests/unit/test_store.py"
TEST_TEXT = "def test_a():\n    assert store() == 1\n"


def reply(
    *,
    tests: dict[str, str] | None = None,
    files: Any = None,
    interfaces: Any = None,
    steps: Any = None,
    covers: Any = None,
    type_: str = "package",
    prose: str = "I read the code.\n",
    done_when: str = "the tests pass",
) -> str:
    tests = {PATH: TEST_TEXT} if tests is None else tests
    parts = [prose]
    for path, text in tests.items():
        parts.append(f"### test file: {path}\n```python\n{text}```\n")
    obj = {
        "type": type_,
        "files": [{"path": "src/store.py", "action": "create"}] if files is None else files,
        "interfaces": [{"file": "src/store.py", "signature": "def store() -> int"}]
        if interfaces is None
        else interfaces,
        "steps": ["create src/store.py with store() returning 1"] if steps is None else steps,
        "covers": {"AC1": [f"{PATH}::test_a"]} if covers is None else covers,
        "done_when": done_when,
    }
    parts.append("```json\n" + json.dumps(obj) + "\n```\n")
    return "\n".join(parts)


def test_a_package_reply_parses_into_tests_files_interfaces_steps_and_covers() -> None:
    p = parse_package(reply(), LAYER, STORY)
    assert p.layer == "store"
    assert p.tests == {PATH: TEST_TEXT}
    assert p.files[0].path == "src/store.py" and p.files[0].action == "create"
    assert p.interfaces[0].signature == "def store() -> int"
    assert p.steps == ("create src/store.py with store() returning 1",)
    assert p.covers == {"AC1": (f"{PATH}::test_a",)}
    assert p.test_ids == (f"{PATH}::test_a",)
    assert p.source_files == ("src/store.py",)
    assert p.done_when == "the tests pass"
    assert Package.from_dict(p.to_dict()) == p
    assert json.loads(json.dumps(p.to_dict())) == p.to_dict()


def test_with_tests_is_the_same_package_with_other_test_text() -> None:
    p = parse_package(reply(), LAYER, STORY)
    q = p.with_tests({PATH: "def test_a():\n    assert 1\n"})
    assert q.tests != p.tests and q.covers == p.covers and q.steps == p.steps


def test_the_package_is_the_last_json_fence_and_test_files_follow_their_headings() -> None:
    inner = (
        '"""Example:\n\n```json\n{"type": "other"}\n```\n"""\n\n\ndef test_a():\n    assert store() == 1\n'
    )
    prose = 'Here is the shape:\n```json\n{"type": "package", "files": []}\n```\nNow the real one.\n'
    text = reply(tests={PATH: inner}, prose=prose)
    p = parse_package(text, LAYER, STORY)
    assert p.tests[PATH] == inner
    assert p.files[0].path == "src/store.py"
    assert package_module.test_files_in(text) == {PATH: inner}


def test_test_files_in_reads_each_heading_and_longer_fences() -> None:
    text = (
        "### test file: tests/a_test.py\n````python\nx = 1\n```\ny = 2\n````\n\n"
        "### test file: tests/test_b.py\n```\nz = 3\n```\n"
    )
    assert package_module.test_files_in(text) == {
        "tests/a_test.py": "x = 1\n```\ny = 2\n",
        "tests/test_b.py": "z = 3\n",
    }


def test_a_test_file_heading_is_read_at_levels_two_to_four_in_any_case_with_its_path_bare_or_quoted() -> None:
    """Roman, 2026-10-31: a resumed draft's test file was not found and the reply cost a retry."""
    for heading in (
        "## Test file: tests/test_b.py",
        "#### test file: `tests/test_b.py`",
        "### TEST FILE: 'tests/test_b.py'",
    ):
        assert package_module.test_files_in(f"{heading}\n```python\nz = 3\n```\n") == {
            "tests/test_b.py": "z = 3\n"
        }


def test_a_file_with_no_action_takes_it_from_the_agreed_split_when_the_split_names_it() -> None:
    """Roman round 6 (2026-10-31): a package that left out one action was a retry."""
    layer = replace(LAYER, files=(ScopedPath("src/store.py", True), ScopedPath("src/old.py")))
    files = [{"path": "src/store.py"}, {"path": "src/old.py", "why": "w"}]
    p = parse_package(reply(files=files), layer, STORY)
    assert [(f.path, f.action) for f in p.files] == [("src/store.py", "create"), ("src/old.py", "modify")]
    with pytest.raises(StoryError, match="action must be create or modify, not None"):
        parse_package(reply(files=[{"path": "src/other.py"}]), layer, STORY)


def test_a_refused_package_keeps_the_replys_end_for_the_retry_event() -> None:
    text = reply(tests={})
    with pytest.raises(StoryError, match="no test file") as e:
        parse_package(text, LAYER, STORY)
    assert e.value.reply == text[-1500:] and e.value.reply.endswith("```\n")


def test_a_heading_with_no_block_is_refused() -> None:
    with pytest.raises(StoryError, match="tests/test_x.py"):
        package_module.test_files_in("### test file: tests/test_x.py\nno block here\n")


def flawed(flaw: str) -> str:
    covers = {"AC1": [f"{PATH}::test_a"]}
    if flaw == "no_json":
        return f"### test file: {PATH}\n```python\n{TEST_TEXT}```\n"
    if flaw == "wrong_type":
        return reply(type_="plan")
    if flaw == "no_test_file":
        return reply(tests={})
    if flaw == "not_a_test_module":
        return reply(tests={"src/helper.py": TEST_TEXT}, covers={"AC1": ["src/helper.py::test_a"]})
    if flaw == "path_escapes":
        return reply(tests={"../test_x.py": TEST_TEXT}, covers={"AC1": ["../test_x.py::test_a"]})
    if flaw == "empty_steps":
        return reply(steps=[])
    if flaw == "covers_unknown_criterion":
        return reply(covers={**covers, "AC9": [f"{PATH}::test_a"]})
    if flaw == "criterion_uncovered":
        return reply(covers={})
    if flaw == "test_id_not_in_a_test_file":
        return reply(covers={"AC1": [f"{PATH}::test_missing"]})
    if flaw == "bad_action":
        return reply(files=[{"path": "src/store.py", "action": "delete"}])
    raise AssertionError(flaw)


@pytest.mark.parametrize(
    ("flaw", "says"),
    [
        ("no_json", "no JSON object"),
        ("wrong_type", '"type": "package"'),
        ("no_test_file", "no test file"),
        ("not_a_test_module", "not a test file"),
        ("path_escapes", "inside the repository"),
        ("empty_steps", "steps"),
        ("covers_unknown_criterion", "AC9"),
        ("criterion_uncovered", "AC1"),
        ("test_id_not_in_a_test_file", "names a test in no test file"),
        ("bad_action", "create or modify"),
    ],
)
def test_a_flawed_package_is_refused_with_a_reason_the_architect_can_act_on(flaw: str, says: str) -> None:
    with pytest.raises(StoryError, match=says):
        parse_package(flawed(flaw), LAYER, STORY)


def test_a_layer_with_no_criteria_needs_one_test_and_no_covers() -> None:
    p = parse_package(reply(covers={}), FOUNDATION, STORY)
    assert p.covers == {} and p.test_ids == ()
    with pytest.raises(StoryError, match="no test file"):
        parse_package(reply(tests={}, covers={}), FOUNDATION, STORY)


def test_a_test_path_is_normalised_without_a_leading_dot_slash() -> None:
    p = parse_package(
        reply(tests={f"./{PATH}": TEST_TEXT}, covers={"AC1": [f"./{PATH}::test_a"]}), LAYER, STORY
    )
    assert list(p.tests) == [PATH]
    assert p.covers == {"AC1": (f"{PATH}::test_a",)}


def test_a_class_test_and_a_parametrized_id_name_a_test_in_the_file() -> None:
    text = "class TestStore:\n    def test_a(self):\n        assert 1\n"
    p = parse_package(
        reply(tests={PATH: text}, covers={"AC1": [f"{PATH}::TestStore::test_a[x]"]}), LAYER, STORY
    )
    assert p.test_ids == (f"{PATH}::TestStore::test_a[x]",)


UNITTEST = (
    "import unittest\n\n\nclass RomanTest(unittest.TestCase):\n"
    "    def test_a(self) -> None:\n        self.assertEqual(1, 1)\n"
)


def test_a_unittest_case_of_any_name_holds_tests_and_a_bare_name_resolves_to_its_method() -> None:
    """The first real run on a unittest repository (airlie-sandbox, 2026-10-25): the architect mirrored the
    repository's `VersionTest(unittest.TestCase)` and covered the agreed ids as the meeting wrote them, with
    no class; both are the tests of the file, and the id is stored as the test's full id."""
    p = parse_package(reply(tests={PATH: UNITTEST}, covers={"AC1": [f"{PATH}::test_a"]}), LAYER, STORY)
    assert p.covers == {"AC1": (f"{PATH}::RomanTest::test_a",)}
    p = parse_package(
        reply(tests={PATH: UNITTEST}, covers={"AC1": [f"{PATH}::RomanTest::test_a"]}), LAYER, STORY
    )
    assert p.test_ids == (f"{PATH}::RomanTest::test_a",)


def test_a_bare_name_two_classes_define_names_no_one_test() -> None:
    text = UNITTEST + "\n\nclass OtherTest(unittest.TestCase):\n    def test_a(self) -> None:\n        pass\n"
    with pytest.raises(StoryError, match="names a test in no test file"):
        parse_package(reply(tests={PATH: text}, covers={"AC1": [f"{PATH}::test_a"]}), LAYER, STORY)


def test_the_contract_names_the_heading_and_the_json_shape() -> None:
    assert "### test file:" in PACKAGE_CONTRACT and '"type": "package"' in PACKAGE_CONTRACT


def test_a_proof_change_reply_is_a_proof_change_and_not_a_package() -> None:
    """D12: when the architect finds the proof itself wrong, it says which tests and why, and writes no
    package."""
    text = (
        "The proof names a test this layer cannot make red.\n```json\n"
        + json.dumps(
            {"type": "proof_change", "changes": [{"test": f"{PATH}::test_a", "why": "store() has no zero"}]}
        )
        + "\n```\n"
    )
    got = parse_package(text, LAYER, STORY)
    assert got == ProofChange(((f"{PATH}::test_a", "store() has no zero"),))


def test_a_note_after_a_test_id_in_covers_is_dropped() -> None:
    """Roman, 2026-10-30: a covers id with a parenthesised note after it was refused twice and blocked."""
    noted = f"{PATH}::test_a (re-agreed to the named cases only; the loop is AC2's)"
    p = parse_package(reply(covers={"AC1": [noted]}), LAYER, STORY)
    assert p.covers == {"AC1": (f"{PATH}::test_a",)}


def test_a_proof_change_may_name_a_false_criterion_of_the_story() -> None:
    """2026-10-29 (roman): a criterion that states something untrue about the domain goes to product."""
    changes = [
        {"criterion": "AC1", "why": "no store holds zero bytes"},
        {"test": f"{PATH}::test_a", "why": "x"},
    ]
    text = "```json\n" + json.dumps({"type": "proof_change", "changes": changes}) + "\n```"
    got = parse_package(text, LAYER, STORY)
    assert got == ProofChange(((f"{PATH}::test_a", "x"),), (("AC1", "no store holds zero bytes"),))
    unknown = text.replace('"AC1"', '"AC9"')
    with pytest.raises(StoryError, match="no criterion 'AC9'"):
        parse_package(unknown, LAYER, STORY)


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ([], "at least one"),
        ("nope", "list"),
        ([{"test": f"{PATH}::test_a"}], "why"),
        ([{"why": "x"}], "test"),
        ([7], "object"),
    ],
)
def test_a_proof_change_without_its_tests_or_reasons_is_refused(changes: Any, why: str) -> None:
    text = "```json\n" + json.dumps({"type": "proof_change", "changes": changes}) + "\n```"
    with pytest.raises(StoryError, match=why):
        parse_package(text, LAYER, STORY)


def test_a_test_with_no_criterion_is_covered_under_behaviour() -> None:
    """A proof test that proves a behaviour of its layer, not a criterion, has no criterion to be listed
    under: `covers` names it under "behaviour"."""
    p = parse_package(
        reply(
            covers={"AC1": [f"{PATH}::test_a"], "behaviour": [f"{PATH}::test_b"]},
            tests={PATH: TEST_TEXT + "\n\ndef test_b():\n    assert store() == 1\n"},
        ),
        LAYER,
        STORY,
    )
    assert p.covers["behaviour"] == (f"{PATH}::test_b",) and p.test_ids == (
        f"{PATH}::test_a",
        f"{PATH}::test_b",
    )
    with pytest.raises(StoryError, match="behaviour"):
        parse_package(
            reply(covers={"AC1": [f"{PATH}::test_a"], "behaviour": [f"{PATH}::test_zzz"]}), LAYER, STORY
        )


def test_a_criterion_the_agreed_proof_leaves_to_the_gate_needs_no_covers() -> None:
    p = parse_package(reply(covers={}), LAYER, STORY, gated={"AC1"})
    assert p.covers == {}
    with pytest.raises(StoryError, match="no test covers it"):
        parse_package(reply(covers={}), LAYER, STORY)


def test_words_in_the_covers_of_a_criterion_left_to_the_gate_are_dropped() -> None:
    """Roman bundle 2 (2026-10-29) was blocked on covers AC6: "tests/test_roman.py (whole file: the test gate
    ... must show all tests ... passing)" for a criterion the agreed proof left to the gate."""
    said = "tests/test_store.py (whole file: the test gate must pass)"
    p = parse_package(reply(covers={"AC1": [said, f"{PATH}::test_a"]}), LAYER, STORY, gated={"AC1"})
    assert p.covers == {"AC1": (f"{PATH}::test_a",)}
    assert parse_package(reply(covers={"AC1": [said]}), LAYER, STORY, gated={"AC1"}).covers == {}
    with pytest.raises(StoryError, match="names a test in no test file"):
        parse_package(reply(covers={"AC1": [said]}), LAYER, STORY)


WEB = "web/src/screens/Wiki.test.ts"
WEB_TEXT = """import { describe, it, expect } from "vitest";

describe("Wiki screen", () => {
  it("lists the topics", () => {
    expect(1).toBe(1);
  });
});
"""


def test_a_package_carries_a_vitest_file_and_covers_its_tests_by_their_vitest_names() -> None:
    """The web-tests design (2026-10-27): a Vue layer's tests are Vitest tests; the agreed id may be the full
    name or its last part when that names one test."""
    p = parse_package(
        reply(tests={WEB: WEB_TEXT}, covers={"AC1": [f"{WEB}::Wiki screen > lists the topics"]}), LAYER, STORY
    )
    assert list(p.tests) == [WEB] and p.test_ids == (f"{WEB}::Wiki screen > lists the topics",)
    short = parse_package(
        reply(tests={WEB: WEB_TEXT}, covers={"AC1": [f"{WEB}::lists the topics"]}), LAYER, STORY
    )
    assert short.test_ids == (f"{WEB}::Wiki screen > lists the topics",)
    with pytest.raises(StoryError, match="names a test in no test file"):
        parse_package(reply(tests={WEB: WEB_TEXT}, covers={"AC1": [f"{WEB}::shows a page"]}), LAYER, STORY)


def test_a_vitest_file_comes_in_a_ts_fence_and_the_contract_says_which_fence_is_whose() -> None:
    """Wiki run B (2026-10-27): the architect wrote web/src/router.test.ts in a ```ts block, as it should, and
    the parser took only ```python blocks: the draft was refused as "never closed"."""
    from airlie.story.package import PACKAGE_CONTRACT, test_files_in

    for lang in ("ts", "typescript", "tsx", "js", "javascript"):
        text = f"### test file: {WEB}\n```{lang}\n{WEB_TEXT}```\n\n```json\n{{}}\n```\n"
        assert test_files_in(text) == {WEB: WEB_TEXT}, lang
    assert "```ts" in PACKAGE_CONTRACT and "```python" in PACKAGE_CONTRACT


EXISTING = (
    '"""The store."""\n\nimport pytest\n\nfrom store import store\n\n\n'
    "def test_old():\n    assert store() == 1\n"
)
ADDED = "import json\n\nimport pytest\n\n\ndef test_a():\n    assert json.loads(str(store())) == 1\n"


def additions(path: str, text: str) -> str:
    """A reply whose one test block adds to `path` rather than writing it whole."""
    return reply(tests={}).replace(
        "```json", f"### test additions: {path}\n```python\n{text}```\n\n```json", 1
    )


def test_test_additions_join_the_file_on_the_base_with_their_imports_in_its_import_block() -> None:
    """AIR-47 (2026-10-31): a layer's tests went in tests/unit/test_config.py, 1299 lines, and the contract
    asked for the whole file back: twice the reply was cut off at max_tokens before its package object. Now
    an existing file's new tests come alone and the pipeline joins them to the base's file."""
    p = parse_package(additions(PATH, ADDED), LAYER, STORY, base={PATH: EXISTING}.get)
    assert isinstance(p, Package)
    whole = p.tests[PATH]
    assert whole.startswith(
        '"""The store."""\n\nimport pytest\n\nfrom store import store\nimport json\n\n\ndef test_old'
    )
    assert whole.count("import pytest") == 1  # an import the file already has is not written twice
    assert "def test_old():" in whole and whole.endswith("assert json.loads(str(store())) == 1\n")
    assert p.test_ids == (f"{PATH}::test_a",)
    assert p.added == {PATH: ADDED}
    assert Package.from_dict(p.to_dict()) == p
    assert "test additions" in PACKAGE_CONTRACT


def test_added_multi_line_imports_are_joined_by_statement_so_the_file_still_parses() -> None:
    """AIR-47 layer 03 (2026-11-02): the join dropped each added line the file already had, the `)` of a
    multi-line import among them, and the package came back unparseable twice until the story blocked."""
    import ast

    from airlie.story.package import joined_tests

    existing = (
        "from store import (\n    load,\n    save,\n)\nfrom helpers import RUN\n\n\n"
        "def test_old():\n    assert load() and save() and RUN\n"
    )
    added = (
        "from helpers import (\n    RUN,\n    cfg_for,\n)\nfrom store import (\n    load,\n    save,\n)\n"
        "from other import (\n    a,\n    b,\n)\n\n\ndef test_new():\n    assert cfg_for() and a and b\n"
    )
    whole = joined_tests(existing, added)
    ast.parse(whole)
    assert whole.startswith(
        "from store import (\n    load,\n    save,\n)\nfrom helpers import RUN\n"
        "from helpers import cfg_for\nfrom other import (\n    a,\n    b,\n)\n\n\ndef test_old"
    )
    assert whole.count("save,") == 1 and whole.endswith("assert cfg_for() and a and b\n")


def test_test_additions_to_a_file_the_base_lacks_are_refused_in_words_the_architect_acts_on() -> None:
    with pytest.raises(StoryError, match=r"not on the layer's base.*### test file:"):
        parse_package(additions(PATH, ADDED), LAYER, STORY, base={}.get)
    with pytest.raises(StoryError, match=r"not on the layer's base"):
        parse_package(additions(PATH, ADDED), LAYER, STORY)


def test_a_drafted_package_rebased_joins_its_additions_to_the_file_on_the_layer_s_real_base() -> None:
    """A draft is written at the story's base; an earlier layer may add tests to the same file before this
    layer takes its draft, and those must be kept."""
    p = parse_package(additions(PATH, ADDED), LAYER, STORY, base={PATH: EXISTING}.get)
    later = EXISTING + "\n\ndef test_layer_one():\n    assert store()\n"
    q = p.rebased({PATH: later}.get)
    assert "def test_layer_one():" in q.tests[PATH] and q.tests[PATH].endswith(
        p.tests[PATH].split("\n\n\n")[-1]
    )
    assert q.added == p.added and q.covers == p.covers
    whole = parse_package(reply(), LAYER, STORY)
    assert whole.rebased({PATH: later}.get) == whole  # a file written whole stays as written


# ---- regression guards and an already satisfied layer (AIR-59, AIR-61, AIR-62, 2026-11-06) ----

TWO_TESTS = "def test_a():\n    assert store() == 1\n\n\ndef test_b():\n    assert unsigned_delete() == 401\n"
GUARDED = Layer("store", "Store", "src/store.py", "build the store", ("AC1", "AC2"))


def guarded_reply(guards: Any) -> str:
    """A package reply with a `guards` object beside its covers."""
    text = reply(tests={PATH: TWO_TESTS}, covers={"AC1": [f"{PATH}::test_a"], "AC2": [f"{PATH}::test_b"]})
    head, _, tail = text.rpartition("```json\n")
    obj = json.loads(tail.removesuffix("\n```\n"))
    obj["guards"] = guards
    return f"{head}```json\n{json.dumps(obj)}\n```\n"


def test_a_test_marked_a_regression_guard_keeps_its_one_line_reason() -> None:
    """AIR-61 layer 02 (2026-11-05): an unsigned DELETE already answers 401 on main, so a correct test of it
    passes before the code and can never be red; marked a guard, the red check lets it pass there."""
    why = "the auth middleware already answers 401"
    p = parse_package(guarded_reply({f"{PATH}::test_b": why}), GUARDED, STORY)
    assert isinstance(p, Package)
    assert p.guards == {f"{PATH}::test_b": why}
    assert Package.from_dict(p.to_dict()) == p
    assert Package.from_dict({k: v for k, v in p.to_dict().items() if k != "guards"}).guards == {}


def test_a_guard_named_by_its_bare_name_resolves_to_the_covered_test() -> None:
    p = parse_package(guarded_reply({"test_b": "already true"}), GUARDED, STORY)
    assert isinstance(p, Package) and p.guards == {f"{PATH}::test_b": "already true"}


@pytest.mark.parametrize(
    ("guards", "says"),
    [
        (["x"], "'guards' must map each guard's test id to a one-line reason"),
        ({f"{PATH}::test_b": ""}, "needs a one-line reason"),
        ({f"{PATH}::test_c": "x"}, "names no test in covers"),
    ],
)
def test_a_guard_that_is_not_a_covered_test_with_a_reason_is_refused(guards: Any, says: str) -> None:
    with pytest.raises(StoryError, match=says):
        parse_package(guarded_reply(guards), GUARDED, STORY)


def test_a_package_whose_tests_are_all_guards_is_refused_as_a_layer_with_nothing_to_build() -> None:
    """AIR-62 layer 02: a layer holding only criteria that already hold has nothing to build; its answer is
    the satisfied reply, not a package of guards for a coder to make pass."""
    guards = {f"{PATH}::test_a": "already true", f"{PATH}::test_b": "already true"}
    with pytest.raises(StoryError, match='"type": "satisfied"'):
        parse_package(guarded_reply(guards), GUARDED, STORY)


def satisfied_reply(by: Any, why: Any = "the auth middleware answers 401 for every unsigned /v1 call") -> str:
    obj = {"type": "satisfied", "by": by, "why": why}
    return "It already holds.\n```json\n" + json.dumps(obj) + "\n```\n"


def test_a_satisfied_reply_names_what_already_holds_the_layer_and_why() -> None:
    """AIR-47 layer 04 and AIR-48 layer 02 were dropped by hand-editing state.json: airlie had no outcome
    for a layer whose criteria already hold."""
    by = ["01", "tests/unit/test_auth.py::TestDelete::test_unsigned_is_401"]
    got = parse_package(satisfied_reply(by), LAYER, STORY, base=AUTH_BASE)
    assert got == Satisfied(tuple(by), "the auth middleware answers 401 for every unsigned /v1 call")
    assert isinstance(got, Satisfied) and Satisfied.from_dict(got.to_dict()) == got
    # a test named without its class is stored as its full id, as covers stores one
    bare = parse_package(
        satisfied_reply(["tests/unit/test_auth.py::test_unsigned_is_401"]), LAYER, STORY, base=AUTH_BASE
    )
    assert isinstance(bare, Satisfied) and bare.by == (
        "tests/unit/test_auth.py::TestDelete::test_unsigned_is_401",
    )


AUTH_BASE = {
    "tests/unit/test_auth.py": (
        "class TestDelete:\n    def test_unsigned_is_401(self):\n        assert True\n"
    ),
    "src/airlie/serve/app.py": "def _require_principal(): ...\n",
}.get


@pytest.mark.parametrize(
    ("by", "why", "says"),
    [
        ([], "x", "'by' needs at least one"),
        (["01"], "", "needs a non-empty 'why'"),
        (
            ["tests/unit/test_nowhere.py::test_f"],
            "x",
            "tests/unit/test_nowhere.py is not on the layer's base",
        ),
        (["tests/unit/test_auth.py::test_gone"], "x", "does not exist on the base"),
        (["src/airlie/serve/app.py"], "x", "name a test that already proves it"),
        (["src/airlie/serve/app.py::_require_principal"], "x", "src/airlie/serve/app.py is not a test file"),
        (["nothing in particular"], "x", "name an earlier layer by its number"),
    ],
)
def test_a_satisfied_reply_that_does_not_say_what_holds_it_is_refused(by: Any, why: Any, says: str) -> None:
    """Review of PR #244 (2026-11-06): satisfied is no escape hatch from a hard layer. It names earlier built
    layers or tests on the base (airlie then runs them there); a bare file or a function proves nothing."""
    with pytest.raises(StoryError, match=says):
        parse_package(satisfied_reply(by, why), LAYER, STORY, base=AUTH_BASE)


def test_the_contract_says_how_to_mark_a_guard_and_when_a_layer_is_satisfied() -> None:
    assert '"guards"' in PACKAGE_CONTRACT
    assert '"type": "satisfied"' in PACKAGE_CONTRACT


def test_the_coders_list_of_tests_marks_a_guard_as_one_that_passes_already() -> None:
    from airlie.story.briefs import GUARD_NOTE, package_blocks

    p = parse_package(guarded_reply({"test_b": "already true"}), GUARDED, STORY)
    assert isinstance(p, Package)
    shown = package_blocks(STORY, (GUARDED,), 0, p)
    assert f"  - {PATH}::test_b{GUARD_NOTE}" in shown
    assert f"  - {PATH}::test_a\n" in shown
