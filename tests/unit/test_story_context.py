"""What the architect is handed: AGENTS.md and the files the story names, from the base commit."""

from __future__ import annotations

from pathlib import Path

from airlie.story.context import FILE_CHARS, GUIDE_CHARS, MAX_FILES, named_paths, repo_guide
from tests.unit.git_helpers import git


def repo_with(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q")
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    return repo, git(repo, "rev-parse", "HEAD")


def test_paths_in_prose_are_found_once_in_order() -> None:
    text = "Edit src/a/schema.py and tests/unit/test_x.py, then schema.py, src/a/schema.py."
    assert named_paths(text) == ["src/a/schema.py", "tests/unit/test_x.py"]
    assert named_paths("see ./docs/a.md (and https://x.org/y.html)") == ["docs/a.md"]
    assert named_paths("no paths, just version 1.2.3 and e.g. foo.py") == []


def test_the_guide_is_agents_md_and_the_files_the_story_names_that_exist(tmp_path: Path) -> None:
    repo, sha = repo_with(tmp_path, {"AGENTS.md": "# rules\nrun the gate\n", "src/m.py": "x = 1\n"})
    guide = repo_guide(repo, sha, "Change src/m.py and src/missing.py")
    assert "### AGENTS.md\n# rules" in guide and "### src/m.py\n```\nx = 1" in guide
    assert "missing.py" not in guide


def test_a_repository_without_agents_md_or_named_files_gets_an_empty_guide(tmp_path: Path) -> None:
    repo, sha = repo_with(tmp_path, {"a.txt": "x"})
    assert repo_guide(repo, sha, "nothing named here") == ""


def test_the_guide_reads_the_commit_not_the_worktree(tmp_path: Path) -> None:
    repo, sha = repo_with(tmp_path, {"AGENTS.md": "committed\n"})
    (repo / "AGENTS.md").write_text("edited since\n")
    assert "committed" in repo_guide(repo, sha, "") and "edited since" not in repo_guide(repo, sha, "")


def test_the_guide_is_clipped_and_names_at_most_a_few_files(tmp_path: Path) -> None:
    files = {"AGENTS.md": "g" * (GUIDE_CHARS + 500)}
    files.update({f"src/f{i}.py": "z" * (FILE_CHARS + 500) for i in range(MAX_FILES + 2)})
    repo, sha = repo_with(tmp_path, files)
    guide = repo_guide(repo, sha, " ".join(f"src/f{i}.py" for i in range(MAX_FILES + 2)))
    assert guide.count("[clipped]") == MAX_FILES + 1 and guide.count("### src/") == MAX_FILES
    assert "f5.py" not in guide
