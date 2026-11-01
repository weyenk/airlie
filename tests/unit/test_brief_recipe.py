"""A brief from a recipe: its entries checked, its parts joined, their sizes told."""

import re
from dataclasses import replace
from pathlib import Path

import pytest

from airlie.story.brief_recipe import (
    SECTIONS,
    BriefInputs,
    RecipeEntry,
    RecipeError,
    assemble,
    load_recipe,
    parse_recipe,
)
from airlie.story.package import Package, PackageFile
from tests.unit.git_helpers import git
from tests.unit.test_story_briefs import LAYERS, PACKAGE, STORY

BIG = "".join(f"def f{i}():\n    return {i}\n\n" for i in range(120))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    for name, text in {
        "src/a.py": BIG,
        "src/b.md": "# B\n" + "text\n" * 600,
        "tests/test_a.py": "base\n",
    }.items():
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(text)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def inputs(repo: Path | None = None) -> BriefInputs:
    """Layer 1 of the shared story; with a `repo`, its scope, brief and package name the repo's files."""
    if repo is None:
        return BriefInputs(STORY, LAYERS, 1, PACKAGE, Path("."), "HEAD", 196_608)
    layers = (LAYERS[0], replace(LAYERS[1], scope="src/a.py, tests/test_a.py", brief="Update `src/b.md`."))
    package = Package(
        "profile",
        {"tests/test_a.py": "def test_locked():\n    pass\n"},
        (PackageFile("src/a.py", "modify", "change"),),
        (),
        (),
        {},
        "done",
    )
    return BriefInputs(STORY, layers, 1, package, repo, git(repo, "rev-parse", "HEAD"), 196_608)


def test_a_recipe_is_its_sections_and_rules_in_order() -> None:
    r = parse_recipe("- section: layer_context\n- rule: brief-layer-plan-agents-md\n", "t")
    assert r == (RecipeEntry(section="layer_context"), RecipeEntry(rule="brief-layer-plan-agents-md"))


@pytest.mark.parametrize(
    "text, phrase",
    [
        ("- section: nope\n", "unknown section 'nope'"),
        ("- rule: no-such-rule\n", "unknown rule 'no-such-rule'"),
        ("- section: package\n  shares: 0.2\n", "unknown key 'shares'"),
        ("- section: package\n  rule: package-contract\n", "both section and rule"),
        ("- section: package\n  over: outline\n", "package does not take over"),
        ("- section: package\n  share: 1.5\n", "share must be between 0 and 1"),
    ],
)
def test_a_wrong_recipe_is_refused_naming_the_entry(text: str, phrase: str) -> None:
    with pytest.raises(RecipeError, match=re.escape(phrase)) as e:
        parse_recipe(text, "t")
    assert "recipe t, entry 0" in str(e.value)


def test_a_missing_recipe_is_refused_by_name() -> None:
    with pytest.raises(RecipeError, match="no-such-recipe"):
        load_recipe("no-such-recipe")


def test_the_assembled_brief_is_its_parts_and_their_sizes_add_up() -> None:
    recipe = parse_recipe("- section: layer_context\n- section: package\n- rule: package-contract\n", "t")
    brief = assemble("t", recipe, inputs())
    assert brief.text == "\n\n".join(p.text for p in brief.parts if p.text)
    joins = 2 * (len([p for p in brief.parts if p.text]) - 1)
    assert sum(s["chars"] for s in brief.sizes()) + joins == len(brief.text)
    assert brief.text.startswith(f"Story: {STORY.title}.")
    assert "## The tests you make pass (in the worktree, locked)" in brief.text


def test_locked_tests_are_the_package_text_whole() -> None:
    part = SECTIONS["locked_tests"](inputs(), RecipeEntry(section="locked_tests"), None, frozenset())
    assert PACKAGE.tests["tests/unit/test_profile.py"] in part.text
    assert part.shown == {"tests/unit/test_profile.py": "whole"}


def test_scope_files_leave_out_the_locked_tests_and_show_the_layers_files_then_the_brief_named_ones(
    repo: Path,
) -> None:
    recipe = parse_recipe("- section: locked_tests\n- section: scope_files\n", "t")
    brief = assemble("t", recipe, inputs(repo))
    assert brief.text.count("### tests/test_a.py") == 1
    assert "def test_locked" in brief.text and "base\n" not in brief.text
    assert brief.text.index("### src/a.py") < brief.text.index("### src/b.md")


def test_scope_files_skip_what_an_earlier_part_shows_whole(repo: Path) -> None:
    entry = RecipeEntry(section="scope_files")
    part = SECTIONS["scope_files"](inputs(repo), entry, None, frozenset({"src/a.py"}))
    assert "src/a.py" not in part.shown and "src/b.md" in part.shown


def test_a_file_past_the_cap_is_outlined_then_listed(repo: Path) -> None:
    entry = RecipeEntry(section="scope_files", over="outline")
    part = SECTIONS["scope_files"](inputs(repo), entry, 2_000, frozenset())
    assert "(outline: read a span with read_file offset and limit)" in part.text
    assert "### Not shown (read with read_file)\n- " in part.text
    assert set(part.shown.values()) == {"outline", "listed"}


def test_a_small_context_lists_what_does_not_fit(repo: Path) -> None:
    part = SECTIONS["scope_files"](inputs(repo), RecipeEntry(section="scope_files"), 50, frozenset())
    assert "### Not shown (read with read_file)" in part.text
    assert "whole" not in part.shown.values()
    assert "- src/a.py (360 lines)" in part.text


def test_no_files_is_an_empty_part(repo: Path) -> None:
    entry = RecipeEntry(section="scope_files")
    part = SECTIONS["scope_files"](inputs(repo), entry, None, frozenset({"src/a.py", "src/b.md"}))
    assert part.text == "" and part.shown == {}
