"""The story dossier (P2 of the performance plan): the files earlier steps read, for later briefs."""

from __future__ import annotations

import json
from pathlib import Path

from airlie.story.dossier import DOSSIER_FILES, dossier, read_paths, story_dossier
from tests.unit.test_story_layer_checks import repo


def step(run: Path, team: str, n: int, name: str, args: object) -> None:
    path = run / "trace" / team / f"step-{n:03d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"u_t": {"name": name, "arguments": args}, "o_t": "", "ok": True}))


def test_the_paths_read_are_most_read_first_then_first_read_first(tmp_path: Path) -> None:
    """Roman noise run 8 (2026-10-28) read sandbox/__init__.py and tests/test_version.py six times each."""
    a, b = tmp_path / "runs" / "a", tmp_path / "runs" / "b"
    step(a, "survey", 1, "read_file", json.dumps({"path": "README.md"}))
    step(a, "survey", 2, "read_file", json.dumps({"path": "./src/app.py"}))
    step(a, "survey", 3, "list_dir", json.dumps({"path": "src"}))  # a listing is no file read
    step(b, "layers", 1, "read_file", {"path": "src/app.py"})  # arguments already an object
    step(b, "layers/round-1", 1, "read_file", json.dumps({"path": ".gitignore"}))
    (b / "trace" / "layers" / "step-009.json").write_text("not json")  # skipped
    assert read_paths([a, b, tmp_path / "runs" / "gone"]) == ["src/app.py", "README.md", ".gitignore"]


def test_the_dossier_is_the_files_text_at_a_commit_without_what_is_not_there_or_skipped(
    tmp_path: Path,
) -> None:
    root, base = repo(tmp_path, {"src/app.py": "x = 1\n", "README.md": "# hi\n"})
    got = dossier(root, base, ["src/app.py", "tests/test_new.py", "src", "README.md"], skip=["README.md"])
    assert got == {"src/app.py": "x = 1\n"}  # a file a later layer creates, a directory, a skipped one: none
    many = {f"m{n}.py": f"n = {n}\n" for n in range(DOSSIER_FILES + 3)}
    (tmp_path / "two").mkdir()
    root2, base2 = repo(tmp_path / "two", many)
    assert len(dossier(root2, base2, list(many))) == DOSSIER_FILES


def test_a_story_dossier_reads_its_runs_then_the_commit(tmp_path: Path) -> None:
    root, base = repo(tmp_path, {"src/app.py": "x = 1\n"})
    run = tmp_path / "runs" / "r1"
    step(run, "survey", 1, "read_file", json.dumps({"path": "src/app.py"}))
    assert story_dossier(root, base, [run]) == {"src/app.py": "x = 1\n"}
    assert story_dossier(root, base, []) == {}


def test_each_brief_carries_the_dossier_and_a_package_brief_not_its_own_sources_twice() -> None:
    from airlie.story.amigos import opening_message
    from airlie.story.briefs import build_brief, critic_brief, dossier_section, layers_brief, package_brief
    from airlie.story.package import Package
    from tests.unit.test_story_briefs import LAYERS, STORY

    files = {"src/app.py": "x = 1\n", "README.md": "# hi\n"}
    heading = dossier_section(files)[0].splitlines()[0]
    assert heading.startswith("## Files the story's earlier steps already read")
    assert dossier_section({}) == []
    assert heading in layers_brief(STORY, 3, dossier=files) and heading not in layers_brief(STORY, 3)
    assert heading in opening_message(STORY, LAYERS, "", (), files)
    assert heading in critic_brief(LAYERS[0], {"tests/test_a.py": "def test_a(): ..."}, (), "", dossier=files)
    pkg = Package(LAYERS[0].slug, {}, (), (), ("write it",), {}, "")
    assert heading in build_brief(STORY, LAYERS, 0, pkg, dossier=files)
    brief = package_brief(
        STORY, LAYERS, 0, max_steps=10, context_budget=1000, sources={"src/app.py": "x = 1\n"}, dossier=files
    )
    assert brief.count("### src/app.py") == 1 and "### README.md" in brief  # lever 2 holds app.py already


def test_a_dossier_leaves_out_what_the_guide_shows_whole_and_lists_what_does_not_fit() -> None:
    from airlie.story.briefs import dossier_section

    files = {
        "src/a.py": "a\n" * 10,
        "src/big.py": "b" * 50_000,  # clipped at 12k
        "src/e.py": "e" * 11_000,
        "src/f.py": "f" * 11_000,  # the 30k budget is spent by here
        "src/c.py": "c\n",
        "src/d.py": "d\n" * 4,
    }
    [text] = dossier_section(files, shown={"src/a.py"})
    assert "### src/a.py" not in text
    assert "[clipped: read it with read_file]" not in text
    assert "### Not shown (read with read_file)\n" in text and "- src/d.py (4 lines)" in text
    assert text.count("### Not shown") == 1


def test_a_package_brief_carries_no_dossier_file_its_guide_shows_whole() -> None:
    from airlie.story.briefs import package_brief
    from tests.unit.test_story_briefs import LAYERS, STORY

    guide = "### src/x.py (whole, 1 lines)\n```py\nx = 1\n```"
    text = package_brief(
        STORY, LAYERS, 0, guide=guide, max_steps=10, context_budget=1000, dossier={"src/x.py": "x = 1\n"}
    )
    assert text.count("### src/x.py") == 1
