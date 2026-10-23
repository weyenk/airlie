"""Builtin file tools, called directly: exact outputs, error messages and edge cases."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from airlie.tools.fs import LOCKED, LOCKED_TESTS, UNPARSEABLE, EditFile, Grep, ListDir, ReadFile, WriteFile
from airlie.tools.protocol import ToolContext
from tests.unit.test_tools import ctx


def put(c, rel: str, text: str | bytes) -> Path:  # type: ignore[no-untyped-def]
    p = c.worktree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text if isinstance(text, bytes) else text.encode())
    return p


# ---- read_file ----


async def test_read_file_numbers_lines_from_one_and_joins_with_newlines(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\nbeta\ngamma\n")
    r = await ReadFile().run({"path": "a.py"}, c)
    assert r.ok and r.output == "1: alpha\n2: beta\n3: gamma"


async def test_read_file_replaces_undecodable_bytes(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "bin.txt", b"a\xffb\n")
    r = await ReadFile().run({"path": "bin.txt"}, c)
    assert r.ok and r.output == "1: a�b"


async def test_read_file_errors_are_observations(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    r = await ReadFile().run({"path": ".env"}, c)
    assert (r.ok, r.output) == (False, "error: refusing to touch a likely secret file: .env")
    r = await ReadFile().run({"path": "nope.py"}, c)
    assert not r.ok and r.output.startswith("error: ") and "nope.py" in r.output
    r = await ReadFile().run({"path": "../x"}, c)
    assert (r.ok, r.output) == (False, "error: path escapes the worktree: ../x")


# ---- write_file ----


async def test_write_file_creates_parents_and_reports_char_count(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    r = await WriteFile().run({"path": "deep/er/a.py", "content": "x = 1\n"}, c)
    assert (r.ok, r.output) == (True, "wrote deep/er/a.py (6 chars)")
    assert (c.worktree / "deep/er/a.py").read_text() == "x = 1\n"


async def test_write_file_refuses_secrets_and_escapes(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    r = await WriteFile().run({"path": "id_rsa", "content": "k"}, c)
    assert (r.ok, r.output) == (False, "error: refusing to touch a likely secret file: id_rsa")
    r = await WriteFile().run({"path": "../out.txt", "content": "k"}, c)
    assert (r.ok, r.output) == (False, "error: path escapes the worktree: ../out.txt")


async def test_write_file_refuses_to_overwrite_a_long_existing_file(tmp_path: Path) -> None:
    """HDS-5 2026-10-16: rewriting a 17,000-character test file in one call hit max_tokens."""
    c = replace(ctx(tmp_path), write_file_max_overwrite_lines=3)
    p = put(c, "tests/test_big.py", "a\nb\nc\nd\n")
    r = await WriteFile().run({"path": "tests/test_big.py", "content": "x = 1\n"}, c)
    assert r.ok is False and r.output == (
        "error: tests/test_big.py already exists with 4 lines (over 3); write_file does not rewrite a file "
        "that long. Change it with edit_file, or put new tests in a new file named for the feature."
    )
    assert p.read_text() == "a\nb\nc\nd\n"


async def test_write_file_overwrites_a_short_file_and_creates_a_new_one(tmp_path: Path) -> None:
    c = replace(ctx(tmp_path), write_file_max_overwrite_lines=3)
    p = put(c, "small.py", "a\nb\nc\n")
    r = await WriteFile().run({"path": "small.py", "content": "x = 1\n"}, c)
    assert r.ok and p.read_text() == "x = 1\n"
    r = await WriteFile().run({"path": "fresh.py", "content": "y\n" * 50}, c)
    assert r.ok and (c.worktree / "fresh.py").read_text() == "y\n" * 50


def test_write_file_overwrite_limit_defaults_to_200_lines(tmp_path: Path) -> None:
    assert ToolContext(worktree=tmp_path).write_file_max_overwrite_lines == 200


def _commit_all(c) -> None:  # type: ignore[no-untyped-def]
    """The worktree becomes a git repository whose one commit holds what is in it now."""
    commit = ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"]
    for cmd in (["init", "-q"], ["add", "-A"], commit):
        subprocess.run(["git", *cmd], cwd=c.worktree, check=True, capture_output=True)


async def test_write_file_refuses_a_long_file_that_exists_at_the_base(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    p = put(c, "tests/test_big.py", "x = 1\n" * 300)
    _commit_all(c)
    r = await WriteFile().run({"path": "tests/test_big.py", "content": "y = 2\n"}, c)
    assert not r.ok and "300 lines (over 200)" in r.output and p.read_text() != "y = 2\n"


async def test_write_file_rewrites_a_long_file_the_coder_itself_created(tmp_path: Path) -> None:
    """The limit protects code that was there before the run. A file that is not at the base is the coder's
    own (AIR-31 layer 02: a 243-line test file it wrote, then could neither rewrite nor edit in one go)."""
    c = ctx(tmp_path)
    put(c, "README.md", "hi\n")
    _commit_all(c)
    p = put(c, "tests/test_new.py", "x = 1\n" * 300)
    r = await WriteFile().run({"path": "tests/test_new.py", "content": "y = 2\n"}, c)
    assert r.ok and p.read_text() == "y = 2\n"


async def test_write_file_rewrites_a_long_file_the_coder_created_even_once_a_round_committed_it(
    tmp_path: Path,
) -> None:
    """AIR-31 layer 03 fix round, 2026-10-19: the review and run_tests commit the work as the run goes, so by
    the fix round the coder's own test file was in HEAD and write_file refused it as if it were the base's.
    With the layer's base on the context (scope_base), what was there before is judged against that."""
    c = ctx(tmp_path)
    put(c, "README.md", "hi\n")
    _commit_all(c)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=c.worktree, capture_output=True, text=True).stdout
    p = put(c, "tests/test_new.py", "x = 1\n" * 300)
    _commit_all(c)  # a wip commit of the run, as the review and a green run_tests make
    refused = await WriteFile().run({"path": "tests/test_new.py", "content": "y = 2\n"}, c)
    assert not refused.ok  # no base known: HEAD is the base
    r = await WriteFile().run(
        {"path": "tests/test_new.py", "content": "y = 2\n"}, replace(c, scope_base=base.strip())
    )
    assert r.ok and p.read_text() == "y = 2\n"


