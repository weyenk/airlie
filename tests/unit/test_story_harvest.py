"""A tool-written package's tests, read from the architect's checkout (the tool-written packages design,
2026-11-06): the test files changed since a commit, with a deleted test refused, and the test blocks of a
reply in the old form written into the checkout first, so both forms take one path."""

from __future__ import annotations

from pathlib import Path

import pytest

from airlie.story.harvest import (
    DELETED_TESTS,
    EditedTest,
    edited_tests,
    harvest,
    merged,
    place_reply_tests,
    rebase_draft,
)
from airlie.story.model import StoryError
from airlie.story.package import joined_tests
from tests.unit.git_helpers import git

OLD = "tests/unit/test_old.py"
OLD_TEXT = "def test_one():\n    assert 1\n\n\ndef test_two():\n    assert 2\n"
WEB = "web/src/a.test.ts"
WEB_TEXT = 'import { it, expect } from "vitest";\n\nit("adds", () => {\n  expect(1).toBe(1);\n});\n'


def repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository with a pytest module, a Vitest file and a source file committed; its root and commit."""
    root = tmp_path / "repo"
    for path, text in ((OLD, OLD_TEXT), (WEB, WEB_TEXT), ("src/a.py", "x = 1\n")):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    return root, git(root, "rev-parse", "HEAD")


def write(root: Path, path: str, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)


def test_the_harvest_is_the_test_files_changed_or_added_since_the_commit(tmp_path: Path) -> None:
    root, base = repo(tmp_path)
    assert harvest(root, base) == {}
    changed = OLD_TEXT.replace("assert 2", "assert 2 == 2")  # an existing test edited in place (decision 1)
    write(root, OLD, changed)
    write(root, "tests/unit/test_new.py", "def test_new():\n    assert 3\n")
    write(root, "src/a.py", "x = 2\n")  # not a test file: not harvested
    assert harvest(root, base) == {"tests/unit/test_new.py": "def test_new():\n    assert 3\n", OLD: changed}
    # committed or not: a retry's checkout is committed after every turn
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "turn")
    assert list(harvest(root, base)) == ["tests/unit/test_new.py", OLD]
    assert harvest(root, git(root, "rev-parse", "HEAD")) == {}


def test_a_test_deleted_since_the_commit_is_refused_by_its_id(tmp_path: Path) -> None:
    """Noah, 2026-11-06: an existing test may be edited in place, never deleted."""
    root, base = repo(tmp_path)
    write(root, OLD, "def test_one():\n    assert 1\n")
    with pytest.raises(StoryError) as e:
        harvest(root, base)
    assert (
        str(e.value)
        == DELETED_TESTS.format(ids=f"{OLD}::test_two")
        == (
            f"your changes deleted {OLD}::test_two, which the layer's base has: a test that is there stays. "
            "Put it back as it was; to change what it asserts, edit it in place with edit_file"
        )
    )


def test_a_deleted_test_file_and_a_deleted_vitest_test_are_refused_too(tmp_path: Path) -> None:
    root, base = repo(tmp_path)
    (root / OLD).unlink()
    write(root, WEB, 'import { it } from "vitest";\n')
    with pytest.raises(StoryError) as e:
        harvest(root, base)
    assert str(e.value) == DELETED_TESTS.format(ids=f"{OLD}::test_one, {OLD}::test_two, {WEB}::adds")


def test_a_renamed_test_is_a_deleted_one(tmp_path: Path) -> None:
    root, base = repo(tmp_path)
    write(root, OLD, OLD_TEXT.replace("test_two", "test_three"))
    with pytest.raises(StoryError, match=f"deleted {OLD}::test_two,"):
        harvest(root, base)


def test_a_test_file_that_does_not_parse_is_harvested_and_left_to_the_readiness_check(tmp_path: Path) -> None:
    root, base = repo(tmp_path)
    write(root, OLD, "def test_one(:\n")
    assert harvest(root, base) == {OLD: "def test_one(:\n"}


def test_a_replys_test_blocks_are_written_into_the_checkout(tmp_path: Path) -> None:
    """A reply in the old form (`### test file:` / `### test additions:` blocks) is still usable: its blocks
    are written into the checkout before the harvest, additions joined to the file there."""
    root, _ = repo(tmp_path)
    text = (
        "I read the code.\n"
        "### test file: tests/unit/test_new.py\n```python\ndef test_new():\n    assert 3\n```\n"
        f"### test additions: ./{OLD}\n```python\nimport json\n\n\ndef test_more():\n    assert json\n```\n"
        '```json\n{"type": "package"}\n```\n'
    )
    assert place_reply_tests(root, text) == ["tests/unit/test_new.py", OLD]
    assert (root / "tests/unit/test_new.py").read_text() == "def test_new():\n    assert 3\n"
    added = "import json\n\n\ndef test_more():\n    assert json\n"
    assert (root / OLD).read_text() == joined_tests(OLD_TEXT, added)
    assert place_reply_tests(root, '```json\n{"type": "package"}\n```\n') == []


def test_a_reply_with_one_bad_block_writes_none_of_them(tmp_path: Path) -> None:
    root, base = repo(tmp_path)
    text = (
        "### test file: tests/unit/test_new.py\n```python\ndef test_new():\n    assert 3\n```\n"
        "### test file: src/a.py\n```python\nx = 2\n```\n"
        '```json\n{"type": "package"}\n```\n'
    )
    with pytest.raises(StoryError, match="src/a.py is not a test file"):
        place_reply_tests(root, text)
    assert harvest(root, base) == {} and (root / "src/a.py").read_text() == "x = 1\n"


@pytest.mark.parametrize(
    ("block", "says"),
    [
        ("### test file: src/a.py\n```python\nx = 2\n```\n", "src/a.py is not a test file"),
        ("### test file: ../test_out.py\n```python\nx = 2\n```\n", "must be a path inside the repository"),
        (
            "### test additions: tests/unit/test_none.py\n```python\ndef test_x():\n    pass\n```\n",
            "test additions to tests/unit/test_none.py: the file is not in your checkout",
        ),
        ("### test file: tests/unit/test_new.py\nno block\n", "no fenced block"),
    ],
)
def test_a_reply_block_that_cannot_be_placed_is_refused_and_nothing_is_written(
    tmp_path: Path, block: str, says: str
) -> None:
    root, base = repo(tmp_path)
    with pytest.raises(StoryError, match=says) as e:
        place_reply_tests(root, block + '```json\n{"type": "package"}\n```\n')
    assert e.value.reply.endswith("```\n")
    assert git(root, "status", "--porcelain") == "" and harvest(root, base) == {}


# ---- edited existing tests, and a draft rebased onto its layer's real base (build step 4) ----


def test_an_existing_test_changed_in_place_is_listed_with_its_source_before_and_after() -> None:
    """Noah, 2026-11-06: editing an existing test is allowed, and the critic and the PR see it."""
    changed = OLD_TEXT.replace("assert 2", "assert 2 == 2")
    new_file = "def test_new():\n    assert 3\n"
    found = edited_tests(
        {OLD: changed + "\n\ndef test_three():\n    pass\n", "tests/test_n.py": new_file}, {OLD: OLD_TEXT}.get
    )
    assert found == [
        EditedTest(f"{OLD}::test_two", "def test_two():\n    assert 2", "def test_two():\n    assert 2 == 2")
    ]
    assert edited_tests({OLD: OLD_TEXT}, {OLD: OLD_TEXT}.get) == []
    assert edited_tests({OLD: "def test_one(:\n"}, {OLD: OLD_TEXT}.get) == []
    web = WEB_TEXT.replace("toBe(1)", "toBe(2)")
    assert [e.id for e in edited_tests({WEB: web}, {WEB: WEB_TEXT}.get)] == [f"{WEB}::adds"]


def test_a_draft_takes_what_the_real_base_changed_in_its_file_by_a_three_way_merge() -> None:
    """Layer 02's draft edits layer 01's test file, drafted at the story's base; by the time layer 02 is
    built, layer 01's checks reformatted the top of that file. Both changes are kept."""
    drafted = "import os\n\n\ndef test_one():\n    assert 1\n"  # layer 01's file as drafted
    real = "import os\nimport sys\n\n\ndef test_one():\n    assert 1\n"  # as layer 01 built it
    draft = drafted + "\n\ndef test_two():\n    assert 2\n"  # layer 02's draft added a test
    out, conflicts = rebase_draft({OLD: draft}, {OLD: drafted}.get, {OLD: real}.get)
    assert conflicts == [] and out == {OLD: real + "\n\ndef test_two():\n    assert 2\n"}
    # the real base unchanged since the draft: the draft as it is
    assert rebase_draft({OLD: draft}, {OLD: drafted}.get, {OLD: drafted}.get) == ({OLD: draft}, [])
    # a new file: no ancestor and nothing on the base
    assert rebase_draft({"tests/test_n.py": "x\n"}, {}.get, {}.get) == ({"tests/test_n.py": "x\n"}, [])


def test_a_draft_whose_change_conflicts_with_the_real_bases_is_named() -> None:
    drafted = "def test_one():\n    assert 1\n"
    out, conflicts = rebase_draft(
        {OLD: "def test_one():\n    assert 10\n", "tests/test_n.py": "x\n"},
        {OLD: drafted}.get,
        {OLD: "def test_one():\n    assert 11\n"}.get,
    )
    assert conflicts == [OLD] and out == {"tests/test_n.py": "x\n"}
    assert merged("a\n", "b\n", "c\n") is None and merged("a\n", "a\n", "c\n") == "c\n"
