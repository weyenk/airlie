"""A repository's testing conventions, read from a commit for the tester: its test levels, its own testing
notes and the guideline library (three-amigos D6)."""

from pathlib import Path

from airlie.skills import Skill
from airlie.story import testing_conventions as tc
from airlie.story.testing_conventions import (
    Conventions,
    conventions,
    conventions_text,
)
from airlie.teams.worktree import tree_paths
from tests.unit.test_story_layer_checks import repo

MOD = "def test_x():\n    assert True\n"


def test_levels_are_the_test_directories_with_modules_and_never_fixtures_or_data(tmp_path: Path) -> None:
    root, sha = repo(
        tmp_path,
        {
            "tests/unit/test_a.py": MOD,
            "tests/unit/sub/test_b.py": MOD,
            "tests/unit/sub/c_test.py": MOD,
            "tests/integration/test_c.py": MOD,
            "tests/e2e/helpers.py": "X = 1\n",  # no test module: not a level
            "tests/fixtures/test_fixture_thing.py": MOD,
            "tests/data/test_data.py": MOD,
            "tests/__pycache__/test_cached.py": MOD,
            "tests/conftest.py": "",
        },
    )
    assert tc.test_levels(root, sha) == {"unit": 3, "integration": 1}
    assert tree_paths(root, sha, "tests/unit") == [
        "tests/unit/sub/c_test.py",
        "tests/unit/sub/test_b.py",
        "tests/unit/test_a.py",
    ]


def test_a_flat_tests_directory_is_one_level(tmp_path: Path) -> None:
    root, sha = repo(tmp_path, {"tests/test_a.py": MOD, "tests/test_b.py": MOD, "tests/fixtures/x.py": ""})
    assert tc.test_levels(root, sha) == {"tests": 2}


def test_a_repository_without_tests_has_no_levels(tmp_path: Path) -> None:
    root, sha = repo(tmp_path, {"src/a.py": "X = 1\n"})
    assert tc.test_levels(root, sha) == {}
    assert tc.test_levels(root, sha, root="spec") == {}
    assert tree_paths(root, sha, "nowhere") == []


def test_notes_come_from_docs_testing_then_the_testing_sections_clipped(tmp_path: Path) -> None:
    root, sha = repo(
        tmp_path,
        {
            "docs/testing.md": "Use fakes, not mocks.\n",
            "AGENTS.md": "# x\n\n## Setup\nuv sync\n\n## Testing\nRun task test.\n\n## Release\nTag it.\n",
            "CONTRIBUTING.md": "## Testing\nAdd a test per bug.\n",
        },
    )
    notes = tc.testing_notes(root, sha)
    assert notes.index("Use fakes, not mocks.") < notes.index("Run task test.") < notes.index("Add a test")
    assert "docs/testing.md" in notes and "AGENTS.md" in notes and "CONTRIBUTING.md" in notes
    assert "uv sync" not in notes and "Tag it." not in notes
    clipped = tc.testing_notes(root, sha, limit=40)
    assert len(clipped) <= 40 + len("[clipped]") + 1 and clipped.endswith("[clipped]")
    (tmp_path / "other").mkdir()
    empty, base = repo(tmp_path / "other", {"src/a.py": ""})
    assert tc.testing_notes(empty, base) == ""


def test_conventions_text_lists_gate_levels_guidelines_and_the_routed_one_in_full(tmp_path: Path) -> None:
    root, sha = repo(
        tmp_path, {"tests/unit/test_a.py": MOD, "tests/e2e/test_b.py": MOD, "docs/testing.md": "N.\n"}
    )
    lib = tmp_path / "qa"
    lib.mkdir()
    (lib / "unit.md").write_text("---\nsummary: Pure functions in isolation\n---\n# Unit\nKeep it fast.\n")
    (lib / "e2e.md").write_text("---\nsummary: Whole flows through the real UI\n---\n# E2E\nA browser.\n")
    skills = {s.name: s for s in conventions(root, sha, lib, None).guidelines}
    assert set(skills) == {"unit", "e2e"}
    c = conventions(root, sha, lib, skills["unit"])
    assert isinstance(c, Conventions) and c.routed is skills["unit"] and c.levels == {"unit": 1, "e2e": 1}
    text = conventions_text(c, ["uv run pytest -q", "uv run ruff check ."])
    assert "uv run pytest -q" in text and "uv run ruff check ." in text
    assert "unit: 1 test module" in text and "e2e: 1 test module" in text
    assert "Pure functions in isolation" in text and "Whole flows through the real UI" in text
    assert "Keep it fast." in text and "A browser." not in text  # the routed one in full, others by summary
    assert "N." in text
    plain = conventions_text(conventions(root, sha, None, None), [])
    assert "Keep it fast." not in plain and "no guideline library" in plain
    assert isinstance(skills["unit"], Skill)


def test_a_top_level_directory_with_vitest_files_is_a_level_of_its_own(tmp_path: Path) -> None:
    """The web-tests design (2026-10-27): airlie's 68 Vitest files under web/ are a level, `web`, beside the
    pytest levels, so the tester can name a Vue layer's tests; node_modules never counts."""
    root, sha = repo(
        tmp_path,
        {
            "tests/unit/test_a.py": MOD,
            "web/src/router.test.ts": "it('x', () => { expect(1).toBe(1) })\n",
            "web/src/stores/a.spec.ts": "it('y', () => { expect(1).toBe(1) })\n",
            "web/node_modules/pkg/b.test.js": "",
            "web/src/Wiki.vue": "<template/>",
        },
    )
    assert tc.test_levels(root, sha) == {"unit": 1, "web": 2}
    c = conventions(root, sha, None, None)
    assert c.js_levels == {"web": 2}
    text = conventions_text(c, ["task web:gate"])
    assert "unit: 1 test module" in text
    assert (
        "web: 2 Vitest files (under web/, named it(...) titles, `describe > title` inside a describe)" in text
    )