async def test_edit_file_skips_whitespace_only_differences_when_naming_the_first_that_differs(
    tmp_path: Path,
) -> None:
    """AIR-30 layer 02, 2026-10-19: the hint named a line of sixteen spaces against an empty line, which the
    loose match ignores anyway, while the real mismatch was twenty lines down; the coder resent the same
    `old` three times. Whitespace-only differences do not count; the first real one is named."""
    c = ctx(tmp_path)
    put(c, "t.py", "def f():\n\n    a = [\n        m\n        for m in xs\n    ]\n")
    old = "def f():\n    \n    a = [\n        m for m in xs\n    ]\n"
    r = await EditFile().run({"path": "t.py", "old": old, "new": "..."}, c)
    assert not r.ok
    assert "the first line of `old` that differs is its line 4, `        m for m in xs`" in r.output
    assert "where the file has line 4: `        m`" in r.output


def test_write_file_tells_the_model_its_overwrite_limit_before_it_tries() -> None:
    """A coder that reads a 1300-line test file and rewrites it whole spends minutes generating a reply
    the tool refuses (AIR-31 layer 02, twice in one layer): the description names the limit and the way
    round it."""
    d = WriteFile().description
    assert "at most 200 lines" in d and "edit_file" in d and "new file" in d


# ---- edit_file ----


