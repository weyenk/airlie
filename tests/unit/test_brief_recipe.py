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
    resolve,
    span,
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


def inputs(
    repo: Path | None = None, *, brief: str | None = None, survey: str = "", steps: tuple[str, ...] = ()
) -> BriefInputs:
    """Layer 1 of the shared story; with a `repo`, its scope, brief and package name the repo's files.
    `brief`, `survey` and `steps` override the layer brief, the survey notes and the package's steps."""
    if repo is None:
        return BriefInputs(STORY, LAYERS, 1, PACKAGE, Path("."), "HEAD", 196_608, survey=survey)
    layers = (
        LAYERS[0],
        replace(LAYERS[1], scope="src/a.py, tests/test_a.py", brief=brief or "Update `src/b.md`."),
    )
    package = Package(
        "profile",
        {"tests/test_a.py": "def test_locked():\n    pass\n"},
        (PackageFile("src/a.py", "modify", "change"),),
        (),
        steps,
        {},
        "done",
    )
    return BriefInputs(
        STORY, layers, 1, package, repo, git(repo, "rev-parse", "HEAD"), 196_608, survey=survey
    )


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


def _numbered(total: int, special: dict[int, str]) -> str:
    return "".join(special.get(n, f"V{n} = {n}\n") for n in range(1, total + 1))


PROTOCOL = _numbered(
    60,
    {40: "def cut_off_text(x):\n", **{n: "    y = x\n" for n in range(41, 48)}, 48: "    return x\n"},
)
BIG_CLASS = _numbered(
    220,
    {
        20: "class Big:\n",
        **{n: "    x = 1\n" for n in range(21, 221)},
        120: "    def method(self):\n",
        **{n: "        pass\n" for n in range(121, 125)},
        125: "        return 1\n",
    },
)
MD = "".join(f"line {n}\n" for n in range(1, 11))
TREE = [
    "docs/x.md",
    "src/a/util.py",
    "src/b/util.py",
    "src/big.py",
    "src/router/protocol.py",
    "src/teams/protocol.py",
]


@pytest.fixture
def span_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    for name, text in {
        "src/teams/protocol.py": PROTOCOL,
        "src/router/protocol.py": "R = 1\n",
        "src/big.py": BIG_CLASS,
        "src/a/util.py": "A = 1\n" * 5,
        "src/b/util.py": "B = 1\n" * 5,
        "docs/x.md": MD,
    }.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def test_a_bare_name_resolves_through_a_full_path_the_story_names() -> None:
    assert resolve("protocol.py", ["src/teams/protocol.py"], TREE) == ["src/teams/protocol.py"]


def test_a_bare_name_with_two_files_and_no_full_path_is_ambiguous() -> None:
    assert sorted(resolve("protocol.py", [], TREE)) == ["src/router/protocol.py", "src/teams/protocol.py"]


def test_a_span_widens_to_its_innermost_def_of_at_most_80_lines() -> None:
    assert span(PROTOCOL, "src/teams/protocol.py", 42, 45) == (40, 48)
    assert span(BIG_CLASS, "src/big.py", 121, 122) == (120, 125)  # the method, not the 200-line class
    assert span(BIG_CLASS, "src/big.py", 10, 10) == (1, 20)  # outside any def: N+-10, clamped at 1
    assert span(MD, "docs/x.md", 3, 5) == (3, 5)  # not Python: as cited


def test_cited_spans_show_the_span_and_list_the_ambiguous(span_repo: Path) -> None:
    part = SECTIONS["cited_spans"](
        inputs(
            span_repo,
            brief="protocol.py:42-48",
            survey="see src/teams/protocol.py:18-48",
            steps=("util.py:3",),
        ),
        RecipeEntry(section="cited_spans"),
        None,
        frozenset(),
    )
    assert "### src/teams/protocol.py (lines 40-48)" in part.text
    assert part.shown["src/teams/protocol.py"] == "span"
    assert "- util.py:3 (more than one file has this name)" in part.text


def test_a_citation_past_the_end_or_to_a_missing_file_is_listed(span_repo: Path) -> None:
    part = SECTIONS["cited_spans"](
        inputs(span_repo, brief="gone.py:10 and src/teams/protocol.py:900"),
        RecipeEntry(section="cited_spans"),
        None,
        frozenset(),
    )
    assert "- gone.py:10 (no such file at this layer's base)" in part.text
    assert "### src/teams/protocol.py (lines 50-60)" in part.text


def test_spans_past_the_cap_are_listed_and_a_shown_file_is_skipped(span_repo: Path) -> None:
    entry = RecipeEntry(section="cited_spans")
    brief = "src/teams/protocol.py:42-48 and src/big.py:121"
    part = SECTIONS["cited_spans"](inputs(span_repo, brief=brief), entry, 190, frozenset())
    assert "### src/teams/protocol.py (lines 40-48)" in part.text
    assert "- src/big.py:120-125" in part.text and "Not shown" in part.text
    skipped = SECTIONS["cited_spans"](inputs(span_repo, brief=brief), entry, None, frozenset({"src/big.py"}))
    assert "src/big.py" not in skipped.text


def test_overlapping_spans_in_one_file_merge(span_repo: Path) -> None:
    brief = "src/teams/protocol.py:3-5 src/teams/protocol.py:5-8 src/teams/protocol.py:55-56"
    entry = RecipeEntry(section="cited_spans")
    part = SECTIONS["cited_spans"](inputs(span_repo, brief=brief), entry, None, frozenset())
    assert "(lines 3-8)" in part.text and "(lines 55-56)" in part.text


def test_no_citations_is_an_empty_part(span_repo: Path) -> None:
    entry = RecipeEntry(section="cited_spans")
    assert SECTIONS["cited_spans"](inputs(span_repo), entry, None, frozenset()).text == ""
