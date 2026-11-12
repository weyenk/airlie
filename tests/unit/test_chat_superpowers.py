"""The vendored superpowers library: skills, siblings, the bootstrap, and the manifest check."""

import hashlib
from pathlib import Path

import pytest

from airlie.chat.superpowers import MANIFEST, SuperpowersLibrary, main, verify_manifest

ROOT = Path(__file__).resolve().parents[2]


def _skill(root: Path, name: str, description: str | None, body: str = "Body.\n") -> Path:
    d = root / "skills" / name
    d.mkdir(parents=True)
    front = (
        f"---\nname: {name}\ndescription: {description}\n---\n"
        if description is not None
        else "---\nname: x\n---\n"
    )
    (d / "SKILL.md").write_text(front + "\n" + body)
    return d


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    _skill(tmp_path, "zeta", "Use when zeta.")
    d = _skill(tmp_path, "alpha", "Use when alpha.", "Alpha body.\n")
    (d / "prompt.md").write_text("sibling text\n")
    (d / "sub").mkdir()
    (d / "sub" / "deep.md").write_text("deep\n")
    _skill(tmp_path, "using-superpowers", "Use when starting.", "Start here.\n")
    (tmp_path / "airlie-tools.md").write_text("# airlie tool mapping\n| a | b |\n")
    return tmp_path


def _manifest(root: Path) -> None:
    lines = [
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root).as_posix()}"
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != MANIFEST
    ]
    (root / MANIFEST).write_text("\n".join(lines) + "\n")


def test_skills_are_listed_with_their_descriptions(tree: Path) -> None:
    lib = SuperpowersLibrary(tree)
    docs = lib.skills()
    assert [d.name for d in docs] == ["alpha", "using-superpowers", "zeta"]
    alpha = docs[0]
    assert alpha.description == "Use when alpha." and alpha.text == "Alpha body.\n"
    assert alpha.dir == tree / "skills" / "alpha"
    assert (
        lib.listing()
        == "- alpha: Use when alpha.\n- using-superpowers: Use when starting.\n- zeta: Use when zeta."
    )


def test_a_skill_without_a_description_is_an_error_naming_the_file(tree: Path) -> None:
    _skill(tree, "bare", None)
    with pytest.raises(ValueError, match="bare/SKILL.md"):
        SuperpowersLibrary(tree).skills()


def test_get_returns_a_skill_and_an_unknown_one_names_the_known_ones(tree: Path) -> None:
    lib = SuperpowersLibrary(tree)
    assert lib.get("zeta").name == "zeta"
    with pytest.raises(KeyError) as e:
        lib.get("nope")
    assert "nope" in str(e.value) and "alpha, using-superpowers, zeta" in str(e.value)


def test_bootstrap_is_using_superpowers_then_the_airlie_mapping(tree: Path) -> None:
    assert SuperpowersLibrary(tree).bootstrap() == (
        "# using-superpowers\n\nStart here.\n\n# How the skills' actions map to airlie's tools\n\n"
        "# airlie tool mapping\n| a | b |\n"
    )


def test_read_sibling_returns_a_file_in_the_skill_directory(tree: Path) -> None:
    lib = SuperpowersLibrary(tree)
    assert lib.read_sibling("alpha", "prompt.md") == "sibling text\n"
    assert lib.read_sibling("alpha", "sub/deep.md") == "deep\n"
    assert lib.read_sibling("alpha", "sub/../prompt.md") == "sibling text\n"


def test_read_sibling_refuses_a_dotdot_escape(tree: Path) -> None:
    (tree / "skills" / "secret.txt").write_text("no")
    with pytest.raises(ValueError, match="leaves the alpha skill directory"):
        SuperpowersLibrary(tree).read_sibling("alpha", "../secret.txt")


def test_read_sibling_refuses_an_absolute_path(tree: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        SuperpowersLibrary(tree).read_sibling("alpha", str(tree / "skills" / "alpha" / "prompt.md"))


def test_read_sibling_refuses_a_symlink_out_of_the_directory(tree: Path) -> None:
    outside = tree / "outside.txt"
    outside.write_text("no")
    (tree / "skills" / "alpha" / "link.md").symlink_to(outside)
    with pytest.raises(ValueError, match="leaves the alpha skill directory"):
        SuperpowersLibrary(tree).read_sibling("alpha", "link.md")


def test_read_sibling_of_a_missing_file_or_a_directory_is_not_found(tree: Path) -> None:
    lib = SuperpowersLibrary(tree)
    with pytest.raises(FileNotFoundError, match="alpha has no file 'gone.md'"):
        lib.read_sibling("alpha", "gone.md")
    with pytest.raises(FileNotFoundError):
        lib.read_sibling("alpha", "sub")
    with pytest.raises(KeyError):
        lib.read_sibling("nope", "x")


def test_verify_manifest_reports_a_changed_a_missing_and_an_extra_file(tree: Path) -> None:
    _manifest(tree)
    assert verify_manifest(tree) == []
    (tree / "airlie-tools.md").write_text("edited\n")
    (tree / "skills" / "alpha" / "prompt.md").unlink()
    (tree / "skills" / "alpha" / "extra.md").write_text("x")
    assert verify_manifest(tree) == [
        "changed: airlie-tools.md",
        "missing: skills/alpha/prompt.md",
        "not in the manifest: skills/alpha/extra.md",
    ]


def test_main_prints_problems_and_exits_one_or_zero(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _manifest(tree)
    assert main([str(tree)]) == 0 and capsys.readouterr().out == ""
    (tree / "extra.md").write_text("x")
    assert main([str(tree)]) == 1 and capsys.readouterr().out == "not in the manifest: extra.md\n"
    assert main([]) == 2
    assert capsys.readouterr().err == "usage: python -m airlie.chat.superpowers <vendor/superpowers>\n"


def test_a_manifest_path_may_hold_two_spaces(tree: Path) -> None:
    (tree / "two  spaces.md").write_text("x")
    _manifest(tree)
    assert verify_manifest(tree) == []


def test_the_committed_vendor_tree_matches_its_manifest() -> None:
    assert verify_manifest(ROOT / "vendor" / "superpowers") == []


def test_the_committed_bootstrap_is_under_2000_tokens() -> None:
    text = SuperpowersLibrary(ROOT / "vendor" / "superpowers").bootstrap()
    assert "airlie's tools" in text and len(text) / 4 <= 2000, len(text) / 4
