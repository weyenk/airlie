"""What the architect is handed: AGENTS.md and the files the story names, from the base commit."""

from __future__ import annotations

from pathlib import Path

from airlie.config import load_config
from airlie.story.context import FILE_HEAD_CHARS, guide_budget, named_paths, repo_guide
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


def test_a_leading_dot_slash_is_dropped_but_a_dot_directory_is_kept() -> None:
    assert named_paths("see ./.github/workflows/a.yml") == [".github/workflows/a.yml"]


def test_a_markdown_file_with_fences_cannot_break_out_of_its_own(tmp_path: Path) -> None:
    body = "# Notes\n\n```sh\ntask test\n```\n\nafter the fence\n"
    repo, sha = repo_with(tmp_path, {"docs/a.md": body})
    whole = repo_guide(repo, sha, "Read docs/a.md", budget_chars=10_000)
    assert f"### docs/a.md (whole, 7 lines)\n````\n{body}\n````" in whole
    big = body + "".join(f"line {i}\n" for i in range(3000))
    (tmp_path / "big").mkdir()
    repo2, sha2 = repo_with(tmp_path / "big", {"docs/a.md": big})
    card = repo_guide(repo2, sha2, "Read docs/a.md", budget_chars=4_000)
    assert (
        "(outline: read a span" in card and "\n````\ndocs/a.md: " in card and card.rstrip().endswith("````")
    )


def test_the_guide_is_agents_md_and_the_files_the_story_names_that_exist(tmp_path: Path) -> None:
    repo, sha = repo_with(tmp_path, {"AGENTS.md": "# rules\nrun the gate\n", "src/m.py": "x = 1\n"})
    guide = repo_guide(repo, sha, "Change src/m.py and src/missing.py", budget_chars=10_000)
    assert "### AGENTS.md\n# rules" in guide and "### src/m.py (whole, 1 lines)\n```\nx = 1" in guide
    assert "missing.py" not in guide


def test_a_repository_without_agents_md_or_named_files_gets_an_empty_guide(tmp_path: Path) -> None:
    repo, sha = repo_with(tmp_path, {"a.txt": "x"})
    assert repo_guide(repo, sha, "nothing named here", budget_chars=10_000) == ""


def test_the_guide_reads_the_commit_not_the_worktree(tmp_path: Path) -> None:
    repo, sha = repo_with(tmp_path, {"AGENTS.md": "committed\n"})
    (repo / "AGENTS.md").write_text("edited since\n")
    assert "committed" in repo_guide(repo, sha, "", budget_chars=10_000) and "edited since" not in repo_guide(
        repo, sha, "", budget_chars=10_000
    )


def test_a_file_that_fits_goes_in_whole(tmp_path: Path) -> None:
    body = "".join(f"line{i} = {i}\n" for i in range(50))
    repo, sha = repo_with(tmp_path, {"src/m.py": body})
    guide = repo_guide(repo, sha, "Change src/m.py", budget_chars=10_000)
    assert "### src/m.py (whole, 50 lines)" in guide and "line49 = 49" in guide


def test_a_file_over_the_budget_goes_in_as_an_outline(tmp_path: Path) -> None:
    lines = ["x = 1"] * 3000
    lines[9] = "def first(): pass"
    lines[2899] = "def last(): pass"
    repo, sha = repo_with(tmp_path, {"src/big.py": "\n".join(lines) + "\n"})
    guide = repo_guide(repo, sha, "Change src/big.py", budget_chars=5_000)
    assert "### src/big.py (outline:" in guide and "2900: def" in guide


def test_the_guide_never_exceeds_its_budget(tmp_path: Path) -> None:
    def big(n: int) -> str:  # 40k chars, 250 defs: an outline of about 5k
        defs = "".join(f"def f{n}_{k}(): pass\n" for k in range(250))
        return defs + "# padding padding padding padding\n" * 1000

    files = {f"src/f{i}.py": big(i) for i in range(7)}
    repo, sha = repo_with(tmp_path, {"AGENTS.md": "rules\n", **files})
    text = " ".join(f"src/f{i}.py" for i in range(7))
    for budget in (20_000, 500, 100, 60):
        assert len(repo_guide(repo, sha, text, budget_chars=budget)) <= budget
    guide = repo_guide(repo, sha, text, budget_chars=20_000)
    assert "### Also named (not shown)" in guide and "- src/f6.py (1250 lines)" in guide


def test_a_big_non_python_file_gets_its_head(tmp_path: Path) -> None:
    body = "".join(f"word{i} " * 9 + "\n" for i in range(1500))
    assert len(body) > 100_000 * 0.9
    repo, sha = repo_with(tmp_path, {"docs/x.md": body})
    guide = repo_guide(repo, sha, "Read docs/x.md", budget_chars=8_000)
    assert "### docs/x.md (outline:" in guide
    fenced = guide.split("```\n")[1]
    assert 0 < len(fenced) <= FILE_HEAD_CHARS + len("docs/x.md: 1500 lines\n") + 5


def test_guide_budget_is_a_share_of_the_smallest_readers_context() -> None:
    root = Path(__file__).resolve().parents[2]
    cfg = load_config(root / "airlie.yaml", root / "no-local-overrides.yaml")
    assert guide_budget(cfg, ["tester", "coder"]) == 39321


def test_a_large_agents_md_never_pushes_the_guide_over_budget(tmp_path: Path) -> None:
    repo, sha = repo_with(tmp_path, {"AGENTS.md": "g" * 7000})
    for budget in (0, 100, 4000, 6020):
        assert len(repo_guide(repo, sha, "", budget_chars=budget)) <= budget


def test_a_small_file_after_a_big_one_still_goes_in_whole(tmp_path: Path) -> None:
    big = "def f(): pass\n" + "x = 1\n" * 4000
    repo, sha = repo_with(tmp_path, {"src/big.py": big, "src/small.py": "a = 1\nb = 2\n"})
    guide = repo_guide(repo, sha, "src/big.py src/small.py", budget_chars=12_000)
    assert "### src/big.py (outline:" in guide
    assert "### src/small.py (whole, 2 lines)" in guide and "b = 2" in guide
