"""A repository's test gate: the commands of the first fenced block of the Test gate section of its root
AGENTS.md, read from a commit (never a worktree), joined with && into one command."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from airlie.story.gate import Gate, gate_commands, missing_gate, read_gate
from airlie.story.model import NoTestGate
from airlie.subproc import without_hooks
from tests.unit.git_helpers import git

GATE = """# OpenHand

Some notes.

## Test gate

Every change must pass these, in order:

```sh
pnpm install --frozen-lockfile --prefer-offline
pnpm static:ratchet
node scripts/run-impacted-tests.mjs
```

## Style

```sh
pnpm lint
```
"""


def test_each_line_of_the_first_block_under_the_heading_is_one_command_in_order() -> None:
    assert gate_commands(GATE) == (
        "pnpm install --frozen-lockfile --prefer-offline",
        "pnpm static:ratchet",
        "node scripts/run-impacted-tests.mjs",
    )
    gate = Gate(gate_commands(GATE), "b10b")
    assert gate.shown == (  # what a note or a PR body says
        "pnpm install --frozen-lockfile --prefer-offline && pnpm static:ratchet && "
        "node scripts/run-impacted-tests.mjs"
    )
    assert gate.command == (  # what runs: each command in a brace group of its own, ended by a newline
        "{ pnpm install --frozen-lockfile --prefer-offline\n} && { pnpm static:ratchet\n} && "
        "{ node scripts/run-impacted-tests.mjs\n}"
    )


def sh(commands: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """The gate of `commands` as the studio runs it: /bin/sh -c, in `cwd`."""
    command = Gate(tuple(commands), "b10b").command
    return subprocess.run(["/bin/sh", "-c", command], cwd=cwd, capture_output=True, text=True)


def test_an_inline_comment_ends_at_its_line_so_every_later_command_still_gates(tmp_path: Path) -> None:
    assert sh(["true   # deps", "false  # stands in for a suite that fails"], tmp_path).returncode == 1
    assert sh(["true   # deps", "true   # the suite"], tmp_path).returncode == 0


def test_the_first_command_that_fails_fails_the_gate_and_no_later_one_runs(tmp_path: Path) -> None:
    done = sh(["true", "false # the first failure", "touch RAN"], tmp_path)
    assert done.returncode == 1 and not (tmp_path / "RAN").exists()


def test_a_cd_carries_over_to_the_commands_after_it_and_a_brace_can_be_an_argument(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "MARK").write_text("")
    assert sh(["cd sub", "test -f MARK"], tmp_path).returncode == 0
    done = sh(["echo }", "echo {"], tmp_path)
    assert done.returncode == 0 and done.stdout.split() == ["}", "{"]


@pytest.mark.parametrize("line", ["pytest -q \\", "pytest -q \\\\\\", "  make test\\  "])
def test_a_command_continued_onto_the_next_line_is_refused_and_named(line: str) -> None:
    text = f"## Test gate\n```sh\nuv sync\n{line}\n  --maxfail=1\n```\n"
    with pytest.raises(NoTestGate) as e:
        gate_commands(text)
    assert str(e.value) == (
        "the Test gate section of its AGENTS.md continues a command onto the next line "
        f"({line.strip()!r}): put each command on one line"
    )


def test_a_line_that_ends_in_an_escaped_backslash_or_a_comment_that_does_is_no_continuation() -> None:
    text = "## Test gate\n```sh\n# a comment that ends in one \\\nprintf '%s' a\\\\\n```\n"
    assert gate_commands(text) == ("printf '%s' a\\\\",)


@pytest.mark.parametrize(
    "heading",
    ["# Test gate", "## Test gate", "###### Test gate", "## test GATE", "##   Test gate  ", "   ## Test gate",
     "## Test gate ##", "### Test gate ###   "],
)  # fmt: skip
def test_the_heading_is_test_gate_at_any_level_in_any_case(heading: str) -> None:
    assert gate_commands(f"# Repo\n\n{heading}\n\n```\nmake test\n```\n") == ("make test",)


@pytest.mark.parametrize(
    "text",
    [
        "# Repo\n\nNo gate here.\n\n```sh\nmake test\n```\n",
        "#Test gate\n\n```sh\nmake test\n```\n",  # no space after the #: no heading
        "    ## Test gate\n\n```sh\nmake test\n```\n",  # four spaces: code, not a heading
        "## Test gates\n\n```sh\nmake test\n```\n",
        "## The test gate\n\n```sh\nmake test\n```\n",
        "```md\n## Test gate\n```\n\n```sh\nmake test\n```\n",  # a heading inside a code block is code
        "",
    ],
)
def test_without_a_test_gate_heading_there_is_no_gate(text: str) -> None:
    with pytest.raises(NoTestGate, match="^its AGENTS.md has no Test gate section$"):
        gate_commands(text)


@pytest.mark.parametrize(
    "text",
    [
        "## Test gate\n\n```sh\n# install first\n\n   # then nothing\n```\n",  # comments and blank lines only
        "## Test gate\n\n```sh\n```\n",
        "## Test gate\n\nRun the tests.\n",  # no block at all
        "## Test gate\n\nRun the tests.\n\n## Style\n\n```sh\npnpm lint\n```\n",  # the next section's block
        "### Test gate\n\nRun the tests.\n\n# Next\n\n```sh\npnpm lint\n```\n",  # a higher heading ends it
        "## Test gate\n\n    make test\n",  # an indented code block is not a fenced one
    ],
)
def test_a_gate_section_without_a_command_in_its_first_block_is_no_gate(text: str) -> None:
    with pytest.raises(NoTestGate, match="^the Test gate section of its AGENTS.md lists no command"):
        gate_commands(text)


def test_a_deeper_heading_stays_inside_the_section() -> None:
    text = "## Test gate\n\n### How\n\nLike this:\n\n```sh\nmake test\n```\n\n## Next\n"
    assert gate_commands(text) == ("make test",)


def test_comment_and_blank_lines_are_skipped_and_each_command_is_trimmed() -> None:
    text = (
        "## Test gate\n```bash\n# deps\nuv sync\n\n  # lint, then the tests  \n  uv run ruff check .  \n\t\n"
        "uv run pytest -q\n```\n"
    )
    assert gate_commands(text) == ("uv sync", "uv run ruff check .", "uv run pytest -q")


def test_only_the_first_block_counts_and_the_first_section_wins() -> None:
    text = "## Test gate\n```\none\n```\n```\ntwo\n```\n## Test gate\n```\nthree\n```\n"
    assert gate_commands(text) == ("one",)


def test_crlf_line_ends_and_a_byte_order_mark_are_read_as_lines() -> None:
    text = "﻿## Test gate\r\n\r\n```sh\r\nuv sync\r\nuv run pytest -q\r\n```\r\n"
    assert gate_commands(text) == ("uv sync", "uv run pytest -q")


def test_tilde_fences_and_longer_fences_close_only_on_their_own_kind() -> None:
    assert gate_commands("## Test gate\n~~~sh\nmake test\n~~~\n") == ("make test",)
    text = "## Test gate\n````sh\nmake a\n```\nmake b\n~~~~\n````\nafter\n"
    assert gate_commands(text) == ("make a", "```", "make b", "~~~~")
    assert gate_commands("## Test gate\n~~~\nmake a\n```\n~~~\n") == ("make a", "```")


def test_an_unclosed_block_runs_to_the_end() -> None:
    assert gate_commands("## Test gate\n```sh\nmake a\nmake b\n") == ("make a", "make b")


def test_a_fence_whose_info_string_holds_a_backtick_is_no_fence() -> None:
    text = "## Test gate\n```a`b\nnot code\n```\nmake test\n```\n"
    assert gate_commands(text) == ("make test",)


@pytest.mark.parametrize("underline", ["=========", "---------", "=", "  ---  ", "==="])
def test_a_setext_heading_is_a_heading(underline: str) -> None:
    assert gate_commands(f"# Repo\n\nTest gate\n{underline}\n\n```\nmake test\n```\n") == ("make test",)


@pytest.mark.parametrize(
    "text",
    [
        "Test gate\n=========\n\nNo block yet.\n\nNext\n====\n\n```\nlint\n```\n",  # a setext h1 ends an h1
        "## Test gate\n\nNo block yet.\n\nNext\n----\n\n```\nlint\n```\n",  # a setext h2 ends an h2
        "Test gate\n---\nNo block yet.\n# Next\n```\nlint\n```\n",  # and an ATX h1 ends a setext h2
    ],
)
def test_a_setext_heading_of_the_sections_level_or_a_higher_one_ends_it(text: str) -> None:
    with pytest.raises(NoTestGate, match="^the Test gate section of its AGENTS.md lists no command"):
        gate_commands(text)


@pytest.mark.parametrize(
    "text",
    [
        "# Test gate\n\nWhy\n---\n\n```\nmake test\n```\n",  # a setext h2 under an h1: still inside
        "## Test gate\n\n---\n\n```\nmake test\n```\n",  # dashes after a blank line: a thematic break
        "## Test gate\n- run it:\n---\n```\nmake test\n```\n",  # dashes after a list item: a break too
        "## Test gate\n```\nmake test\n```\nText\n===\n",  # a heading after the block changes nothing
    ],
)
def test_setext_underlines_that_end_no_section(text: str) -> None:
    assert gate_commands(text) == ("make test",)


# ---- read from a commit ----


def commit_agents_md(repo: Path, text: str, message: str = "AGENTS.md") -> str:
    (repo / "AGENTS.md").write_text(text)
    git(repo, "add", "AGENTS.md")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def a_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("hi\n")
    git(repo, "add", "README.md")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


def test_the_gate_is_read_from_the_commit_never_from_the_working_tree(tmp_path: Path) -> None:
    repo = a_repo(tmp_path)
    first = commit_agents_md(repo, "## Test gate\n```\n./check.sh\n```\n")
    commit_agents_md(repo, "## Test gate\n```\ntrue\n```\n", "a later gate")
    (repo / "AGENTS.md").write_text("## Test gate\n```\necho uncommitted\n```\n")
    gate = read_gate(repo, first)
    assert gate == Gate(("./check.sh",), git(repo, "rev-parse", f"{first}:AGENTS.md"))
    assert read_gate(repo, git(repo, "rev-parse", "HEAD")).commands == ("true",)


def test_a_commit_without_an_agents_md_has_no_gate(tmp_path: Path) -> None:
    repo = a_repo(tmp_path)
    (repo / "AGENTS.md").write_text("## Test gate\n```\nmake test\n```\n")  # not committed
    (repo / "docs").mkdir()
    (repo / "docs" / "AGENTS.md").write_text("## Test gate\n```\nmake test\n```\n")
    git(repo, "add", "docs")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "not at the root")
    with pytest.raises(NoTestGate, match="^it has no AGENTS.md$"):
        read_gate(repo, git(repo, "rev-parse", "HEAD"))


def test_an_agents_md_that_links_to_another_file_of_the_commit_is_read_through_the_link(
    tmp_path: Path,
) -> None:
    repo = a_repo(tmp_path)
    (repo / "CLAUDE.md").write_text("## Test gate\n```\nmake test\n```\n")
    (repo / "AGENTS.md").symlink_to("CLAUDE.md")
    git(repo, "add", "AGENTS.md", "CLAUDE.md")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "linked")
    head = git(repo, "rev-parse", "HEAD")
    assert read_gate(repo, head) == Gate(("make test",), git(repo, "rev-parse", f"{head}:CLAUDE.md"))


def test_a_link_that_leaves_the_commit_is_not_followed_and_is_named_as_one(tmp_path: Path) -> None:
    repo = a_repo(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("## Test gate\n```\nmake test\n```\n")
    (repo / "AGENTS.md").symlink_to("../outside.md")
    git(repo, "add", "AGENTS.md")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "linked out")
    with pytest.raises(NoTestGate) as e:
        read_gate(repo, git(repo, "rev-parse", "HEAD"))
    assert str(e.value) == (
        "its AGENTS.md is a symbolic link out of the repository (to '../outside.md'), and airlie reads only "
        "the files of the commit"
    )
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").symlink_to(outside)  # a long path is cut where it is quoted: it is the file's text
    git(repo, "add", "AGENTS.md")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "linked out, absolute")
    with pytest.raises(NoTestGate) as e:
        read_gate(repo, git(repo, "rev-parse", "HEAD"))
    assert f"(to {repr(str(outside))[:79]}…)" in str(e.value)


@pytest.mark.parametrize("target", ["MISSING.md", "AGENTS.md"])  # a file the commit lacks; a link to itself
def test_a_link_that_leads_to_no_file_of_the_commit_is_named_as_one(tmp_path: Path, target: str) -> None:
    repo = a_repo(tmp_path)
    (repo / "AGENTS.md").symlink_to(target)
    git(repo, "add", "AGENTS.md")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a link to nothing")
    nowhere = "^its AGENTS.md is a symbolic link that leads to no file of the commit$"
    with pytest.raises(NoTestGate, match=nowhere):
        read_gate(repo, git(repo, "rev-parse", "HEAD"))


def test_replacements_planted_from_a_layer_worktree_change_nothing_airlie_reads(tmp_path: Path) -> None:
    """A coder's shell in a layer worktree can `git replace` the base commit, or AGENTS.md's blob:
    refs/replace is shared by every worktree, and plain git follows it. airlie's own git does not
    (core.useReplaceRefs), and neither does the git of a publish step (the same setting in its
    environment)."""
    repo = a_repo(tmp_path)
    base = commit_agents_md(repo, "## Test gate\n```\n./check.sh\n```\n")
    blob = git(repo, "rev-parse", f"{base}:AGENTS.md")
    layer = tmp_path / "layer"
    git(repo, "worktree", "add", "-q", "-b", "layer-01", str(layer))
    weak = commit_agents_md(layer, "## Test gate\n```\ntrue\n```\n", "a weaker gate")
    git(layer, "replace", base, weak)  # the base commit, replaced
    git(layer, "replace", blob, git(layer, "rev-parse", f"{weak}:AGENTS.md"))  # and AGENTS.md's blob
    # plain git reads the planted gate, through either replacement
    assert git(repo, "cat-file", "-p", f"{base}:AGENTS.md").endswith("true\n```")
    assert git(repo, "cat-file", "-p", blob).endswith("true\n```")
    assert read_gate(repo, base) == Gate(("./check.sh",), blob)  # airlie's: the real one
    published = without_hooks(dict(os.environ))  # a publish step's environment holds the same setting
    shown = subprocess.run(
        ["git", "cat-file", "-p", blob], cwd=repo, env=published, capture_output=True, text=True
    )
    assert shown.stdout.endswith("./check.sh\n```\n")


def test_the_note_for_a_missing_gate_names_the_repository_and_the_trunk_and_shows_an_example() -> None:
    text = missing_gate("weyenk/OpenHand", "origin/main", "0123456789abcdef", "it has no AGENTS.md")
    assert text == (
        "weyenk/OpenHand has no test gate on origin/main (0123456789ab): it has no AGENTS.md. Add a "
        "`## Test gate` section to AGENTS.md at the repository root (make the file if there is none): one "
        "fenced block listing the commands every change must pass, one per line, run in order. For example:\n"
        "```sh\nuv sync --frozen\nuv run ruff check .\nuv run pytest -q\n```"
    )
