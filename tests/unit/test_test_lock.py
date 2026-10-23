"""Locking what the tester wrote: top-level definitions new or changed since the layer's base."""

from __future__ import annotations

from airlie.tools.test_lock import touched, written_by_tester

BASE = """\
import pytest


class FakeBoard:
    def post(self) -> None:
        return None


def test_old() -> None:
    assert 1 == 1


def helper() -> int:
    return 1
"""


def test_new_and_changed_definitions_are_the_testers() -> None:
    locked = BASE.replace("assert 1 == 1", "assert 2 == 2") + "\n\ndef test_new() -> None:\n    pass\n"
    assert written_by_tester(BASE, locked) == {"test_new", "test_old"}


def test_a_reformat_or_a_move_is_no_change() -> None:
    reordered = """\
import pytest

# a comment
def helper() -> int:
    return 1



def test_old() -> None:
    assert 1 == 1   # same


class FakeBoard:

    def post(self) -> None:
        return None
"""
    assert written_by_tester(BASE, reordered) == frozenset()


def test_a_new_file_is_all_the_testers() -> None:
    assert written_by_tester(None, BASE) == {"FakeBoard", "test_old", "helper"}


def test_a_method_added_to_an_existing_class_locks_the_class() -> None:
    base = "class TestX:\n    def a(self) -> None:\n        pass\n"
    locked = base + "    def b(self) -> None:\n        pass\n"
    assert written_by_tester(base, locked) == {"TestX"}


def test_imports_and_constants_are_never_locked() -> None:
    assert written_by_tester(BASE, "import os\nX = 1\n" + BASE) == frozenset()


def test_an_unparseable_locked_file_is_locked_whole() -> None:
    assert written_by_tester(BASE, "def broken(:\n") is None
    assert written_by_tester(BASE, "x = 1\0\n") is None


def test_an_unparseable_base_counts_as_a_new_file() -> None:
    assert written_by_tester("def broken(:\n", BASE) == {"FakeBoard", "test_old", "helper"}


def test_the_last_of_duplicate_names_is_the_definition() -> None:
    base = "def f() -> int:\n    return 1\n"
    assert written_by_tester(base, base + "def f() -> int:\n    return 2\n") == {"f"}
    assert (
        written_by_tester(base + "def f() -> int:\n    return 2\n", base + "def f() -> int:\n    return 2\n")
        == frozenset()
    )


def test_touched_names_a_changed_or_removed_definition() -> None:
    before = BASE + "\n\ndef test_new() -> None:\n    pass\n"
    names = {"test_new"}
    edited_fake = before.replace("return None", "return 5")
    assert touched(names, before, edited_fake) == []
    changed = before.replace("    pass\n", "    assert True\n")
    assert touched(names, before, changed) == ["test_new"]
    deleted = BASE
    assert touched(names, before, deleted) == ["test_new"]
    assert touched(names, before, "def broken(:\n") is None
