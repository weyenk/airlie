"""The repository a Jira story is for: its one repo:<owner>/<name> label, the checkout under story.repos_root
that bears that name in any case, and that checkout's remote as written."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from airlie.story.checkout import check_checkout, find_checkout, repo_label, repo_labels
from airlie.story.github_app import masked
from airlie.story.model import RepositoryError, StorySetupError
from tests.unit.git_helpers import git

# ---- the label ----


@pytest.mark.parametrize(
    "labels",
    [(), ("frontend",), ("repository:weyenk/OpenHand", "repos:weyenk/OpenHand", "repo-weyenk/OpenHand")],
)
def test_an_issue_without_a_repo_label_names_no_repository(labels: tuple[str, ...]) -> None:
    with pytest.raises(RepositoryError) as e:
        repo_label(labels)
    assert str(e.value) == (
        "the issue names no repository: add the label repo:<owner>/<name> (repo:weyenk/OpenHand, say), the "
        "GitHub repository it is for"
    )


def test_the_one_repo_label_names_the_repository_whatever_the_case_of_its_prefix() -> None:
    assert repo_label(["frontend", "repo:weyenk/OpenHand", "p1"]) == "weyenk/OpenHand"
    for prefix in ("REPO:", "Repo:", "rEpO:"):  # owner and name keep their own case
        assert repo_label([f"{prefix}weyenk/OpenHand"]) == "weyenk/OpenHand"
    assert repo_label(["repo:We.yen-k_1/Open.Hand-2_x"]) == "We.yen-k_1/Open.Hand-2_x"  # GitHub's characters


def test_two_repo_labels_are_one_too_many_and_both_are_named() -> None:
    with pytest.raises(RepositoryError) as e:
        repo_label(["repo:weyenk/OpenHand", "x", "REPO:weyenk/airlie"])
    assert str(e.value) == (
        "the issue has 2 repo: labels ('repo:weyenk/OpenHand', 'REPO:weyenk/airlie'); keep the one that "
        "names the repository it is for"
    )
    assert repo_labels(["repo:a/b", "x", "Repo:c/d"]) == ["repo:a/b", "Repo:c/d"]


@pytest.mark.parametrize(
    "label",
    [
        "repo:",
        "repo:weyenk",
        "repo:weyenk/",
        "repo:/OpenHand",
        "repo:weyenk/Open/Hand",
        "repo:weyenk/Open Hand",
        "repo:weyenk/Open+Hand",
        "repo:weyenk/Öpen",
        "repo:weyenk/..",
        "repo:./OpenHand",
        "repo:weyenk/OpenHand\n",
    ],
)
def test_a_malformed_repo_label_is_named(label: str) -> None:
    with pytest.raises(RepositoryError) as e:
        repo_label([label])
    assert str(e.value) == (
        f"the label {label!r} is not repo:<owner>/<name> (each of letters, digits, '.', '_' and '-'; "
        "repo:weyenk/OpenHand, say)"
    )


def test_a_long_label_is_cut_where_it_is_quoted() -> None:
    label = "repo:" + "x" * 300
    with pytest.raises(RepositoryError) as e:
        repo_label([label])
    assert "x" * 81 not in str(e.value) and "…" in str(e.value)


def test_a_repo_label_is_a_story_setup_error_tick_sets_aside() -> None:
    assert issubclass(RepositoryError, StorySetupError)


# ---- the checkout ----


def test_the_checkout_is_the_directory_under_the_root_named_after_the_repository_in_any_case(
    tmp_path: Path,
) -> None:
    root = tmp_path / "git"
    (root / "OpenHand").mkdir(parents=True)
    (root / "airlie").mkdir()
    (root / "openhand.txt").write_text("not a checkout")
    assert find_checkout(root, "weyenk/openhand") == root / "OpenHand"
    assert find_checkout(root, "weyenk/OPENHAND") == root / "OpenHand"
    assert find_checkout(root, "someone/Airlie") == root / "airlie"  # the owner plays no part


@pytest.mark.parametrize("how", ["empty", "missing", "a file"])
def test_no_checkout_names_the_root_and_says_to_clone_the_repository_there(tmp_path: Path, how: str) -> None:
    root = tmp_path / "git"
    if how != "missing":
        root.mkdir()
    if how == "a file":
        (root / "OpenHand").write_text("a file, not a directory")
    with pytest.raises(RepositoryError) as e:
        find_checkout(root, "weyenk/OpenHand")
    assert str(e.value).startswith(
        f"no checkout of weyenk/OpenHand in {root} (story.repos_root): clone it there: "
        f"git clone git@github.com:weyenk/OpenHand.git {root / 'OpenHand'}"
    )


def test_two_checkouts_of_one_name_are_both_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "git"
    for name in ("OpenHand", "openhand"):  # one directory where names are case-insensitive (APFS), else two
        (root / name).mkdir(parents=True, exist_ok=True)

    def both(self: Path) -> Iterator[Path]:  # what a case-sensitive volume lists
        yield from (self / "openhand", self / "OpenHand")

    monkeypatch.setattr(Path, "iterdir", both)
    with pytest.raises(RepositoryError) as e:
        find_checkout(root, "weyenk/OpenHand")
    assert str(e.value) == (
        f"more than one directory in {root} (story.repos_root) is named OpenHand in some case: "
        f"{root / 'OpenHand'}, {root / 'openhand'}; keep the one that is the checkout of weyenk/OpenHand"
    )


# ---- the checkout's remote ----


def clone_of(path: Path, url: str | None) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    if url is not None:
        git(path, "remote", "add", "origin", url)
    return path


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:weyenk/OpenHand.git",
        "git@github.com:weyenk/OpenHand",
        "ssh://git@github.com/weyenk/OpenHand.git",
        "https://github.com/weyenk/OpenHand.git",
        "https://github.com/weyenk/openhand",  # GitHub's names are any case
    ],
)
def test_a_checkout_whose_remote_names_the_repository_in_any_form_is_the_one(
    tmp_path: Path, url: str
) -> None:
    check_checkout(clone_of(tmp_path / "OpenHand", url), "origin", "weyenk/OpenHand")


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:weyenk/other.git",
        "ssh://git@github.com/weyenk/other.git",
        "https://github.com/weyenk/other.git",
        "https://github.com/someone/OpenHand.git",
        "git@gitlab.com:weyenk/OpenHand.git",
        "/Users/someone/OpenHand.git",
    ],
)
def test_a_remote_that_names_another_repository_is_named_with_the_one_expected(
    tmp_path: Path, url: str
) -> None:
    path = clone_of(tmp_path / "OpenHand", url)
    with pytest.raises(RepositoryError) as e:
        check_checkout(path, "origin", "weyenk/OpenHand")
    shown = masked(url)  # as every reason that reaches the issue: ssh://git@ shows as ssh://***@
    assert str(e.value) == (
        f"{path} is not a checkout of weyenk/OpenHand: its remote 'origin' (story.remote) is {shown!r}, and "
        "the issue's label wants git@github.com:weyenk/OpenHand.git (or "
        "ssh://git@github.com/weyenk/OpenHand.git, or https://github.com/weyenk/OpenHand.git)"
    )


def test_the_remote_is_read_as_written_and_a_password_in_it_is_masked(tmp_path: Path) -> None:
    path = clone_of(tmp_path / "OpenHand", "https://me:s3cret@github.com/weyenk/other.git")
    # an insteadOf rule would send it to the right repository; the remote as written still names another
    git(
        path,
        "config",
        "url.https://github.com/weyenk/OpenHand.git.insteadOf",
        "https://me:s3cret@github.com/",
    )
    with pytest.raises(RepositoryError) as e:
        check_checkout(path, "origin", "weyenk/OpenHand")
    assert "s3cret" not in str(e.value) and "'https://***@github.com/weyenk/other.git'" in str(e.value)


def test_a_checkout_without_the_remote_says_how_to_add_it(tmp_path: Path) -> None:
    path = clone_of(tmp_path / "OpenHand", "git@github.com:weyenk/OpenHand.git")
    with pytest.raises(RepositoryError) as e:
        check_checkout(path, "upstream", "weyenk/OpenHand")
    assert str(e.value) == (
        f"{path} has no remote 'upstream' (story.remote): add it with "
        f"git -C {path} remote add upstream git@github.com:weyenk/OpenHand.git"
    )


def test_a_directory_that_is_not_a_git_repository_is_no_checkout(tmp_path: Path) -> None:
    path = tmp_path / "OpenHand"
    path.mkdir()
    with pytest.raises(RepositoryError) as e:
        check_checkout(path, "origin", "weyenk/OpenHand")
    assert str(e.value) == (
        f"{path} is not a git repository: clone weyenk/OpenHand there "
        "(git clone git@github.com:weyenk/OpenHand.git)"
    )