async def test_edit_file_replaces_exactly_one_occurrence(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    p = put(c, "a.txt", "one two three\n")
    r = await EditFile().run({"path": "a.txt", "old": "two", "new": "2"}, c)
    assert (r.ok, r.output) == (True, "edited a.txt") and p.read_text() == "one 2 three\n"


async def test_edit_file_names_the_first_line_of_a_long_old_that_differs(tmp_path: Path) -> None:
    """AIR-31 layer 03, 2026-10-19: the coder quoted a 12-line `old` of a test it had just written, with one
    line from memory; the hint named only the first line, which matched, and the coder sent the same `old`
    again. The error now says which line of `old` is the first to differ, and what the file has there."""
    c = ctx(tmp_path)
    put(c, "t.py", "async def test_x() -> None:\n    a = 1\n    b = 2\n    assert a + b == 3\n")
    old = "async def test_x() -> None:\n    a = 1\n    b = 3\n    assert a + b == 3\n"
    r = await EditFile().run({"path": "t.py", "old": old, "new": "..."}, c)
    assert not r.ok
    assert r.output == (
        "error: `old` text not found in t.py. Its first line is line 1 of the file; the first line of `old` "
        "that differs is its line 3, `    b = 3`, where the file has line 3: `    b = 2`. Read the file "
        "there (read_file with offset and limit) and quote it exactly, whitespace included."
    )


async def test_edit_file_names_the_closest_line_when_old_is_misquoted(tmp_path: Path) -> None:
    """AIR-7 layer 03, 2026-10-18: every failed edit of the day was an anchor quoted from memory (a Qwen2.5
    coder tried three guesses at `app.include_router(...)` without reading the file). The error names the
    line that comes closest and says how to see it."""
    c = ctx(tmp_path)
    put(
        c,
        "app.py",
        "import x\n\n\ndef make():\n    app.include_router(auth_router(store, resolver, cfg.auth))\n",
    )
    r = await EditFile().run(
        {"path": "app.py", "old": "    app.include_router(auth_router(store, resolver))\n", "new": "..."}, c
    )
    assert not r.ok
    assert r.output == (
        "error: `old` text not found in app.py. The closest line is 5: "
        "`    app.include_router(auth_router(store, resolver, cfg.auth))`. Read the file there (read_file "
        "with offset and limit) and quote it exactly, whitespace included."
    )


async def test_edit_file_matches_old_with_blank_lines_and_trailing_spaces_ignored(tmp_path: Path) -> None:
    """AIR-7 layer 03, 2026-10-18: a Qwen2.5 coder quoted two imports without the blank line between them,
    three times, after reading the file. When the exact text is absent and exactly one span matches with
    blank lines and trailing spaces ignored, that span is edited and the reply says so."""
    c = ctx(tmp_path)
    p = put(c, "t.py", "from a import A\n\nfrom b import B   \n\n\ndef f():\n    pass\n")
    r = await EditFile().run(
        {
            "path": "t.py",
            "old": "from a import A\nfrom b import B",
            "new": "from a import A\nfrom c import C",
        },
        c,
    )
    assert (r.ok, r.output) == (
        True,
        "edited t.py (`old` matched with its blank lines and trailing spaces ignored; quote the file exactly "
        "next time)",
    )
    assert p.read_text() == "from a import A\nfrom c import C\n\n\ndef f():\n    pass\n"
    # a trailing newline on `old` takes the last line's newline with it, as an exact match would
    r = await EditFile().run(
        {"path": "t.py", "old": "def f():\n    pass\n", "new": "def g():\n    pass\n"}, c
    )
    assert r.ok and p.read_text() == "from a import A\nfrom c import C\n\n\ndef g():\n    pass\n"


async def test_edit_file_loose_match_still_needs_indentation_and_uniqueness(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "t.py", "def f():\n    x = 1\n\n\ndef g():\n    x = 1\n")
    # two-space indentation where the file has four: no exact substring, and no loose match either
    r = await EditFile().run({"path": "t.py", "old": "def f():\n  x = 1", "new": "def f():\n  x = 2"}, c)
    assert not r.ok and "Its first line is line 1 of the file" in r.output
    r = await EditFile().run({"path": "t.py", "old": "    x = 1\n", "new": "    x = 2\n"}, c)  # two spans
    assert (r.ok, r.output) == (False, "error: `old` text occurs 2 times in t.py; make it unique")
    r = await EditFile().run(
        {"path": "t.py", "old": "def f():\n\n    x = 1", "new": "def f():\n    x = 2"}, c
    )
    assert r.ok and (c.worktree / "t.py").read_text() == "def f():\n    x = 2\n\n\ndef g():\n    x = 1\n"


async def test_edit_file_messages_for_missing_and_ambiguous_text(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    p = put(c, "a.txt", "x x\nx\n")
    r = await EditFile().run({"path": "a.txt", "old": "zzz", "new": "y"}, c)
    assert (r.ok, r.output) == (
        False,
        "error: `old` text not found in a.txt. Read the file (read_file with offset and limit) and quote "
        "the text exactly, whitespace included.",
    )
    r = await EditFile().run({"path": "a.txt", "old": "x", "new": "y"}, c)
    assert (r.ok, r.output) == (False, "error: `old` text occurs 3 times in a.txt; make it unique")
    p2 = put(c, "b.txt", "x x\n")
    r = await EditFile().run({"path": "b.txt", "old": "x", "new": "y"}, c)
    assert (r.ok, r.output) == (False, "error: `old` text occurs 2 times in b.txt; make it unique")
    assert p.read_text() == "x x\nx\n" and p2.read_text() == "x x\n"


async def test_edit_file_errors_on_missing_or_secret_files(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    r = await EditFile().run({"path": "missing.txt", "old": "a", "new": "b"}, c)
    assert not r.ok and r.output.startswith("error: ") and "missing.txt" in r.output
    r = await EditFile().run({"path": "cert.pem", "old": "a", "new": "b"}, c)
    assert (r.ok, r.output) == (False, "error: refusing to touch a likely secret file: cert.pem")


# ---- list_dir ----


async def test_list_dir_lists_files_with_sizes_in_sorted_order(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "b.py", "1234")
    put(c, "a.py", "123")
    put(c, "sub/c.py", "12")
    r = await ListDir().run({"path": "."}, c)
    assert r.output == "a.py (3 bytes)\nb.py (4 bytes)\nsub/c.py (2 bytes)"


async def test_list_dir_defaults_to_the_worktree_root_and_honours_a_subpath(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "top.py", "1")
    put(c, "pkg/in.py", "12")
    assert (await ListDir().run({}, c)).output == "pkg/in.py (2 bytes)\ntop.py (1 bytes)"
    assert (await ListDir().run({"path": ""}, c)).output == "pkg/in.py (2 bytes)\ntop.py (1 bytes)"
    assert (await ListDir().run({"path": "pkg"}, c)).output == "in.py (2 bytes)"


async def test_list_dir_skips_vcs_build_dirs_and_secrets_but_keeps_listing(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, ".git/config", "x")  # sorts before the real files: a `break` would end the listing here
    put(c, ".env", "SECRET=1")
    put(c, "node_modules/m.js", "x")
    put(c, "key.pem", "k")
    put(c, "keep.py", "12")
    put(c, "src/.env.local", "S")
    put(c, "src/ok.py", "1")
    r = await ListDir().run({}, c)
    assert r.output == "keep.py (2 bytes)\nsrc/ok.py (1 bytes)"


async def test_list_dir_skip_dir_filter_applies_to_files_that_are_not_secret_too(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "__pycache__/x.pyc", "x")
    put(c, "a.py", "1")
    assert (await ListDir().run({}, c)).output == "a.py (1 bytes)"


async def test_list_dir_empty_and_not_a_directory(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    assert (await ListDir().run({}, c)).output == "(empty)"
    put(c, "a.py", "1")
    r = await ListDir().run({"path": "a.py"}, c)
    assert (r.ok, r.output) == (False, "error: not a directory: a.py")


async def test_list_dir_escape_is_an_error_observation(tmp_path: Path) -> None:
    r = await ListDir().run({"path": "../.."}, ctx(tmp_path))
    assert (r.ok, r.output) == (False, "error: path escapes the worktree: ../..")


# ---- grep ----


async def test_grep_reports_path_line_and_stripped_text_sorted_by_file(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "pkg/b.py", "nothing\n    y = 2   \n")
    put(c, "pkg/a.py", "y = 1\nz\ny = 3\n")
    r = await Grep().run({"pattern": r"y = \d", "path": "pkg"}, c)
    assert r.output == "pkg/a.py:1: y = 1\npkg/a.py:3: y = 3\npkg/b.py:2: y = 2"


async def test_grep_defaults_to_the_whole_worktree_and_accepts_a_single_file(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "hit\n")
    put(c, "d/b.py", "hit\n")
    assert (await Grep().run({"pattern": "hit"}, c)).output == "a.py:1: hit\nd/b.py:1: hit"
    assert (await Grep().run({"pattern": "hit", "path": ""}, c)).output == "a.py:1: hit\nd/b.py:1: hit"
    assert (await Grep().run({"pattern": "hit", "path": "d/b.py"}, c)).output == "d/b.py:1: hit"
    assert (await Grep().run({"pattern": "hit", "path": "d"}, c)).output == "d/b.py:1: hit"


async def test_grep_skips_vcs_dirs_and_secrets_but_keeps_searching(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, ".git/x", "hit\n")  # sorts first: a `break` would end the search here
    put(c, ".env", "hit\n")
    put(c, "id_rsa", "hit\n")
    put(c, "node_modules/m.js", "hit\n")
    put(c, "ok.py", "hit\n")
    put(c, "sub/.env.local", "hit\n")
    r = await Grep().run({"pattern": "hit"}, c)
    assert r.output == "ok.py:1: hit"


async def test_grep_tolerates_undecodable_bytes(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.bin", b"\xff\xfe needle \xff\n")
    r = await Grep().run({"pattern": "needle"}, c)
    assert r.ok and r.output.startswith("a.bin:1: ") and "needle" in r.output


async def test_grep_no_matches_and_bad_inputs(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "x\n")
    assert (await Grep().run({"pattern": "zzz"}, c)).output == "(no matches)"
    r = await Grep().run({"pattern": "("}, c)
    assert (r.ok, r.output) == (False, "error: missing ), unterminated subpattern at position 0")
    r = await Grep().run({"pattern": "x", "path": "../.."}, c)
    assert (r.ok, r.output) == (False, "error: path escapes the worktree: ../..")


async def test_grep_stops_searching_after_the_file_that_passes_500_matches(tmp_path: Path) -> None:
    c = ctx(tmp_path, cap=10**7)
    put(c, "a.txt", "hit\n" * 500)
    put(c, "b.txt", "hit\n")
    r = await Grep().run({"pattern": "hit"}, c)
    assert len(r.output.splitlines()) == 501 and r.output.splitlines()[-1] == "b.txt:1: hit"
    put(c, "a.txt", "hit\n" * 501)
    r = await Grep().run({"pattern": "hit"}, c)
    assert len(r.output.splitlines()) == 501 and "b.txt" not in r.output


async def test_grep_skips_an_unreadable_file_and_keeps_searching(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    locked = put(c, "a.txt", "hit\n")  # sorts first
    put(c, "b.txt", "hit\n")
    locked.chmod(0o000)
    try:
        r = await Grep().run({"pattern": "hit"}, c)
    finally:
        locked.chmod(0o644)
    assert r.output == "b.txt:1: hit"


# ---- locked files: the tester's tests, for the coder ----


async def test_write_file_and_edit_file_refuse_a_locked_file_and_say_why(tmp_path: Path) -> None:
    c = replace(ctx(tmp_path), read_only=frozenset({"tests/test_calc.py"}))
    put(c, "tests/test_calc.py", "def test_mul():\n    assert mul(2, 3) == 6\n")
    for tool, args in (
        (WriteFile(), {"path": "tests/test_calc.py", "content": "def test_mul():\n    assert True\n"}),
        (EditFile(), {"path": "tests/test_calc.py", "old": "== 6", "new": "== 6 or True"}),
        (
            WriteFile(),
            {"path": "./tests/../tests/test_calc.py", "content": "x"},
        ),  # the same file, spelled oddly
    ):
        r = await tool.run(args, c)
        assert r.ok is False and r.output == LOCKED.format(path=args["path"])
        assert "you cannot change them" in r.output and "say which and why" in r.output
        assert r.output.startswith(f"error: {args['path']} holds this layer's acceptance tests")
    assert (c.worktree / "tests/test_calc.py").read_text().endswith("== 6\n")
    # a file beside it is the coder's own
    assert (await WriteFile().run({"path": "tests/test_more.py", "content": "x = 1\n"}, c)).ok


BOARD_FILE = (
    "import os\n\n\n"
    "class FakeBoard:\n"
    "    def comment(self, key, text):\n"
    "        return None\n\n\n"
    "def test_new():\n"
    "    assert FakeBoard().comment('A', 'b') is None\n"
)


def board_ctx(tmp_path: Path) -> ToolContext:
    c = replace(ctx(tmp_path), locked_tests={"tests/test_p.py": frozenset({"test_new"})})
    put(c, "tests/test_p.py", BOARD_FILE)
    return c


async def test_the_coder_may_change_a_fake_in_a_file_the_tester_added_tests_to(tmp_path: Path) -> None:
    c = board_ctx(tmp_path)
    r = await EditFile().run(
        {
            "path": "tests/test_p.py",
            "old": "def comment(self, key, text):",
            "new": "def comment(self, key, text, role=None):",
        },
        c,
    )
    assert r.ok, r.output
    assert "role=None" in (c.worktree / "tests/test_p.py").read_text()
    # the same through a loose match (blank lines ignored in `old`)
    r = await EditFile().run(
        {
            "path": "tests/test_p.py",
            "old": "import os\nclass FakeBoard:",
            "new": "import os\n\n\nclass FakeBoard:",
        },
        c,
    )
    assert r.ok, r.output


async def test_an_edit_that_touches_the_testers_test_is_refused_and_names_it(tmp_path: Path) -> None:
    c = board_ctx(tmp_path)
    r = await EditFile().run({"path": "tests/test_p.py", "old": "'b') is None", "new": "'b') is not None"}, c)
    assert r.ok is False
    assert r.output == LOCKED_TESTS.format(path="tests/test_p.py", names="test_new")
    assert "test_new" in r.output
    assert (c.worktree / "tests/test_p.py").read_text() == BOARD_FILE
    # a loose match that lands inside the test is refused too
    r = await EditFile().run(
        {
            "path": "tests/test_p.py",
            "old": "def test_new():\n\n    assert FakeBoard().comment('A', 'b') is None",
            "new": "def test_new():\n    assert FakeBoard().comment('A', 'b') is not None",
        },
        c,
    )
    assert r.ok is False and "test_new" in r.output
    assert (c.worktree / "tests/test_p.py").read_text() == BOARD_FILE


async def test_write_file_that_drops_the_testers_test_is_refused(tmp_path: Path) -> None:
    c = board_ctx(tmp_path)
    r = await WriteFile().run({"path": "tests/test_p.py", "content": "import os\n"}, c)
    assert r.ok is False and "test_new" in r.output
    assert (c.worktree / "tests/test_p.py").read_text() == BOARD_FILE
    kept = BOARD_FILE.replace("return None", "return 1")
    assert (await WriteFile().run({"path": "tests/test_p.py", "content": kept}, c)).ok
    # a file that is not in locked_tests, and a new file, write as before
    assert (await WriteFile().run({"path": "tests/test_q.py", "content": "x = 1\n"}, c)).ok


async def test_an_edit_that_leaves_the_file_unparseable_is_refused(tmp_path: Path) -> None:
    c = board_ctx(tmp_path)
    r = await EditFile().run({"path": "tests/test_p.py", "old": "return None", "new": "return ("}, c)
    assert r.ok is False and r.output == UNPARSEABLE.format(path="tests/test_p.py")
    assert (c.worktree / "tests/test_p.py").read_text() == BOARD_FILE
    r = await WriteFile().run({"path": "tests/test_p.py", "content": "def test_new(:\n"}, c)
    assert r.ok is False and r.output == UNPARSEABLE.format(path="tests/test_p.py")
    assert (c.worktree / "tests/test_p.py").read_text() == BOARD_FILE


# ---- the helpers, to the character (PR 105's mutation gate: fs.py had 65 surviving mutants) ----


async def test_read_file_takes_an_empty_or_zero_line_argument_as_the_start(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\nbeta\ngamma\n")
    whole = "1: alpha\n2: beta\n3: gamma"
    for args in ({}, {"offset": ""}, {"offset": 0}, {"limit": ""}, {"offset": "", "limit": ""}):
        r = await ReadFile().run({"path": "a.py", **args}, c)
        assert r.ok and r.output == whole, args
    assert (await ReadFile().run({"path": "a.py", "offset": 2, "limit": 1}, c)).output == "2: beta"


def test_cap_lines_keeps_whole_lines_up_to_the_limit_and_says_what_is_missing() -> None:
    from airlie.tools.fs import _cap_lines

    lines = ["1: aa", "2: bb", "3: cc"]  # 17 characters joined
    assert _cap_lines(lines, 1, 3, 17) == "1: aa\n2: bb\n3: cc"  # exactly the limit: whole
    assert _cap_lines(lines, 1, 3, 11) == (  # two lines and their newline are exactly 11
        "1: aa\n2: bb\n… [truncated: lines 3-3 not shown; read on with offset=3] [truncated 6 chars]"
    )
    assert _cap_lines(lines, 1, 3, 10) == (  # one character short of the second line
        "1: aa\n… [truncated: lines 2-3 not shown; read on with offset=2] [truncated 12 chars]"
    )
    assert _cap_lines(lines[1:], 2, 9, 5) == (  # a read that started at line 2 of 9
        "2: bb\n… [truncated: lines 3-9 not shown; read on with offset=3] [truncated 6 chars]"
    )


async def test_the_overwrite_limit_holds_in_a_repository_with_nothing_committed(tmp_path: Path) -> None:
    """git 2.4x says "invalid object name" where older ones said "Not a valid object name": the limit was
    read from that text, and a repository with no commit lost it."""
    c = replace(ctx(tmp_path), write_file_max_overwrite_lines=3)
    subprocess.run(["git", "init", "-q"], cwd=c.worktree, check=True)
    p = put(c, "big.py", "a\nb\nc\nd\n")
    r = await WriteFile().run({"path": "big.py", "content": "x = 1\n"}, c)
    assert r.ok is False and "already exists with 4 lines" in r.output and p.read_text() == "a\nb\nc\nd\n"


async def test_a_long_file_created_after_the_base_commit_may_be_rewritten(tmp_path: Path) -> None:
    c = replace(ctx(tmp_path), write_file_max_overwrite_lines=3)
    put(c, "kept.py", "x = 1\n")
    _commit_all(c)
    p = put(c, "new.py", "a\nb\nc\nd\n")  # not in HEAD
    assert (await WriteFile().run({"path": "new.py", "content": "y = 2\n"}, c)).ok
    assert p.read_text() == "y = 2\n"


def test_not_found_names_the_end_of_the_file_when_old_runs_past_it() -> None:
    from airlie.tools.fs import _not_found

    assert _not_found("a.txt", "b\nc\n", "a\nb\n") == (
        "error: `old` text not found in a.txt. Its first line is line 2 of the file; the first line of `old` "
        "that differs is its line 2, `c`, where the file has line 3: ``. Read the file there (read_file with "
        "offset and limit) and quote it exactly, whitespace included."
    )


def test_not_found_for_a_blank_old_names_no_line_even_where_the_file_has_blank_ones() -> None:
    from airlie.tools.fs import _not_found

    generic = (
        "error: `old` text not found in a.txt. Read the file (read_file with offset and limit) and quote the "
        "text exactly, whitespace included."
    )
    assert _not_found("a.txt", "\n \n", "a\n\nb\n") == generic
    assert _not_found("a.txt", "zzz", "") == generic  # an empty file has no closest line


def test_loose_span_is_the_exact_offsets_of_the_lines_it_matched() -> None:
    from airlie.tools.fs import _loose_span

    text = "aX\r\n\nb  \nc\n"  # a CRLF line ending in X, a blank line, trailing spaces
    assert _loose_span(text, "aX\nb\n") == (0, 9)  # through b's newline, since `old` ends with one
    assert _loose_span(text, "aX\nb") == (0, 8)  # to the end of b's text (its spaces too), since it does not
    assert _loose_span(text, "c\n") == (9, 11)  # the last line of the file
    assert _loose_span(text, "b\nc") == (5, 10)
    assert _loose_span(text, "a\nb\n") is None  # aX is not a
    assert _loose_span("x\nx\n", "x\n") is None  # twice: no one span


def test_asking_git_whether_a_file_is_at_the_base_prints_nothing(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """git's own output (rev-parse prints the commit) stays out of the process's: a model's tool output is
    what the tool returns, not what leaked to the terminal."""
    from airlie.tools.fs import _at_base

    c = ctx(tmp_path)
    put(c, "kept.py", "x = 1\n")
    _commit_all(c)
    capfd.readouterr()
    assert _at_base(c.worktree, c.worktree / "kept.py", "HEAD") is True
    assert _at_base(c.worktree, c.worktree / "new.py", "HEAD") is False
    assert _at_base(c.worktree, c.worktree / "kept.py", "no-such-ref") is True  # no such commit
    assert capfd.readouterr() == ("", "")


# ---- read_file with several paths ----


async def test_read_file_paths_shows_each_file_with_its_header(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\nbeta\n")
    put(c, "b.py", "gamma\n")
    r = await ReadFile().run({"paths": ["a.py", "b.py"]}, c)
    assert r.ok
    assert "=== a.py (2 lines) ===\n1: alpha\n2: beta" in r.output
    assert "=== b.py (1 lines) ===\n1: gamma" in r.output


async def test_read_file_paths_stay_under_the_output_cap(tmp_path: Path) -> None:
    c = ctx(tmp_path, cap=20_000)
    for name in ("a.txt", "b.txt", "c.txt"):
        put(c, name, "\n".join("x" * 99 for _ in range(150)))  # about 15k chars
    r = await ReadFile().run({"paths": ["a.txt", "b.txt", "c.txt"]}, c)
    assert r.ok
    assert len(r.output) <= 20_000 + 400  # the cap plus the notices
    assert "read on with offset=" in r.output
    assert "c.txt: not shown, over the output cap; read it with path" in r.output


async def test_read_file_paths_missing_one_still_shows_the_others(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\n")
    r = await ReadFile().run({"paths": ["nope.py", "a.py"]}, c)
    assert r.ok
    assert "=== nope.py ===\nerror:" in r.output
    assert "1: alpha" in r.output


async def test_read_file_paths_with_offset_is_refused(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\n")
    for extra in ({"offset": 2}, {"limit": 1}):
        r = await ReadFile().run({"paths": ["a.py"], **extra}, c)
        assert not r.ok and r.output == "error: offset and limit read one file: use path"


async def test_read_file_needs_path_or_paths(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\n")
    for args in ({}, {"path": "a.py", "paths": ["a.py"]}):
        r = await ReadFile().run(args, c)
        assert not r.ok and "path" in r.output and "paths" in r.output and "one of" in r.output


async def test_read_file_paths_accepts_a_json_string_and_refuses_more_than_five(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\n")
    r = await ReadFile().run({"paths": '["a.py"]'}, c)
    assert r.ok and "1: alpha" in r.output
    r = await ReadFile().run({"paths": ["a.py"] * 6}, c)
    assert not r.ok and "at most 5" in r.output
    r = await ReadFile().run({"paths": "a.py"}, c)
    assert not r.ok and "list of paths" in r.output


async def test_read_file_with_no_arguments_shows_a_correct_call(tmp_path: Path) -> None:
    """HDS-5: a model that sends read_file {} is shown a worked call, as the registry's missing-argument
    error showed it before `path` stopped being required."""
    r = await ReadFile().run({}, ctx(tmp_path))
    assert not r.ok and 'A correct call looks like: read_file(path="src/app.py")' in r.output


async def test_read_file_paths_outside_the_worktree_is_refused_in_its_own_section(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\n")
    r = await ReadFile().run({"paths": ["../x", "a.py"]}, c)
    assert r.ok
    assert "=== ../x ===\nerror:" in r.output and "escapes the worktree" in r.output
    assert "=== a.py (1 lines) ===\n1: alpha" in r.output


def test_a_context_with_locked_tests_stays_hashable_and_compares_as_before(tmp_path: Path) -> None:
    plain = ToolContext(worktree=tmp_path)
    locked = replace(plain, locked_tests={"tests/test_p.py": frozenset({"test_new"})})
    assert hash(locked) == hash(plain)
    assert locked == plain


# ---- read_file with several paths, to the character (PR 137's mutation gate) ----

ONE_LINE_HEAD = "=== a.txt (1 lines) ==="


async def test_read_file_paths_shows_a_file_that_exactly_fills_the_cap_whole(tmp_path: Path) -> None:
    """The header and its newline come out of the room; a first line taking all that is left is shown."""
    c = ctx(tmp_path, cap=200)
    line = "1: " + "x" * (200 - len(ONE_LINE_HEAD) - 1 - len("1: "))
    put(c, "a.txt", line[3:] + "\n")
    r = await ReadFile().run({"paths": ["a.txt"]}, c)
    assert (r.ok, r.output) == (True, f"{ONE_LINE_HEAD}\n{line}")
    assert len(r.output) == 200


async def test_read_file_paths_cuts_a_file_one_char_over_the_cap_and_says_where_to_read_on(
    tmp_path: Path,
) -> None:
    c = ctx(tmp_path, cap=200)
    head = "=== a.txt (2 lines) ==="
    body_room = 200 - len(head) - 1
    first = "1: " + "x" * 10
    second = "2: " + "y" * (body_room + 1 - len(first) - 1 - len("2: "))  # the two lines: one char too many
    put(c, "a.txt", f"{first[3:]}\n{second[3:]}\n")
    r = await ReadFile().run({"paths": ["a.txt"]}, c)
    assert (r.ok, r.output) == (
        True,
        f"{head}\n{first}\n… [truncated: lines 2-2 not shown; read on with offset=2] [truncated "
        f"{len(second) + 1} chars]",
    )


async def test_read_file_paths_gives_the_next_file_what_room_the_first_left(tmp_path: Path) -> None:
    """Each section and the newline after it come out of the room: the second file gets exactly the rest."""
    c = ctx(tmp_path, cap=200)
    put(c, "a.txt", "alpha\n")
    first = f"{ONE_LINE_HEAD}\n1: alpha"
    head_b = "=== b.txt (1 lines) ==="
    fits = 200 - len(first) - 1 - len(head_b) - 1 - len("1: ")
    put(c, "b.txt", "x" * fits + "\n")
    r = await ReadFile().run({"paths": ["a.txt", "b.txt"]}, c)
    assert (r.ok, r.output) == (True, f"{first}\n{head_b}\n1: {'x' * fits}")
    assert len(r.output) == 200
    put(c, "b.txt", "x" * (fits + 1) + "\n")  # one char more: listed, not shown
    r = await ReadFile().run({"paths": ["a.txt", "b.txt"]}, c)
    assert (r.ok, r.output) == (True, f"{first}\nb.txt: not shown, over the output cap; read it with path")


async def test_read_file_paths_replaces_undecodable_bytes(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "bin.txt", b"a\xffb\n")
    r = await ReadFile().run({"paths": ["bin.txt"]}, c)
    assert (r.ok, r.output) == (True, "=== bin.txt (1 lines) ===\n1: a�b")


async def test_read_file_paths_takes_empty_offset_and_limit_as_not_given(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\n")
    r = await ReadFile().run({"paths": ["a.py"], "offset": "", "limit": ""}, c)
    assert (r.ok, r.output) == (True, "=== a.py (1 lines) ===\n1: alpha")
    r = await ReadFile().run({"paths": ["a.py"], "offset": 2}, c)
    assert r.ok is False


async def test_read_file_paths_must_be_a_non_empty_list_of_at_most_five(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    for name in "abcde":
        put(c, f"{name}.py", f"{name}\n")
    refusal = 'error: paths must be a list of paths, e.g. ["a.py", "b.py"]'
    for paths in ([], "[]", '"a.py"', "a.py", '{"a.py": 1}'):
        r = await ReadFile().run({"paths": paths}, c)
        assert (r.ok, r.output) == (False, refusal), paths
    r = await ReadFile().run({"paths": [f"{name}.py" for name in "abcde"]}, c)  # five is allowed
    assert r.ok and r.output.count("===\n") == 5 and "=== e.py (1 lines) ===\n1: e" in r.output


async def test_read_file_needs_one_of_path_or_paths_and_says_so_exactly(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\n")
    needs = "error: read_file needs exactly one of path (one file) or paths (several)."
    r = await ReadFile().run({"path": "a.py", "paths": ["a.py"]}, c)  # both: no worked call, it had one
    assert (r.ok, r.output) == (False, needs)
    r = await ReadFile().run({}, c)
    assert (r.ok, r.output) == (False, f'{needs} A correct call looks like: read_file(path="src/app.py")')
    r = await ReadFile().run({"paths": ["a.py"], "limit": 1}, c)
    assert (r.ok, r.output) == (False, "error: offset and limit read one file: use path")


async def test_read_file_paths_joins_the_sections_with_one_newline(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    put(c, "a.py", "alpha\n")
    put(c, "b.py", "beta\n")
    r = await ReadFile().run({"paths": ["a.py", "b.py"]}, c)
    assert (r.ok, r.output) == (True, "=== a.py (1 lines) ===\n1: alpha\n=== b.py (1 lines) ===\n1: beta")


# ---- the definition lock and write_file's reads, to the character (PR 137's mutation gate) ----


async def test_write_file_overwrites_a_short_file_with_undecodable_bytes(tmp_path: Path) -> None:
    c = ctx(tmp_path)
    p = put(c, "bin.txt", b"a\xffb\n")
    r = await WriteFile().run({"path": "bin.txt", "content": "ab\n"}, c)
    assert (r.ok, r.output) == (True, "wrote bin.txt (3 chars)")
    assert p.read_text() == "ab\n"


async def test_a_refused_change_names_every_test_it_touches(tmp_path: Path) -> None:
    c = replace(ctx(tmp_path), locked_tests={"tests/test_p.py": frozenset({"test_a", "test_b"})})
    put(c, "tests/test_p.py", "def test_a():\n    assert 1\n\n\ndef test_b():\n    assert 1\n")
    r = await WriteFile().run({"path": "tests/test_p.py", "content": "x = 1\n"}, c)
    assert (r.ok, r.output) == (False, LOCKED_TESTS.format(path="tests/test_p.py", names="test_a, test_b"))


async def test_a_loose_edit_that_touches_the_testers_test_names_the_file(tmp_path: Path) -> None:
    c = board_ctx(tmp_path)
    r = await EditFile().run(
        {
            "path": "tests/test_p.py",
            "old": "def test_new():\n\n    assert FakeBoard().comment('A', 'b') is None",
            "new": "def test_new():\n    assert FakeBoard().comment('A', 'b') is not None",
        },
        c,
    )
    assert (r.ok, r.output) == (False, LOCKED_TESTS.format(path="tests/test_p.py", names="test_new"))
    assert (c.worktree / "tests/test_p.py").read_text() == BOARD_FILE
