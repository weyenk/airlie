"""The layer package: the architect's reply for one layer (its tests as code, the files, the interfaces,
the steps, what covers which criterion) parsed into a Package, with refusals a role can act on (P1,
2026-10-24)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from airlie.story import package as package_module
from airlie.story.model import Criterion, Layer, Story, StoryError
from airlie.story.package import PACKAGE_CONTRACT, Package, ProofChange, parse_package

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
        ("not_a_test_module", "not a pytest test module"),
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
